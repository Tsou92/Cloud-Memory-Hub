"""FR-06/10/11: isolated installed HTTP replay after ordinary-memory forgetting.

Run beside probe_agent_access.py, probe_claude_recall.py and probe_claude_native.py
with the installed Hub Python. Only temporary identities/storage and a synthetic
extractor are used. No external model, existing Agent configuration, purge or
backup restore is exercised. The current blocked client queue is reported as a
limitation, not as completed client-side rule propagation.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import hy_memory.pipelines.writer as native_writer
from cloud_memory_hub.agent_config import AgentSettings
from cloud_memory_hub.events import prepare_event
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner
from one_memory.memory_server.durable_queue import COMPLETED
from probe_agent_access import LiveHub, device_config

TEXT = "Synthetic orchard plan: the shared release meeting takes place every Friday afternoon."
NEW_TEXT = "Synthetic new packaging detail accompanies the old orchard plan."
LATER_TEXT = "Synthetic unrelated telescope progress follows the suppressed event."


def invoke(settings, command, payload=None):
    """Each invocation reopens the installed client's persistent local queue."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cloud_memory_hub.agent_cli",
            "--config-file",
            str(settings),
            command,
        ],
        input=json.dumps(payload or {}),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, "installed_cli_failed_" + command
    return json.loads(result.stdout)


async def stop_drain(hub):
    hub.app.core.state.memory_write_drain.stop()
    await hub.app.core.state.memory_write_drain_task


async def seed_memory(hub, receipt):
    """Reuse the governance probe's real SDK write and lineage observation."""
    core = hub.app.core
    sdk = await _sdk_library_bridge(core)._get_client()
    queue = core.state.memory_write_queue
    frozen = queue.payload_for(receipt)
    assert frozen is not None, "seed_payload_missing"
    request = MemoryTaskRunner.request_for(frozen.idempotency_key, frozen.input_records)

    async def extract(self, **kwargs):
        return AgentResult(success=True, extracted_info={"memory": [{"content": TEXT}]})

    with (
        patch.object(MemAgent, "process_add", extract),
        patch.object(native_writer, "_RECONCILE_ENABLED", False),
    ):
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
    assert result["success"] and len(result["memory_ids"]) == 1, "seed_sdk_write_failed"
    await core.state.cmh_lineage.observe(receipt, frozen)
    queue.settle(receipt, state=COMPLETED)
    return result["memory_ids"][0]


def enqueue(service, texts):
    identity = str(uuid4())
    event = prepare_event(
        {
            "event_id": identity,
            "instance_id": service.connection.instance_id,
            "device_id": service.connection.device_id,
            "source_id": service.source_id,
            "scope_id": service.scope_id,
            "conversation_id": str(uuid4()),
            "record_id": identity,
            "revision": 1,
            "kind": "conversation_turn",
            "content": {"messages": [{"role": "user", "text": text} for text in texts]},
        },
        service.privacy,
    )
    payload = event.model_dump(mode="json")
    result = service.outbox.enqueue(payload)
    assert result["status"] == "queued" and result["receipt_id"] is None
    return payload


def server_state(hub):
    """Compare only authoritative identifiers, never transcript or secret data."""
    with sqlite3.connect(hub.app.core.state.memory_write_queue.database_path) as db:
        jobs = db.execute(
            "SELECT job_id,idempotency_key FROM memory_write_jobs ORDER BY job_id"
        ).fetchall()
    with hub.app.core.state.cmh_ingest._database() as db:
        receipts = db.execute(
            "SELECT device_id,event_id,receipt_id FROM cmh_event_receipts "
            "ORDER BY device_id,event_id"
        ).fetchall()
        receipts = [tuple(row) for row in receipts]
    return jobs, receipts


def forget(hub, scope, memory_id):
    preview = hub.call(
        "/hub/v1/governance/preview",
        method="POST",
        body={"scope_id": scope, "action": "forget", "record_ids": [memory_id], "limit": 50},
    )
    result = hub.call(
        "/hub/v1/governance/execute",
        method="POST",
        body={
            "scope_id": scope,
            "action": "forget",
            "record_ids": [memory_id],
            "expected_view": preview["view_digest"],
            "operation_id": str(uuid4()),
        },
    )
    assert result["state"] == "completed", "forget_not_completed"


