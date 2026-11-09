"""Installed governance: real Core/SDK/storage; synthetic model and supervisor."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import hy_memory.pipelines.writer as native_writer
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from httpx import ASGITransport, AsyncClient
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HyMemoryIngestResult, HyMemoryOutput, _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED, RUNNING
from probe_original_archive import environment
from probe_original_archive import phase as archive_phase

MARK = "CMH_SYNTHETIC_PURGE_MARK"


async def check_forget_history(app, sdk, web, device, first):
    """Observe a new real SDK write after restart/restore, without paid models."""
    for identity in (first["raw"], first["memory"]):
        node = await _joined_thread(
            lambda identity=identity: sdk._loop_thread.run(sdk._vector_store.get_by_id(identity))
        )
        assert node is not None and node.status.value == "archived" and not node.is_latest, (
            "restored_forget_target_not_suppressed"
        )
    bridge = _sdk_library_bridge(app.core)
    queue = app.core.state.memory_write_queue
    captured = []
    text = "Synthetic fresh weather fact after governance recovery."

    async def extract(self, **kwargs):
        captured.append(kwargs["history_context"])
        return AgentResult(success=True, extracted_info={"memory": [{"content": text}]})

    async def process(records, *, request_id, before_hy_invocation):
        async with bridge.exclusive_access():
            await before_hy_invocation()
            job = queue.get(request_id)
            frozen = queue.payload_for(job.job_id)
            request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)
            result = await _joined_thread(
                sdk.add,
                [turn.model_dump() for turn in request.turns],
                scene="chat",
                user_id=request.user_id,
                agent_id=request.agent_id,
                session_id=request.session_id,
                source=request.source_id,
                request_id=request.request_id,
            )
            assert result["success"] and len(result["memory_ids"]) == 1
            return HyMemoryIngestResult(
                success=True,
                raw_ids=(result["memory_id"],),
                memories=tuple(
                    HyMemoryOutput(memory_id=identity, content=text)
                    for identity in result["memory_ids"]
                ),
            )

    fresh = {
        **first["event"],
        **{key: str(uuid4()) for key in ("event_id", "record_id", "conversation_id")},
        "content": {"messages": [{"role": "user", "text": "A new weather topic after recovery."}]},
    }
    reply = await web.post("/hub/v1/events", headers=device, json=fresh)
    assert reply.status_code == 202, "fresh_post_recovery_event_rejected"
    receipt = reply.json()["receipt_id"]
    frozen = queue.payload_for(receipt)
    with (
        patch.object(MemAgent, "process_add", extract),
        patch.object(native_writer, "_RECONCILE_ENABLED", False),
        patch.object(app.core.state.memory_processing_supervisor, "process_sdk_job", process),
    ):
        queue.settle(receipt, state=RUNNING, hy_invoked=False)
        await asyncio.wait_for(app.core.state.memory_write_drain.execute(receipt), 30)
    assert queue.get(frozen.idempotency_key).state == COMPLETED, "post_recovery_write_not_completed"
    assert len(captured) == 1, "post_recovery_model_boundary_not_observed"
    forgotten = first["event"]["content"]["messages"][0]["text"]
    assert forgotten not in captured[0], "restored_forget_reentered_model_history"


def leftover(root, needle):
    # data_level0.bin slack is the documented Chroma gap (ADR-0032).
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "data_level0.bin"
        and needle.encode() in path.read_bytes()
    )


async def phase(root, mode):
    os.environ.update(environment(root))
    if mode == "seed":
        await archive_phase(root, "seed")
    state_path = root / "state.json"
    state = json.loads(state_path.read_text())
    config = HubConfig.from_environment()
    app = create_hub(config)
    async with app.core.router.lifespan_context(app.core):
        await asyncio.sleep(0)
        app.core.state.memory_write_drain.stop()
        await app.core.state.memory_write_drain_task
        sdk = await _sdk_library_bridge(app.core)._get_client()
        queue, lineage = app.core.state.memory_write_queue, app.core.state.cmh_lineage
        async with AsyncClient(transport=ASGITransport(app), base_url=config.public_origin) as web:
            event = state["event"]
            scope = event["scope_id"]
            admin = {"Authorization": "Bearer " + config.admin_token}
            device = {"Authorization": "Bearer " + state["credential"]}

            async def get(kind, identity):
                return (
                    await web.post(
                        f"/hub/v1/{kind}/get",
                        headers=device,
                        json={"scope_id": scope, "record_id": identity},
                    )
                ).status_code

            async def execute(action, selected, *, included_revision=None):
                view = await web.post(
                    "/hub/v1/governance/preview",
                    headers=admin,
                    json={
                        "scope_id": scope,
                        "action": action,
                        "record_ids": [selected],
                        "limit": 50,
                    },
                )
                assert view.status_code == 200, "preview_failed"
                if included_revision is not None:
                    revisions = {item["record_id"]: item for item in view.json()["items"]}
                    assert included_revision in revisions, "accepted_revision_missing_from_preview"
                    assert "evolution" in revisions[included_revision]["reasons"]
                request = {
                    "scope_id": scope,
                    "action": action,
                    "record_ids": [selected],
                    "expected_view": view.json()["view_digest"],
                    "operation_id": str(uuid4()),
                }
                reply = await web.post(
                    "/hub/v1/governance/execute",
                    headers=admin,
                    json=request,
                )
                assert reply.status_code == 200, "execute_failed"
                assert reply.json()["state"] == "completed", "execute_not_completed"
                return {"request": request, "result": reply.json()}

            if mode == "seed":
                await web.put(
                    "/hub/v1/devices/" + event["device_id"] + "/grants",
                    headers=admin,
                    json={
                        "expected_revision": 2,
                        "grants": [
                            {
                                "scope_id": scope,
                                "permissions": ["events:write", "history:read", "memory:read"],
                            }
                        ],
                    },
                )
                rows = []

                async def extract(self, **kwargs):
                    return AgentResult(
                        success=True,
                        extracted_info={"memory": [{"content": f"{MARK} fact {len(rows)}"}]},
                    )

                previous = MemAgent.process_add, native_writer._RECONCILE_ENABLED
                MemAgent.process_add, native_writer._RECONCILE_ENABLED = extract, False
                try:
                    for _ in range(2):
                        current = {
                            **event,
                            **{
                                k: str(uuid4())
                                for k in ("event_id", "record_id", "conversation_id")
                            },
                            "content": {
                                "messages": [
                                    {
                                        "role": "user",
                                        "text": f"Synthetic governed original {len(rows)} has "
                                        "distinct long content for reimport checks.",
                                    }
                                ],
                                "metadata": {"synthetic": True},
                            },
                        }
                        reply = await web.post("/hub/v1/events", headers=device, json=current)
                        assert reply.status_code == 202
                        receipt = reply.json()["receipt_id"]
                        frozen = queue.payload_for(receipt)
                        request = MemoryTaskRunner.request_for(
                            frozen.idempotency_key, frozen.input_records
                        )
                        result = await _joined_thread(
                            sdk.add,
                            [turn.model_dump() for turn in request.turns],
                            scene="chat",
                            user_id=request.user_id,
                            agent_id=request.agent_id,
                            session_id=request.session_id,
                            source=request.source_id,
                            request_id=request.request_id,
                        )
                        await lineage.observe(receipt, frozen)
                        queue.settle(receipt, state=COMPLETED)
                        rows.append(
                            {
                                "receipt": receipt,
                                "request_id": frozen.idempotency_key,
                                "memory": result["memory_ids"][0],
                                "raw": result["memory_id"],
                                "event": current,
                            }
                        )
                    queue.clear_terminal_payloads()
                finally:
                    MemAgent.process_add, native_writer._RECONCILE_ENABLED = previous
                # Accepted but intentionally not extracted: its stable source
                # identity must place it in the first original's version scope.
                revised = {
                    **rows[0]["event"],
                    "event_id": str(uuid4()),
                    "revision": 2,
                    "content": {
                        "messages": [
                            {
                                "role": "user",
                                "text": "Synthetic unprocessed second revision keeps its own "
                                "distinct long original for explicit history reads.",
                            }
                        ]
                    },
                }
                reply = await web.post("/hub/v1/events", headers=device, json=revised)
                assert reply.status_code == 202, "revision_accept_failed"
                state["unprocessed_revision"] = {
                    "receipt": reply.json()["receipt_id"],
                    "event": revised,
                }
                state["rows"] = rows
                state_path.write_text(json.dumps(state))
                return
            if mode == "govern":
                rows = state["rows"]
                # Forget the first turn, purge the second.
                state["governance_requests"] = [
                    await execute(
                        "forget",
                        "cmhe:" + rows[0]["receipt"],
                        included_revision="cmhe:" + state["unprocessed_revision"]["receipt"],
                    ),
                    await execute("purge", "cmhe:" + rows[1]["receipt"]),
                ]
                state_path.write_text(json.dumps(state))
                return
            first, second = state["rows"]
            assert await get("memories", first["memory"]) == 404
            assert await get("history", "cmhe:" + first["receipt"]) == 200
            assert await get("memories", second["memory"]) == 404
            assert await get("history", "cmhe:" + second["receipt"]) == 404
            assert (
                await get("history", "cmhe:" + state["unprocessed_revision"]["receipt"]) == 200
            ), "forgotten_revision_not_retained_for_explicit_read"
            journal = app.core.state.cmh_governance.journal
            checkpoint = journal.head(scope)
            assert checkpoint.sequence == 2, "unexpected_governance_intent_count"
            for saved in state["governance_requests"]:
                request = saved["request"]
                retried = await web.post("/hub/v1/governance/execute", headers=admin, json=request)
                assert retried.status_code == 200, "identical_governance_retry_failed"
                for key in ("operation_id", "scope_id", "action", "state", "suppressed_count"):
                    assert retried.json()[key] == saved["result"][key], "retry_result_mismatch"
                changed = {
                    **request,
                    "action": "purge" if request["action"] == "forget" else "forget",
                }
                conflict = await web.post("/hub/v1/governance/execute", headers=admin, json=changed)
                assert conflict.status_code == 409, "changed_action_retry_was_accepted"
                assert conflict.json() == {"detail": "governance_operation_conflict"}
                assert journal.head(scope) == checkpoint, "retry_appended_new_intent"
            for index, row in enumerate(state["rows"]):
                identity = row["request_id"]
                outcome = await _joined_thread(sdk.get_write_outcome, identity)
                assert (outcome is None) == (index == 1), "purge_result_cache_mismatch"
            # New revisions must not resurrect either forgotten or purged
            # originals, including after restoring the old business volumes.
            for row in state["rows"]:
                # Revision 2 of the first source is already retained above.
                revised = {**row["event"], "event_id": str(uuid4()), "revision": 3}
                reply = await web.post("/hub/v1/events", headers=device, json=revised)
                assert reply.status_code == 409, "source_reimport_was_accepted"
                assert reply.json() == {"detail": "governance_source_suppressed"}
                with sqlite3.connect(queue.database_path) as jobs:
                    assert not jobs.execute(
                        "SELECT 1 FROM memory_write_jobs WHERE idempotency_key LIKE ?",
                        ("%:" + revised["device_id"] + ":" + revised["event_id"],),
                    ).fetchone(), "suppressed_source_was_queued"
                with app.core.state.cmh_ingest._database() as db:
                    assert not db.execute(
                        "SELECT 1 FROM cmh_event_receipts WHERE event_id=?",
                        (revised["event_id"],),
                    ).fetchone(), "suppressed_source_received_receipt"
            # Each action has a distinct protected text. Re-registering the
            # source and changing every client record identity must not bypass
            # either rule, even when the event also contains new content.
            for action, row in zip(("forget", "purge"), state["rows"], strict=True):
                registered = await web.post(
                    "/hub/v1/sources",
                    headers=admin,
                    json={
                        "display_name": "Synthetic reimport " + action,
                        "adapter_id": "synthetic.reimport",
                        "device_id": event["device_id"],
                        "scope_id": scope,
                    },
                )
                assert registered.status_code == 201, "reimport_source_registration_failed"
                mixed = {
                    **row["event"],
                    **{key: str(uuid4()) for key in ("event_id", "conversation_id", "record_id")},
                    "source_id": registered.json()["source_id"],
                    "content": {
                        "messages": [
                            {"role": "user", "text": "New synthetic work accompanies old content."},
                            *row["event"]["content"]["messages"],
                        ]
                    },
                }
                reply = await web.post("/hub/v1/events", headers=device, json=mixed)
                assert reply.status_code == 409, "content_reimport_was_accepted_" + action
                assert reply.json() == {"detail": "governance_content_suppressed"}
                with sqlite3.connect(queue.database_path) as jobs:
                    assert not jobs.execute(
                        "SELECT 1 FROM memory_write_jobs WHERE idempotency_key LIKE ?",
                        ("%:" + mixed["device_id"] + ":" + mixed["event_id"],),
                    ).fetchone(), "suppressed_content_was_queued_" + action
                with app.core.state.cmh_ingest._database() as db:
                    assert not db.execute(
                        "SELECT 1 FROM cmh_event_receipts WHERE event_id=?", (mixed["event_id"],)
                    ).fetchone(), "suppressed_content_received_receipt_" + action
            app.core.state.cmh_governance.require_writes_ready()
            await check_forget_history(app, sdk, web, device, first)
            assert await get("history", first["raw"]) == 200, (
                "forgotten_raw_not_explicitly_readable"
            )


def run(root, mode):
    with (root / f"{mode}.log").open("w") as log:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
            stdout=log,
            stderr=log,
            timeout=180,
        )
    if result.returncode:
        raise RuntimeError("governance_execution_probe_failed_" + mode)


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "govern", "read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
        return
    with TemporaryDirectory(prefix="cmh-governance-execution-") as temporary:
        root = Path(temporary) / "instance"
        root.mkdir()
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env=environment(root),
            check=True,
            capture_output=True,
        )
        backup = Path(temporary) / "backup"
        run(root, "seed")
        # Business-data backup before governance; the governance dir is separate.
        for name in ("data", "config"):
            shutil.copytree(root / name, backup / name)
        run(root, "govern")
        run(root, "read")
        assert leftover(root, MARK + " fact 1") == [], "purge_residue"
        assert leftover(root, MARK + " fact 0"), "forget_must_keep_content"
        # Restore only the SDK outcome cache. No raw/vector node is restored,
        # so ID existence alone cannot trigger replay (ADR-0035).
        state = json.loads((root / "state.json").read_text())
        request_id = state["rows"][1]["request_id"]
        caches = list((backup / "data").rglob("cache.sqlite3"))
        assert len(caches) == 1, "cache_location_unproven"
        cache = caches[0]
        current_cache = root / "data" / cache.relative_to(backup / "data")
        with sqlite3.connect(cache) as source, sqlite3.connect(current_cache) as destination:
            source.backup(destination)
            assert destination.execute(
                "SELECT 1 FROM write_outcomes WHERE request_id=?", (request_id,)
            ).fetchone(), "cache_restore_fixture_invalid"
        run(root, "read")
        # Restore old business data: startup must reapply both forget and purge.
        for name in ("data", "config"):
            shutil.rmtree(root / name)
            shutil.copytree(backup / name, root / name)
        assert leftover(root, MARK + " fact 1"), "restore_fixture_invalid"
        run(root, "read")
        assert leftover(root, MARK + " fact 1") == [], "restore_replay_residue"
    print(
        "Forget, purge, restart, full/cache-only restore, identical/conflicting request retries, "
        "accepted revision scope/retention, source/content reimport prevention and restored "
        "forget exclusion from new SDK history passed across processes (synthetic extraction "
        "and supervisor; pending-job cancellation not verified)."
    )


if __name__ == "__main__":
    main()
