"""ADR-0043: cold maintenance reads do not initialize or enqueue ingestion."""

import sqlite3
from pathlib import Path

import pytest
from cloud_memory_hub.original_archive import ARCHIVE_VERSION
from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore, NamedResource
from cloud_memory_hub.remote_ingest import RemoteIngest, SourceRegistration
from test_readonly_governance_authority import unchanged
from test_remote_ingest import config_for


def forbidden(*args, **kwargs):
    raise AssertionError("read-only ingestion called a writer or callback")


def open_ingest(config, *, read_only):
    access = DeviceAccessStore(config.config_dir / "remote-access.sqlite3", read_only=read_only)
    return RemoteIngest(access, forbidden, read_only=read_only)


def seed_source(ingest):
    project = ingest.access.create_project(NamedResource(display_name="Synthetic project"))
    issued = ingest.access.create_device(NamedResource(display_name="Synthetic device"))
    return ingest.create_source(
        SourceRegistration(
            display_name="Synthetic source",
            adapter_id="synthetic-cli",
            device_id=issued.device["device_id"],
            scope_id=project["project_id"],
        )
    )


def test_cold_reader_preserves_existing_schema_and_rows_without_queue(tmp_path):
    config = config_for(tmp_path)
    source = seed_source(open_ingest(config, read_only=False))
    with unchanged(config):
        reader = open_ingest(config, read_only=True)
        assert reader.read_only and reader.access.read_only
        assert reader.list_sources() == [source]
        with reader._database() as db:
            assert db.execute("PRAGMA query_only").fetchone()[0] == 1
            assert db.execute("SELECT version FROM cmh_ingest_schema").fetchone()[0] == (
                ARCHIVE_VERSION
            )


def test_missing_extension_refuses_without_initializing_any_ingest_tables(tmp_path):
    config = config_for(tmp_path)
    with unchanged(config), pytest.raises(AccessError, match="^ingest_store_missing$"):
        open_ingest(config, read_only=True)
    # Explicit compatible writer construction remains the initialization path.
    open_ingest(config, read_only=False)
    with unchanged(config):
        assert open_ingest(config, read_only=True).list_sources() == []


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("legacy", "original_retention_migration_required"),
        ("future", "ingest_store_version_unsupported"),
        ("missing_version", "ingest_store_version_unsupported"),
        ("missing_table", "access_store_unavailable"),
        ("missing_column", "access_store_unavailable"),
    ],
)
def test_cold_reader_reuses_schema_checks_without_repair_or_migration(tmp_path, damage, reason):
    config = config_for(tmp_path)
    ingest = open_ingest(config, read_only=False)
    with ingest._database(writing=True) as db:
        if damage in {"legacy", "future"}:
            db.execute(
                "UPDATE cmh_ingest_schema SET version=?",
                (1 if damage == "legacy" else ARCHIVE_VERSION + 1,),
            )
        elif damage == "missing_version":
            db.execute("DELETE FROM cmh_ingest_schema")
        elif damage == "missing_table":
            db.execute("DROP TABLE cmh_sources")
        else:
            db.execute("ALTER TABLE cmh_sources RENAME COLUMN adapter_id TO invalid_column")
    with unchanged(config), pytest.raises(AccessError, match="^" + reason + "$"):
        open_ingest(config, read_only=True)


def test_readonly_mode_requires_readonly_access_before_any_open(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    access = DeviceAccessStore(config.config_dir / "remote-access.sqlite3")
    monkeypatch.setattr(access, "_connection", forbidden)
    with unchanged(config), pytest.raises(AccessError, match="^ingest_read_only_access_required$"):
        RemoteIngest(access, forbidden, read_only=True)


def test_all_ingestion_writers_refuse_before_input_render_or_callbacks(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    open_ingest(config, read_only=False)
    reader = open_ingest(config, read_only=True)
    reader.enroll_request = forbidden
    reader.require_source_allowed = forbidden
    reader.require_source_rules = forbidden
    reader.suppressed = forbidden
    monkeypatch.setattr(reader.access, "_connection", forbidden)
    with unchanged(config):
        for invoke in (
            lambda: reader.accept(object(), object()),
            lambda: reader.create_source(object()),
            lambda: reader.revoke_source(object(), object()),
        ):
            with pytest.raises(AccessError, match="^ingest_store_read_only$"):
                invoke()
        with pytest.raises(AccessError, match="^ingest_store_read_only$"):
            with reader._database(writing=True):
                pytest.fail("read-only writer transaction was entered")


def test_readonly_cold_ingest_observes_committed_wal_without_checkpoint(tmp_path):
    config = config_for(tmp_path)
    writer = open_ingest(config, read_only=False)
    path = writer.access.path
    with sqlite3.connect(path) as anchor:
        assert anchor.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        old_database = path.read_bytes()
        source = seed_source(writer)
        assert path.read_bytes() == old_database
        assert Path(str(path) + "-wal").stat().st_size > 0
        with unchanged(config):
            assert open_ingest(config, read_only=True).list_sources() == [source]
        assert path.read_bytes() == old_database
