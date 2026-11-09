"""Stopped, local review and signed submission; never a receipt of a full purge.

The locked primitives require the caller's existing three-root StorageLease.
Only the orchestration functions acquire it. Output-domain admission belongs to
the product entry point; this module has no default-success coverage switch.
"""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from uuid import uuid4
from weakref import WeakKeyDictionary

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_diagnostic_confirmation import (
    CLEANUP_POLICY,
    CleanupAuthority,
    RepairParent,
    cleanup_to_dict,
)
from cloud_memory_hub.governance_diagnostic_files import _RecoveryCapture
from cloud_memory_hub.governance_diagnostic_inventory import InventoryBudget, _inspection
from cloud_memory_hub.governance_diagnostic_records import RecordGrant, RecordInspector
from cloud_memory_hub.governance_diagnostic_recovery import (
    build_recovery_graph,
    resolve_cleanup_authority,
)
from cloud_memory_hub.governance_diagnostic_requests import (
    CONFIRMATION_POLICY,
    RepairRequest,
    repair_intent,
    require_same_repair_request,
)
from cloud_memory_hub.governance_execution_store import ExecutionStore
from cloud_memory_hub.governance_journal import GovernanceError, _json
from cloud_memory_hub.privacy import MAX_DEPTH, MAX_NODES, CredentialFilter, PrivacyError
from cloud_memory_hub.storage_lease import StorageLease

_REVIEWS = WeakKeyDictionary()
_APPROVALS = WeakKeyDictionary()
_SCOPE = re.compile(r"cmh:([0-9a-f-]{36}):(global|[0-9a-f-]{36})(?![0-9a-z-])")
MAX_PARENTS = 10_000
MAX_DISPLAY_BYTES = 128 * 1024 * 1024


def _error(code):
    return GovernanceError("diagnostic_review_" + code)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _parent_ids(values):
    if type(values) is not tuple or not 0 < len(values) <= MAX_PARENTS:
        raise _error("selection_invalid")
    try:
        for value in values:
            canonical_uuid(value)
    except (TypeError, ValueError, AttributeError):
        raise _error("selection_invalid") from None
    if len(set(values)) != len(values):
        raise _error("selection_invalid")
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    grant: RecordGrant
    parent_ids: tuple[str, ...]
    copies: tuple[tuple[str, str, int], ...]
    text: str = field(repr=False)
    redacted: bool
    blocked: bool


@dataclass(frozen=True, slots=True)
class ReviewArtifact:
    domain: str
    name: str
    operation_id: str
    pass_id: str
    formal_key: str
    registered_retained_sha256: str
    registered_retained_bytes: int
    identity: tuple[int, ...]
    sha256: str


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class DiagnosticReview:
    instance_id: str
    ledger_id: str
    store_id: str
    parent_ids: tuple[str, ...]
    selections: tuple[tuple[str, str, CleanupAuthority], ...]
    records: tuple[ReviewRecord, ...] = field(repr=False)
    artifacts: tuple[ReviewArtifact, ...]
    inventory: object = field(repr=False)
    digest: str
    budget: InventoryBudget
    origin_proven: bool = False
    body_reviewed: bool = False


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class ReviewApproval:
    """An in-process confirmation receipt, not a serializable boolean claim."""

    requests: tuple[RepairRequest, ...]


@dataclass(frozen=True, slots=True)
class RepairSubmissionResult:
    operation_id: str
    parent_operation_id: str
    state: str = "signed"
    cleanup_state: str = "pending"


def _inventory_value(inventory):
    return {
        "domains": [
            {
                "domain": item.domain,
                "exists": item.exists,
                "device": item.device,
                "inode": item.inode,
            }
            for item in sorted(inventory.domains, key=lambda item: item.domain)
        ],
        "files": [
            asdict(item)
            for item in sorted(inventory.files, key=lambda item: (item.domain, item.key))
        ],
        "bytes": inventory.byte_length,
        "records": inventory.record_count,
    }


