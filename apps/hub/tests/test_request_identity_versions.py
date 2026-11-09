"""FR-10/FR-11: a valid new birth never substitutes for another accepted birth."""

import asyncio
import sqlite3
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope, split_request_identity
from cloud_memory_hub.original_archive import _recover_original, recover_event, validate_original
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import write_request
from test_governance_candidates import confirmed, observe
from test_original_archive import config_for
from test_original_proof import event_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


def request_id(event):
    return f"cmh2:{event.instance_id}:{uuid4()}:{event.device_id}:{event.event_id}"


def receipt(event, identity):
    return {
        **event.model_dump(),
        "request_id": identity,
        "payload_json": event.canonical_payload(),
        "payload_digest": event.payload_digest(),
    }


@pytest.mark.parametrize("version", [1, 2])
def test_both_internal_versions_preserve_full_original_roundtrip(version):
    event = event_for()
    identity = request_id(event) if version == 2 else None
    request = write_request(event, request_id=identity)
    parts = split_request_identity(request.idempotency_key)
    assert parts[:3] == (event.instance_id, event.device_id, event.event_id)
    assert (parts[3] is not None) == (version == 2)
    assert recover_event(request) == event
    row = receipt(event, request.idempotency_key)
    assert validate_original(row, event.instance_id) == event
    assert _recover_original(request, row, event.instance_id) == event.canonical_payload()


@pytest.mark.parametrize("changed", ["birth", "version"])
def test_receipt_and_frozen_request_cannot_substitute_birth_or_version(changed):
    event = event_for()
    identity = request_id(event)
    original = receipt(event, identity)
    other = write_request(event, request_id=request_id(event) if changed == "birth" else None)
    assert recover_event(other) == event  # Self-consistency is not receipt authority.
    with pytest.raises(AccessError, match="^original_migration_source_unproven$"):
        _recover_original(other, original, event.instance_id)


@pytest.mark.parametrize("damage", ["empty", "extra", "missing", "uppercase", "other_event"])
def test_new_identity_is_strict_and_event_bound(damage):
    event = event_for()
    identity = request_id(event)
    if damage == "empty":
        identity = ""
    elif damage == "extra":
        identity += ":extra"
    elif damage == "missing":
        identity = identity.replace(":", "", 1)
    elif damage == "uppercase":
        identity = identity.upper()
    else:
        identity = identity.rsplit(":", 1)[0] + ":" + str(uuid4())
    with pytest.raises(ValueError):
        write_request(event, request_id=identity)


def test_candidate_rejects_frozen_birth_different_from_job_and_receipt(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            job = await accepted(web, headers, raw)
            scope = confirmed(app, raw, [job])
            assert len(observe(app, scope).confirmed_original_candidates) == 1
            queue = app.core.state.memory_write_queue
            frozen = queue.payload_for(job)
            altered = frozen.model_copy(
                update={"idempotency_key": request_id(EventEnvelope.model_validate(raw))}
            )
            with sqlite3.connect(queue.database_path) as db:
                db.execute(
                    "UPDATE memory_write_jobs SET payload_json=? WHERE job_id=?",
                    (altered.model_dump_json(), job),
                )
            result = observe(app, scope)
            assert result.confirmed_original_candidates == ()
            assert any(
                item.reason == "governance_candidate_payload_unproven" and item.job_id == job
                for item in result.blockers
            )

    asyncio.run(scenario())
