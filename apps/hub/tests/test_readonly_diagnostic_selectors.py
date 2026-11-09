"""ADR-0043: stopped real stores compose with the existing pure selector proof."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_inflight import InflightGovernance
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.remote_ingest import RemoteIngest
from cloud_memory_hub.storage_lease import StorageLease
from cloud_memory_hub.write_admissions import WriteAdmissions
from test_diagnostic_selectors import all_rows
from test_governance_candidates import confirmed
from test_governance_inflight import coordinator, may_invoke, signed_scope
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision


@pytest.mark.parametrize("state", ["pending", "unknown", "possible", "reaped"])
def test_stopped_readonly_selector_combination_preserves_every_table_and_head(
    tmp_path, monkeypatch, state
):
    config = config_for(tmp_path)

    async def seed():
        async with fixture(config) as (app, web):
            _, headers, _, event = await provision(web, config)
            job = await accepted(web, headers, event)
            planner, queue = coordinator(app), app.core.state.memory_write_queue
            scope = confirmed(app, event, [job])
            prepared = planner.prepare_cleanup_selectors(scope)
            operation = signed_scope(planner, scope, prepared.rule_targets)
            # This fixture helper appends directly. Explicitly establish the
            # real external floor before the later read-only maintenance phase.
            app.governance.verify()
            if state == "unknown":
                with sqlite3.connect(queue.database_path) as db:
                    db.execute("UPDATE memory_write_jobs SET state='unknown',hy_invoked=1")
            elif state == "possible":
                may_invoke(planner, queue, job)
            elif state == "reaped":
                payload = planner.apply(operation)[0]
                assert (
                    queue.clear_terminal_payload_for_governance(
                        job,
                        expected_request_id=payload.request_id,
                        expected_payload_digest=payload.input_digest,
                    )
                    == "reaped"
                )
                with planner.ingest._database(writing=True) as db:
                    db.execute("DELETE FROM cmh_event_receipts WHERE receipt_id=?", (job,))
            return planner, operation, prepared

    original, operation, expected = asyncio.run(seed())
    # The real Core lifespan and its workers have stopped. Cold-open all readers;
    # the queue provider supplies only its path, without constructing a writer.
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        before = all_rows(config, original)
        authority = open_authority(config, read_only=True)
        admissions = WriteAdmissions(authority.access, authority.journal, read_only=True)
        queue = SimpleNamespace(database_path=original.ingest._queue().database_path)
        ingest = RemoteIngest(authority.access, lambda: queue, read_only=True)
        planner = InflightGovernance(ingest, admissions, authority.journal)

        def forbidden(*args, **kwargs):
            raise AssertionError("pure read-only selection attempted cancellation")

        monkeypatch.setattr(queue, "cancel_unpermitted_for_governance", forbidden, raising=False)
        if state == "possible":
            with pytest.raises(GovernanceError, match="^governance_inflight_invocation_unproven$"):
                planner.prepare_cleanup_selectors(operation, signed=True)
        else:
            assert (
                planner.prepare_cleanup_selectors(
                    operation, signed=True, completed=state == "reaped"
                )
                == expected
            )
        assert all_rows(config, planner) == before
        with pytest.raises(GovernanceError, match="^governance_inflight_store_unavailable$"):
            planner.apply(operation, completed=state == "reaped")
        assert all_rows(config, planner) == before
