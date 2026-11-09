"""ADR-0042: bounded, read-only observations of two explicit diagnostic domains.

Callers establish request selection and writer exclusion. This scan neither
authorizes deletion nor covers Core, Docker, or arbitrary external log copies.
Every absolute path component is opened without following symbolic links.
"""

import errno
import hashlib
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from cloud_memory_hub.governance_diagnostic_records import (
    RecordGrant,
    RecordInspector,
    WriteRequest,
)
from cloud_memory_hub.governance_journal import GovernanceError

_PIPELINE = ("hy-memory", "governed-v2", "logs")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_LIMITS = (256, 32 * 1024 * 1024, 128 * 1024 * 1024, 2 * 1024 * 1024, 100_000, 10_000)


@dataclass(frozen=True, slots=True)
class InventoryBudget:
    max_files: int = _LIMITS[0]
    max_file_bytes: int = _LIMITS[1]
    max_total_bytes: int = _LIMITS[2]
    max_record_bytes: int = _LIMITS[3]
    max_records: int = _LIMITS[4]
    max_grants: int = _LIMITS[5]

    def __post_init__(self):
        values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_record_bytes,
            self.max_records,
            self.max_grants,
        )
        if (
            any(
                type(value) is not int or not 0 <= value <= limit
                for value, limit in zip(values, _LIMITS, strict=True)
            )
            or not self.max_record_bytes
        ):
            raise GovernanceError("diagnostic_inventory_budget_invalid")


@dataclass(frozen=True, slots=True)
class FileObservation:
    domain: Literal["pipeline", "sdk"]
    key: str
    device: int
    inode: int
    byte_length: int
    mtime_ns: int
    ctime_ns: int
    sha256: str
    record_count: int
    kept_count: int
    candidate_count: int
    removed_count: int


@dataclass(frozen=True, slots=True)
class DomainObservation:
    domain: Literal["pipeline", "sdk"]
    path: Path
    exists: bool
    device: int | None = None
    inode: int | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticInventory:
    domains: tuple[DomainObservation, ...]
    files: tuple[FileObservation, ...]
    grants: tuple[RecordGrant, ...]
    byte_length: int
    record_count: int


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)


def _snapshot(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path(value):
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or len(value.parts) > 128
    ):
        raise GovernanceError("diagnostic_inventory_path_unsafe")
    return value


