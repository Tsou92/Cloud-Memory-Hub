"""FR-01/03/10/12: real scoped dashboard counts and acceptance-time trends."""

import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cloud_memory_hub.console_dashboard import dashboard
from cloud_memory_hub.remote_access import AccessError
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision

PATH = "/hub/v1/console/dashboard"


async def fetch(web, admin, scope):
    return await web.post(PATH, headers=admin, json={"scope_id": scope})


def test_dashboard_requires_admin_origin_and_exact_scope(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (_, web):
            admin, device, _, event = await provision(web, config)
            scope = event["scope_id"]
            for headers in ({}, device, {**admin, "Origin": "https://foreign.invalid"}):
                assert (await fetch(web, headers, scope)).status_code == 403
            for body in ({}, {"scope_id": scope, "limit": 1}, {"scope_id": "local"}):
                assert (await web.post(PATH, headers=admin, json=body)).status_code == 422
            assert (await fetch(web, admin, str(uuid4()))).status_code == 404
            response = await fetch(web, admin, "global")
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            value = response.json()
            assert value["statistics"] == dict(
                device_connections=0, agent_connections=0, shared_records=0, handoffs=0
            )
            assert value["activity_state"] == "ready"
            assert len(value["daily_activity"]) == 7
            assert sum(day["count"] for day in value["daily_activity"]) == 0

    asyncio.run(scenario())


def test_counts_real_events_once_and_uses_queue_acceptance_time(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    monkeypatch.setattr("cloud_memory_hub.console_dashboard._BATCH_SIZE", 2)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, device, token, base = await provision(web, config)
            now = datetime(2026, 11, 10, 12, tzinfo=UTC)
            jobs = []
            for kind in ("conversation", "remember", "handoff", "other"):
                event = deepcopy(base)
                event.update(event_id=str(uuid4()), record_id=str(uuid4()))
                event["occurred_at"] = "2020-01-01T00:00:00+00:00"
                if kind != "conversation":
                    event["kind"] = "operation"
                    event["content"] = {
                        "text": "Synthetic private text must not appear in dashboard",
                        "metadata": {"agent_access_kind": kind, "agent_access_version": 1},
                    }
                job = await accepted(web, device, event)
                assert await accepted(web, device, event) == job
                jobs.append(job)
            _, other, _, unrelated = await provision(web, config)
            await accepted(web, other, unrelated)
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                for offset, job in enumerate(jobs):
                    db.execute(
                        "UPDATE memory_write_jobs SET created_at=? WHERE job_id=?",
                        ((now - timedelta(days=offset)).isoformat(), job),
                    )
            console = app.core.state.cmh_console
            value = dashboard(console, base["scope_id"], console.privacy, now=now)
            assert value["statistics"] == dict(
                device_connections=1, agent_connections=1, shared_records=4, handoffs=1
            )
            assert {row["kind"]: row["count"] for row in value["record_types"]} == dict(
                conversation=1, remember=1, handoff=1, other=1
            )
            assert [row["count"] for row in value["daily_activity"]] == [0, 0, 0, 1, 1, 1, 1]
            assert value["activity_timezone"] == "UTC"
            serialized = json.dumps(value)
            assert token not in serialized and config.admin_token not in serialized
            assert "Synthetic private" not in serialized
            assert base["device_id"] not in serialized and jobs[0] not in serialized

    asyncio.run(scenario())


def test_network_counts_authorized_sources_and_deduplicates_devices(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (_, web):
            admin, _, _, event = await provision(web, config)
            scope, device = event["scope_id"], event["device_id"]
            second = await web.post(
                "/hub/v1/sources",
                headers=admin,
                json=dict(
                    display_name="Second agent",
                    adapter_id="claude-code.cli",
                    device_id=device,
                    scope_id=scope,
                ),
            )
            assert second.status_code == 201, second.text
            assert (await fetch(web, admin, scope)).json()["statistics"]["agent_connections"] == 2
            for revision, permissions, expected in (
                (2, ["history:read"], 2),
                (3, ["sync:read"], 0),
                (4, ["memory:read"], 2),
            ):
                changed = await web.put(
                    f"/hub/v1/devices/{device}/grants",
                    headers=admin,
                    json={
                        "expected_revision": revision,
                        "grants": [{"scope_id": scope, "permissions": permissions}],
                    },
                )
                assert changed.status_code == 200, changed.text
                stats = (await fetch(web, admin, scope)).json()["statistics"]
                assert stats["agent_connections"] == expected
                assert stats["device_connections"] == int(expected > 0)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "timestamp", [None, "invalid", "2026-01-01T12:00:00", "2099-01-01T00:00:00Z"]
)
def test_missing_or_invalid_timestamps_do_not_invent_zero_activity(tmp_path, timestamp):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (_, web):
            admin, device, _, event = await provision(web, config)
            job = await accepted(web, device, event)
            with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                if timestamp is None:
                    db.execute("DELETE FROM memory_write_jobs WHERE job_id=?", (job,))
                else:
                    db.execute(
                        "UPDATE memory_write_jobs SET created_at=? WHERE job_id=?", (timestamp, job)
                    )
            response = await fetch(web, admin, event["scope_id"])
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["statistics"]["shared_records"] == 1
            assert value["daily_activity"] == [] and value["missing_timestamps"] == 1
            assert value["activity_state"] == "unavailable"

    asyncio.run(scenario())


def test_suppressed_records_are_excluded_and_changed_authority_is_rejected(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, device, _, event = await provision(web, config)
            job = await accepted(web, device, event)
            console = app.core.state.cmh_console
            monkeypatch.setattr(console.reads, "suppressed", lambda scope: {"cmhe:" + job})
            result = await fetch(web, admin, event["scope_id"])
            assert result.status_code == 200, result.text
            assert result.json()["statistics"]["shared_records"] == 0
            reads = iter([set(), {"cmhe:" + job}])
            monkeypatch.setattr(console.reads, "suppressed", lambda scope: next(reads))
            result = await fetch(web, admin, event["scope_id"])
            assert result.status_code == 409
            assert result.json() == {"detail": "read_authorization_changed"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "permissions",
    [
        '"memory:read"',
        '{"x":"memory:read"}',
        '["memory:read","unknown"]',
        '["memory:read","memory:read"]',
        "invalid",
        None,
    ],
)
def test_corrupt_grant_or_missing_device_cannot_be_reported_as_connected(tmp_path, permissions):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (_, web):
            admin, _, _, event = await provision(web, config)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                if permissions is None:
                    db.execute("DELETE FROM devices WHERE device_id=?", (event["device_id"],))
                else:
                    db.execute("UPDATE grants SET permissions=?", (permissions,))
            result = await fetch(web, admin, event["scope_id"])
            assert result.status_code == 503, result.text

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["invalid_json", "wrong_identity", "invalid_kind"])
def test_damaged_receipts_fail_closed_without_leaking_content(tmp_path, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (_, web):
            admin, device, _, event = await provision(web, config)
            await accepted(web, device, event)
            with sqlite3.connect(config.config_dir / "remote-access.sqlite3") as db:
                if damage == "invalid_json":
                    db.execute(
                        "UPDATE cmh_event_receipts SET payload_json='private broken payload'"
                    )
                elif damage == "wrong_identity":
                    db.execute(
                        "UPDATE cmh_event_receipts SET request_id=?", ("cmh:" + str(uuid4()),)
                    )
                else:
                    db.execute(
                        "UPDATE cmh_event_receipts "
                        "SET payload_json=json_set(payload_json,'$.kind','invalid')"
                    )
            result = await fetch(web, admin, event["scope_id"])
            assert result.status_code == 503, result.text
            assert "private" not in result.text

    asyncio.run(scenario())


def test_slow_dashboard_is_bounded(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, device, _, event = await provision(web, config)
            await accepted(web, device, event)
            ticks = iter([0, 4])
            monkeypatch.setattr("cloud_memory_hub.console_dashboard.monotonic", lambda: next(ticks))
            console = app.core.state.cmh_console
            with pytest.raises(AccessError, match="console_dashboard_busy"):
                dashboard(console, event["scope_id"], console.privacy)

    asyncio.run(scenario())


def test_final_queue_read_cannot_publish_after_deadline(tmp_path, monkeypatch):
    import cloud_memory_hub.console_dashboard as projection

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, device, _, event = await provision(web, config)
            await accepted(web, device, event)
            clock = [0]
            original = projection._queue_times

            def delayed(queue, rows, deadline):
                result = original(queue, rows, deadline)
                clock[0] = 4
                return result

            monkeypatch.setattr(projection, "monotonic", lambda: clock[0])
            monkeypatch.setattr(projection, "_queue_times", delayed)
            console = app.core.state.cmh_console
            with pytest.raises(AccessError, match="console_dashboard_busy"):
                dashboard(console, event["scope_id"], console.privacy)

    asyncio.run(scenario())
