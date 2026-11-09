"""Immutable physical-record selection, never evidence of provenance or completion.

These pure types bind the exact record bytes to an intent. Live input proofs,
the committed parent chain, file coverage and the user's separate confirmation
remain the responsibility of the maintenance/execution boundary.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cloud_memory_hub.events import canonical_uuid, split_request_identity
from cloud_memory_hub.governance_diagnostic_records import MAX_SELECTION, RecordGrant
from cloud_memory_hub.governance_inflight import (
    CleanupSelector,
    inflight_job_target,
    inflight_policy_target,
)
from cloud_memory_hub.governance_journal import GovernanceError, Intent, _json
from cloud_memory_hub.source_history import PREFIX

CLEANUP_POLICY = "sdk-two-domains-exact-records-v1"
_HEX = re.compile(r"[0-9a-f]{64}")


def _invalid():
    return GovernanceError("governance_diagnostic_confirmation_invalid")


def _uuid(value):
    if type(value) is not str:
        raise ValueError
    return canonical_uuid(value)


def _hex(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError
    return value


def _selector_key(selector):
    return selector.job_id, selector.request_id, selector.input_digest, selector.disposition


def _validate_selector(selector):
    if type(selector) is not CleanupSelector:
        raise ValueError
    _uuid(selector.job_id)
    if type(selector.request_id) is not str:
        raise ValueError
    split_request_identity(selector.request_id)
    _hex(selector.input_digest)
    if selector.disposition not in {"cancel_unpermitted", "terminal_cleanup"}:
        raise ValueError


@dataclass(frozen=True, slots=True)
class RepairParent:
    operation_id: str
    intent_digest: str
    ledger_id: str

    def __post_init__(self):
        try:
            _uuid(self.operation_id)
            _hex(self.intent_digest)
            _uuid(self.ledger_id)
        except (ValueError, TypeError, AttributeError):
            raise _invalid() from None


@dataclass(frozen=True, slots=True)
class CleanupAuthority:
    mode: Literal["legacy", "records", "repair"]
    selectors: tuple[CleanupSelector, ...]
    grants: tuple[RecordGrant, ...]
    parent: RepairParent | None = None
    policy: str = CLEANUP_POLICY

    def __post_init__(self):
        try:
            if self.mode not in {"legacy", "records", "repair"} or self.policy != CLEANUP_POLICY:
                raise ValueError
            if type(self.selectors) is not tuple or type(self.grants) is not tuple:
                raise ValueError
            if len(self.selectors) > MAX_SELECTION or len(self.grants) > MAX_SELECTION:
                raise ValueError
            for selector in self.selectors:
                _validate_selector(selector)
            if len({item.job_id for item in self.selectors}) != len(self.selectors):
                raise ValueError
            requests = {item.request_id for item in self.selectors}
            if len(requests) != len(self.selectors):
                raise ValueError
            if any(type(grant) is not RecordGrant for grant in self.grants):
                raise ValueError
            # Revalidate immutable instances too; callers may have bypassed construction.
            for grant in self.grants:
                RecordGrant(grant.format, grant.request_id, grant.sha256, grant.byte_length)
            if len(set(self.grants)) != len(self.grants):
                raise ValueError
            if any(grant.request_id not in requests for grant in self.grants):
                raise ValueError
            if self.mode == "legacy" and self.grants:
                raise ValueError
            if self.mode == "repair":
                if type(self.parent) is not RepairParent:
                    raise ValueError
                RepairParent(
                    self.parent.operation_id, self.parent.intent_digest, self.parent.ledger_id
                )
            elif self.parent is not None:
                raise ValueError
            object.__setattr__(self, "selectors", tuple(sorted(self.selectors, key=_selector_key)))
            object.__setattr__(self, "grants", tuple(sorted(self.grants)))
        except (ValueError, TypeError, AttributeError):
            raise _invalid() from None


def cleanup_to_dict(authority: CleanupAuthority) -> dict:
    if type(authority) is not CleanupAuthority:
        raise _invalid()
    authority = CleanupAuthority(
        authority.mode, authority.selectors, authority.grants, authority.parent, authority.policy
    )
    parent = authority.parent
    return {
        "version": 1,
        "mode": authority.mode,
        "policy": authority.policy,
        "selectors": [
            dict(
                zip(
                    ("job_id", "request_id", "input_digest", "disposition"),
                    _selector_key(item),
                    strict=True,
                )
            )
            for item in authority.selectors
        ],
        "grants": [
            {
                "format": item.format,
                "request_id": item.request_id,
                "sha256": item.sha256,
                "byte_length": item.byte_length,
            }
            for item in authority.grants
        ],
        "parent": None
        if parent is None
        else {
            "operation_id": parent.operation_id,
            "intent_digest": parent.intent_digest,
            "ledger_id": parent.ledger_id,
        },
    }


def cleanup_from_dict(value: dict) -> CleanupAuthority:
    try:
        if type(value) is not dict or set(value) != {
            "version",
            "mode",
            "policy",
            "selectors",
            "grants",
            "parent",
        }:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError
        if type(value["selectors"]) is not list or type(value["grants"]) is not list:
            raise ValueError
        if len(value["selectors"]) > MAX_SELECTION or len(value["grants"]) > MAX_SELECTION:
            raise ValueError
        parent = value["parent"]
        return CleanupAuthority(
            mode=value["mode"],
            policy=value["policy"],
            selectors=tuple(CleanupSelector(**item) for item in value["selectors"]),
            grants=tuple(RecordGrant(**item) for item in value["grants"]),
            parent=None if parent is None else RepairParent(**parent),
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        raise _invalid() from None


def cleanup_digest(authority: CleanupAuthority) -> str:
    return hashlib.sha256(_json(cleanup_to_dict(authority)).encode()).hexdigest()


def cleanup_policy_targets(journal, scope_id, authority: CleanupAuthority):
    """New intents commit both the domain contract and immutable exact selection."""
    digest = cleanup_digest(authority)
    if authority.mode == "legacy":
        return ()
    return tuple(
        journal.target(scope_id, "content", _json(value))
        for value in (
            [1, "diagnostic-cleanup-policy", CLEANUP_POLICY],
            [1, "diagnostic-cleanup-selection", digest],
        )
    )


def validate_cleanup_for_intent(
    journal, intent: Intent, records: Mapping[str, str], authority: CleanupAuthority
) -> None:
    """Static validation only; store must separately prove the actual parent entry."""
    try:
        cleanup_to_dict(authority)
        if intent.action != "purge" or not isinstance(records, Mapping):
            raise ValueError
        if (
            authority.mode == "legacy"
            and journal.target(
                intent.scope_id, "content", _json([1, "diagnostic-cleanup-policy", CLEANUP_POLICY])
            )
            in intent.targets
        ):
            raise ValueError
        if any(kind not in {"raw", "memory"} for kind in records.values()):
            raise ValueError
        for selector in authority.selectors:
            if split_request_identity(selector.request_id)[0] != journal.instance_id:
                raise ValueError
        if any(
            target not in intent.targets
            for target in cleanup_policy_targets(journal, intent.scope_id, authority)
        ):
            raise ValueError
        parent = authority.parent
        if authority.mode == "repair":
            # Its signed parent carries whole-write certificates and raw membership.
            # A repair never re-runs ordinary business mutation on that membership.
            if (
                records
                or parent.operation_id == intent.operation_id
                or parent.ledger_id != journal.ledger_id
                or any(target.kind in {"memory", "raw"} for target in intent.targets)
            ):
                raise ValueError
            return
        expected = {
            identity[len(PREFIX) :]
            for identity, kind in records.items()
            if kind == "raw" and identity.startswith(PREFIX)
        }
        if expected != {selector.job_id for selector in authority.selectors}:
            raise ValueError
        if (
            authority.selectors
            and inflight_policy_target(journal, intent.scope_id) not in intent.targets
        ):
            raise ValueError
        for selector in authority.selectors:
            if (
                inflight_job_target(
                    journal, intent.scope_id, selector, disposition=selector.disposition
                )
                not in intent.targets
            ):
                raise ValueError
    except (ValueError, TypeError, AttributeError, KeyError):
        raise _invalid() from None
