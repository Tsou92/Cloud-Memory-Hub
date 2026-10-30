import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_inventory import observe_inventory
from cloud_memory_hub.governance_plan import plan_relations
from cloud_memory_hub.privacy import PrivacyError
from cloud_memory_hub.productivity_read import record_id
from cloud_memory_hub.reconcile_evidence import capture_inputs, digest, encode, stored_inputs
from cloud_memory_hub.reconcile_graph import stage_id
from cloud_memory_hub.remote_access import AccessError
from hy_memory.coding.fact_reconciler import WorkingFactReconciler
from hy_memory.coding.reconciler import CodingMemoryReconciler
from hy_memory.coding.store import CodingMemoryStore
from hy_memory.coding.types import CodingMemoryDraft, WorkingFactDraft
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture, read_memory
from test_remote_ingest import provision
from test_remote_read import grant_reads
from test_sdk_reconcile import sdk_request


async def produce(app, web, config, monkeypatch, variant, *, facts=False):
    from hy_memory.coding.writer import CodingWriter

    admin, headers, _, event = await provision(web, config)
    event["content"]["messages"].append({"role": "tool", "text": "Synthetic edit result"})
    queue = app.core.state.memory_write_queue
    bridge = _sdk_library_bridge(app.core)
    sdk = await bridge._get_client()
    store = CodingMemoryStore(
        sdk._config, sdk._embed_service, db_path=sdk._config.coding.db_path, variant=variant
    )
    await _joined_thread(lambda: sdk._loop_thread.run(store.initialize()))
    jobs, requests, results, calls = [], [], [], []

    async def complete(**kwargs):
        calls.append(kwargs["audit_stage"])
        if kwargs["audit_stage"] == "coding_fact_summary":
            return SimpleNamespace(content="Synthetic history summary")
        if kwargs["audit_stage"] == "coding_reconcile":
            op = {"draft_idx": 0, "action": "ADD" if not results else "UPDATE"}
            if results:
                op["target_experience_id"] = results[0]["experience_ids"][0]
        else:
            op = {"draft_idx": 0, "supersedes": results[-1]["fact_ids"] if results else []}
        return SimpleNamespace(content=json.dumps({"ops": [op]}))

    async def extract(messages, **kwargs):
        request = requests[-1]
        return [
            CodingMemoryDraft(
                task="Synthetic task",
                solution=f"version-{len(results)}",
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
            )
        ], [
            WorkingFactDraft(statement="Synthetic progress", details=f"fact-{len(results)}")
        ] if facts else []

    provider = SimpleNamespace(complete=complete)
    fact_rec = WorkingFactReconciler(store, sdk._embed_service, provider)
    fact_rec.enable_chain_summary = fact_rec.inherit_details = True
    writer = CodingWriter(
        store,
        SimpleNamespace(extract=extract),
        CodingMemoryReconciler(store, sdk._embed_service, provider),
        provider,
        sdk._embed_service,
        fact_reconciler=fact_rec,
    )

    async def get_writer(**kwargs):
        return writer

    monkeypatch.setattr(sdk, "_get_coding_writer", get_writer)
    try:
        for _ in range(3):
            current = {**event, "event_id": str(uuid4()), "record_id": str(uuid4())}
            job = await accepted(web, headers, current)
            jobs.append(job)
            frozen = queue.payload_for(job)
            request = sdk_request(frozen)
            requests.append(request)
            result = await _joined_thread(
                sdk.add,
                [turn.model_dump() for turn in request.turns],
                scene=variant,
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                source=request.source_id,
                request_id=request.request_id,
            )
            assert result["success"] and result["productivity_variant"] == variant
            results.append(result)
            await app.core.state.cmh_lineage.observe(job, frozen)
            queue.settle(job, state=COMPLETED)
        assert len({result["experience_ids"][0] for result in results}) == 1
        await grant_reads(web, admin, event)
        return jobs, requests, results, headers, event, calls
    finally:
        await _joined_thread(lambda: sdk._loop_thread.run(store.close()))


