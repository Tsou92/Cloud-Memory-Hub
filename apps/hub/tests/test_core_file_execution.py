"""Actual four-domain file passes reuse signed SDK grants and the Core writer gate."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from cloud_memory_hub import diagnostics
from cloud_memory_hub import governance_diagnostic_files as engine
from cloud_memory_hub.governance_execution_binding import select_execution_store
from cloud_memory_hub.governance_execution_store import manifest_to_dict
from cloud_memory_hub.governance_journal import GovernanceError
from hy_memory.utils import file_log_lifecycle as native
from test_core_records import native as core_native_fixture
from test_diagnostic_file_execution import prepare as prepare_sdk
from test_diagnostic_file_execution import retained
from test_diagnostic_inventory import case as case_fixture
from test_diagnostic_inventory import contents, pipeline
from test_diagnostic_inventory import native_files as native_files_fixture
from test_governance_execution_store import database_rows

case = case_fixture
native_files = native_files_fixture
core_native = core_native_fixture


def prepare(case):
    store, intent, options = prepare_sdk(case)
    # Execution derives requests from signed rows, not the inventory fixture.
    return store, intent, {key: options[key] for key in ("data_root", "sdk_log_root")}


def roots(options):
    return tuple(
        dict.fromkeys(
            (
                options["data_root"] / "hy-memory/governed-v2/logs",
                options["sdk_log_root"],
                options["data_root"] / "logs",
            )
        )
    )


def install_core(options, source):
    directory = options["data_root"] / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for domain, basename in (
        ("core-log", "core.log"),
        ("core-diagnostic", "diagnostic-core.jsonl"),
    ):
        for suffix in ("", ".1", ".2", ".3"):
            path = directory / (basename + suffix)
            path.write_bytes(source[domain])
            result[path] = source[domain]
    return result


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("core_present", [False, True])
def test_actual_required_four_domains_preserve_core_bytes_and_remove_only_sdk_grants(
    case, core_native, batch, core_present
):
    root, _ = case
    store, intent, options = prepare(case)
    expected_sdk = retained(case, options)
    expected_core = install_core(options, core_native["strict"]) if core_present else {}
    original = store.cleanup(intent.operation_id).authority
    store.journal.bind_file_coverage(store.snapshot().binding.store_id)
    with diagnostics.diagnostic_file_maintenance(roots=roots(options)) as gate:
        if batch:
            result = engine.recover_diagnostic_files(store, **options, gate=gate)
            assert len(result) == 1
            receipt = result[0]
        else:
            receipt = engine.execute_diagnostic_files(
                store, intent.operation_id, **options, gate=gate
            )
    assert receipt.state == "verified"
    assert receipt.manifest.profile == receipt.verification.profile == "sdk-core-four-domains-v1"
    assert manifest_to_dict(receipt.manifest)["version"] == 3
    assert receipt.manifest.contributors
    assert receipt.authority == original
    assert all(entry.before.domain in {"pipeline", "sdk"} for entry in receipt.manifest.artifacts)
    assert all((root / key).read_bytes() == value for key, value in expected_sdk.items())
    assert all(path.read_bytes() == value for path, value in expected_core.items())
    # File progress does not mark the actual business deletion complete.
    state = select_execution_store(store.journal).read_state()
    assert state.operations[intent.operation_id].state == "planned"
    assert state.pending == (intent.operation_id,)


def test_unknown_core_body_refuses_before_sdk_replacement_or_progress(case, core_native):
    root, _ = case
    store, intent, options = prepare(case)
    install_core(options, core_native["legacy"])
    store.journal.bind_file_coverage(store.snapshot().binding.store_id)
    before = contents(root), database_rows(store.path), database_rows(store.journal.path)
    with diagnostics.diagnostic_file_maintenance(roots=roots(options)) as gate:
        with pytest.raises(GovernanceError, match="^diagnostic_core_authorization_required$"):
            engine.recover_diagnostic_files(store, **options, gate=gate)
    assert (contents(root), database_rows(store.path), database_rows(store.journal.path)) == before
    assert not tuple(root.rglob(".cmh-cleanup-*.tmp"))


@pytest.mark.parametrize("gate_kind", ["sdk-only", "missing-core-root"])
def test_core_file_pass_requires_the_actual_combined_gate_for_all_roots(case, gate_kind):
    root, _ = case
    store, intent, options = prepare(case)
    store.journal.bind_file_coverage(store.snapshot().binding.store_id)
    before = contents(root), database_rows(store.path)
    factory = (
        native.diagnostic_file_maintenance
        if gate_kind == "sdk-only"
        else diagnostics.diagnostic_file_maintenance
    )
    held = roots(options) if gate_kind == "sdk-only" else (pipeline(root), options["sdk_log_root"])
    with factory(roots=held) as gate:
        with pytest.raises(GovernanceError, match="^diagnostic_core_maintenance_required$"):
            engine.execute_diagnostic_files(store, intent.operation_id, **options, gate=gate)
    assert (contents(root), database_rows(store.path)) == before


def test_combined_lease_is_bound_to_its_thread_lifetime_and_real_roots(tmp_path):
    root = tmp_path.resolve()

    def require(gate):
        with pytest.raises(GovernanceError, match="^diagnostic_core_maintenance_required$"):
            diagnostics.require_diagnostic_file_maintenance(gate, roots=(root,))

    with diagnostics.diagnostic_file_maintenance(roots=(root,)) as gate:
        diagnostics.require_diagnostic_file_maintenance(gate, roots=(root / "logs",))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(require, gate).result(timeout=5)
        with pytest.raises(GovernanceError, match="^diagnostic_core_maintenance_required$"):
            diagnostics.require_diagnostic_file_maintenance(object(), roots=(root,))
    require(gate)
