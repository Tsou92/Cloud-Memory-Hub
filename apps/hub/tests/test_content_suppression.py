"""FR-10/FR-11, ADR-0034: signed exact-text rules, not semantic matching."""

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.events import EventEnvelope
from cloud_memory_hub.governance_execution import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError, GovernanceJournal, Intent
from cloud_memory_hub.governance_sources import (
    CONTENT_POLICY,
    SourceSuppression,
    capture_rules,
    capture_sources,
    content_policy_target,
    content_targets,
)
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore, NamedResource
from cloud_memory_hub.remote_ingest import RemoteIngest, SourceRegistration

TEXT = "Café project roadmap has forty synthetic milestones."


@pytest.fixture
def storage(tmp_path):
    access = DeviceAccessStore(tmp_path / "config" / "access.sqlite3")
    scope = access.create_project(NamedResource(display_name="Synthetic project"))["project_id"]
    device = access.create_device(NamedResource(display_name="Synthetic device"))
    ingest = RemoteIngest(access, lambda: None)
    source = ingest.create_source(
        SourceRegistration(
            display_name="Synthetic source",
            device_id=device.device["device_id"],
            scope_id=scope,
            adapter_id="synthetic",
        )
    )
    journal = GovernanceJournal.initialize(
        tmp_path / "governance" / "journal.sqlite3",
        instance_id=access.instance_id,
        key=os.urandom(32),
    )
    event = EventEnvelope(
        instance_id=access.instance_id,
        device_id=device.device["device_id"],
        source_id=source["source_id"],
        scope_id=scope,
        event_id=str(uuid4()),
        conversation_id=str(uuid4()),
        record_id=str(uuid4()),
        revision=1,
        kind="conversation_turn",
        content={"messages": [{"role": "user", "text": TEXT}]},
    )
    return SimpleNamespace(
        journal=journal, store=ExecutionStore(journal), ingest=ingest, event=event
    )


def changed(event, texts=None, **updates):
    content = event.content
    if texts is not None:
        content = {"messages": [{"role": "user", "text": text} for text in texts]}
    return EventEnvelope.model_validate(
        {
            **event.model_dump(),
            **{key: str(uuid4()) for key in ("event_id", "conversation_id", "record_id")},
            "content": content,
            **updates,
        }
    )


def archive(storage, event=None):
    event = event or storage.event
    receipt = str(uuid4())
    row = {
        **event.model_dump(exclude={"content"}),
        "receipt_id": receipt,
        "payload_digest": event.payload_digest(),
        "payload_json": event.canonical_payload(),
        "request_id": f"cmh:{event.instance_id}:{event.device_id}:{event.event_id}",
    }
    with storage.ingest._database(writing=True) as db:
        db.execute(
            "INSERT INTO cmh_event_receipts VALUES "
            "(:device_id,:event_id,:source_id,:scope_id,:conversation_id,:record_id,"
            ":revision,:payload_digest,:receipt_id,:request_id,:payload_json)",
            row,
        )
    return {"cmhe:" + receipt: "raw"}


def plan(storage, records, rules, *, action="forget", scope=None):
    scope = scope or storage.event.scope_id
    intent = Intent(
        operation_id=str(uuid4()),
        actor_id=str(uuid4()),
        scope_id=scope,
        action=action,
        targets=tuple(storage.journal.target(scope, kind, key) for key, kind in records.items())
        + tuple(rules),
    )
    storage.store.plan(intent, records)
    storage.journal.append(intent)
    storage.store.complete(intent)
    return intent


def forget(storage, *, event=None, action="forget", legacy=False, no_sources=False):
    event = event or storage.event
    records = archive(storage, event)
    capture = capture_sources if legacy else capture_rules
    rules = () if no_sources else capture(storage.journal, storage.ingest, event.scope_id, records)
    intent = plan(storage, records, rules, action=action, scope=event.scope_id)
    return records, intent


def remove_originals(storage):
    with storage.ingest._database(writing=True) as db:
        db.execute("DELETE FROM cmh_event_receipts")