@pytest.mark.parametrize("variant", ["working", "coding"])
@pytest.mark.parametrize("facts", [False, True])
def test_actual_sdk_multiround_graph_has_no_self_or_future_origin(
    tmp_path, monkeypatch, variant, facts
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, headers, event, calls = await produce(
                app, web, config, monkeypatch, variant, facts=facts
            )
            prefix = "w" if variant == "working" else "c"
            identity = record_id(prefix, "e", results[-1]["experience_ids"][0])
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            first, second, third = (stage_id(job, 0) for job in jobs)
            assert nodes[first].parents == {"cmhe:" + jobs[0]}
            assert nodes[second].parents == {"cmhe:" + jobs[0], "cmhe:" + jobs[1], first}
            assert second in nodes[third].parents and identity not in nodes[second].parents
            assert "cmhe:" + jobs[2] not in nodes[second].parents
            selected = plan_relations(nodes, (second,))
            assert third in selected["record_ids"] and identity in selected["record_ids"]
            assert "cmhe:" + jobs[2] not in selected["record_ids"]
            if facts:
                assert all(result["fact_ids"] for result in results)
                assert any(
                    node.layer == "reconcile_input" and "fact_summary" in node.content
                    for node in nodes.values()
                )
            with app.core.state.cmh_ingest._database() as db:
                saved = stored_inputs(db, jobs[1], requests[1])
                assert saved["capture"] == results[1]["reconcile_inputs"]
            app.core.state.memory_write_queue.clear_terminal_payloads()
        async with fixture(config) as (app, web):
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            assert nodes[second].parents == {"cmhe:" + jobs[0], "cmhe:" + jobs[1], first}
            reply = await read_memory(web, headers, event, identity)
            assert reply.status_code == 200, reply.text
            assert set(reply.json()["items"][0]["source_raw_ids"]) >= {
                "cmhe:" + job for job in jobs
            }

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cycle", "missing", "foreign", "cross_project", "budget"])
def test_producer_resolution_rejects_cycles_unknown_requests_cross_scope_and_limits(
    tmp_path, monkeypatch, failure
):
    from cloud_memory_hub.governance_journal import GovernanceError
    from hy_memory.coding.record_origin import record_digest

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, *_ = await produce(app, web, config, monkeypatch, "working")
            with app.core.state.cmh_ingest._database() as db:
                value = stored_inputs(db, jobs[1], requests[1])
            record = value["capture"]["stages"][0]["inputs"][0]["record"]
            target = jobs[1]
            if failure == "cycle":
                # A manufactured backward dependency A -> B complements the real B -> A.
                target = jobs[0]
                record["origin_request_id"] = requests[1].request_id
            elif failure == "missing":
                record["origin_request_id"] = "unknown-producing-request"
            elif failure == "cross_project":
                _, other_headers, _, other_event = await provision(web, config)
                other = await accepted(web, other_headers, other_event)
                record["origin_request_id"] = sdk_request(
                    app.core.state.memory_write_queue.payload_for(other)
                ).request_id
            elif failure == "foreign":
                # Valid digest alone cannot establish the producer's source/agent identity.
                record["agent_id"] = "different-agent"
            else:
                monkeypatch.setattr("cloud_memory_hub.governance_inventory.MAX_EDGES", 1)
            record["origin_digest"] = record_digest("experience", record)
            encoded = encode(value)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "UPDATE cmh_event_inputs SET inputs_json=?,inputs_digest=? WHERE receipt_id=?",
                    (encoded, digest(encoded), target),
                )
            with pytest.raises(
                GovernanceError,
                match={
                    "cycle": "governance_input_cycle",
                    "missing": "governance_input_original_unproven",
                    "foreign": "governance_input_scope_mismatch",
                    "cross_project": "governance_input_scope_mismatch",
                    "budget": "governance_scan_limit",
                }[failure],
            ):
                await observe_inventory(
                    _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
                )

    asyncio.run(scenario())