def exercise(hub, root):
    hub.run(stop_drain(hub))
    scope = hub.call(
        "/hub/v1/projects",
        method="POST",
        status=201,
        body={"display_name": "Synthetic offline suppression"},
    )["project_id"]
    first = device_config(hub, root / "first", scope, "Synthetic writer")
    second = device_config(hub, root / "second", scope, "Synthetic offline reader")
    initial = invoke(first, "remember", {"text": TEXT, "request_id": str(uuid4())})
    assert initial["status"] == "accepted", "seed_event_not_accepted"
    memory_id = hub.run(seed_memory(hub, initial["receipt_id"]))
    before = invoke(second, "search", {"query": TEXT})
    assert any(row["record_id"] == memory_id for row in before["items"]), "seed_not_readable"

    # B's distinct source and fresh record identities eliminate a source-only
    # refusal; the old long sentence must suppress this whole mixed event.
    service = AgentSettings.from_file(second).open()
    mixed = enqueue(service, [NEW_TEXT, TEXT])
    later = enqueue(service, [LATER_TEXT])
    hub.offline = True
    offline = invoke(second, "sync")
    assert offline["delivery"] == "failed" and offline["retryable"] is True
    assert offline["error"] == "remote_temporarily_unavailable"
    assert offline["queued"] == 2 and offline["accepted"] == 0, "offline_queue_not_retained"
    assert len(server_state(hub)[1]) == 1, "offline_events_received_by_server"

    forget(hub, scope, memory_id)
    baseline = server_state(hub)
    hub.offline = False
    resumed = invoke(second, "sync")
    assert resumed["delivery"] == "failed" and resumed["retryable"] is False
    assert resumed["error"] == "remote_identity_conflict", "unexpected_client_409_mapping"
    assert resumed["queued"] == 2 and resumed["accepted"] == 0, "blocked_queue_changed"
    assert server_state(hub) == baseline, "rejected_replay_changed_server_intake"

    # Observe the precise public server reason without relying on the client's
    # current generic 409 mapping. The duplicate rejection remains side-effect free.
    rejection = hub.call(
        "/hub/v1/events",
        method="POST",
        status=409,
        credential=service.connection.credential.get_secret_value(),
        body=mixed,
    )
    assert rejection == {"detail": "governance_content_suppressed"}
    assert server_state(hub) == baseline, "repeated_rejection_changed_server_intake"
    pending = AgentSettings.from_file(second).open().outbox.pending()
    assert [row.event_id for row in pending] == [mixed["event_id"], later["event_id"]]
    after = invoke(second, "search", {"query": TEXT})
    assert not any(row["record_id"] == memory_id for row in after["items"]), "forgotten_recalled"
    history = hub.call(
        "/hub/v1/history/get",
        method="POST",
        credential=service.connection.credential.get_secret_value(),
        body={"scope_id": scope, "record_id": "cmhe:" + initial["receipt_id"]},
    )
    assert len(history["items"]) == 1 and TEXT in history["items"][0]["content"]
    return {
        "server_checks_passed": [
            "peer_read_before_forget",
            "offline_queue_persisted_across_client_processes",
            "ordinary_forget_completed",
            "changed_source_mixed_event_rejected_in_full",
            "no_new_receipt_or_write_job_after_reconnect",
            "forgotten_memory_not_returned_by_search",
            "retained_original_explicitly_readable",
        ],
        "server_rejection": "governance_content_suppressed",
        "client_error": resumed["error"],
        "client_pending": resumed["queued"],
        "client_limitation": "permanent_rejection_retains_and_blocks_later_queue_items",
        "client_rule_propagation_verified": False,
        "external_model_used": False,
        "real_two_host_network_test": False,
    }


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="cmh-offline-suppression-") as temporary:
        root = Path(temporary)
        hub = LiveHub(root)
        try:
            result = exercise(hub, root)
        finally:
            hub.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