def refused(suppression, event, code, status=409):
    with pytest.raises(AccessError) as error:
        suppression.require_allowed(event)
    assert (error.value.code, error.value.status) == (code, status)


@pytest.mark.parametrize("action", ["forget", "purge"])
def test_new_rules_reject_any_normalized_message_after_reopening(storage, action):
    forget(storage, action=action)
    if action == "purge":
        remove_originals(storage)
    journal = GovernanceJournal(
        storage.journal.path,
        instance_id=storage.journal.instance_id,
        key=storage.journal._key,
    )
    suppression = SourceSuppression(ExecutionStore(journal))
    mixed = changed(
        storage.event,
        [
            "An entirely new synthetic sentence.",
            "  Cafe\u0301\tproject\nroadmap\u3000has forty\u00a0synthetic   milestones.  ",
            "好",
        ],
        source_id=str(uuid4()),
    )
    refused(suppression, mixed, "governance_content_suppressed")
    suppression.require_allowed(changed(storage.event, ["A different synthetic sentence."]))
    suppression.require_allowed(changed(storage.event, [TEXT.lower()]))
    suppression.require_allowed(changed(storage.event, scope_id=str(uuid4())))


@pytest.mark.parametrize(
    ("text", "eligible"),
    [
        ("短句", False),
        ("e\u0301" * 15, False),
        ("e\u0301" * 16, True),
        ("a" + "\t\n\u3000" * 20 + "b", False),
        ("a" * 16, True),
    ],
)
def test_minimum_counts_normalized_unicode_codepoints(storage, text, eligible):
    event = changed(storage.event, [text])
    assert bool(content_targets(storage.journal, event)) is eligible


def test_operation_text_reuses_message_domain_but_metadata_never_contributes(storage):
    forget(storage)
    suppression = SourceSuppression(storage.store)
    operation = changed(storage.event, kind="operation", content={"text": TEXT})
    refused(suppression, operation, "governance_content_suppressed")
    harmless = changed(
        storage.event,
        kind="operation",
        content={"text": "短句", "metadata": {"note": TEXT}},
    )
    suppression.require_allowed(harmless)


def test_short_message_policy_marker_has_a_separate_domain(storage):
    event = changed(storage.event, ["短句", "e\u0301" * 15])
    records, intent = forget(storage, event=event, action="purge")
    marker = content_policy_target(storage.journal, event.scope_id)
    assert {target for target in intent.targets if target.kind == "content"} == {marker}
    assert marker not in content_targets(storage.journal, changed(event, [CONTENT_POLICY]))
    assert marker not in content_targets(
        storage.journal, changed(event, [json.dumps([1, "policy", CONTENT_POLICY])])
    )
    remove_originals(storage)
    suppression = SourceSuppression(storage.store)
    suppression.require_allowed(changed(event))
    suppression.require_allowed(changed(event, [TEXT]))
    assert marker in suppression.targets(event.scope_id)
    assert len(records) == 1


def test_legacy_forget_rebuilds_verified_rules_without_resigning(storage):
    _, intent = forget(storage, legacy=True, no_sources=True)
    before = storage.journal.path.read_bytes(), storage.store.path.read_bytes()
    suppression = SourceSuppression(storage.store, storage.ingest)
    refused(suppression, changed(storage.event), "governance_content_suppressed")
    same_source = changed(
        storage.event,
        conversation_id=storage.event.conversation_id,
        record_id=storage.event.record_id,
        revision=2,
    )
    refused(suppression, same_source, "governance_source_suppressed")
    assert storage.store.operations()[intent.operation_id][1] == intent
    assert (storage.journal.path.read_bytes(), storage.store.path.read_bytes()) == before