def test_fallback_copy_is_retained_without_becoming_output_input(tmp_path, monkeypatch):
    from cloud_memory_hub.output_lineage import _digest
    from cloud_memory_hub.reconcile_graph import ReconcileGraph

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, *_ = await produce(app, web, config, monkeypatch, "working")
            value = capture_inputs(results[1], requests[1])
            value["capture"]["stages"][0]["status"] = "fallback"
            encoded = encode(value)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "UPDATE cmh_event_inputs SET inputs_json=?,inputs_digest=? WHERE receipt_id=?",
                    (encoded, digest(encoded), jobs[1]),
                )
            with app.core.state.cmh_ingest._database() as db:
                lineage = app.core.state.cmh_lineage
                graph = ReconcileGraph(
                    db,
                    app.core.state.cmh_ingest.access.instance_id,
                    requests[0].user_id.rsplit(":", 1)[1],
                    lineage._stored_links,
                    _digest,
                )
                identity = record_id("w", "e", results[1]["experience_ids"][0])
                assert graph.output_parents(jobs[1], identity) == frozenset()
            nodes = await observe_inventory(
                _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
            )
            assert stage_id(jobs[1], 0) in nodes
            assert stage_id(jobs[1], 0) not in nodes[identity].parents
            assert "cmhe:" + jobs[0] in nodes[stage_id(jobs[1], 0)].parents

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["pending", "duplicate", "unknown", "large", "self", "shape"])
def test_capture_rejects_malformed_or_unbounded_evidence(damage, monkeypatch):
    from hy_memory.coding.input_capture import ReconcileInputCapture
    from hy_memory.coding.record_origin import stamp_origin
    from test_reconcile_input_capture import experience

    record = experience()
    stamp_origin(record, "experience", "prior")
    capture = ReconcileInputCapture()
    token = capture.begin("experience_decision", "experience", [record], "actually shown")
    capture.end(token, "used")
    result = {
        "scene": "productivity",
        "productivity_variant": "working",
        "reconcile_inputs": capture.result(),
    }
    stage = result["reconcile_inputs"]["stages"][0]
    if damage == "pending":
        stage["status"] = "pending"
    elif damage == "duplicate":
        result["reconcile_inputs"]["stages"].append(copy.deepcopy(stage))
    elif damage == "unknown":
        stage["stage"] = "unrecognized"
    elif damage == "large":
        monkeypatch.setattr("cloud_memory_hub.reconcile_evidence.MAX_BYTES", 10)
    elif damage == "shape":
        stage["inputs"][0]["record"]["boundary_envs"] = []
    with pytest.raises(ValueError):
        capture_inputs(
            result,
            SimpleNamespace(user_id="scope", request_id="prior" if damage == "self" else "now"),
        )


@pytest.mark.parametrize("damage", ["version", "unrecognized", "digest", "duplicate_json", "size"])
def test_input_archive_schema_and_storage_checks_fail_closed(damage, monkeypatch):
    from cloud_memory_hub.reconcile_evidence import initialize_inputs

    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE cmh_event_outcomes(receipt_id TEXT PRIMARY KEY)")
        if damage == "unrecognized":
            db.execute("CREATE TABLE cmh_event_inputs(receipt_id TEXT)")
            with pytest.raises(AccessError, match="lineage_inputs_store_unrecognized"):
                initialize_inputs(db)
            return
        initialize_inputs(db)
        if damage == "version":
            db.execute("UPDATE cmh_inputs_schema SET version=99")
            with pytest.raises(AccessError, match="lineage_inputs_store_version_unsupported"):
                initialize_inputs(db)
            return
        text = encode({"variant": "w", "capture": None})
        if damage == "duplicate_json":
            text = '{"variant":"w","capture":null,"capture":null}'
        if damage == "size":
            monkeypatch.setattr("cloud_memory_hub.reconcile_evidence.MAX_BYTES", 2)
        db.execute(
            "INSERT INTO cmh_event_inputs VALUES (?,?,?)",
            ("receipt", text, "0" * 64 if damage == "digest" else digest(text)),
        )
        with pytest.raises(PrivacyError if damage == "duplicate_json" else ValueError):
            stored_inputs(db, "receipt", SimpleNamespace(user_id="scope", request_id="now"))


def test_legacy_absence_is_distinct_from_observed_empty_and_reserved_ids_are_rejected():
    from cloud_memory_hub.governance_inventory import _node
    from cloud_memory_hub.governance_journal import GovernanceError

    request = SimpleNamespace(user_id="scope", request_id="now")
    result = {"scene": "productivity", "productivity_variant": "working"}
    assert capture_inputs(result, request)["capture"] is None
    result["reconcile_inputs"] = {"schema_version": 1, "status": "complete", "stages": []}
    assert capture_inputs(result, request)["capture"]["status"] == "complete"
    result["experience_ids"] = ["produced"]
    with pytest.raises(ValueError, match="lineage_input_stage_missing"):
        capture_inputs(result, request)
    with pytest.raises(GovernanceError, match="governance_record_identity_mismatch"):
        _node({"user_id": "scope", "node_id": "cmhi:receipt:0"}, "scope")


