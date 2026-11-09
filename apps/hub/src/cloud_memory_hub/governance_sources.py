"""Content-free source and text rules survive purging their original receipts."""

import hashlib
import json
import unicodedata

from pydantic import ValidationError

from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.original_archive import validate_original
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.source_history import PREFIX

CONTENT_POLICY = "nfc-whitespace-min16-any-sha256-v1"
MAX_TARGETS = 10_000


def _content_target(journal, scope_id, domain, value):
    identity = json.dumps([1, domain, value], ensure_ascii=False, separators=(",", ":"))
    return journal.target(scope_id, "content", identity)


def content_policy_target(journal, scope_id):
    """Distinguish an evaluated empty text set from an unproven legacy set."""
    return _content_target(journal, scope_id, "policy", CONTENT_POLICY)


def content_targets(journal, event):
    # Reuse the intake contract; only message text contributes, never metadata
    # or the source-reference markup appended for SDK input materialization.
    from cloud_memory_hub.remote_ingest import Conversation, Operation

    try:
        texts = (
            [message.text for message in Conversation.model_validate(event.content).messages]
            if event.kind == "conversation_turn"
            else [Operation.model_validate(event.content).text]
        )
    except ValidationError:
        raise AccessError("original_archive_invalid", 503) from None
    targets = set()
    for text in texts:
        normalized = " ".join(unicodedata.normalize("NFC", text).split())
        if len(normalized) >= 16:
            # Bound the HMAC identity even when JSON escaping expands a valid
            # event. This intermediate digest never leaves process memory.
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            targets.add(_content_target(journal, event.scope_id, "message", digest))
    return targets


def source_target(journal, event):
    identity = json.dumps(
        [1, event.source_id, event.conversation_id, event.record_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return journal.target(event.scope_id, "source_record", identity)


def capture_sources(journal, ingest, scope_id, records):
    """Capture only verified originals in the confirmed directional scope."""
    return _capture(journal, ingest, scope_id, records, include_content=False)


def capture_rules(journal, ingest, scope_id, records):
    """Read verified originals once; refuse a plan exceeding its signed budget."""
    return _capture(journal, ingest, scope_id, records, include_content=True)


def _capture(journal, ingest, scope_id, records, *, include_content):
    targets = {content_policy_target(journal, scope_id)} if include_content else set()

    def require_budget():
        if len(records) + len(targets) > MAX_TARGETS:
            raise GovernanceError("governance_scan_limit")

    require_budget()
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
            if include_content:
                targets.update(content_targets(journal, event))
            require_budget()
    return tuple(sorted(targets, key=lambda item: (item.kind, item.key)))


class SourceSuppression:
    def __init__(self, store, ingest=None):
        self.store = store
        self.ingest = ingest

    def _rules(self, scope_id):
        targets = set()
        unavailable = None
        marker = content_policy_target(self.store.journal, scope_id)
        operations = {
            operation: intent
            for operation, (_, intent) in self.store.operations().items()
            if intent.scope_id == scope_id
        }
        covered_originals = {
            record
            for operation, intent in operations.items()
            if marker in intent.targets
            for record in self.store.records(operation)
            if record.startswith(PREFIX)
        }
        for operation, intent in operations.items():
            sources = {target for target in intent.targets if target.kind == "source_record"}
            targets.update(
                target for target in intent.targets if target.kind in {"source_record", "content"}
            )
            if marker in intent.targets:
                continue
            records = self.store.records(operation)
            if intent.action == "purge":
                # A deleted original cannot prove either an empty rule set or
                # content coverage. Never rewrite or re-sign historical intents.
                if not sources and any(record.startswith(PREFIX) for record in records):
                    unavailable = "governance_source_rules_unavailable"
                elif unavailable is None:
                    unavailable = "governance_content_rules_unavailable"
            else:
                # Later signed captures can prove deleted originals while the
                # remaining originals are still available for reconstruction.
                # An unrelated policy marker never substitutes for ID coverage.
                remaining = [record for record in records if record not in covered_originals]
                if not any(record.startswith(PREFIX) for record in remaining):
                    continue
                try:
                    if self.ingest is None:
                        raise GovernanceError("governance_source_unproven")
                    targets.update(
                        capture_rules(self.store.journal, self.ingest, scope_id, remaining)
                    )
                except (GovernanceError, AccessError):
                    if unavailable is None:
                        unavailable = "governance_content_rules_unavailable"
        return targets, unavailable

    def targets(self, scope_id):
        targets, unavailable = self._rules(scope_id)
        if unavailable is not None:
            raise AccessError(unavailable, 503)
        return targets

    def require_allowed(self, event):
        targets, unavailable = self._rules(event.scope_id)
        if source_target(self.store.journal, event) in targets:
            raise AccessError("governance_source_suppressed", 409)
        if unavailable is not None:
            raise AccessError(unavailable, 503)
        if content_targets(self.store.journal, event) & targets:
            raise AccessError("governance_content_suppressed", 409)