@pytest.mark.parametrize("covering_operations", [0, 1, 2])
def test_legacy_forget_requires_full_original_coverage_after_later_purges(
    storage, covering_operations
):
    first = storage.event
    second = changed(first, ["Another distinct synthetic sentence from a separate original."])
    groups = [archive(storage, first), archive(storage, second)]
    records = groups[0] | groups[1]
    legacy = plan(
        storage,
        records,
        capture_sources(storage.journal, storage.ingest, first.scope_id, records),
    )
    # Different signed operations may together prove full coverage. One policy
    # marker, even in the same project, must not hide an uncovered original.
    for group in groups[:covering_operations]:
        plan(
            storage,
            group,
            capture_rules(storage.journal, storage.ingest, first.scope_id, group),
            action="purge",
        )
    remove_originals(storage)
    before = storage.journal.path.read_bytes(), storage.store.path.read_bytes()
    suppression = SourceSuppression(storage.store, storage.ingest)
    if covering_operations == 2:
        suppression.require_allowed(changed(first, ["Unrelated synthetic work remains possible."]))
        for original in (first, second):
            refused(suppression, changed(original), "governance_content_suppressed")
        assert suppression.targets(first.scope_id)
    else:
        refused(suppression, changed(first, ["短句"]), "governance_content_rules_unavailable", 503)
    same_source = changed(
        first, conversation_id=first.conversation_id, record_id=first.record_id, revision=2
    )
    refused(suppression, same_source, "governance_source_suppressed")
    assert storage.store.operations()[legacy.operation_id][1] == legacy
    assert (storage.journal.path.read_bytes(), storage.store.path.read_bytes()) == before


@pytest.mark.parametrize("different_scope", [False, True])
def test_unrelated_policy_marker_cannot_cover_legacy_forget(storage, different_scope):
    records, _ = forget(storage, legacy=True)
    scope = str(uuid4()) if different_scope else storage.event.scope_id
    proof_records = records if different_scope else {"cmhe:" + str(uuid4()): "raw"}
    plan(storage, proof_records, (content_policy_target(storage.journal, scope),), scope=scope)
    remove_originals(storage)
    refused(
        SourceSuppression(storage.store, storage.ingest),
        changed(storage.event, ["短句"]),
        "governance_content_rules_unavailable",
        503,
    )


@pytest.mark.parametrize("remaining_original", ["valid", "missing", "invalid"])
def test_partial_signed_coverage_rebuilds_only_the_remaining_original(storage, remaining_original):
    first = storage.event
    second = changed(first, ["Another retained original must remain protected from reimport."])
    covered, retained = archive(storage, first), archive(storage, second)
    records = covered | retained
    plan(
        storage, records, capture_sources(storage.journal, storage.ingest, first.scope_id, records)
    )
    plan(
        storage,
        covered,
        capture_rules(storage.journal, storage.ingest, first.scope_id, covered),
        action="purge",
    )
    with storage.ingest._database(writing=True) as db:
        db.execute("DELETE FROM cmh_event_receipts WHERE event_id=?", (first.event_id,))
        if remaining_original == "missing":
            db.execute("DELETE FROM cmh_event_receipts WHERE event_id=?", (second.event_id,))
        elif remaining_original == "invalid":
            db.execute(
                "UPDATE cmh_event_receipts SET payload_json='{}' WHERE event_id=?",
                (second.event_id,),
            )
    suppression = SourceSuppression(storage.store, storage.ingest)
    unrelated = changed(first, ["Unrelated synthetic work remains allowed."])
    if remaining_original == "valid":
        suppression.require_allowed(unrelated)
        for original in (first, second):
            refused(suppression, changed(original), "governance_content_suppressed")
    else:
        refused(suppression, unrelated, "governance_content_rules_unavailable", 503)


def test_tampered_covering_operation_cannot_authorize_legacy_forget(storage):
    records, _ = forget(storage, legacy=True)
    proof = plan(
        storage,
        records,
        capture_rules(storage.journal, storage.ingest, storage.event.scope_id, records),
        action="purge",
    )
    remove_originals(storage)
    with sqlite3.connect(storage.store.path) as db:
        db.execute("DELETE FROM suppressed WHERE operation_id=?", (proof.operation_id,))
    with pytest.raises(GovernanceError, match="governance_execution_tampered"):
        SourceSuppression(storage.store, storage.ingest).require_allowed(changed(storage.event))


