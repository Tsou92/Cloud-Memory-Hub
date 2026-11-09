"""Stable source rules survive purging the receipts that established them."""

import json

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.source_history import PREFIX


def source_target(journal, event):
    identity = json.dumps(
        [1, event.source_id, event.conversation_id, event.record_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return journal.target(event.scope_id, "source_record", identity)


def capture_sources(journal, ingest, scope_id, records):
    """Capture only verified originals in the confirmed directional scope."""
    targets = set()
    with ingest._database() as db:
        for identity in sorted(records):
            if not identity.startswith(PREFIX):
                continue
            row = db.execute(
                "SELECT * FROM cmh_event_receipts WHERE receipt_id=? AND scope_id=?",
                (identity[len(PREFIX) :], scope_id),
            ).fetchone()
            if row is None:
                raise GovernanceError("governance_source_unproven")
            event = validate_original(row, ingest.access.instance_id)
            targets.add(source_target(journal, event))
    return tuple(sorted(targets, key=lambda item: item.key))


class SourceSuppression:
    def __init__(self, store):
        self.store = store

    def targets(self, scope_id):
        targets = set()
        for operation, (_, intent) in self.store.operations().items():
            if intent.scope_id != scope_id:
                continue
            sources = {target for target in intent.targets if target.kind == "source_record"}
            if not sources and intent.action == "purge":
                if any(record.startswith(PREFIX) for record in self.store.records(operation)):
                    # A legacy purge destroyed the mapping. Do not mistake an
                    # unknowable source set for an empty one after upgrading.
                    raise AccessError("governance_source_rules_unavailable", 503)
            targets.update(sources)
        return targets

    def require_allowed(self, event):
        if source_target(self.store.journal, event) in self.targets(event.scope_id):
            raise AccessError("governance_source_suppressed", 409)
