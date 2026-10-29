import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hy_memory.coding.fact_reconciler import WorkingFactReconciler, _build_inherited_details
from hy_memory.coding.input_capture import ReconcileInputCapture
from hy_memory.coding.reconciler import CodingMemoryReconciler
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemory, CodingMemoryDraft, WorkingFact, WorkingFactDraft
from hy_memory.coding.writer import CodingWriter
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from test_original_archive import config_for
from test_output_lineage import fixture
from test_sdk_context_capture import edit_messages


def experience(identity="old", **kwargs):
    return CodingMemory(
        experience_id=identity,
        task="Synthetic build fix",
        solution="original solution",
        user_id="scope",
        agent_id="agent",
        session_id="session",
        **kwargs,
    )


def fact(identity, **kwargs):
    return WorkingFact(
        fact_id=identity,
        statement="Synthetic build progress " + identity,
        user_id="scope",
        agent_id="agent",
        session_id="session",
        **kwargs,
    )


@pytest.mark.parametrize("failure", ["bytes", "records", "stages", "object", "pending"])
def test_capture_never_returns_partial_complete_evidence(monkeypatch, failure):
    capture = ReconcileInputCapture()
    token = capture.begin("first", "experience", [experience()], "shown")
    capture.end(token, "used")
    record = experience("second")
    if failure == "bytes":
        monkeypatch.setattr(capture, "MAX_BYTES", 1)
    elif failure == "records":
        monkeypatch.setattr(capture, "MAX_RECORDS", 1)
    elif failure == "stages":
        monkeypatch.setattr(capture, "MAX_STAGES", 1)
    elif failure == "object":
        record.solution = object()
    token = capture.begin("second", "experience", [record], "shown")
    if failure != "pending":
        capture.end(token, "used")
    assert capture.result() == {"schema_version": 1, "status": "unavailable", "stages": []}


def test_snapshots_and_return_values_do_not_alias_mutable_records():
    capture = ReconcileInputCapture()
    record = experience(source_fact_ids=["before"])
    token = capture.begin("experience_decision", "experience", [record], "old text")
    capture.end(token, "used")
    record.solution = "overwritten"
    record.source_fact_ids.append("after")
    first = capture.result()
    snapshot = first["stages"][0]["inputs"][0]["record"]
    assert snapshot["solution"] == "original solution" and snapshot["source_fact_ids"] == ["before"]
    snapshot["solution"] = "caller mutation"
    assert capture.result()["stages"][0]["inputs"][0]["record"]["solution"] == "original solution"
    assert capture.result(["fact_decision"])["status"] == "unavailable"


@pytest.mark.parametrize("mode", ["normal", "error", "empty"])
def test_experience_decision_captures_presented_candidates_before_execution(mode, monkeypatch):
    async def scenario():
        old = experience()
        other = experience("not-offered")
        capture = ReconcileInputCapture()
        observed = []

        async def complete(**kwargs):
            observed.append(kwargs["prompt"])
            if mode == "error":
                raise RuntimeError("synthetic private exception text")
            return SimpleNamespace(
                content=json.dumps({"ops": [{"action": "ADD", "draft_idx": 0}]})
                if mode == "normal"
                else "{}"
            )

        rec = CodingMemoryReconciler(SimpleNamespace(), None, SimpleNamespace(complete=complete))

        async def retrieve(drafts):
            return [[old], [other]]

        async def execute(ops, drafts, candidates):
            old.solution = "changed during execution"
            return ops

        monkeypatch.setattr(rec, "_retrieve_candidates", retrieve)
        monkeypatch.setattr(rec, "_execute", execute)
        await rec.reconcile([CodingMemoryDraft(task="Synthetic")], input_capture=capture)
        result = capture.result()
        stage = result["stages"][0]
        assert result["status"] == "complete"
        assert stage["status"] == ("used" if mode == "normal" else "fallback")
        assert stage["rendered"] in observed[0]
        assert [item["id"] for item in stage["inputs"]] == ["old"]
        assert stage["inputs"][0]["record"]["solution"] == "original solution"
        assert "private exception" not in json.dumps(result)

    asyncio.run(scenario())


def test_details_capture_uses_only_budget_selected_generations(monkeypatch):
    import hy_memory.coding.fact_reconciler as module

    older = fact("older", details="O" * 100)
    recent = fact("recent", details="R" * 100)
    blank = fact("blank", details="")
    monkeypatch.setattr(module, "_MAX_INHERITED_DETAILS_CHARS", 140)
    selected = []
    block, taken, skipped = _build_inherited_details([older, recent, blank], selected=selected)
    assert taken == 1 and skipped == 1 and selected == [recent]
    assert "R" * 100 in block and "O" * 100 not in block