@pytest.mark.parametrize("damage", ["missing", "invalid", "no_ingest"])
def test_unprovable_legacy_forget_blocks_content_checks(storage, damage):
    forget(storage, legacy=True)
    if damage == "missing":
        remove_originals(storage)
    elif damage == "invalid":
        with storage.ingest._database(writing=True) as db:
            db.execute("UPDATE cmh_event_receipts SET payload_json='{}'")
    suppression = SourceSuppression(
        storage.store, None if damage == "no_ingest" else storage.ingest
    )
    refused(
        suppression, changed(storage.event, ["短句"]), "governance_content_rules_unavailable", 503
    )
    with pytest.raises(AccessError, match="governance_content_rules_unavailable"):
        suppression.targets(storage.event.scope_id)


@pytest.mark.parametrize("no_sources", [False, True])
def test_legacy_purge_fails_closed_without_rules_even_for_short_uploads(storage, no_sources):
    forget(storage, action="purge", legacy=True, no_sources=no_sources)
    remove_originals(storage)
    suppression = SourceSuppression(storage.store, storage.ingest)
    expected = (
        "governance_source_rules_unavailable"
        if no_sources
        else "governance_content_rules_unavailable"
    )
    refused(suppression, changed(storage.event, ["短句"]), expected, 503)
    suppression.require_allowed(changed(storage.event, scope_id=str(uuid4())))


def test_legacy_purge_without_original_targets_still_requires_content_proof(storage):
    plan(storage, {str(uuid4()): "memory"}, (), action="purge")
    refused(
        SourceSuppression(storage.store, storage.ingest),
        changed(storage.event),
        "governance_content_rules_unavailable",
        503,
    )


def test_known_source_match_precedes_content_match_and_legacy_content_gap(storage):
    forget(storage)
    same_source = changed(
        storage.event,
        conversation_id=storage.event.conversation_id,
        record_id=storage.event.record_id,
    )
    suppression = SourceSuppression(storage.store, storage.ingest)
    refused(suppression, same_source, "governance_source_suppressed")
    plan(storage, {str(uuid4()): "memory"}, (), action="purge")
    refused(suppression, same_source, "governance_source_suppressed")
    refused(suppression, changed(storage.event), "governance_content_rules_unavailable", 503)


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_source_unavailability_precedes_content_unavailability(storage, reverse):
    operations = [({"cmhe:" + str(uuid4()): "raw"}, ()), ({str(uuid4()): "memory"}, ())]
    if reverse:
        operations.reverse()
    for records, rules in operations:
        plan(storage, records, rules, action="purge")
    suppression = SourceSuppression(storage.store, storage.ingest)
    refused(suppression, changed(storage.event), "governance_source_rules_unavailable", 503)


def test_capture_once_keeps_source_only_api_and_deduplicates_rules(storage, monkeypatch):
    records = archive(storage)
    records.update(archive(storage, changed(storage.event, [TEXT, TEXT])))
    sources = capture_sources(storage.journal, storage.ingest, storage.event.scope_id, records)
    assert len(sources) == 2 and all(target.kind == "source_record" for target in sources)
    original_database = storage.ingest._database
    reads = []

    @contextmanager
    def counted_database():
        with original_database() as db:
            db.set_trace_callback(reads.append)
            yield db

    monkeypatch.setattr(storage.ingest, "_database", counted_database)
    rules = capture_rules(storage.journal, storage.ingest, storage.event.scope_id, records)
    assert len(rules) == 4  # Two sources, one text, one policy marker.
    assert (
        len([query for query in reads if query.startswith("SELECT * FROM cmh_event_receipts")]) == 2
    )
    assert len([query for query in reads if query == "COMMIT"]) == 1


