"""ADR-0043: stopped-service proofs read latest signed state without repairing it."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_authority import GovernanceAuthority, open_authority
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore, NamedResource
from cloud_memory_hub.write_admissions import WriteAdmissions
from test_governance_authority import intent_for
from test_remote_ingest import config_for
from test_write_admissions import enroll, event_for_store, restore, snapshot


@pytest.fixture
def stores(tmp_path):
    config = config_for(tmp_path)
    authority = open_authority(config)
    admissions = WriteAdmissions(authority.access, authority.journal)
    return config, authority, admissions


def database_state(config):
    paths = [config.config_dir / "remote-access.sqlite3", *config.governance_dir.glob("*.sqlite3")]
    result = {}
    for path in sorted(paths):
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            schema = db.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
            tables = [name for kind, name, _ in schema if kind == "table"]
            rows = {
                name: db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall()
                for name in tables
            }
            result[path.name] = (db.execute("PRAGMA user_version").fetchone(), schema, rows)
    return result


@contextmanager
def unchanged(config):
    before = database_state(config)
    paths = [config.config_dir / "remote-access.sqlite3", *config.governance_dir.glob("*.sqlite3")]
    # SQLite sidecar lock/shared-memory I/O is allowed, but committed DB/WAL
    # content must stay unchanged. WAL tests hold writer anchors to prevent GC.
    files = {path: path.read_bytes() for path in paths}
    files.update(
        {
            Path(str(path) + "-wal"): Path(str(path) + "-wal").read_bytes()
            for path in paths
            if Path(str(path) + "-wal").exists()
        }
    )
    try:
        yield
    finally:
        assert database_state(config) == before
        assert {path: path.read_bytes() for path in files} == files


def read_admissions(config):
    authority = open_authority(config, read_only=True)
    return authority, WriteAdmissions(authority.access, authority.journal, read_only=True)


def latest_proof(admissions, binding):
    with admissions.access._connection() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        return admissions.proof_in(db, binding.request_id)


def test_pending_authority_and_admissions_open_without_ready_or_any_repairs(stores, monkeypatch):
    config, authority, admissions = stores
    head = authority.append(intent_for(authority))
    binding = enroll(admissions, event_for_store(admissions))

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only opening invoked service readiness")

    monkeypatch.setattr(GovernanceAuthority, "require_ready", forbidden)
    monkeypatch.setattr(GovernanceAuthority, "require_writes_ready", forbidden)
    with unchanged(config):
        reader, proofs = read_admissions(config)
        assert reader.read_only and reader.access.read_only and reader.journal.read_only
        assert proofs.read_only and proofs.journal.read_only
        assert reader.verify() == {"global": head}
        proofs.verify_all()
        assert latest_proof(proofs, binding).state == "enrolled"
        assert proofs.journal.head(binding.birth_id).sequence == 1


def test_missing_actual_floor_refuses_then_explicit_default_verification_repairs(stores):
    config, authority, _ = stores
    head = authority.journal.append(intent_for(authority))
    with unchanged(config), pytest.raises(GovernanceError, match="^governance_witness_missing$"):
        open_authority(config, read_only=True)
    assert open_authority(config).verify()["global"] == head
    with unchanged(config):
        assert open_authority(config, read_only=True).verify()["global"] == head


def test_unused_zero_scope_needs_no_floor(stores):
    config, authority, _ = stores
    assert authority.journal.head("global").sequence == 0
    with unchanged(config):
        assert open_authority(config, read_only=True).verify() == {}


def test_lagging_floor_returns_latest_without_advancing_external_anchor(stores):
    config, authority, _ = stores
    first = authority.append(intent_for(authority))
    latest = authority.journal.append(intent_for(authority, action="purge"))
    with unchanged(config):
        reader = open_authority(config, read_only=True)
        assert reader.verify()["global"] == latest
        with reader.access._connection() as db:
            saved = json.loads(
                db.execute("SELECT checkpoint FROM cmh_governance_floors").fetchone()[0]
            )
        assert saved["sequence"] == first.sequence
    assert open_authority(config).verify()["global"] == latest


@pytest.mark.parametrize("damage", ["ahead", "fork", "signature"])
def test_readonly_floor_validation_still_rejects_rollback_and_tampering(stores, damage):
    config, authority, _ = stores
    authority.append(intent_for(authority))
    old = snapshot(authority.journal.path)
    try:
        if damage in {"ahead", "fork"}:
            authority.append(intent_for(authority))
            restore(old, authority.journal.path)
            if damage == "fork":
                authority.journal.append(intent_for(authority, action="purge"))
            reason = (
                "governance_watermark_ahead"
                if damage == "ahead"
                else "governance_watermark_diverged"
            )
        else:
            with authority.access._connection(writing=True) as db:
                db.execute("UPDATE cmh_governance_floors SET signature=?", ("0" * 64,))
            reason = "governance_witness_invalid"
        with unchanged(config), pytest.raises(GovernanceError, match="^" + reason + "$"):
            open_authority(config, read_only=True)
    finally:
        old.close()


def test_missing_admission_witness_refuses_then_default_verify_all_repairs(stores):
    config, _, admissions = stores
    binding = enroll(admissions, event_for_store(admissions))
    with admissions.access._connection(writing=True) as db:
        db.execute("DELETE FROM cmh_admission_witnesses")
    with (
        unchanged(config),
        pytest.raises(GovernanceError, match="^governance_admission_witness_missing$"),
    ):
        read_admissions(config)
    admissions.verify_all()
    with unchanged(config):
        _, reader = read_admissions(config)
        assert latest_proof(reader, binding).state == "enrolled"


def test_lagging_admission_witness_returns_possible_and_cannot_silently_refresh(stores):
    config, authority, admissions = stores
    binding = enroll(admissions, event_for_store(admissions))
    admissions.journal.append(admissions._entry(binding, "possible"))
    with unchanged(config):
        _, reader = read_admissions(config)
        assert latest_proof(reader, binding).state == "possible"
        # An explicitly read-only facade also refuses repairs over a borrowed
        # normal writer connection, even with proof_in's compatible True default.
        facade = WriteAdmissions(authority.access, authority.journal, read_only=True)
        with authority.access._connection(writing=True) as db:
            proof = facade.proof_in(db, binding.request_id, refresh_witness=True)
            assert proof.state == "possible" and proof.checkpoint.sequence == 2
            saved = json.loads(
                db.execute("SELECT checkpoint FROM cmh_admission_witnesses").fetchone()[0]
            )
            assert saved["sequence"] == 1


@pytest.mark.parametrize("damage", ["ahead", "fork", "signature", "birth"])
def test_readonly_admission_constructor_validates_every_witness(stores, damage):
    config, _, admissions = stores
    binding = enroll(admissions, event_for_store(admissions))
    old = snapshot(admissions.journal.path)
    try:
        if damage == "ahead":
            admissions.journal.append(admissions._entry(binding, "possible"))
            admissions.verify_all()
            restore(old, admissions.journal.path)
            reason = "governance_admission_watermark_ahead"
        else:
            with admissions.access._connection(writing=True) as db:
                if damage == "birth":
                    db.execute("UPDATE cmh_admission_witnesses SET birth_id=?", (str(uuid4()),))
                elif damage == "signature":
                    db.execute("UPDATE cmh_admission_witnesses SET signature=?", ("0" * 64,))
                else:
                    row = dict(db.execute("SELECT * FROM cmh_admission_witnesses").fetchone())
                    checkpoint = json.loads(row["checkpoint"]) | {"digest": "0" * 64}
                    payload = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
                    signature = admissions.journal._mac(
                        "admission-witness", [row["event_key"], row["birth_id"], payload]
                    )
                    db.execute(
                        "UPDATE cmh_admission_witnesses SET checkpoint=?,signature=?",
                        (payload, signature),
                    )
            reason = "governance_admission_witness_invalid"
        with unchanged(config), pytest.raises(GovernanceError, match="^" + reason + "$"):
            read_admissions(config)
    finally:
        old.close()


def test_readonly_mutators_refuse_before_transactions_or_callbacks(stores, monkeypatch):
    config, _, _ = stores
    reader, admissions = read_admissions(config)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only mutator reached a transaction or callback")

    with unchanged(config):
        monkeypatch.setattr(reader.access, "_connection", forbidden)
        monkeypatch.setattr(reader.journal, "append", forbidden)
        with pytest.raises(GovernanceError, match="^governance_authority_read_only$"):
            reader.append(object())
        with pytest.raises(GovernanceError, match="^governance_authority_read_only$"):
            reader.append(None)
        with pytest.raises(GovernanceError, match="^governance_authority_read_only$"):
            GovernanceAuthority.activate(reader.access, reader.journal)
        with pytest.raises(GovernanceError, match="^governance_admission_read_only$"):
            WriteAdmissions.activate(reader.access, reader.journal)
        with pytest.raises(GovernanceError, match="^governance_admission_read_only$"):
            admissions.enroll_in(None, object(), forbidden, ())
        with pytest.raises(GovernanceError, match="^governance_admission_read_only$"):
            admissions.permit_once(object(), require_allowed=forbidden)


@pytest.mark.parametrize("missing", ["access", "journal", "admissions"])
def test_readonly_open_never_recreates_missing_databases(stores, missing):
    config, authority, admissions = stores
    path = {
        "access": authority.access.path,
        "journal": authority.journal.path,
        "admissions": admissions.journal.path,
    }[missing]
    path.unlink()
    before = {
        p: p.read_bytes()
        for directory in (config.config_dir, config.governance_dir)
        for p in directory.iterdir()
        if p.is_file()
    }
    with pytest.raises((AccessError, GovernanceError)):
        read_admissions(config)
    assert not path.exists()
    assert {p: p.read_bytes() for p in before} == before


def test_readonly_access_never_initializes_schema_and_sql_writes_are_blocked(tmp_path, stores):
    config, authority, _ = stores
    missing = tmp_path / "missing-parent" / "access.sqlite3"
    with pytest.raises(AccessError):
        DeviceAccessStore(missing, read_only=True)
    assert not missing.parent.exists()
    empty = config.config_dir / "empty.sqlite3"
    empty.touch(mode=0o600)
    with pytest.raises(AccessError, match="^access_store_unrecognized$"):
        DeviceAccessStore(empty, read_only=True)
    assert empty.read_bytes() == b""
    with unchanged(config):
        reader = DeviceAccessStore(authority.access.path, read_only=True)
        with pytest.raises(AccessError, match="^access_store_read_only$"):
            with reader._connection(writing=True):
                pytest.fail("write transaction admitted")
        with reader._connection() as db:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("DELETE FROM projects")
            # mode=ro must remain a second barrier even if a caller changes
            # the connection's query_only setting after borrowing it.
            db.execute("PRAGMA query_only=OFF")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                db.execute("DELETE FROM projects")
            assert db.total_changes == 0


def test_readonly_keeps_latest_committed_wal_visible_without_advancing_witnesses(stores):
    config, authority, admissions = stores
    first = authority.append(intent_for(authority))
    binding = enroll(admissions, event_for_store(admissions))
    anchors = []
    try:
        for path in (authority.access.path, authority.journal.path, admissions.journal.path):
            connection = sqlite3.connect(path)
            anchors.append(connection)
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        main_before = authority.journal.path.read_bytes()
        admission_before = admissions.journal.path.read_bytes()
        latest = authority.journal.append(intent_for(authority, action="purge"))
        admissions.journal.append(admissions._entry(binding, "possible"))
        project = authority.access.create_project(
            NamedResource(display_name="Synthetic WAL project")
        )
        assert latest.sequence == first.sequence + 1
        assert authority.journal.path.read_bytes() == main_before
        assert admissions.journal.path.read_bytes() == admission_before
        assert Path(str(authority.journal.path) + "-wal").stat().st_size > 0
        assert Path(str(admissions.journal.path) + "-wal").stat().st_size > 0
        with unchanged(config):
            reader, proofs = read_admissions(config)
            assert reader.verify()["global"] == latest
            assert project in reader.access.list_projects()
            proof = latest_proof(proofs, binding)
            assert proof.state == "possible" and proof.checkpoint.sequence == 2
    finally:
        for connection in anchors:
            connection.close()