def test_concurrent_calls_on_one_reconciler_keep_distinct_inputs(monkeypatch):
    async def scenario():
        reached = asyncio.Event()
        entered = 0

        async def complete(**kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                reached.set()
            await reached.wait()
            return SimpleNamespace(content='{"ops":[{"action":"ADD","draft_idx":0}]}')

        rec = CodingMemoryReconciler(None, None, SimpleNamespace(complete=complete))

        async def retrieve(drafts):
            return [[experience(drafts[0].task)]]

        async def execute(ops, drafts, candidates):
            return ops

        monkeypatch.setattr(rec, "_retrieve_candidates", retrieve)
        monkeypatch.setattr(rec, "_execute", execute)
        captures = [ReconcileInputCapture(), ReconcileInputCapture()]
        await asyncio.gather(
            *[
                rec.reconcile([CodingMemoryDraft(task=identity)], input_capture=capture)
                for identity, capture in zip(("first", "second"), captures, strict=True)
            ]
        )
        assert [capture.result()["stages"][0]["inputs"][0]["id"] for capture in captures] == [
            "first",
            "second",
        ]

    asyncio.run(scenario())


def test_empty_fact_candidates_are_captured_without_a_model_call():
    async def scenario():
        async def retrieve(*args, **kwargs):
            return [[]]

        async def execute(*args, **kwargs):
            return [], []

        rec = WorkingFactReconciler(None, None, SimpleNamespace())
        rec._retrieve_candidates = retrieve
        rec._execute = execute
        capture = ReconcileInputCapture()
        await rec.reconcile(
            [WorkingFactDraft(statement="Synthetic")], user_id="scope", input_capture=capture
        )
        assert capture.result()["stages"][0]["status"] == "skipped"
        assert capture.result()["stages"][0]["inputs"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_summary_inputs_and_fallback_are_separate_from_details(failure):
    async def scenario():
        prompts = []

        async def complete(**kwargs):
            prompts.append(kwargs["prompt"])
            if failure:
                raise RuntimeError("private model message")
            return SimpleNamespace(content="Synthetic summary")

        rec = WorkingFactReconciler(None, None, SimpleNamespace(complete=complete))
        rec.enable_chain_summary = True
        capture = ReconcileInputCapture()
        old = fact("old", details="details excluded from summary")
        result = await rec._summarize_chain(
            [old], "current", truncated=True, input_capture=capture, output_id="new"
        )
        stage = capture.result()["stages"][0]
        assert stage["stage"] == "fact_summary" and stage["output_id"] == "new"
        assert stage["status"] == ("fallback" if failure else "used")
        assert bool(result) != failure and stage["rendered"] in prompts[0]
        assert "details excluded" not in stage["rendered"]
        assert "older revisions omitted" in stage["rendered"]
        assert stage["inputs"][0]["record"]["details"] == old.details

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["working", "coding"])
def test_real_writer_update_and_fact_history_survive_durable_reopen(tmp_path, monkeypatch, variant):
    config = config_for(tmp_path)
    request_id = str(uuid4())

    async def scenario():
        async with fixture(config) as (app, _):
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            store = CodingMemoryStore(
                sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
            )
            prompts = []

            async def complete(**kwargs):
                prompts.append(kwargs)
                if kwargs["audit_stage"] == "coding_reconcile":
                    payload = {
                        "ops": [{"action": "UPDATE", "draft_idx": 0, "target_experience_id": "old"}]
                    }
                elif kwargs["audit_stage"] == "coding_fact_summary":
                    return SimpleNamespace(content="Synthetic historical summary")
                else:
                    payload = {"ops": [{"draft_idx": 0, "supersedes": ["previous"]}]}
                return SimpleNamespace(content=json.dumps(payload))

            provider = SimpleNamespace(complete=complete)
            rec = CodingMemoryReconciler(store, sdk._embed_service, provider)
            fact_rec = WorkingFactReconciler(store, sdk._embed_service, provider)
            fact_rec.enable_chain_summary = True
            fact_rec.inherit_details = True

            async def prepare():
                await store.initialize()
                await store.insert(experience())
                await store.insert_fact(
                    fact("ancestor", details="older value", created_at=datetime(2026, 1, 1))
                )
                await store.insert_fact(
                    fact("previous", details="previous value", created_at=datetime(2026, 1, 2))
                )
                await store.supersede_fact("ancestor", "previous")

            async def extract(messages, **kwargs):
                return [
                    CodingMemoryDraft(
                        task="Synthetic build fix",
                        solution="new solution",
                        user_id="scope",
                        agent_id="agent",
                        session_id="session",
                    )
                ], [
                    WorkingFactDraft(
                        statement="Synthetic build progress previous", details="new value"
                    )
                ]

            writer = CodingWriter(
                store,
                SimpleNamespace(extract=extract),
                rec,
                provider,
                sdk._embed_service,
                fact_reconciler=fact_rec,
            )

            async def get_writer(**kwargs):
                return writer

            monkeypatch.setattr(sdk, "_get_coding_writer", get_writer)
            await _joined_thread(lambda: sdk._loop_thread.run(prepare()))
            messages = [message.to_dict() for message in edit_messages()]
            options = dict(
                user_id="scope",
                agent_id="agent",
                session_id="session",
                request_id=request_id,
                scene=variant,
            )
            try:
                result = await _joined_thread(sdk.add, messages, **options)
                captured = result["reconcile_inputs"]
                assert captured["status"] == "complete"
                stages = {stage["stage"]: stage for stage in captured["stages"]}
                assert set(stages) == {
                    "experience_decision",
                    "fact_decision",
                    "fact_summary",
                    "fact_details",
                }
                assert (
                    stages["experience_decision"]["inputs"][0]["record"]["solution"]
                    == "original solution"
                )
                assert stages["fact_decision"]["inputs"][0]["record"]["status"] == "active"
                assert {row["id"] for row in stages["fact_summary"]["inputs"]} == {
                    "ancestor",
                    "previous",
                }
                assert stages["fact_summary"]["output_id"] in result["fact_ids"]
                current = await _joined_thread(
                    lambda: sdk._loop_thread.run(store.get_by_id("old", "scope"))
                )
                assert current.solution == "new solution"
                count = len(prompts)
                replay = await _joined_thread(sdk.add, messages, **options)
                assert replay["reconcile_inputs"] == captured and len(prompts) == count
                saved = await bridge.get_sdk_write_outcome(request_id)
                assert saved["result"]["reconcile_inputs"] == captured
            finally:
                await _joined_thread(lambda: sdk._loop_thread.run(store.close()))
        async with fixture(config) as (app, _):
            saved = await _sdk_library_bridge(app.core).get_sdk_write_outcome(request_id)
            assert saved["result"]["reconcile_inputs"] == captured

    asyncio.run(scenario())