def _valid_date(value):
    if not _DATE.fullmatch(value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


class _Scan:
    def __init__(
        self,
        stack,
        inspector,
        budget,
        *,
        on_record=None,
        on_file=None,
        artifact=None,
        artifact_limit=0,
    ):
        self.stack, self.inspector, self.budget = stack, inspector, budget
        self.on_record, self.on_file, self.artifact = on_record, on_file, artifact
        self.locations = {}
        self.artifact_limit = artifact_limit
        self.links = []
        self.missing = []
        self.directories = []
        self.files = []
        self.grants = set()
        self.bytes = self.records = 0

    def open_directory(self, name, *, parent=None):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=parent)
        self.stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise GovernanceError("diagnostic_inventory_path_unsafe")
        if parent is not None:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(info)[:3] != _identity(linked)[:3]:
                raise GovernanceError("diagnostic_inventory_changed")
            # Ancestors can contain unrelated application directories. Their
            # link counts are not part of either selected diagnostic domain.
            self.links.append((parent, name, _identity(info)[:3]))
        return descriptor

    def open_path(self, path):
        descriptor = self.open_directory("/")
        for component in path.parts[1:]:
            try:
                descriptor = self.open_directory(component, parent=descriptor)
            except FileNotFoundError:
                self.missing.append((descriptor, component))
                return None
        return descriptor

    def entries(self, descriptor):
        result = {}
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(result) >= self.budget.max_files + self.artifact_limit + 1:
                    raise GovernanceError("diagnostic_inventory_budget_exceeded")
                result[entry.name] = _snapshot(entry.stat(follow_symlinks=False))
        return result

    def remember_directory(self, descriptor):
        before = _snapshot(os.fstat(descriptor))
        entries = self.entries(descriptor)
        if _snapshot(os.fstat(descriptor)) != before:
            raise GovernanceError("diagnostic_inventory_changed")
        self.directories.append((descriptor, before, entries))
        return entries

    def scan_domain(self, domain, path, descriptor, *, shared=False):
        if descriptor is None:
            return DomainObservation(domain, path, False)
        info = os.fstat(descriptor)
        if domain == "sdk" or shared:
            self.locations[("sdk", "")] = descriptor
        entries = self.remember_directory(descriptor)
        for name, identity in sorted(entries.items()):
            if (
                (domain == "sdk" or shared)
                and name.startswith(".cmh-cleanup-")
                and self.artifact is not None
                and self.artifact("sdk", descriptor, name, identity, info.st_dev)
            ):
                continue
            if domain == "pipeline" and name == "default":
                if not stat.S_ISDIR(identity[2]) or identity[0] != info.st_dev:
                    raise GovernanceError("diagnostic_inventory_path_unsafe")
                child = self.open_directory(name, parent=descriptor)
                self.locations[("pipeline", "default")] = child
                for filename, file_identity in sorted(self.remember_directory(child).items()):
                    if (
                        filename.startswith(".cmh-cleanup-")
                        and self.artifact is not None
                        and self.artifact("pipeline", child, filename, file_identity, info.st_dev)
                    ):
                        continue
                    if not filename.endswith(".log") or not _valid_date(filename[:-4]):
                        raise GovernanceError("diagnostic_inventory_unsupported")
                    self.scan_file(
                        domain, child, filename, "default/" + filename, file_identity, info.st_dev
                    )
            elif (domain == "sdk" or shared) and (
                name == "hy_memory.log"
                or name.startswith("hy_memory.log.")
                and _valid_date(name[len("hy_memory.log.") :])
            ):
                self.scan_file("sdk", descriptor, name, name, identity, info.st_dev)
            else:
                raise GovernanceError("diagnostic_inventory_unsupported")
        return DomainObservation(domain, path, True, info.st_dev, info.st_ino)

    def scan_file(self, domain, directory, name, key, expected, device):
        if not stat.S_ISREG(expected[2]) or expected[3] != 1 or expected[0] != device:
            raise GovernanceError("diagnostic_inventory_path_unsafe")
        if (
            len(self.files) >= self.budget.max_files
            or expected[4] > self.budget.max_file_bytes
            or self.bytes + expected[4] > self.budget.max_total_bytes
        ):
            raise GovernanceError("diagnostic_inventory_budget_exceeded")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            if _snapshot(before) != expected:
                raise GovernanceError("diagnostic_inventory_changed")
            digest = hashlib.sha256()
            counts = {"keep": 0, "candidate": 0, "remove": 0}
            size = 0
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                while raw := source.readline(self.budget.max_record_bytes + 1):
                    size += len(raw)
                    self.bytes += len(raw)
                    self.records += 1
                    if (
                        size > self.budget.max_file_bytes
                        or self.bytes > self.budget.max_total_bytes
                        or self.records > self.budget.max_records
                        or len(raw) > self.budget.max_record_bytes
                    ):
                        raise GovernanceError("diagnostic_inventory_budget_exceeded")
                    digest.update(raw)
                    decision = self.inspector.inspect(raw, kind=domain)
                    if decision.action == "blocked":
                        raise GovernanceError(decision.reason)
                    if decision.action not in counts:
                        raise GovernanceError("diagnostic_inventory_unavailable")
                    counts[decision.action] += 1
                    if self.on_record is not None:
                        self.on_record(domain, key, raw, decision.action)
                    if decision.grant is not None:
                        self.grants.add(decision.grant)
                        if len(self.grants) > self.budget.max_grants:
                            raise GovernanceError("diagnostic_inventory_budget_exceeded")
            if _snapshot(os.fstat(descriptor)) != expected or size != before.st_size:
                raise GovernanceError("diagnostic_inventory_changed")
            observation = FileObservation(
                domain,
                key,
                before.st_dev,
                before.st_ino,
                size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                digest.hexdigest(),
                sum(counts.values()),
                counts["keep"],
                counts["candidate"],
                counts["remove"],
            )
            self.files.append(observation)
            if self.on_file is not None:
                self.on_file(observation, directory, name, expected)
        finally:
            os.close(descriptor)

    def verify_unchanged(self):
        for descriptor, before, entries in self.directories:
            if (
                _snapshot(os.fstat(descriptor)) != before
                or self.entries(descriptor) != entries
                or _snapshot(os.fstat(descriptor)) != before
            ):
                raise GovernanceError("diagnostic_inventory_changed")
        self.verify_paths()

    def verify_paths(self):
        """Recheck anchored directory identities without rejecting owned replacements."""
        for parent, name, identity in self.links:
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))[:3] != identity:
                raise GovernanceError("diagnostic_inventory_changed")
        for parent, name in self.missing:
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise GovernanceError("diagnostic_inventory_changed")


