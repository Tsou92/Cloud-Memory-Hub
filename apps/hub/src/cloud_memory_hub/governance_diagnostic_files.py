"""Execute exact diagnostic-record grants under an already-held writer/file gate.

This synchronous component owns neither the service lease nor SDK admission.
The caller first verifies Authority's external witnesses and holds the service
lease, SDK admission and diagnostic drain through the joined thread's completion.
Progress is a retry cache: every invocation validates all current records again.
"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

from cloud_memory_hub.governance_diagnostic_confirmation import cleanup_digest
from cloud_memory_hub.governance_diagnostic_inventory import (
    FileObservation,
    InventoryBudget,
    _inspection,
    _snapshot,
)
from cloud_memory_hub.governance_diagnostic_profiles import CORE_PROFILE, SDK_PROFILE
from cloud_memory_hub.governance_diagnostic_records import WriteRequest
from cloud_memory_hub.governance_diagnostic_recovery import (
    build_recovery_graph,
    resolve_cleanup_authority,
)
from cloud_memory_hub.governance_execution_store import (
    ArtifactAbsence,
    ArtifactEntry,
    CleanupContributor,
    CleanupManifest,
    CleanupVerification,
    manifest_digest,
    stage_name,
)
from cloud_memory_hub.governance_journal import GovernanceError


@dataclass(slots=True)
class _File:
    observation: FileObservation
    directory: int
    name: str
    expected: tuple
    retained: bytearray = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self.retained).hexdigest()


class _Capture:
    def __init__(self, receipt, budget):
        self.retained, self.files, self.stages = {}, {}, {}
        self.budget = budget
        self.retained_bytes = self.stage_bytes = 0
        self.registered = {
            (entry.before.domain, entry.stage_name): entry
            for entry in (() if receipt.manifest is None else receipt.manifest.artifacts)
        }

    def record(self, domain, key, raw, action):
        if action == "keep":
            self.retained_bytes += len(raw)
            if self.retained_bytes > self.budget.max_total_bytes:
                raise GovernanceError("diagnostic_inventory_budget_exceeded")
            self.retained.setdefault((domain, key), bytearray()).extend(raw)

    def file(self, observation, directory, name, expected):
        key = (observation.domain, observation.key)
        self.files[key] = _File(
            observation, directory, name, expected, self.retained.pop(key, bytearray())
        )

    def artifact(self, domain, directory, name, expected, device):
        entry = self.registered.get((domain, name))
        if entry is None:
            return False
        if (
            not stat.S_ISREG(expected[2])
            or stat.S_IMODE(expected[2]) != 0o600
            or expected[3] != 1
            or expected[0] != device
            or expected[4] > entry.retained_bytes
        ):
            raise GovernanceError("diagnostic_file_artifact_unproven")
        self.stage_bytes += expected[4]
        if self.stage_bytes > self.budget.max_total_bytes:
            raise GovernanceError("diagnostic_inventory_budget_exceeded")
        self.stages[(domain, name)] = (directory, expected)
        return True

    def prove_retained_artifacts(self):
        """A stage must not be the only surviving copy of retained bytes."""
        for key, entry in self.registered.items():
            if key not in self.stages:
                # Old progress is not a permanent content/location anchor.
                # With no artifact, the fresh authorized full scan is the proof.
                continue
            current = self.files.get((entry.before.domain, entry.before.key))
            if (
                current is None
                or len(current.retained) < entry.retained_bytes
                or hashlib.sha256(memoryview(current.retained)[: entry.retained_bytes]).hexdigest()
                != entry.retained_sha256
            ):
                raise GovernanceError("diagnostic_file_retained_unproven")
            directory, expected = self.stages[key]
            _prove_stage(directory, entry.stage_name, expected, current.retained)


class _RecoveryCapture(_Capture):
    """Resolve only extant stages; historical absent names consume no file budget."""

    _NAME = re.compile(r"\.cmh-cleanup-([0-9a-f-]{36})-([0-9a-f-]{36})-([0-9]{3})\.tmp")

    def __init__(self, receipts, budget):
        super().__init__(receipts[0], budget)
        self.registered = {}
        self.receipts = {receipt.operation_id: receipt for receipt in receipts}

    def artifact(self, domain, directory, name, expected, device):
        match = self._NAME.fullmatch(name)
        if match is None:
            return False
        operation_id, pass_id, index = match.groups()
        receipt = self.receipts.get(operation_id)
        if (
            receipt is None
            or receipt.manifest is None
            or receipt.pass_id != pass_id
            or int(index) >= len(receipt.manifest.artifacts)
        ):
            return False
        entry = receipt.manifest.artifacts[int(index)]
        if entry.before.domain != domain or entry.stage_name != name:
            return False
        key = (domain, name)
        if key in self.registered:
            raise GovernanceError("diagnostic_file_artifact_unproven")
        if len(self.registered) >= self.budget.max_files:
            raise GovernanceError("diagnostic_inventory_budget_exceeded")
        self.registered[key] = entry
        return super().artifact(domain, directory, name, expected, device)


def _prove_stage(directory, name, expected, retained, *, digest=None):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if _snapshot(os.fstat(descriptor)) != expected:
            raise GovernanceError("diagnostic_inventory_changed")
        offset, actual = 0, hashlib.sha256()
        while chunk := os.read(descriptor, 64 * 1024):
            if offset + len(chunk) > expected[4]:
                raise GovernanceError("diagnostic_inventory_changed")
            if chunk != retained[offset : offset + len(chunk)]:
                raise GovernanceError("diagnostic_file_artifact_unproven")
            offset += len(chunk)
            actual.update(chunk)
        if (
            offset != expected[4]
            or _snapshot(os.fstat(descriptor)) != expected
            or _snapshot(os.stat(name, dir_fd=directory, follow_symlinks=False)) != expected
        ):
            raise GovernanceError("diagnostic_inventory_changed")
        if digest is not None and (offset != len(retained) or actual.hexdigest() != digest):
            raise GovernanceError("diagnostic_file_artifact_unproven")
    finally:
        os.close(descriptor)


def _path(roots, observation):
    return roots[observation.domain] / observation.key


def _bindings(roots, files):
    return tuple((_path(roots, item), item.device, item.inode) for item in files)


def _unchanged_file(file):
    if _snapshot(os.stat(file.name, dir_fd=file.directory, follow_symlinks=False)) != file.expected:
        raise GovernanceError("diagnostic_inventory_changed")


def _absence(scan, receipt):
    names = []
    for entry in () if receipt.manifest is None else receipt.manifest.artifacts:
        parent = "default" if entry.before.domain == "pipeline" else ""
        directory = scan.locations.get((entry.before.domain, parent))
        if directory is not None:
            try:
                os.stat(entry.stage_name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise GovernanceError("diagnostic_file_artifact_pending")
        names.append((entry.before.domain, entry.stage_name))
    scan.verify_paths()
    return ArtifactAbsence(
        receipt.operation_id,
        receipt.pass_id,
        manifest_digest(receipt.manifest),
        tuple(sorted(names)),
    )


def _retire_previous(store, receipt, capture, scan):
    # All current files and every retained prefix are proven before the first unlink.
    owned = {
        (entry.before.domain, entry.stage_name)
        for entry in (() if receipt.manifest is None else receipt.manifest.artifacts)
    }
    for key, (directory, expected) in sorted(capture.stages.items()):
        if key not in owned:
            continue
        _domain, name = key
        scan.verify_paths()
        if _snapshot(os.stat(name, dir_fd=directory, follow_symlinks=False)) != expected:
            raise GovernanceError("diagnostic_inventory_changed")
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    if receipt.manifest is not None and any(
        item.state not in {"verified", "abandoned"} for item in receipt.manifest.artifacts
    ):
        receipt = store.checkpoint(
            receipt,
            manifest=replace(
                receipt.manifest,
                artifacts=tuple(
                    item
                    if item.state in {"verified", "abandoned"}
                    else replace(item, state="abandoned")
                    for item in receipt.manifest.artifacts
                ),
            ),
        )
    return receipt, _absence(scan, receipt)


def _new_manifest(operation_id, capture, *, contributors=None, profile=SDK_PROFILE):
    pass_id = str(uuid4())
    entries = []
    for _key, file in sorted(capture.files.items()):
        if file.observation.removed_count:
            entries.append(
                ArtifactEntry(
                    file.observation,
                    file.digest,
                    len(file.retained),
                    stage_name(operation_id, pass_id, len(entries)),
                )
            )
    return CleanupManifest(
        operation_id, pass_id, tuple(entries), contributors=contributors, profile=profile
    )


def _checkpoint(store, receipt, index, state):
    entries = list(receipt.manifest.artifacts)
    entries[index] = replace(entries[index], state=state)
    return store.checkpoint(receipt, manifest=replace(receipt.manifest, artifacts=tuple(entries)))


def _stage(file, entry):
    descriptor = os.open(
        entry.stage_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=file.directory,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(file.retained)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset : offset + 64 * 1024])
            if written <= 0:
                raise GovernanceError("diagnostic_file_write_failed")
            offset += written
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(view):
            raise GovernanceError("diagnostic_file_artifact_unproven")
        expected = _snapshot(info)
    finally:
        os.close(descriptor)
    os.fsync(file.directory)
    return expected


def _adopt(gate, path, file, requests, identity):
    streams = {}
    try:
        for request in requests:
            if request.mode != "a":
                raise GovernanceError("diagnostic_file_reopen_unsupported")
            descriptor = os.open(
                file.name,
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=file.directory,
            )
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != identity
                ):
                    raise GovernanceError("diagnostic_inventory_changed")
                stream = os.fdopen(
                    descriptor, request.mode, encoding=request.encoding, errors=request.errors
                )
            except BaseException:
                os.close(descriptor)
                raise
            streams[request.token] = stream
        gate.adopt(path, streams, identity=identity)
    except BaseException:
        for stream in streams.values():
            stream.close()
        raise


def _execute(
    store,
    receipt,
    *,
    data_root,
    sdk_log_root,
    gate,
    budget,
    profile,
    recovery_receipts=None,
    resolved=None,
):
    roots = {"pipeline": data_root / "hy-memory/governed-v2/logs", "sdk": sdk_log_root}
    if profile == CORE_PROFILE:
        from cloud_memory_hub.diagnostics import require_diagnostic_file_maintenance

        roots.update({"core-log": data_root / "logs", "core-diagnostic": data_root / "logs"})
        require_diagnostic_file_maintenance(gate, roots=tuple(roots.values()))
    requests = tuple(
        WriteRequest(item.request_id, f"cmh:{store.journal.instance_id}:{receipt.scope_id}")
        for item in receipt.authority.selectors
    )
    options = dict(
        data_root=data_root,
        sdk_log_root=sdk_log_root,
        requests=requests if resolved is None else resolved.requests,
        grants=receipt.authority.grants if resolved is None else resolved.grants,
        mode="authorized",
        budget=budget,
        profile=profile,
    )
    capture = (
        _Capture(receipt, budget)
        if recovery_receipts is None
        else _RecoveryCapture(recovery_receipts, budget)
    )
    with _inspection(
        **options,
        on_record=capture.record,
        on_file=capture.file,
        artifact=capture.artifact,
        artifact_limit=len(capture.registered) if recovery_receipts is None else budget.max_files,
    ) as (before, scan):
        if recovery_receipts is None:
            old_files = (
                ()
                if receipt.manifest is None
                else tuple(item.before for item in receipt.manifest.artifacts)
            )
        else:
            # A historical absent stage does not authorize an old open inode.
            # Failed native rebinds are sealed and handled by the existing gate.
            old_files = tuple(item.before for item in capture.registered.values())
        gate.require_bound(_bindings(roots, (*before.files, *old_files)))
        capture.prove_retained_artifacts()
        scan.verify_unchanged()
        if recovery_receipts is None:
            receipt, absent = _retire_previous(store, receipt, capture, scan)
        else:
            retired = {}
            for previous in recovery_receipts:
                retired[previous.operation_id] = _retire_previous(store, previous, capture, scan)
            receipt, absent = retired[receipt.operation_id]
        for file in capture.files.values():
            _unchanged_file(file)
        manifest = _new_manifest(
            receipt.operation_id,
            capture,
            contributors=None if resolved is None else resolved.contributors,
            profile=profile,
        )
        receipt = store.begin_pass(
            receipt.operation_id, expected=receipt, manifest=manifest, artifact_absence=absent
        )
        replaced = set()
        for index, entry in enumerate(manifest.artifacts):
            key = (entry.before.domain, entry.before.key)
            file, path = capture.files[key], _path(roots, entry.before)
            scan.verify_paths()
            staged = _stage(file, entry)
            _prove_stage(
                file.directory,
                entry.stage_name,
                staged,
                file.retained,
                digest=entry.retained_sha256,
            )
            receipt = _checkpoint(store, receipt, index, "staged")
            stream_requests = gate.suspend(path)
            scan.verify_paths()
            _unchanged_file(file)
            if (
                _snapshot(os.stat(entry.stage_name, dir_fd=file.directory, follow_symlinks=False))
                != staged
            ):
                raise GovernanceError("diagnostic_inventory_changed")
            os.replace(
                entry.stage_name, file.name, src_dir_fd=file.directory, dst_dir_fd=file.directory
            )
            os.fsync(file.directory)
            info = os.stat(file.name, dir_fd=file.directory, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != staged[:2]:
                raise GovernanceError("diagnostic_inventory_changed")
            _adopt(gate, path, file, stream_requests, (info.st_dev, info.st_ino))
            receipt = _checkpoint(store, receipt, index, "replaced")
            replaced.add(key)
        # Even an already-clean file may have a sealed handler from a prior attempt.
        for key, file in sorted(capture.files.items()):
            if key not in replaced:
                path = _path(roots, file.observation)
                stream_requests = gate.suspend(path)
                scan.verify_paths()
                _unchanged_file(file)
                _adopt(gate, path, file, stream_requests, file.expected[:2])
        scan.verify_paths()
        with _inspection(**options) as (after, final_scan):
            if {(item.domain, item.key) for item in after.files} != capture.files.keys():
                raise GovernanceError("diagnostic_file_retained_unproven")
            for item in after.files:
                kept = capture.files[(item.domain, item.key)]
                if (
                    item.sha256 != kept.digest
                    or item.byte_length != len(kept.retained)
                    or item.removed_count
                    or item.candidate_count
                ):
                    raise GovernanceError("diagnostic_file_retained_unproven")
            gate.verify_bound(_bindings(roots, after.files))
            for index in range(len(manifest.artifacts)):
                receipt = _checkpoint(store, receipt, index, "verified")
            verification = CleanupVerification(
                after, _absence(final_scan, receipt), profile=profile
            )
            verified = store.verify_pass(receipt, verification=verification)
            if recovery_receipts is None:
                return verified
            results = {verified.operation_id: verified}
            for previous, absent in retired.values():
                if previous.operation_id == verified.operation_id:
                    continue
                # The single physical pass already proved the entire current
                # domain. Each authority keeps its own CAS/absence evidence;
                # no repair grant is written back into its legacy parent.
                empty = CleanupManifest(
                    previous.operation_id, str(uuid4()), (), resolved.contributors, profile
                )
                current = store.begin_pass(
                    previous.operation_id,
                    expected=previous,
                    manifest=empty,
                    artifact_absence=absent,
                )
                results[current.operation_id] = store.verify_pass(
                    current,
                    verification=CleanupVerification(
                        after, _absence(final_scan, current), profile=profile
                    ),
                )
            return tuple(results[key] for key in sorted(results))


def execute_diagnostic_files(
    store,
    operation_id: str,
    *,
    data_root: Path,
    sdk_log_root: Path,
    gate,
    budget: InventoryBudget | None = None,
):
    """Return progress for the actual required domains, never whole-purge completion.

    `gate` is the native synchronous maintenance lease already held by the caller.
    All mutations and reopening must execute on that same thread, without await.
    """
    store.require_capability()
    recovery = store.recovery_snapshot()
    if operation_id not in recovery.committed:
        raise GovernanceError("governance_operation_uncommitted")
    receipt = recovery.snapshot.operations[operation_id].cleanup
    if receipt is None or receipt.orphan_only:
        raise GovernanceError("governance_cleanup_missing")
    budget = InventoryBudget() if budget is None else budget
    if not isinstance(budget, InventoryBudget):
        raise GovernanceError("diagnostic_inventory_budget_invalid")
    resolved = None
    if recovery.required_profile == CORE_PROFILE:
        if recovery.unplanned:
            raise GovernanceError("governance_execution_pending")
        resolved = resolve_cleanup_authority(
            recovery,
            (
                CleanupContributor(
                    operation_id,
                    recovery.committed_digests[operation_id],
                    cleanup_digest(receipt.authority),
                ),
            ),
            owner_operation_id=operation_id,
        )
    try:
        return _execute(
            store,
            receipt,
            data_root=data_root,
            sdk_log_root=sdk_log_root,
            gate=gate,
            budget=budget,
            profile=recovery.required_profile,
            resolved=resolved,
        )
    except OSError:
        raise GovernanceError("diagnostic_file_io_failed") from None


def recover_diagnostic_files(
    store,
    *,
    data_root: Path,
    sdk_log_root: Path,
    gate,
    budget: InventoryBudget | None = None,
):
    """Replay all committed exact grants together; never complete business purges.

    The caller retains the same real service/SDK/diagnostic exclusion required by
    execute_diagnostic_files. Every pass rechecks the actual signed recovery graph,
    all current files and all extant registered artifacts, including verified rows.
    """
    store.require_capability()
    recovery = store.recovery_snapshot()
    graph = build_recovery_graph(recovery)
    if graph.unplanned:
        raise GovernanceError("governance_execution_pending")
    budget = InventoryBudget() if budget is None else budget
    if not isinstance(budget, InventoryBudget):
        raise GovernanceError("diagnostic_inventory_budget_invalid")
    resolved = graph.authority
    if len(resolved.grants) > budget.max_grants:
        raise GovernanceError("diagnostic_inventory_budget_exceeded")
    receipts = tuple(
        recovery.snapshot.operations[item.operation_id].cleanup for item in resolved.contributors
    )
    if not receipts:
        return ()
    try:
        return _execute(
            store,
            receipts[0],
            data_root=data_root,
            sdk_log_root=sdk_log_root,
            gate=gate,
            budget=budget,
            profile=recovery.required_profile,
            recovery_receipts=receipts,
            resolved=resolved,
        )
    except OSError:
        raise GovernanceError("diagnostic_file_io_failed") from None
