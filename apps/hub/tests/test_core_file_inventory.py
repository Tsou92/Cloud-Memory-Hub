"""ADR-0047: native four-domain inventory, no new Core deletion permission."""

import hashlib
import os
import shutil
from collections import Counter
from dataclasses import replace

import pytest
from cloud_memory_hub import governance_diagnostic_files as engine
from cloud_memory_hub import governance_diagnostic_inventory as inventory
from cloud_memory_hub.governance_diagnostic_profiles import CORE_PROFILE, SDK_PROFILE
from cloud_memory_hub.governance_journal import GovernanceError
from test_core_records import emit_native
from test_diagnostic_inventory import contents
from test_diagnostic_inventory import native_files as native_sdk_fixture
from test_diagnostic_recovery_files import (
    native_recovery_files as native_recovery_fixture,
)
from test_diagnostic_recovery_files import (
    prepare,
    recover,
    restored_stages,
)


@pytest.fixture(scope="module")
def native_files(tmp_path_factory):
    return native_sdk_fixture.__wrapped__(tmp_path_factory)


@pytest.fixture(scope="module")
def native_recovery_files(tmp_path_factory):
    return native_recovery_fixture.__wrapped__(tmp_path_factory)


@pytest.fixture(scope="module")
def core_images(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-core-file-inventory")
    return {mode: emit_native(root / mode, mode) for mode in ("strict", "legacy", "large")}


@pytest.fixture
def case(tmp_path, core_images):
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    for domain, name in (("core-log", "core.log"), ("core-diagnostic", "diagnostic-core.jsonl")):
        (logs / name).write_bytes(core_images["strict"][domain])
    return dict(data_root=data, sdk_log_root=tmp_path / "sdk", requests=(), mode="propose")


def inspect(case, **kwargs):
    return inventory.inspect_diagnostic_files(**(case | {"profile": CORE_PROFILE} | kwargs))


def inspect_callbacks(case, **kwargs):
    captured = []
    with inventory._inspection(
        **(case | {"profile": CORE_PROFILE} | kwargs),
        on_record=lambda domain, key, raw, action: captured.append((domain, key, raw, action)),
    ) as (result, scan):
        scan.verify_unchanged()
        locations = dict(scan.locations)
        directories = len(scan.directories)
    return result, captured, locations, directories


def test_native_strict_files_keep_exact_bytes_with_four_explicit_domains(case):
    before = contents(case["data_root"])
    result, records, locations, directories = inspect_callbacks(case, mode="authorized")
    assert result.profile == CORE_PROFILE
    assert tuple(item.domain for item in result.domains) == (
        "core-diagnostic",
        "core-log",
        "pipeline",
        "sdk",
    )
    assert [item.exists for item in result.domains] == [True, True, False, False]
    assert result.grants == () and len(result.files) == 2
    assert locations[("core-log", "")] == locations[("core-diagnostic", "")]
    assert directories == 1
    assert result.byte_length == sum(map(len, before.values()))
    for item in result.files:
        raw = before["logs/" + item.key]
        actual = [entry for entry in records if entry[:2] == (item.domain, item.key)]
        assert b"".join(entry[2] for entry in actual) == raw
        assert all(entry[3] == "keep" for entry in actual)
        assert item.sha256 == hashlib.sha256(raw).hexdigest()
        assert item.byte_length == len(raw) and item.candidate_count == item.removed_count == 0
    assert contents(case["data_root"]) == before


@pytest.mark.parametrize(
    "domain,name", [("core-log", "core.log"), ("core-diagnostic", "diagnostic-core.jsonl")]
)
def test_native_legacy_is_raw_candidate_but_never_authorized(case, core_images, domain, name):
    path = case["data_root"] / "logs" / name
    path.write_bytes(core_images["legacy"][domain])
    before = contents(case["data_root"])
    result, records, _, _ = inspect_callbacks(case)
    raw = [item for item in records if item[0] == domain]
    assert raw == [(domain, name, path.read_bytes(), "candidate")]
    assert result.grants == ()
    with pytest.raises(GovernanceError, match="^diagnostic_core_authorization_required$"):
        inspect(case, mode="authorized")
    assert contents(case["data_root"]) == before


def test_native_large_message_has_independent_block_budget(case, core_images):
    raw = core_images["large"]["core-log"]
    (case["data_root"] / "logs/core.log").write_bytes(raw)
    result, records, _, _ = inspect_callbacks(case)
    assert len(raw) > 4 * 1024 * 1024
    assert any(item == ("core-log", "core.log", raw, "candidate") for item in records)
    assert any(item.byte_length == len(raw) for item in result.files)
    before = contents(case["data_root"])
    with pytest.raises(GovernanceError, match="diagnostic_inventory_budget_exceeded"):
        inspect(case, budget=inventory.InventoryBudget(max_core_block_bytes=len(raw) - 1))
    assert contents(case["data_root"]) == before


@pytest.mark.parametrize("tail", [b"\xff\x00\r\n", b"no final newline", b"\xe2\x80\xa8"])
def test_raw_eof_and_invalid_unicode_are_never_lost_or_approved(case, tail):
    path = case["data_root"] / "logs/core.log"
    before = path.read_bytes()
    path.write_bytes(before + tail)
    _, records, _, _ = inspect_callbacks(case)
    selected = [item for item in records if item[0] == "core-log"]
    assert b"".join(item[2] for item in selected) == before + tail
    assert selected[-1][2:] == (tail, "candidate")
    with pytest.raises(GovernanceError, match="diagnostic_core_authorization_required"):
        inspect(case, mode="authorized")


def test_known_rotation_families_are_exact_copies_and_counted_once(case):
    logs = case["data_root"] / "logs"
    # Exact copies of actual native output model restored rotated files.
    for name in ("core.log", "diagnostic-core.jsonl"):
        for index in range(1, 4):
            shutil.copyfile(logs / name, logs / f"{name}.{index}")
    before = contents(case["data_root"])
    result = inspect(case, mode="authorized", budget=inventory.InventoryBudget(max_files=8))
    assert len(result.files) == 8 and result.byte_length == sum(map(len, before.values()))
    assert Counter(item.domain for item in result.files) == {"core-log": 4, "core-diagnostic": 4}
    assert contents(case["data_root"]) == before


@pytest.mark.parametrize(
    "name",
    [
        "core.log.0",
        "core.log.4",
        "core.log.01",
        "core.log.gz",
        "diagnostic-core.jsonl.4",
        "diagnostic-other.jsonl",
        "unknown",
        "subdirectory",
    ],
)
def test_unknown_core_entries_never_disappear_behind_domain_aliases(case, name):
    path = case["data_root"] / "logs" / name
    path.mkdir() if name == "subdirectory" else path.write_bytes(b"synthetic unknown")
    before = contents(case["data_root"])
    with pytest.raises(GovernanceError, match="diagnostic_inventory_unsupported"):
        inspect(case)
    assert contents(case["data_root"]) == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "fifo", "root-link"])