@pytest.mark.parametrize(
    "failure", ["digest", "scope", "origin", "target", "unavailable", "variant", "missing_stage"]
)
def test_snapshot_corruption_or_missing_producer_never_yields_partial_graph(
    tmp_path, monkeypatch, failure
):
    from cloud_memory_hub.governance_journal import GovernanceError

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, *_ = await produce(app, web, config, monkeypatch, "working")
            inputs = capture_inputs(results[1], requests[1])
            if failure == "variant":
                inputs["variant"] = "c"
            elif failure == "missing_stage":
                inputs["capture"]["stages"] = []
            elif failure == "scope":
                inputs["capture"]["stages"][0]["inputs"][0]["record"]["user_id"] = "foreign"
            elif failure == "origin":
                inputs["capture"]["stages"][0]["inputs"][0]["record"]["origin_request_id"] = None
                inputs["capture"]["stages"][0]["inputs"][0]["record"]["origin_digest"] = None
            elif failure == "unavailable":
                inputs["capture"] = {"schema_version": 1, "status": "unavailable", "stages": []}
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                if failure == "target":
                    db.execute("DELETE FROM cmh_event_output_links WHERE receipt_id=?", (jobs[0],))
                    from cloud_memory_hub.output_lineage import _digest

                    db.execute(
                        "UPDATE cmh_event_outcomes SET links_digest=? WHERE receipt_id=?",
                        (_digest(()), jobs[0]),
                    )
                else:
                    value = encode(inputs)
                    db.execute(
                        "UPDATE cmh_event_inputs SET inputs_json=?,inputs_digest=? "
                        "WHERE receipt_id=?",
                        (value, "0" * 64 if failure == "digest" else digest(value), jobs[1]),
                    )
            with pytest.raises((ValueError, GovernanceError)):
                await observe_inventory(
                    _sdk_library_bridge(app.core), requests[0].user_id, app.core.state.cmh_lineage
                )

    asyncio.run(scenario())


def test_evidence_failure_rolls_back_all_receipt_extensions_and_recovery_is_idempotent(
    tmp_path, monkeypatch
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, requests, results, *_ = await produce(app, web, config, monkeypatch, "working")
            lineage = app.core.state.cmh_lineage
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute("DELETE FROM cmh_event_inputs WHERE receipt_id=?", (jobs[-1],))
                db.execute("DELETE FROM cmh_event_contexts WHERE receipt_id=?", (jobs[-1],))
                db.execute("DELETE FROM cmh_event_output_links WHERE receipt_id=?", (jobs[-1],))
                db.execute("DELETE FROM cmh_event_outcomes WHERE receipt_id=?", (jobs[-1],))
                db.execute(
                    "CREATE TRIGGER reject_input BEFORE INSERT ON cmh_event_inputs "
                    "BEGIN SELECT RAISE(ABORT,'synthetic archive failure'); END"
                )
            with pytest.raises(AccessError, match="access_store_unavailable"):
                await lineage.observe(jobs[-1], None)
            with app.core.state.cmh_ingest._database() as db:
                for table in (
                    "cmh_event_outcomes",
                    "cmh_event_output_links",
                    "cmh_event_contexts",
                    "cmh_event_inputs",
                ):
                    assert not db.execute(
                        f"SELECT 1 FROM {table} WHERE receipt_id=?", (jobs[-1],)
                    ).fetchone()
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute("DROP TRIGGER reject_input")
            sdk = await _sdk_library_bridge(app.core)._get_client()
            monkeypatch.setattr(sdk, "add", lambda *a, **k: pytest.fail("recovery must not write"))
            assert await lineage.reconcile_completed() == 1
            await lineage.observe(jobs[-1], None)
            with app.core.state.cmh_ingest._database() as db:
                assert (
                    stored_inputs(db, jobs[-1], requests[-1])["capture"]
                    == results[-1]["reconcile_inputs"]
                )
            changed = copy.deepcopy(results[-1])
            changed["reconcile_inputs"]["stages"][0]["rendered"] += " changed"
            event, _ = lineage._request(jobs[-1], None)
            with app.core.state.cmh_ingest._database() as db:
                links = lineage._stored_links(db, jobs[-1])
                from cloud_memory_hub.input_context import stored_context

                context = stored_context(db, jobs[-1])
            with pytest.raises(ValueError, match="lineage_inputs_changed"):
                lineage._commit(
                    jobs[-1],
                    event,
                    links,
                    context,
                    capture_inputs(changed, requests[-1]),
                    {"scene": "productivity", "capture": None},
                )

    asyncio.run(scenario())
