"""FR-10/FR-11: real native records, bounded domains, no file mutation."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub import governance_diagnostic_inventory as inventory
from cloud_memory_hub.governance_diagnostic_records import WriteRequest
from cloud_memory_hub.governance_journal import GovernanceError

PROBE = Path(__file__).resolve().parents[3] / "scripts/probe_diagnostic_inventory.py"


@pytest.fixture(scope="module")
def native_files(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-diagnostic-inventory").resolve()
    instance, scope = str(uuid4()), str(uuid4())
    bindings = [
        {
            "request_id": "cmh2:" + ":".join((instance, *(str(uuid4()) for _ in range(3)))),
            "user_id": f"cmh:{instance}:{scope}",
        }
        for _ in range(2)
    ]
    (root / "bindings.json").write_text(json.dumps(bindings))
    result = subprocess.run(
        [sys.executable, str(PROBE), "--phase", "emit", "--root", str(root)],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    return root / "files", tuple(WriteRequest(**item) for item in bindings)


@pytest.fixture
def case(native_files, tmp_path):
    source, requests = native_files
    root = tmp_path.resolve() / "files"
    shutil.copytree(source, root)
    return root, requests


@pytest.fixture(scope="module")
def native_shared_files(native_files, tmp_path_factory):
    _, requests = native_files
    root = tmp_path_factory.mktemp("native-shared-diagnostic-inventory").resolve()
    (root / "bindings.json").write_text(
        json.dumps([{"request_id": item.request_id, "user_id": item.user_id} for item in requests])
    )
    result = subprocess.run(
        [sys.executable, str(PROBE), "--phase", "emit-shared", "--root", str(root)],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    return root / "files", requests


@pytest.fixture
def shared_case(native_shared_files, tmp_path):
    source, requests = native_shared_files
    root = tmp_path.resolve() / "files"
    shutil.copytree(source, root)
    return root, requests


def inspect_shared(case, **kwargs):
    root, requests = case
    return inventory.inspect_diagnostic_files(
        data_root=root / "data",
        sdk_log_root=pipeline(root),
        requests=requests,
        mode=kwargs.pop("mode", "propose"),
        **kwargs,
    )


def inspect(case, **kwargs):
    root, requests = case
    return inventory.inspect_diagnostic_files(
        data_root=root / "data",
        sdk_log_root=root / "sdk",
        requests=kwargs.pop("requests", requests),
        mode=kwargs.pop("mode", "propose"),
        **kwargs,
    )


def contents(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def pipeline(root):
    return root / "data/hy-memory/governed-v2/logs"


def test_native_legacy_and_strict_success_is_bounded_frozen_and_read_only(case):
    root, _ = case
    # Ordinary Hub state is outside the two explicitly selected log domains.
    (root / "data/main.sqlite3").write_bytes(b"unrelated synthetic business state")
    before = contents(root)
    proposed = inspect(case)
    assert len(proposed.files) == 2 and len(proposed.grants) == 6
    assert proposed.record_count == 8
    assert sum(item.candidate_count for item in proposed.files) == 6
    assert sum(item.kept_count for item in proposed.files) == 2
    assert proposed.grants == tuple(sorted(set(proposed.grants)))
    assert "Synthetic diagnostic inventory original" not in repr(proposed)
    for item in proposed.files:
        directory = pipeline(root) if item.domain == "pipeline" else root / "sdk"
        raw = (directory / item.key).read_bytes()
        assert item.sha256 == hashlib.sha256(raw).hexdigest()
        assert item.byte_length == len(raw)
    with pytest.raises(FrozenInstanceError):
        proposed.record_count = 0
    with pytest.raises(FrozenInstanceError):
        proposed.files[0].byte_length = 0
    authorized = inspect(case, mode="authorized", grants=proposed.grants)
    assert authorized.grants == proposed.grants
    assert sum(item.removed_count for item in authorized.files) == 6
    assert sum(item.candidate_count for item in authorized.files) == 0
    assert contents(root) == before  # Decisions do not execute deletion.


def test_rotated_duplicate_records_share_exact_grants(case):
    root, _ = case
    proposed = inspect(case)
    shutil.copyfile(root / "sdk/hy_memory.log", root / "sdk/hy_memory.log.2024-02-29")
    before = contents(root)
    result = inspect(case, mode="authorized", grants=proposed.grants)
    assert result.grants == proposed.grants and len(result.files) == 3
    assert sum(item.removed_count for item in result.files) == 10
    assert contents(root) == before


def test_real_native_shared_root_is_scanned_once_with_two_domain_labels(shared_case):
    root, _ = shared_case
    before = contents(root)
    proposed = inspect_shared(shared_case)
    assert len(proposed.files) == 2 and proposed.record_count == 8
    assert proposed.byte_length == sum(len(raw) for raw in before.values())
    assert len(proposed.grants) == 6
    assert {item.domain for item in proposed.domains} == {"pipeline", "sdk"}
    assert proposed.domains[0].inode == proposed.domains[1].inode
    assert proposed.domains[0].path == proposed.domains[1].path
    authorized = inspect_shared(shared_case, mode="authorized", grants=proposed.grants)
    assert authorized.grants == proposed.grants
    assert sum(item.removed_count for item in authorized.files) == 6
    assert contents(root) == before


def test_shared_root_unknown_files_are_not_hidden_by_domain_merging(shared_case):
    root, _ = shared_case
    (pipeline(root) / "core.log").write_bytes(b"unknown synthetic content")
    before = contents(root)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_unsupported"):
        inspect_shared(shared_case)
    assert contents(root) == before


def test_shared_root_new_entry_during_scan_is_changed(shared_case, monkeypatch):
    root, _ = shared_case
    original = inventory.RecordInspector.inspect
    changed = False

    def mutate(self, raw, *, kind):
        nonlocal changed
        if not changed:
            changed = True
            (pipeline(root) / "hy_memory.log.2024-02-29").touch()
        return original(self, raw, kind=kind)

    monkeypatch.setattr(inventory.RecordInspector, "inspect", mutate)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_changed"):
        inspect_shared(shared_case)


@pytest.mark.parametrize("kind", ["unselected", "missing-grant", "changed-record"])
def test_uncovered_records_never_return_partial_success(case, kind):
    root, requests = case
    proposed = inspect(case)
    args = {}
    reason = "coverage_unproven"
    if kind == "unselected":
        args["requests"] = requests[:1]
    else:
        args.update(mode="authorized", grants=proposed.grants)
        reason = "grant_missing"
        if kind == "missing-grant":
            args["grants"] = proposed.grants[:-1]
        else:
            path = root / "sdk/hy_memory.log"
            path.write_bytes(path.read_bytes().replace(b"Synthetic", b"Different", 1))
    before = contents(root)
    with pytest.raises(GovernanceError, match=reason):
        inspect(case, **args)
    assert contents(root) == before


@pytest.mark.parametrize(
    ("domain", "name", "directory"),
    [
        ("pipeline", "unknown", False),
        ("pipeline", "default/nested", True),
        ("pipeline", "default/2026-02-30.log", False),
        ("pipeline", "default/2026-01-01.log.gz", False),
        ("sdk", "core.log", False),
        ("sdk", "hy_memory.log.1", False),
        ("sdk", "hy_memory.log.2025-02-29", False),
        ("sdk", "unexpected", True),
    ],
)
def test_unknown_paths_and_invalid_calendar_dates_are_not_ignored(case, domain, name, directory):
    root, _ = case
    path = (pipeline(root) if domain == "pipeline" else root / "sdk") / name
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b"synthetic unknown bytes")
    before = contents(root)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_unsupported"):
        inspect(case)
    assert contents(root) == before


@pytest.mark.parametrize("kind", ["file-link", "default-link", "ancestor-link", "hardlink", "fifo"])
def test_links_and_special_files_are_rejected_without_following(case, kind):
    root, requests = case
    outside = root.parent / "outside"
    outside.mkdir()
    protected = outside / "protected"
    protected.write_bytes(b"private synthetic outside content")
    options = {}
    if kind in {"file-link", "hardlink", "fifo"}:
        path = root / "sdk/hy_memory.log"
        path.unlink()
        if kind == "file-link":
            path.symlink_to(protected)
        elif kind == "hardlink":
            os.link(protected, path)
        else:
            os.mkfifo(path)
    elif kind == "default-link":
        shutil.rmtree(pipeline(root) / "default")
        (pipeline(root) / "default").symlink_to(outside, target_is_directory=True)
    else:
        alias = root.parent / "alias"
        alias.symlink_to(root, target_is_directory=True)
        options["data_root"] = alias / "data"
    with pytest.raises(GovernanceError, match="diagnostic_inventory_path_unsafe"):
        inventory.inspect_diagnostic_files(
            data_root=options.get("data_root", root / "data"),
            sdk_log_root=root / "sdk",
            requests=requests,
            mode="propose",
        )
    assert protected.read_bytes() == b"private synthetic outside content"


def test_foreign_device_entry_metadata_is_rejected_before_file_open(case, monkeypatch):
    # A deterministic stat contract test; it does not mount a foreign device.
    original = inventory.os.scandir

    class Entry:
        def __init__(self, entry):
            self.entry, self.name = entry, entry.name

        def stat(self, **kwargs):
            info = self.entry.stat(**kwargs)
            if self.name != "hy_memory.log":
                return info
            values = {
                name: getattr(info, name)
                for name in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            }
            values["st_dev"] += 1
            return SimpleNamespace(**values)

    class Entries:
        def __init__(self, descriptor):
            self.entries = original(descriptor)

        def __enter__(self):
            return (Entry(entry) for entry in self.entries)

        def __exit__(self, *args):
            self.entries.close()

    monkeypatch.setattr(inventory.os, "scandir", Entries)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_path_unsafe"):
        inspect(case)


def test_early_rejection_closes_every_opened_descriptor(case, monkeypatch):
    original_open, original_close = inventory.os.open, inventory.os.close
    opened = set()

    def track_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def track_close(descriptor):
        opened.discard(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(inventory.os, "open", track_open)
    monkeypatch.setattr(inventory.os, "close", track_close)
    with pytest.raises(GovernanceError, match="coverage_unproven"):
        inspect(case, requests=case[1][:1])
    assert not opened


@pytest.mark.parametrize("path", [Path("relative"), Path("/tmp/../unsafe")])
def test_unanchored_or_parent_traversal_inputs_are_rejected(path):
    with pytest.raises(GovernanceError, match="diagnostic_inventory_path_unsafe"):
        inventory.inspect_diagnostic_files(
            data_root=path, sdk_log_root=path, requests=(), mode="propose"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"max_files": 1},
        {"max_file_bytes": 1},
        {"max_total_bytes": 1},
        {"max_record_bytes": 32},
        {"max_records": 7},
        {"max_grants": 5},
    ],
)
def test_each_independent_budget_is_enforced_without_changes(case, change):
    before = contents(case[0])
    with pytest.raises(GovernanceError, match="diagnostic_inventory_budget_exceeded"):
        inspect(case, budget=replace(inventory.InventoryBudget(), **change))
    assert contents(case[0]) == before


def test_unknown_directory_enumeration_is_bounded(case):
    root, _ = case
    for index in range(4):
        (root / "sdk" / f"unknown-{index}").mkdir()
    with pytest.raises(GovernanceError, match="diagnostic_inventory_budget_exceeded"):
        inspect(case, budget=inventory.InventoryBudget(max_files=2))


def test_empty_and_absent_domains_are_explicit_and_never_created(tmp_path):
    root = tmp_path.resolve()
    args = {
        "data_root": root / "data",
        "sdk_log_root": root / "sdk",
        "requests": (),
        "mode": "propose",
    }
    absent = inventory.inspect_diagnostic_files(**args)
    assert len(absent.domains) == 2 and not any(item.exists for item in absent.domains)
    assert absent.domains[0].path == root / "data/hy-memory/governed-v2/logs"
    assert not (root / "data").exists() and not (root / "sdk").exists()
    (pipeline(root) / "default").mkdir(parents=True)
    (root / "sdk").mkdir()
    assert not inventory.inspect_diagnostic_files(**args).files
    (pipeline(root) / "default/2026-01-01.log").touch()
    (root / "sdk/hy_memory.log").touch()
    empty = inventory.inspect_diagnostic_files(**args)
    assert all(item.exists for item in empty.domains)
    assert len(empty.files) == 2 and empty.byte_length == empty.record_count == 0
    assert empty.grants == ()


@pytest.mark.parametrize("change", ["append", "replace", "same-size", "new-entry", "rename-domain"])
def test_changes_during_scan_are_not_a_closed_inventory(case, monkeypatch, change):
    root, _ = case
    path = next((pipeline(root) / "default").glob("*.log"))
    original = inventory.RecordInspector.inspect
    changed = False

    def mutate(self, raw, *, kind):
        nonlocal changed
        decision = original(self, raw, kind=kind)
        if not changed:
            changed = True
            if change == "append":
                strict = path.read_bytes().rsplit(b"\n", 2)[1] + b"\n"
                with path.open("ab") as stream:
                    stream.write(strict)
            elif change == "replace":
                replacement = path.with_name("temporary")
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)
            elif change == "same-size":
                with path.open("r+b") as stream:
                    stream.write(b" ")
            elif change == "new-entry":
                (pipeline(root) / "default/2024-02-29.log").touch()
            else:
                pipeline(root).rename(pipeline(root).with_name("moved"))
        return decision

    monkeypatch.setattr(inventory.RecordInspector, "inspect", mutate)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_changed"):
        inspect(case)


def test_missing_directory_appearing_before_return_is_changed(case, monkeypatch):
    root, _ = case
    shutil.rmtree(root / "sdk")
    original = inventory._Scan.verify_unchanged

    def create_missing(self):
        (root / "sdk").mkdir()
        original(self)

    monkeypatch.setattr(inventory._Scan, "verify_unchanged", create_missing)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_changed"):
        inspect(case)


def test_unrelated_ancestor_directory_activity_does_not_invalidate_domains(case, monkeypatch):
    root, _ = case
    original = inventory.RecordInspector.inspect
    changed = False

    def create_unrelated(self, raw, *, kind):
        nonlocal changed
        if not changed:
            changed = True
            (root / "data/unrelated-business-directory").mkdir()
            (root / "unrelated-root-directory").mkdir()
        return original(self, raw, kind=kind)

    monkeypatch.setattr(inventory.RecordInspector, "inspect", create_unrelated)
    assert len(inspect(case).grants) == 6


def test_ancestor_link_count_change_between_open_and_link_stat_is_harmless(case, monkeypatch):
    root, _ = case
    original = inventory.os.stat
    changed = False

    def add_directory(name, *args, **kwargs):
        nonlocal changed
        if name == "data" and kwargs.get("dir_fd") is not None and not changed:
            changed = True
            (root / "data/new-unrelated-directory").mkdir()
        return original(name, *args, **kwargs)

    monkeypatch.setattr(inventory.os, "stat", add_directory)
    assert len(inspect(case).grants) == 6
    assert changed