def _review_value(review):
    return {
        "version": 1,
        "policy": CLEANUP_POLICY,
        "instance": review.instance_id,
        "ledger": review.ledger_id,
        "store": review.store_id,
        "parents": [
            [identity, scope, cleanup_to_dict(selection)]
            for identity, scope, selection in review.selections
        ],
        "inventory": _inventory_value(review.inventory),
        "artifacts": [asdict(item) for item in review.artifacts],
        "records": [
            [
                asdict(item.grant),
                list(item.parent_ids),
                list(item.copies),
                item.redacted,
                item.blocked,
                hashlib.sha256(item.text.encode()).hexdigest(),
            ]
            for item in review.records
        ],
    }


def _registered_review(review):
    if type(review) is not DiagnosticReview or _REVIEWS.get(review) != review.digest:
        raise _error("unproven")
    if _digest(_review_value(review)) != review.digest:
        raise _error("changed")


def _filtered_record(raw, grant, privacy):
    text = raw.decode("utf-8")
    # Native JSON records are already strictly parsed by RecordInspector. Filter
    # the complete structure, avoiding a false redaction label caused only by
    # normalizing the physical line's whitespace or final LF.
    if grant.format.endswith("jsonl-v1"):
        filtered = privacy.protect(json.loads(text))
        if filtered.redacted:
            text = json.dumps(filtered.value, ensure_ascii=False) + "\n"
    else:
        filtered = privacy.protect({"content": text})
        text = filtered.value["content"]
    return text, filtered.redacted


