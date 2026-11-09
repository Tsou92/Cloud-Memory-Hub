"""ADR-0034: receipt-free frozen-input proof never asserts accepted originals."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.original_archive import (
    _recover_original,
    recover_event,
    require_frozen_match,
)
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_ingest import write_request
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_ingest import provision

MARKER = "\n\n<cmh_source_reference>\n"
SUFFIX = "\n</cmh_source_reference>"


def event_for(kind="conversation_turn"):
    content = {
        "messages": [
            {
                "role": "user",
                "text": "Synthetic original" + MARKER + "{}" + SUFFIX,
                "metadata": {"label": "synthetic first"},
            },
            {"role": "assistant", "text": "Synthetic response"},
        ],
        "metadata": {"label": "synthetic conversation"},
    }
    if kind == "operation":
        content = {"text": "Synthetic operation", "metadata": {"label": "synthetic operation"}}
    return EventEnvelope(
        **{
            key: str(uuid4())
            for key in (
                "event_id",
                "instance_id",
                "device_id",
                "source_id",
                "scope_id",
                "conversation_id",
                "record_id",
            )
        },
        revision=2,
        kind=kind,
        occurred_at=datetime(2026, 11, 9, tzinfo=UTC),
        content=content,
    )


def replace_record(request, index=0, **changes):
    records = list(request.input_records)
    records[index] = records[index].model_copy(update=changes)
    return request.model_copy(update={"input_records": tuple(records)})


def require_unproven(call):
    with pytest.raises(AccessError) as caught:
        call()
    assert caught.value.code == "original_migration_source_unproven"
    assert caught.value.status == 409
    assert str(caught.value) == "original_migration_source_unproven"


@pytest.mark.parametrize("kind", ["conversation_turn", "operation"])
def test_queue_only_proof_recovers_exact_event_and_scheduling_variation(kind):
    event = event_for(kind)
    request = write_request(event)
    assert recover_event(request).canonical_payload() == event.canonical_payload()
    rescheduled = request.model_copy(
        update={"event_at": None, "time_quality": "approximate", "priority": "normal"}
    )
    require_frozen_match(event, rescheduled)
    assert recover_event(rescheduled).canonical_payload() == event.canonical_payload()


@pytest.mark.parametrize(
    "field",
    ["internal_session_id", "source_id", "idempotency_key", "batch_identity", "evidence_ids"],
)
def test_frozen_request_identity_must_match_rebuilt_event(field):
    event = event_for()
    request = write_request(event)
    if field == "batch_identity":
        value = request.batch_identity.model_copy(update={"first_round": 2})
    elif field == "evidence_ids":
        value = list(reversed(request.evidence_ids))
    else:
        value = "synthetic-private-mismatch"
    altered = request.model_copy(update={field: value})
    require_unproven(lambda: require_frozen_match(event, altered))
    require_unproven(lambda: recover_event(altered))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "synthetic-other-owner"),
        ("source_id", "synthetic-other-source"),
        ("workspace", "global"),
        ("project", "global"),
        ("conversation_id", "synthetic-other-conversation"),
        ("message_id", "synthetic-other-record"),
        ("revision", 3),
        ("role", "tool"),
        ("created_at", None),
        ("verbatim", "synthetic-private-mismatch"),
    ],
)
def test_each_frozen_record_must_match_authoritative_event(field, value):
    event = event_for()
    altered = replace_record(write_request(event), **{field: value})
    require_unproven(lambda: require_frozen_match(event, altered))
    require_unproven(lambda: recover_event(altered))


@pytest.mark.parametrize(
    "damage",
    [
        "invalid_json",
        "missing_event",
        "wrong_index",
        "boolean_index",
        "different_header",
        "extra_field",
        "trailing_text",
        "noncanonical_json",
        "nonfinite_metadata",
        "excessive_nesting",
    ],
)
def test_corrupt_or_noncanonical_footer_is_never_recovery_proof(damage):
    request = write_request(event_for())
    text, _, tail = request.input_records[1].verbatim.rpartition(MARKER)
    provenance = json.loads(tail[: -len(SUFFIX)])
    if damage == "missing_event":
        del provenance["event"]
    elif damage == "wrong_index":
        provenance["message_index"] = 9
    elif damage == "boolean_index":
        provenance["message_index"] = True
    elif damage == "different_header":
        provenance["event"]["record_id"] = str(uuid4())
    elif damage == "extra_field":
        provenance["synthetic_private_field"] = "synthetic-private-mismatch"
    elif damage == "nonfinite_metadata":
        provenance["message_metadata"] = {"synthetic": float("nan")}
    payload = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
    if damage == "invalid_json":
        payload = "synthetic-private-mismatch"
    elif damage == "excessive_nesting":
        payload = "[" * 2000 + "0" + "]" * 2000
    elif damage == "noncanonical_json":
        payload = json.dumps(provenance, ensure_ascii=False, sort_keys=True, indent=2)
    verbatim = text + MARKER + payload + SUFFIX
    if damage == "trailing_text":
        verbatim += "synthetic-private-mismatch"
    altered = replace_record(request, index=1, verbatim=verbatim)
    require_unproven(lambda: recover_event(altered))


def test_legacy_identity_only_request_is_not_original_proof():
    request = write_request(event_for()).model_copy(update={"input_records": ()})
    for missing in (None, request):
        require_unproven(lambda missing=missing: recover_event(missing))
        require_unproven(lambda missing=missing: require_frozen_match(event_for(), missing))


def test_actual_queue_only_job_recovers_without_creating_a_receipt(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "CREATE TRIGGER refuse_original BEFORE INSERT ON cmh_event_receipts "
                    "BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END"
                )
            failed = await web.post("/hub/v1/events", headers=headers, json=raw)
            assert failed.status_code == 503, failed.text
            queue = app.core.state.memory_write_queue
            request_id = f"cmh:{raw['instance_id']}:{raw['device_id']}:{raw['event_id']}"
            job = queue.get(request_id)
            assert job is not None
            request = queue.payload_for(job.job_id)
            assert request is not None
            recovered = recover_event(request)
            assert (
                recovered.canonical_payload()
                == EventEnvelope.model_validate(raw).canonical_payload()
            )
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                assert db.execute("SELECT COUNT(*) FROM cmh_event_receipts").fetchone()[0] == 0
            assert queue.get(request_id) == job

    asyncio.run(scenario())


def test_migration_preserves_actual_receipt_validation_and_error_priority(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, headers, _, raw = await provision(web, config)
            accepted = await web.post("/hub/v1/events", headers=headers, json=raw)
            assert accepted.status_code == 202, accepted.text
            job_id = accepted.json()["receipt_id"]
            request = app.core.state.memory_write_queue.payload_for(job_id)
            with app.core.state.cmh_ingest._database() as db:
                row = dict(db.execute("SELECT * FROM cmh_event_receipts").fetchone())
            original = row.pop("payload_json")
            assert _recover_original(request, row, raw["instance_id"]) == original
            damaged_request = request.model_copy(update={"source_id": "synthetic-other-source"})
            damaged_row = {**row, "payload_digest": "synthetic-bad-digest"}
            with pytest.raises(AccessError) as caught:
                _recover_original(damaged_request, damaged_row, raw["instance_id"])
            assert (caught.value.code, caught.value.status) == ("original_archive_invalid", 503)
            require_unproven(lambda: _recover_original(damaged_request, row, raw["instance_id"]))

    asyncio.run(scenario())