@pytest.mark.parametrize("damage", ["missing", "invalid", "other_scope"])
def test_capture_requires_valid_scoped_originals(storage, damage):
    records = archive(storage)
    scope = storage.event.scope_id
    if damage == "missing":
        remove_originals(storage)
    elif damage == "invalid":
        with storage.ingest._database(writing=True) as db:
            db.execute("UPDATE cmh_event_receipts SET payload_digest=?", ("0" * 64,))
    else:
        scope = str(uuid4())
    expected = "original_archive_invalid" if damage == "invalid" else "governance_source_unproven"
    with pytest.raises((AccessError, GovernanceError), match=expected):
        capture_rules(storage.journal, storage.ingest, scope, records)
    assert storage.store.operations() == {}


def test_governance_files_contain_no_text_plain_digest_or_source_identity(storage):
    forget(storage)
    forbidden = [
        TEXT,
        hashlib.sha256(TEXT.encode()).hexdigest(),
        storage.event.source_id,
        storage.event.conversation_id,
        storage.event.record_id,
    ]
    for path in storage.journal.path.parent.iterdir():
        if path.is_file():
            content = path.read_bytes()
            assert all(value.encode() not in content for value in forbidden)


@pytest.mark.parametrize("physical_count", [9997, 9998])
def test_combined_physical_and_rule_budget_is_exact(storage, physical_count):
    records = archive(storage)
    records.update({f"synthetic-{index}": "raw" for index in range(physical_count - 1)})
    if physical_count == 9997:
        rules = capture_rules(storage.journal, storage.ingest, storage.event.scope_id, records)
        assert len(records) + len(rules) == 10_000
    else:
        with pytest.raises(GovernanceError, match="governance_scan_limit"):
            capture_rules(storage.journal, storage.ingest, storage.event.scope_id, records)
    assert storage.store.operations() == {}


def test_marker_alone_counts_against_budget_before_reading_originals(storage):
    records = {f"synthetic-{index}": "raw" for index in range(10_000)}
    with pytest.raises(GovernanceError, match="governance_scan_limit"):
        capture_rules(storage.journal, None, storage.event.scope_id, records)


@pytest.mark.parametrize("text", [TEXT, "\\" * 520_000], ids=["ordinary", "json-escaped-limit"])
def test_http_mixed_event_rejection_leaves_queue_and_receipts_unchanged(tmp_path, text):
    from test_original_archive import config_for
    from test_output_lineage import accepted, fixture
    from test_remote_ingest import provision

    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            event["content"]["messages"][0]["text"] = text
            receipt = await accepted(web, headers, event)
            ingest = app.core.state.cmh_ingest
            journal = app.core.state.cmh_governance.journal
            store = ExecutionStore(journal)
            scope = event["scope_id"]
            records = {"cmhe:" + receipt: "raw"}
            state = SimpleNamespace(
                journal=journal, store=store, event=EventEnvelope.model_validate(event)
            )
            plan(state, records, capture_rules(journal, ingest, scope, records))
            suppression = SourceSuppression(store, ingest)
            ingest.require_source_allowed = suppression.require_allowed
            ingest.require_source_rules = suppression.targets
            registered = await web.post(
                "/hub/v1/sources",
                headers=admin,
                json={
                    "display_name": "Reinstalled synthetic connector",
                    "adapter_id": "synthetic.reimport",
                    "device_id": event["device_id"],
                    "scope_id": scope,
                },
            )
            assert registered.status_code == 201
            candidate = changed(
                state.event,
                ["New sentence with enough characters.", text],
                source_id=registered.json()["source_id"],
            )

            def queue_rows():
                with sqlite3.connect(config.data_dir / "one-memory.sqlite3") as db:
                    return db.execute("SELECT * FROM memory_write_jobs ORDER BY job_id").fetchall()

            before = queue_rows()
            reply = await web.post(
                "/hub/v1/events", headers=headers, json=candidate.model_dump(mode="json")
            )
            assert reply.status_code == 409
            assert reply.json() == {"detail": "governance_content_suppressed"}
            assert queue_rows() == before
            with ingest._database() as db:
                assert db.execute("SELECT COUNT(*) FROM cmh_event_receipts").fetchone()[0] == 1

    asyncio.run(scenario())