def test_core_links_and_special_files_are_rejected(case, tmp_path, damage):
    logs = case["data_root"] / "logs"
    protected = tmp_path / "protected"
    protected.write_bytes(b"synthetic outside data")
    if damage == "root-link":
        moved = tmp_path / "actual-logs"
        logs.rename(moved)
        logs.symlink_to(moved, target_is_directory=True)
    else:
        path = logs / "core.log"
        path.unlink()
        if damage == "symlink":
            path.symlink_to(protected)
        elif damage == "hardlink":
            os.link(protected, path)
        else:
            os.mkfifo(path)
    with pytest.raises(GovernanceError, match="diagnostic_inventory_path_unsafe"):
        inspect(case)
    assert protected.read_bytes() == b"synthetic outside data"


@pytest.mark.parametrize(
    "change",
    [dict(max_files=1), dict(max_total_bytes=1), dict(max_file_bytes=0), dict(max_records=1)],
)
def test_four_domains_share_each_global_budget(case, change):
    before = contents(case["data_root"])
    with pytest.raises(GovernanceError, match="diagnostic_inventory_budget_exceeded"):
        inspect(case, budget=replace(inventory.InventoryBudget(), **change))
    assert contents(case["data_root"]) == before


def test_empty_files_do_not_turn_zero_file_budget_into_invalid_core_framer(case):
    for name in ("core.log", "diagnostic-core.jsonl"):
        (case["data_root"] / "logs" / name).write_bytes(b"")
    result = inspect(case, budget=inventory.InventoryBudget(max_file_bytes=0))
    assert len(result.files) == 2 and result.byte_length == result.record_count == 0
    assert inventory.InventoryBudget(max_record_bytes=1).max_core_block_bytes == 32 * 1024 * 1024


def test_default_sdk_profile_stays_exactly_two_domains_and_ignores_core_root(case):
    (case["data_root"] / "logs/unrecognized").write_bytes(b"synthetic outside SDK contract")
    result = inventory.inspect_diagnostic_files(**case)
    assert result.profile == SDK_PROFILE and tuple(item.domain for item in result.domains) == (
        "pipeline",
        "sdk",
    )
    assert result.files == result.grants == ()
    with pytest.raises(GovernanceError, match="diagnostic_inventory_unsupported"):
        inspect(case)


def test_absent_four_domains_are_observed_without_creation(tmp_path):
    args = dict(
        data_root=tmp_path / "missing",
        sdk_log_root=tmp_path / "sdk",
        requests=(),
        mode="authorized",
    )
    result = inspect(args)
    assert len(result.domains) == 4 and not any(item.exists for item in result.domains)
    assert result.files == () and not args["data_root"].exists()