@contextmanager
def _scan_errors():
    """Translate scan failures without intercepting a session consumer's work."""
    try:
        yield
    except GovernanceError:
        raise
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise GovernanceError("diagnostic_inventory_path_unsafe") from None
        if error.errno == errno.ENOENT:
            raise GovernanceError("diagnostic_inventory_changed") from None
        raise GovernanceError("diagnostic_inventory_unavailable") from None
    except ValueError:
        raise GovernanceError("diagnostic_inventory_unavailable") from None


@contextmanager
def _inspection(
    *,
    data_root: Path,
    sdk_log_root: Path,
    requests: tuple[WriteRequest, ...],
    mode: Literal["propose", "authorized"],
    grants: tuple[RecordGrant, ...] = (),
    budget: InventoryBudget | None = None,
    on_record=None,
    on_file=None,
    artifact=None,
    artifact_limit=0,
):
    """Private shared scan session; callbacks do not weaken the public inventory."""
    data_root, sdk_log_root = _path(data_root), _path(sdk_log_root)
    budget = InventoryBudget() if budget is None else budget
    if not isinstance(budget, InventoryBudget):
        raise GovernanceError("diagnostic_inventory_budget_invalid")
    if type(artifact_limit) is not int or not 0 <= artifact_limit <= budget.max_files:
        raise GovernanceError("diagnostic_inventory_budget_invalid")
    try:
        inspector = RecordInspector(
            requests, mode=mode, grants=grants, max_record_bytes=budget.max_record_bytes
        )
    except (ValueError, TypeError):
        raise GovernanceError("diagnostic_inventory_selection_invalid") from None
    with ExitStack() as stack:
        with _scan_errors():
            scan = _Scan(
                stack,
                inspector,
                budget,
                on_record=on_record,
                on_file=on_file,
                artifact=artifact,
                artifact_limit=artifact_limit,
            )
            pipeline = data_root.joinpath(*_PIPELINE)
            data_fd = scan.open_path(data_root)
            pipeline_fd = data_fd
            if data_fd is not None:
                device = os.fstat(data_fd).st_dev
                for component in _PIPELINE:
                    try:
                        pipeline_fd = scan.open_directory(component, parent=pipeline_fd)
                    except FileNotFoundError:
                        scan.missing.append((pipeline_fd, component))
                        pipeline_fd = None
                        break
                    if os.fstat(pipeline_fd).st_dev != device:
                        raise GovernanceError("diagnostic_inventory_path_unsafe")
            sdk_fd = scan.open_path(sdk_log_root)
            shared = (
                pipeline_fd is not None
                and sdk_fd is not None
                and _identity(os.fstat(pipeline_fd))[:2] == _identity(os.fstat(sdk_fd))[:2]
            )
            first = scan.scan_domain("pipeline", pipeline, pipeline_fd, shared=shared)
            second = (
                DomainObservation("sdk", sdk_log_root, True, first.device, first.inode)
                if shared
                else scan.scan_domain("sdk", sdk_log_root, sdk_fd)
            )
            scan.verify_unchanged()
            result = DiagnosticInventory(
                (first, second),
                tuple(scan.files),
                tuple(sorted(scan.grants)),
                scan.bytes,
                scan.records,
            )
        yield result, scan


def inspect_diagnostic_files(
    *,
    data_root: Path,
    sdk_log_root: Path,
    requests: tuple[WriteRequest, ...],
    mode: Literal["propose", "authorized"],
    grants: tuple[RecordGrant, ...] = (),
    budget: InventoryBudget | None = None,
) -> DiagnosticInventory:
    """Inspect current files only; absent domains do not prove absent global copies."""
    with (
        _scan_errors(),
        _inspection(
            data_root=data_root,
            sdk_log_root=sdk_log_root,
            requests=requests,
            mode=mode,
            grants=grants,
            budget=budget,
        ) as (result, _),
    ):
        return result