def _scope_conflict(raw, expected):
    """Detect positive scope markers before any credential masking or formatting."""
    pending = [(raw.decode("utf-8"), 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _error("scope_unresolved")
        if isinstance(value, str):
            if any("cmh:" + ":".join(match) != expected for match in _SCOPE.findall(value)):
                return True
            if value.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(value)
                except (ValueError, RecursionError):
                    continue  # CredentialFilter still rejects malformed structured text.
                pending.append((decoded, depth + 1))
        elif isinstance(value, dict):
            pending.extend((item, depth + 1) for pair in value.items() for item in pair)
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return False  # This does not prove absence of independent content.


def review_diagnostic_repairs_locked(config, parent_ids, *, budget=None):
    """Read a full multi-parent selection under the caller's existing StorageLease."""
    parent_ids = _parent_ids(parent_ids)
    budget = InventoryBudget() if budget is None else budget
    if type(budget) is not InventoryBudget:
        raise _error("budget_invalid")
    authority = open_authority(config, read_only=True)
    store = ExecutionStore(authority.journal, read_only=True)
    recovery = store.recovery_snapshot()
    graph = build_recovery_graph(recovery)
    if graph.unplanned:
        raise GovernanceError("governance_execution_pending")
    operations = recovery.snapshot.operations
    for identity in parent_ids:
        operation = operations.get(identity)
        if (
            identity not in recovery.committed
            or operation is None
            or operation.intent.action != "purge"
            or operation.cleanup.authority.mode == "repair"
        ):
            raise GovernanceError("governance_diagnostic_parent_unproven")
    references = tuple(
        item for item in graph.authority.contributors if item.operation_id in parent_ids
    )
    selected = resolve_cleanup_authority(recovery, references, owner_operation_id=parent_ids[0])
    # Existing committed grants participate in coverage, but never silently
    # become a new human selection for another parent.
    requests = {item.request_id: item for item in graph.authority.requests}
    requests.update({item.request_id: item for item in selected.requests})
    request_tuple = tuple(requests[key] for key in sorted(requests))
    inspector = RecordInspector(
        request_tuple, mode="propose", max_record_bytes=budget.max_record_bytes
    )
    evidence = _RecoveryCapture(
        tuple(operations[item.operation_id].cleanup for item in graph.authority.contributors),
        budget,
    )
    owners = {}
    for identity in parent_ids:
        for selector in operations[identity].cleanup.authority.selectors:
            owners.setdefault(selector.request_id, []).append(identity)
    collected, offsets = {}, {}
    privacy = CredentialFilter((config.admin_token, config.encryption_key.decode()))
    display_bytes = 0

    def capture(domain, key, raw, action):
        nonlocal display_bytes
        evidence.record(domain, key, raw, action)
        offset = offsets.get((domain, key), 0)
        offsets[(domain, key)] = offset + len(raw)
        if action != "candidate":
            return
        grant = inspector.inspect(raw, kind=domain).grant
        parents = tuple(sorted(owners.get(grant.request_id, ())))
        if not parents:
            if grant not in graph.authority.grants:
                raise _error("unselected_record")
            return
        if grant in collected:
            collected[grant]["copies"].append((domain, key, offset))
            return
        blocked = _scope_conflict(raw, requests[grant.request_id].user_id)
        try:
            text, redacted = _filtered_record(raw, grant, privacy)
        except PrivacyError:
            raise _error("privacy_unavailable") from None
        display_bytes += len(text.encode())
        if display_bytes > MAX_DISPLAY_BYTES:
            raise _error("budget_exceeded")
        collected[grant] = {
            "parents": parents,
            "copies": [(domain, key, offset)],
            "text": text,
            "redacted": redacted,
            "blocked": blocked,
        }

    with _inspection(
        data_root=config.data_dir.absolute(),
        sdk_log_root=config.data_dir.absolute() / "hy-memory/governed-v2/logs",
        requests=request_tuple,
        mode="propose",
        budget=budget,
        on_record=capture,
        on_file=evidence.file,
        artifact=evidence.artifact,
        artifact_limit=budget.max_files,
    ) as (inventory, scan):
        # A candidate is never a kept byte. Reuse the engine's strict-only
        # retained-prefix and registered-artifact proof without retiring,
        # checkpointing or creating any file during review.
        evidence.prove_retained_artifacts()
        scan.verify_unchanged()
        artifacts = []
        for (domain, name), (_directory, identity) in sorted(evidence.stages.items()):
            entry = evidence.registered[(domain, name)]
            operation_id, pass_id, _index = evidence._NAME.fullmatch(name).groups()
            retained = evidence.files[(domain, entry.before.key)].retained
            artifacts.append(
                ReviewArtifact(
                    domain,
                    name,
                    operation_id,
                    pass_id,
                    entry.before.key,
                    entry.retained_sha256,
                    entry.retained_bytes,
                    identity,
                    hashlib.sha256(memoryview(retained)[: identity[4]]).hexdigest(),
                )
            )
        records = tuple(
            ReviewRecord(
                grant,
                item["parents"],
                tuple(sorted(item["copies"])),
                item["text"],
                item["redacted"],
                item["blocked"],
            )
            for grant, item in sorted(collected.items())
        )
        selections = tuple(
            (
                identity,
                operations[identity].intent.scope_id,
                CleanupAuthority(
                    "repair",
                    operations[identity].cleanup.authority.selectors,
                    tuple(item.grant for item in records if identity in item.parent_ids),
                    RepairParent(
                        identity, recovery.committed_digests[identity], authority.journal.ledger_id
                    ),
                ),
            )
            for identity in parent_ids
        )
        review = DiagnosticReview(
            authority.journal.instance_id,
            authority.journal.ledger_id,
            recovery.snapshot.binding.store_id,
            parent_ids,
            selections,
            records,
            tuple(artifacts),
            inventory,
            "",
            budget,
        )
        review = replace(review, digest=_digest(_review_value(review)))
        _REVIEWS[review] = review.digest
        return review


def review_diagnostic_repairs(config, parent_ids, *, budget=None):
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        return review_diagnostic_repairs_locked(config, parent_ids, budget=budget)


def _tty(stream):
    try:
        return stream.isatty() and os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False


def _write(stream, value):
    stream.write(value)
    stream.flush()


class _BoundedWriter:
    def __init__(self, stream):
        self.stream = stream
        self.bytes = 0

    def write(self, value):
        self.bytes += len(value.encode("utf-8"))
        if self.bytes > MAX_DISPLAY_BYTES:
            raise _error("budget_exceeded")
        return self.stream.write(value)

    def flush(self):
        self.stream.flush()


def _answer(reader, writer, prompt):
    _write(writer, prompt)
    try:
        value = reader.readline(64)
    except (OSError, ValueError):
        raise _error("cancelled") from None
    if not value or not value.endswith("\n"):
        raise _error("cancelled")
    return value.strip()


def _selection_digest(review, choices):
    return _digest(
        [
            1,
            CONFIRMATION_POLICY,
            review.digest,
            [[key, [asdict(g) for g in grants]] for key, grants in choices],
        ]
    )


def _terminal_text(value):
    """Lossless quoted display: readable Unicode, inert nonprinting characters."""
    result = ['"']
    escaped = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for character in value:
        if character in escaped:
            result.append(escaped[character])
        elif character.isprintable():
            result.append(character)
        else:
            code = ord(character)
            result.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
    return "".join((*result, '"'))


def confirm_diagnostic_review_tty(review, *, reader, writer, operation_ids=None, page_chars=3000):
    """Display all filtered content; terminal acknowledgements grant exact records only."""
    _registered_review(review)
    if not _tty(reader) or not _tty(writer):
        raise _error("tty_required")
    writer = _BoundedWriter(writer)
    if type(page_chars) is not int or not 64 <= page_chars <= 16_384:
        raise _error("pagination_invalid")
    operation_ids = (
        {identity: str(uuid4()) for identity in review.parent_ids}
        if operation_ids is None
        else dict(operation_ids)
    )
    if set(operation_ids) != set(review.parent_ids):
        raise _error("selection_invalid")
    try:
        for value in operation_ids.values():
            canonical_uuid(value)
    except (ValueError, TypeError, AttributeError):
        raise _error("selection_invalid") from None
    if len(set(operation_ids.values())) != len(operation_ids):
        raise _error("selection_invalid")
    _write(
        writer,
        f"审阅清单：{len(review.inventory.files)} 个文件，"
        f"{review.inventory.record_count} 条记录，{len(review.records)} 条需要选择。\n",
    )
    for domain in sorted(review.inventory.domains, key=lambda item: item.domain):
        _write(writer, f"日志域 {domain.domain}：{'存在' if domain.exists else '未发现'}\n")
    for item in sorted(review.inventory.files, key=lambda item: (item.domain, item.key)):
        _write(
            writer,
            f"文件 {item.domain}/{item.key}，{item.byte_length} 字节，SHA256 {item.sha256}，"
            f"候选 {item.candidate_count} 条，保留 {item.kept_count} 条。\n",
        )
    for item in review.artifacts:
        _write(
            writer,
            f"已验证的恢复暂存 {item.domain}/{item.name}，{item.identity[4]} 字节，"
            f"SHA256 {item.sha256}；本次提交不删除该文件。\n",
        )
    chosen = {identity: [] for identity in review.parent_ids}
    rendered = 0
    for index, record in enumerate(review.records, 1):
        _write(
            writer,
            f"\n记录 {index}/{len(review.records)}，{record.grant.byte_length} 字节，"
            f"SHA256 {record.grant.sha256}\n",
        )
        _write(writer, "父操作：" + ", ".join(record.parent_ids) + "\n")
        _write(writer, "副本：" + json.dumps(record.copies, ensure_ascii=True) + "\n")
        _write(
            writer,
            "隐私凭证已遮盖；以下不是原字节全文。\n"
            if record.redacted
            else "完整显示过滤后的记录；不证明来源。\n",
        )
        # Printable Chinese/Unicode stays readable. History remains inert text;
        # no terminal control or format character is emitted as a control.
        escaped = _terminal_text(record.text)
        rendered += len(escaped.encode("utf-8"))
        if rendered > MAX_DISPLAY_BYTES:
            raise _error("budget_exceeded")
        pages = tuple(
            escaped[start : start + page_chars] for start in range(0, len(escaped), page_chars)
        )
        for number, page in enumerate(pages, 1):
            _write(writer, f"内容页 {number}/{len(pages)}：\n{page}\n")
            if _answer(reader, writer, "输入 n 查看下一页/完成本条显示；q 取消：") != "n":
                raise _error("cancelled")
        if record.blocked:
            _write(writer, "已发现跨范围标记，本条不能批准。\n")
            continue
        answer = _answer(
            reader,
            writer,
            "整条仅属于哪个父操作？输入其 UUID；m 混合、u 不确定、s 不批准、q 取消：",
        )
        if answer == "q":
            raise _error("cancelled")
        if answer in {"m", "u", "s"}:
            continue
        if answer not in record.parent_ids:
            raise _error("selection_invalid")
        chosen[answer].append(record.grant)
    choices = tuple((key, tuple(sorted(value))) for key, value in sorted(chosen.items()) if value)
    if not choices:
        raise _error("nothing_selected")
    digest = _selection_digest(review, choices)
    selections = {key: (scope, selection) for key, scope, selection in review.selections}
    requests = tuple(
        RepairRequest(
            operation_ids[key],
            selections[key][0],
            replace(selections[key][1], grants=grants),
            digest,
        )
        for key, grants in choices
    )
    _write(writer, "\n将授权删除所选整条原始记录及全部同字节副本；不扩大父业务删除范围。\n")
    for request in requests:
        _write(
            writer,
            f"父 {request.selection.parent.operation_id} → 修复 {request.operation_id}，"
            f"{len(request.selection.grants)} 条\n",
        )
    if (
        _answer(reader, writer, f"审阅摘要 {digest}\n输入 CONFIRM 提交；其他输入取消：")
        != "CONFIRM"
    ):
        raise _error("cancelled")
    approval = ReviewApproval(requests)
    _APPROVALS[approval] = (review, requests)
    return approval


def submit_diagnostic_repairs_locked(config, requests, *, approval=None):
    """Caller owns StorageLease; all new approvals rechecked before the first plan."""
    if type(requests) is not tuple or not 0 < len(requests) <= MAX_PARENTS:
        raise _error("selection_invalid")
    if any(type(item) is not RepairRequest for item in requests):
        raise _error("selection_invalid")
    for item in requests:
        item.__post_init__()
    if len({item.operation_id for item in requests}) != len(requests) or len(
        {item.selection.parent.operation_id for item in requests}
    ) != len(requests):
        raise _error("selection_invalid")
    # Read-only admission first: invalid requests cannot refresh a witness.
    observer = open_authority(config, read_only=True)
    observed = ExecutionStore(observer.journal, read_only=True)
    recovery = observed.recovery_snapshot()
    new = []
    for request in requests:
        operation = recovery.snapshot.operations.get(request.operation_id)
        if operation is None:
            new.append(request)
        else:
            require_same_repair_request(
                observer.journal, operation, request, observer.access.instance_id
            )
            if request.operation_id not in recovery.committed:
                new.append(request)
    if not new:
        # This is a receipt lookup, not file recovery or a new authorization.
        # Preserve access to already-signed request identities during unrelated
        # maintenance faults, without opening any writable authority or files.
        return tuple(
            RepairSubmissionResult(
                item.operation_id,
                item.selection.parent.operation_id,
                cleanup_state=recovery.snapshot.operations[item.operation_id].cleanup.state,
            )
            for item in requests
        )
    if recovery.unplanned:
        raise GovernanceError("governance_execution_pending")
    if type(approval) is not ReviewApproval or approval not in _APPROVALS:
        raise _error("confirmation_required")
    review, approved = _APPROVALS[approval]
    _registered_review(review)
    if requests != approved or approval.requests != approved:
        raise _error("confirmation_invalid")
    current = review_diagnostic_repairs_locked(config, review.parent_ids, budget=review.budget)
    if current.digest != review.digest:
        raise _error("changed")
    choices = tuple(
        (item.selection.parent.operation_id, item.selection.grants) for item in requests
    )
    if any(item.review_digest != _selection_digest(current, choices) for item in requests):
        raise _error("confirmation_invalid")
    authority = open_authority(config)
    store = ExecutionStore(authority.journal)
    # Plan all rows before appending any intent. A crash leaves known orphan
    # plans; they never contribute authority until their real append commits.
    for request in new:
        store.plan(
            repair_intent(authority.journal, request, authority.access.instance_id),
            {},
            cleanup=request.selection,
        )
    for request in new:
        authority.append(repair_intent(authority.journal, request, authority.access.instance_id))
    return tuple(
        RepairSubmissionResult(
            item.operation_id,
            item.selection.parent.operation_id,
            cleanup_state=store.cleanup(item.operation_id).state,
        )
        for item in requests
    )


def review_and_submit_diagnostic_repairs(
    config, parent_ids, *, reader, writer, operation_ids=None, budget=None
):
    """One stopped lease spans observation, explicit TTY confirmation and signing."""
    with StorageLease(config.data_dir, config.config_dir, config.governance_dir):
        review = review_diagnostic_repairs_locked(config, parent_ids, budget=budget)
        approval = confirm_diagnostic_review_tty(
            review, reader=reader, writer=writer, operation_ids=operation_ids
        )
        return submit_diagnostic_repairs_locked(config, approval.requests, approval=approval)