def test_alias_sdk_and_core_roots_apply_union_whitelist_once(case, native_files):
    source, requests = native_files
    logs = case["data_root"] / "logs"
    shutil.copyfile(source / "sdk/hy_memory.log", logs / "hy_memory.log")
    options = case | {"sdk_log_root": logs, "requests": requests}
    before = contents(case["data_root"])
    result, _, locations, directories = inspect_callbacks(options)
    assert len(result.files) == 3 and directories == 1
    assert len({locations[(domain, "")] for domain in ("sdk", "core-log", "core-diagnostic")}) == 1
    assert result.byte_length == sum(map(len, before.values()))
    assert len(result.grants) == 4
    with pytest.raises(GovernanceError, match="diagnostic_inventory_unsupported"):
        inventory.inspect_diagnostic_files(**options)
    (logs / "unknown").touch()
    with pytest.raises(GovernanceError, match="diagnostic_inventory_unsupported"):
        inspect(options)


@pytest.mark.parametrize("damage", ["new-entry", "changed-file", "replaced-root"])
def test_core_changes_during_actual_callbacks_abort_full_snapshot(case, damage):
    logs = case["data_root"] / "logs"
    changed = False

    def mutate(domain, key, raw, action):
        nonlocal changed
        if changed:
            return
        changed = True
        if damage == "new-entry":
            (logs / "core.log.1").touch()
        elif damage == "changed-file":
            with (logs / key).open("ab") as target:
                target.write(b"synthetic append\n")
        else:
            logs.rename(logs.with_name("old-logs"))
            logs.mkdir()

    with pytest.raises(GovernanceError, match="diagnostic_inventory_changed"):
        with inventory._inspection(**case, profile=CORE_PROFILE, on_record=mutate):
            pass


def test_registered_sdk_stage_in_core_shared_root_is_routed_exactly_once(
    native_recovery_files, tmp_path, core_images
):
    source, requests = native_recovery_files
    root = tmp_path / "files"
    shutil.copytree(source, root)
    family = prepare((root, requests))
    receipts = recover(family)
    restored = restored_stages(family, receipts)
    logs = family.options["data_root"] / "logs"
    logs.mkdir()
    old_sdk = family.options["sdk_log_root"]
    for path in old_sdk.iterdir():
        if path.is_file():
            path.rename(logs / path.name)
    for domain, name in (("core-log", "core.log"), ("core-diagnostic", "diagnostic-core.jsonl")):
        (logs / name).write_bytes(core_images["strict"][domain])
    before = contents(root), family.store.recovery_snapshot()
    evidence = engine._RecoveryCapture(receipts, inventory.InventoryBudget())
    matched = []

    def artifact(domain, *args):
        result = evidence.artifact(domain, *args)
        if result:
            matched.append((domain, args[1]))
        return result

    with inventory._inspection(
        data_root=family.options["data_root"],
        sdk_log_root=logs,
        requests=requests,
        mode="authorized",
        profile=CORE_PROFILE,
        on_record=evidence.record,
        on_file=evidence.file,
        artifact=artifact,
        artifact_limit=4,
    ) as (result, scan):
        evidence.prove_retained_artifacts()
        scan.verify_unchanged()
        assert scan.locations[("sdk", "")] == scan.locations[("core-log", "")]
    assert len(matched) == len(restored) and len(set(matched)) == len(matched)
    assert {domain for domain, _ in matched} == {"pipeline", "sdk"}
    assert len(result.files) == 4
    assert (contents(root), family.store.recovery_snapshot()) == before
    # A violating callback claiming multiple colocated domains is rejected,
    # even though the actual bytes and signed registration really exist.
    with pytest.raises(GovernanceError, match="diagnostic_file_artifact_unproven"):
        with inventory._inspection(
            data_root=family.options["data_root"],
            sdk_log_root=logs,
            requests=requests,
            mode="authorized",
            profile=CORE_PROFILE,
            artifact=lambda domain, *_: domain in {"sdk", "core-log", "pipeline"},
            artifact_limit=4,
        ):
            pass
    assert (contents(root), family.store.recovery_snapshot()) == before


@pytest.mark.parametrize("value", [0, -1, True, 32 * 1024 * 1024 + 1])
def test_core_budget_is_positive_bounded_and_separate(value):
    with pytest.raises(GovernanceError, match="diagnostic_inventory_budget_invalid"):
        inventory.InventoryBudget(max_core_block_bytes=value)


def test_unknown_profile_is_rejected_before_opening_any_path(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid profile opened a path")

    monkeypatch.setattr(inventory.os, "open", forbidden)
    with pytest.raises(GovernanceError, match="governance_cleanup_profile_invalid"):
        inspect(
            dict(data_root=tmp_path, sdk_log_root=tmp_path, requests=(), mode="propose"),
            profile="unknown",
        )
