"""ADR-0034: strict metadata proof is independent of payload and source status."""

import asyncio
import sqlite3
from dataclasses import FrozenInstanceError, asdict
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_request_owner import verify_request_owner
from cloud_memory_hub.remote_access import AccessError
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_ingest import provision


def metadata(scope_id=None):
    instance_id, device_id, source_id, event_id = (str(uuid4()) for _ in range(4))
    scope_id = scope_id or str(uuid4())
    job = {
        "job_id": str(uuid4()),
        "idempotency_key": f"cmh:{instance_id}:{device_id}:{event_id}",
        "source_key": "cmh:" + source_id,
    }
    source = {"source_id": source_id, "device_id": device_id, "scope_id": scope_id}
    return job, source, {"instance_id": instance_id, "scope_id": scope_id}


def as_row(db, values):
    return db.execute(
        "SELECT " + ",".join(f"? AS {key}" for key in values), tuple(values.values())
    ).fetchone()


@pytest.mark.parametrize("scope_id", ["global", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"])
@pytest.mark.parametrize("status", ["active", "revoked"])
def test_owner_proof_accepts_real_rows_without_reading_payload_or_source_status(scope_id, status):
    job, source, context = metadata(scope_id)
    source["status"] = status
    job["payload_json"] = "synthetic-private-body"
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        result = verify_request_owner(as_row(db, job), as_row(db, source), **context)
    assert asdict(result) == {
        "job_id": job["job_id"],
        "request_id": job["idempotency_key"],
        **context,
        "source_id": source["source_id"],
        "device_id": source["device_id"],
        "event_id": job["idempotency_key"].split(":")[-1],
        "birth_id": None,
    }
    assert "synthetic-private-body" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.scope_id = "synthetic-other-project"


@pytest.mark.parametrize(
    "damage",
    [
        "missing_job",
        "missing_source",
        "missing_job_id",
        "missing_device_id",
        "missing_scope_id",
        "source_prefix",
        "other_source",
        "other_scope",
        "other_instance",
        "other_device",
        "request_prefix",
        "request_suffix",
        "request_not_string",
        "job_not_string",
        "scope_not_string",
    ],
)
def test_incomplete_or_cross_owner_metadata_fails_with_safe_reason(damage):
    job, source, context = metadata()
    if damage == "missing_job":
        job = None
    elif damage == "missing_source":
        source = None
    elif damage == "missing_job_id":
        del job["job_id"]
    elif damage == "missing_device_id":
        del source["device_id"]
    elif damage == "missing_scope_id":
        del source["scope_id"]
    elif damage == "source_prefix":
        job["source_key"] = "bad:" + source["source_id"]
    elif damage == "other_source":
        job["source_key"] = "cmh:" + str(uuid4())
    elif damage == "other_scope":
        source["scope_id"] = str(uuid4())
    elif damage == "other_instance":
        context["instance_id"] = str(uuid4())
    elif damage == "other_device":
        source["device_id"] = str(uuid4())
    elif damage == "request_prefix":
        job["idempotency_key"] = job["idempotency_key"].replace("cmh:", "bad:", 1)
    elif damage == "request_suffix":
        job["idempotency_key"] += ":synthetic-private-mismatch"
    elif damage == "request_not_string":
        job["idempotency_key"] = 123
    elif damage == "job_not_string":
        job["job_id"] = None
    elif damage == "scope_not_string":
        context["scope_id"] = source["scope_id"] = 123
    with pytest.raises(GovernanceError) as caught:
        verify_request_owner(job, source, **context)
    assert str(caught.value) == "governance_request_owner_unproven"


@pytest.mark.parametrize("field", ["job", "source", "device", "instance", "scope", "event"])
@pytest.mark.parametrize(
    "invalid", ["AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa"]
)
def test_matching_but_noncanonical_or_non_v4_identifiers_are_not_proof(field, invalid):
    job, source, context = metadata()
    parts = job["idempotency_key"].split(":")
    if field == "job":
        job["job_id"] = invalid
    elif field == "source":
        source["source_id"] = invalid
        job["source_key"] = "cmh:" + invalid
    elif field == "device":
        source["device_id"] = parts[2] = invalid
    elif field == "instance":
        context["instance_id"] = parts[1] = invalid
    elif field == "scope":
        source["scope_id"] = context["scope_id"] = invalid
    elif field == "event":
        parts[3] = invalid
    job["idempotency_key"] = ":".join(parts)
    with pytest.raises(GovernanceError, match="^governance_request_owner_unproven$"):
        verify_request_owner(job, source, **context)


def test_missing_sqlite_row_column_is_also_a_safe_identity_failure():
    job, source, context = metadata()
    del source["device_id"]
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(GovernanceError, match="^governance_request_owner_unproven$"):
            verify_request_owner(as_row(db, job), as_row(db, source), **context)


def test_purger_keeps_revoked_source_ownership_and_actual_receipt_validation(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, raw = await provision(web, config)
            accepted = await web.post("/hub/v1/events", headers=headers, json=raw)
            assert accepted.status_code == 202, accepted.text
            job_id = accepted.json()["receipt_id"]
            revoked = await web.post(
                f"/hub/v1/sources/{raw['source_id']}/revoke",
                headers=admin,
                json={"expected_revision": 1},
            )
            assert revoked.status_code == 200, revoked.text
            purger = app.core.state.cmh_executor.purger
            frozen = app.core.state.memory_write_queue.payload_for(job_id)
            expected = {frozen.idempotency_key}
            assert purger._requests({job_id}, set(), raw["scope_id"]) == expected
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                db.execute(
                    "UPDATE cmh_event_receipts SET payload_digest=? WHERE receipt_id=?",
                    ("synthetic-bad-digest", job_id),
                )
            with pytest.raises(AccessError) as caught:
                purger._requests({job_id}, set(), raw["scope_id"])
            assert (caught.value.code, caught.value.status) == ("original_archive_invalid", 503)
            with pytest.raises(GovernanceError, match="^governance_purge_request_unproven$"):
                purger._requests({job_id}, set(), "global")

    asyncio.run(scenario())
