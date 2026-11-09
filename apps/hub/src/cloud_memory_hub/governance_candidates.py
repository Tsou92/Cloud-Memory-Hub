"""FR-10/FR-11: bounded, read-only queue observations, never SDK effect proof.

The caller supplies an internally verified scope while holding governance,
drain and intake exclusion. This module has no HTTP or execution integration.
Limits bound returned rows and bytes, not SQLite scan time or internal I/O.
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from one_memory.database_access import main_database
from one_memory.memory_server.contracts import WriteRequest
from one_memory.memory_server.durable_queue import (
    _TERMINAL_WRITE_STATES,
    _VALID_JOB_STATES,
    REAPED_PAYLOAD_MARKER,
    UNKNOWN,
)

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.governance_inventory import MAX_CONTENT_BYTES, MAX_RECORDS
from cloud_memory_hub.governance_journal import GovernanceError, Target
from cloud_memory_hub.governance_request_owner import verify_request_owner
from cloud_memory_hub.governance_sources import (
    content_policy_target,
    content_targets,
    source_target,
)
from cloud_memory_hub.original_archive import (
    recover_event,
    require_frozen_match,
    validate_original,
)
from cloud_memory_hub.remote_access import AccessError, ScopeGrant
from cloud_memory_hub.source_history import PREFIX

BATCH_SIZE = 500
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE = ("source_id", "device_id", "scope_id", "adapter_id", "status")
_RECEIPT = (
    "receipt_id",
    "request_id",
    "device_id",
    "event_id",
    "source_id",
    "scope_id",
    "conversation_id",
    "record_id",
    "revision",
    "payload_digest",
)
_JOB = ("job_id", "idempotency_key", "source_key", "state", "hy_invoked", "queue_seq", "updated_at")
_TERMINALS = tuple(sorted(_TERMINAL_WRITE_STATES))
_LIVE = f"(state IS NULL OR state NOT IN ({','.join('?' * len(_TERMINALS))}))"


@dataclass(frozen=True)
class ConfirmedScope:
    """An internal value, not an authorization proof or a client DTO."""

    scope_id: str
    action: Literal["forget", "purge"]
    records: Mapping[str, str]
    rule_targets: tuple[Target, ...]
    basis: Literal["preview", "operation"]
    view_digest: str | None = None
    operation_id: str | None = None

    def __post_init__(self):
        try:
            ScopeGrant.valid_scope(self.scope_id)
            if self.action not in {"forget", "purge"}:
                raise ValueError
            if self.operation_id is not None:
                canonical_uuid(self.operation_id)
            if self.view_digest is not None and (
                not isinstance(self.view_digest, str) or not _HEX.fullmatch(self.view_digest)
            ):
                raise ValueError
            if self.basis == "preview":
                if not isinstance(self.view_digest, str) or not _HEX.fullmatch(self.view_digest):
                    raise ValueError
            elif self.basis == "operation":
                canonical_uuid(self.operation_id)
            else:
                raise ValueError
            if not isinstance(self.records, Mapping) or len(self.records) > MAX_RECORDS:
                raise ValueError
            records = dict(self.records)
            if any(
                not isinstance(key, str)
                or not key
                or len(key) > 1024
                or not isinstance(kind, str)
                or not kind
                or len(kind) > 64
                for key, kind in records.items()
            ):
                raise ValueError
            for key in records:
                if key.startswith(PREFIX):
                    canonical_uuid(key[len(PREFIX) :])
            targets = tuple(self.rule_targets)
            if len(targets) > MAX_RECORDS or any(not isinstance(t, Target) for t in targets):
                raise ValueError
            object.__setattr__(self, "records", MappingProxyType(records))
            object.__setattr__(self, "rule_targets", targets)
        except (ValueError, TypeError, AttributeError):
            raise GovernanceError("governance_candidate_scope_invalid") from None


@dataclass(frozen=True)
class CandidateBudget:
    max_records: int = MAX_RECORDS
    max_bytes: int = MAX_CONTENT_BYTES

    def __post_init__(self):
        if (
            type(self.max_records) is not int
            or not 0 <= self.max_records <= MAX_RECORDS
            or type(self.max_bytes) is not int
            or not 0 <= self.max_bytes <= MAX_CONTENT_BYTES
        ):
            raise GovernanceError("governance_candidate_budget_invalid")


@dataclass(frozen=True)
class CandidateUsage:
    sources: int = 0
    receipts: int = 0
    jobs: int = 0
    orphan_metadata_rows: int = 0
    payload_bytes: int = 0
    metadata_bytes: int = 0


@dataclass(frozen=True)
class CandidateBlocker:
    reason: str
    job_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    job_id: str
    request_id: str
    source_id: str
    device_id: str
    scope_id: str
    state: str
    hy_invoked: bool
    source_status: str
    receipt_presence: Literal["present", "absent"]
    event_proof: Literal["accepted_original", "queue_roundtrip", "unavailable"]
    conversation_id: str | None
    record_id: str | None
    revision: int | None
    reasons: tuple[str, ...]
    input_digest: str
    sdk_effect: Literal["not_checked"] = field(default="not_checked", init=False)


@dataclass(frozen=True)
class CandidateInventory:
    confirmed_original_candidates: tuple[Candidate, ...]
    rule_only_candidates: tuple[Candidate, ...]
    blockers: tuple[CandidateBlocker, ...]
    ownership_coverage: Literal["complete", "incomplete"]
    usage: CandidateUsage
    scope_nonterminal_job_ids: tuple[str, ...]


class _Budget:
    def __init__(self, limit):
        self.limit = limit
        self.values = dict.fromkeys(CandidateUsage.__dataclass_fields__, 0)

    def add(self, name, amount=1):
        if type(amount) is not int or amount < 0:
            raise GovernanceError("governance_candidate_read_failed")
        self.values[name] += amount
        used = self.values[name]
        ceiling = self.limit.max_records
        if name in {"payload_bytes", "metadata_bytes"}:
            used = self.values["payload_bytes"] + self.values["metadata_bytes"]
            ceiling = self.limit.max_bytes
        if used > ceiling:
            raise GovernanceError("governance_candidate_budget_exceeded")


def _metadata(db, table, columns, where, params, order, limit, budget):
    """Measure first; no arbitrary stored field reaches Python before its budget."""
    size = "+".join(f"coalesce(length(CAST({c} AS BLOB)),0)" for c in columns)
    cursor = db.execute(
        f"SELECT rowid AS scan_rowid,({size}) AS metadata_bytes FROM {table} "
        f"WHERE {where} ORDER BY {order} LIMIT ?",
        (*params, limit),
    )
    for measured in cursor:
        amount = measured["metadata_bytes"]
        budget.add("metadata_bytes", amount + 16)  # rowid and byte length scalars
        row = db.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE rowid=:identity AND ({size})<=:budget",
            {"budget": amount, "identity": measured["scan_rowid"]},
        ).fetchone()
        if row is None:
            raise GovernanceError("governance_candidate_read_failed")
        yield {**dict(row), "scan_rowid": measured["scan_rowid"]}


def _payload(db, table, rowid, budget):
    row = db.execute(
        f"SELECT typeof(payload_json),length(CAST(payload_json AS BLOB)) FROM {table} "
        "WHERE rowid=?",
        (rowid,),
    ).fetchone()
    if row is None or row[0] != "text":
        raise ValueError("payload_unavailable")
    budget.add("metadata_bytes", 8)
    budget.add("payload_bytes", row[1])
    value = db.execute(
        f"SELECT CASE WHEN typeof(payload_json)='text' AND "
        f"length(CAST(payload_json AS BLOB))=? THEN payload_json END FROM {table} WHERE rowid=?",
        (row[1], rowid),
    ).fetchone()
    if value is None or not isinstance(value[0], str):
        raise ValueError("payload_unavailable")
    return value[0]


def _reaped_digest(payload):
    pairs = json.loads(payload, object_pairs_hook=lambda items: items)
    if (
        isinstance(pairs, list)
        and len(pairs) == 1
        and isinstance(pairs[0], tuple)
        and pairs[0][0] == REAPED_PAYLOAD_MARKER
        and isinstance(pairs[0][1], str)
        and _HEX.fullmatch(pairs[0][1])
    ):
        return pairs[0][1]
    return None


class _Collector:
    def __init__(self, ingest, scope, journal, budget, db, jobs):
        self.ingest, self.scope, self.journal = ingest, scope, journal
        self.budget, self.db, self.jobs = _Budget(budget), db, jobs
        self.sources, self.receipts, self.selected = {}, {}, {}
        self.receipts_by_request = {}
        self.blockers, self.confirmed, self.rules = set(), [], []
        self.nonterminal_jobs = set()
        self.coverage = True
        self.targets = frozenset(scope.rule_targets)

    def block(self, reason, job_id=None, *, ownership=False):
        try:
            canonical_uuid(job_id)
        except (ValueError, TypeError, AttributeError):
            job_id = None
        self.blockers.add(CandidateBlocker(reason, job_id))
        if ownership:
            self.coverage = False

    def rows(self, db, table, columns, where, params=(), order="rowid", limit=None):
        return _metadata(
            db,
            table,
            columns,
            where,
            params,
            order,
            self.budget.limit.max_records + 1 if limit is None else limit,
            self.budget,
        )

    def source(self, source_id):
        if source_id not in self.sources:
            rows = list(
                self.rows(self.db, "cmh_sources", _SOURCE, "source_id=?", (source_id,), limit=1)
            )
            self.sources[source_id] = rows[0] if rows else None
            if rows:
                self.budget.add("sources")
        return self.sources[source_id]

    def owner(self, job, *, expected_scope=None):
        try:
            source_key = job["source_key"]
            if not isinstance(source_key, str) or not source_key.startswith("cmh:"):
                raise ValueError
            source_id = canonical_uuid(source_key[4:])
        except (ValueError, TypeError, AttributeError):
            self.block("governance_candidate_owner_unproven", job.get("job_id"), ownership=True)
            return None
        # Budget/read failures must abort the observation, never become a
        # partial inventory disguised as an ownership blocker.
        source = self.source(source_id)
        try:
            if source is None or source["status"] not in {"active", "revoked"}:
                raise ValueError
            identity = verify_request_owner(
                job,
                source,
                instance_id=self.ingest.access.instance_id,
                scope_id=expected_scope or source["scope_id"],
            )
            return identity, source
        except (ValueError, TypeError, AttributeError, GovernanceError):
            self.block("governance_candidate_owner_unproven", job.get("job_id"), ownership=True)
            return None

    def select(self, job):
        identity = job["job_id"]
        try:
            canonical_uuid(identity)
        except (ValueError, TypeError, AttributeError):
            self.block("governance_candidate_owner_unproven", ownership=True)
            return
        if identity not in self.selected:
            self.budget.add("jobs")
            self.selected[identity] = job

    def enumerate(self):
        if content_policy_target(self.journal, self.scope.scope_id) not in self.targets:
            self.block("governance_candidate_rules_unavailable")
        for source in self.rows(
            self.db,
            "cmh_sources",
            _SOURCE,
            "scope_id=?",
            (self.scope.scope_id,),
            "source_id",
        ):
            self.budget.add("sources")
            try:
                canonical_uuid(source["source_id"])
            except (ValueError, TypeError, AttributeError):
                self.block("governance_candidate_owner_unproven", ownership=True)
                continue
            self.sources[source["source_id"]] = source
        for receipt in self.rows(
            self.db,
            "cmh_event_receipts",
            _RECEIPT,
            "scope_id=?",
            (self.scope.scope_id,),
        ):
            self.budget.add("receipts")
            try:
                canonical_uuid(receipt["receipt_id"])
                if not isinstance(receipt["request_id"], str):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self.block("governance_candidate_original_unproven", ownership=True)
                continue
            self.receipts[receipt["receipt_id"]] = receipt
            self.receipts_by_request.setdefault(receipt["request_id"], []).append(receipt)
        own_sources = sorted(key for key, row in self.sources.items() if row is not None)
        for offset in range(0, len(own_sources), BATCH_SIZE):
            keys = ["cmh:" + key for key in own_sources[offset : offset + BATCH_SIZE]]
            for job in self.rows(
                self.jobs,
                "memory_write_jobs",
                _JOB,
                f"source_key IN ({','.join('?' * len(keys))}) AND {_LIVE}",
                (*keys, *_TERMINALS),
                "source_key,state,queue_seq,rowid",
            ):
                self.select(job)
        anchors = {key[len(PREFIX) :] for key in self.scope.records if key.startswith(PREFIX)}
        anchors.update(self.receipts)
        self.exact_jobs("job_id", anchors)
        self.exact_jobs("idempotency_key", {row["request_id"] for row in self.receipts.values()})
        for job in self.rows(
            self.jobs,
            "memory_write_jobs",
            _JOB,
            f"(source_key GLOB 'cmh:*' OR idempotency_key GLOB 'cmh:*') AND {_LIVE}",
            _TERMINALS,
            "rowid",
        ):
            self.budget.add("orphan_metadata_rows")
            owner = self.owner(job)
            if owner is not None and owner[0].scope_id == self.scope.scope_id:
                self.select(job)

    def exact_jobs(self, column, values):
        values = sorted(values)
        for offset in range(0, len(values), BATCH_SIZE):
            chunk = values[offset : offset + BATCH_SIZE]
            for row in self.rows(
                self.jobs,
                "memory_write_jobs",
                _JOB,
                f"{column} IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                self.select(row)

    def original(self, row):
        row["payload_json"] = _payload(
            self.db,
            "cmh_event_receipts",
            row["scan_rowid"],
            self.budget,
        )
        try:
            return validate_original(row, self.ingest.access.instance_id)
        finally:
            row.pop("payload_json", None)

    def observe(self, job):
        owned = self.owner(job, expected_scope=self.scope.scope_id)
        if owned is None:
            return
        identity, source = owned
        # Even an unmatched or unreadable task may consume selected native
        # memory later. Observing its identity grants no cancellation authority.
        if job["state"] not in _TERMINAL_WRITE_STATES:
            self.nonterminal_jobs.add(identity.job_id)
        receipt = self.receipts.get(identity.job_id)
        matches = self.receipts_by_request.get(identity.request_id, ())
        if any(row["receipt_id"] != identity.job_id for row in matches):
            self.block("governance_candidate_owner_unproven", identity.job_id, ownership=True)
            return
        event = None
        confirmed = PREFIX + identity.job_id in self.scope.records
        try:
            if receipt is not None:
                if any(
                    receipt[key] != getattr(identity, key)
                    for key in (
                        "request_id",
                        "source_id",
                        "device_id",
                        "scope_id",
                        "event_id",
                    )
                ):
                    raise ValueError
                event = self.original(receipt)
                if confirmed:
                    self.check_rules(event, identity.job_id)
        except (ValueError, TypeError, AccessError):
            self.block("governance_candidate_original_unproven", identity.job_id)
            return
        state = job["state"]
        if state not in _VALID_JOB_STATES or state in {UNKNOWN, "accepted"}:
            self.block("governance_candidate_state_unproven", identity.job_id)
            if state not in _VALID_JOB_STATES:
                return
        if type(job["hy_invoked"]) is not int or job["hy_invoked"] not in {0, 1}:
            self.block("governance_candidate_state_unproven", identity.job_id)
            return
        try:
            payload = _payload(self.jobs, "memory_write_jobs", job["scan_rowid"], self.budget)
            reaped = _reaped_digest(payload)
            if reaped is not None:
                if state not in _TERMINAL_WRITE_STATES:
                    raise ValueError
                proof, event, digest = "unavailable", None, reaped
            else:
                request = WriteRequest.model_validate_json(payload)
                if event is None:
                    event = recover_event(request)
                    proof = "queue_roundtrip"
                else:
                    require_frozen_match(event, request)
                    proof = "accepted_original"
                if any(
                    getattr(event, key) != getattr(identity, key)
                    for key in (
                        "instance_id",
                        "source_id",
                        "device_id",
                        "scope_id",
                        "event_id",
                    )
                ):
                    raise ValueError
                digest = hashlib.sha256(payload.encode()).hexdigest()
        except (ValueError, TypeError, AccessError, RecursionError):
            self.block("governance_candidate_payload_unproven", identity.job_id)
            return
        reasons = {"confirmed_cmhe"} if confirmed else set()
        if event is not None:
            source_rule = source_target(self.journal, event)
            text_rules = content_targets(self.journal, event)
            if confirmed and not {source_rule, *text_rules} <= self.targets:
                self.block("governance_candidate_rules_unavailable", identity.job_id)
            if source_rule in self.targets:
                reasons.add("source_record")
            if text_rules & self.targets:
                reasons.add("content")
        if not reasons:
            return
        candidate = Candidate(
            job_id=identity.job_id,
            request_id=identity.request_id,
            source_id=identity.source_id,
            device_id=identity.device_id,
            scope_id=identity.scope_id,
            state=state,
            hy_invoked=bool(job["hy_invoked"]),
            source_status=source["status"],
            receipt_presence="present" if receipt is not None else "absent",
            event_proof=proof,
            conversation_id=event.conversation_id if event else None,
            record_id=event.record_id if event else None,
            revision=event.revision if event else None,
            reasons=tuple(sorted(reasons)),
            input_digest=digest,
        )
        (self.confirmed if confirmed else self.rules).append(candidate)

    def check_rules(self, event, job_id):
        expected = {source_target(self.journal, event), *content_targets(self.journal, event)}
        if not expected <= self.targets:
            self.block("governance_candidate_rules_unavailable", job_id)

    def collect(self):
        self.enumerate()
        for job_id in sorted(self.selected):
            self.observe(self.selected[job_id])
        return CandidateInventory(
            tuple(self.confirmed),
            tuple(self.rules),
            tuple(sorted(self.blockers, key=lambda item: (item.reason, item.job_id or ""))),
            "complete" if self.coverage else "incomplete",
            CandidateUsage(**self.budget.values),
            tuple(sorted(self.nonterminal_jobs)),
        )


def collect_inflight_candidates(ingest, scope, *, journal, budget=None):
    """Observe only; the caller establishes scope authority and writer exclusion."""
    if budget is None:
        budget = CandidateBudget()
    if not isinstance(scope, ConfirmedScope) or not isinstance(budget, CandidateBudget):
        raise GovernanceError("governance_candidate_scope_invalid")
    queue = ingest._queue()
    try:
        with ingest._database() as db:
            db.execute("PRAGMA query_only=ON")
            with main_database(queue.database_path).read(row_factory=sqlite3.Row) as jobs:
                jobs.execute("BEGIN")
                try:
                    return _Collector(ingest, scope, journal, budget, db, jobs).collect()
                finally:
                    jobs.rollback()
    except (sqlite3.Error, AccessError, OSError):
        raise GovernanceError("governance_candidate_read_failed") from None
