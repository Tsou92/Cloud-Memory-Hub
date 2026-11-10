"""Portable backup archive checks use the complete synthetic three-volume fixture."""

import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from cloud_memory_hub import backup_archive as portable
from cloud_memory_hub.backup_store import BackupError, create_backup, inspect_backup
from test_backup_store import layout as backup_layout


@pytest.fixture
def layout(tmp_path):
    return backup_layout.__wrapped__(tmp_path)


def export(layout, tmp_path):
    config, versions = layout
    result = create_backup(config, versions, revision="synthetic-archive")
    output_dir = tmp_path / "exports"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "private.cmh.tar"
    receipt = portable.export_backup(versions / result["backup_id"], output)
    return result, output, receipt


def rewrite(source, target, transform):
    with tarfile.open(source, mode="r:") as incoming, tarfile.open(target, mode="w") as outgoing:
        for member in incoming:
            payload = incoming.extractfile(member).read() if member.isreg() else None
            values = transform(member, payload)
            for replacement, content in values:
                outgoing.addfile(replacement, io.BytesIO(content) if content is not None else None)
    target.chmod(0o600)


def test_complete_private_roundtrip_and_owned_temporary_cleanup(layout, tmp_path):
    original, output, receipt = export(layout, tmp_path)
    assert receipt["backup_id"] == original["backup_id"]
    assert receipt["format"] == "cmh-backup-tar-v1"
    assert receipt["bytes"] == output.stat().st_size
    assert len(receipt["sha256"]) == 64
    assert output.stat().st_mode & 0o777 == 0o600
    working = tmp_path / "imports"
    with portable.unpack_backup(output, working) as extracted:
        assert inspect_backup(extracted) == original
        assert all(item.stat().st_mode & 0o077 == 0 for item in extracted.rglob("*"))
        assert not any(item.is_symlink() for item in extracted.rglob("*"))
    assert not extracted.exists()
    assert output.exists()
    assert list(working.iterdir()) == []


def test_existing_export_and_source_snapshot_are_never_overwritten(layout, tmp_path):
    original, output, _ = export(layout, tmp_path)
    before = output.read_bytes()
    source = layout[1] / original["backup_id"]
    with pytest.raises(BackupError, match="export_exists"):
        portable.export_backup(source, output)
    assert output.read_bytes() == before
    with pytest.raises(BackupError, match="target_invalid"):
        portable.export_backup(source, source / "bad.tar")
    assert inspect_backup(source) == original


@pytest.mark.parametrize(
    "variant",
    ["traversal", "absolute", "symlink", "hardlink", "fifo", "duplicate", "extra", "content"],
)
def test_invalid_member_rejected_without_writing_outside_stage(layout, tmp_path, variant):
    _, archive, _ = export(layout, tmp_path)
    damaged = archive.parent / "damaged.tar"

    def transform(member, payload):
        if member.name != "config/synthetic-probe.private.json":
            return [(member, payload)]
        if variant == "traversal":
            member.name = "../escaped"
        elif variant == "absolute":
            member.name = "/escaped"
        elif variant in {"symlink", "hardlink", "fifo"}:
            member.type = {
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[variant]
            member.linkname = "../../escaped"
            member.size = 0
            payload = None
        elif variant == "duplicate":
            return [(member, payload), (member, payload)]
        elif variant == "extra":
            member.name = "config/unlisted"
        else:
            payload = b"x" * member.size
        return [(member, payload)]

    rewrite(archive, damaged, transform)
    work = tmp_path / "imports"
    with pytest.raises(BackupError):
        with portable.unpack_backup(damaged, work):
            pytest.fail("invalid archive yielded a restore source")
    assert not (tmp_path / "escaped").exists()
    assert list(work.iterdir()) == []


def test_size_limit_and_corrupted_manifest_fail_before_restore(layout, tmp_path):
    _, archive, _ = export(layout, tmp_path)
    with pytest.raises(BackupError, match="too_large"):
        with portable.unpack_backup(archive, tmp_path / "imports", max_bytes=100):
            pytest.fail()
    damaged = archive.parent / "manifest.tar"

    def transform(member, payload):
        if member.name == "manifest.json":
            document = json.loads(payload)
            document["backup_id"] = "../../elsewhere"
            payload = json.dumps(document).encode()
            member.size = len(payload)
        return [(member, payload)]

    rewrite(archive, damaged, transform)
    with pytest.raises(BackupError, match="invalid"):
        with portable.unpack_backup(damaged, tmp_path / "imports"):
            pytest.fail()


def test_eager_tar_metadata_allocation_is_bounded(tmp_path):
    archive = tmp_path / "huge-pax.tar"
    header = tarfile.TarInfo("pax")
    header.type = tarfile.XHDTYPE
    header.size = portable.store.MAX_MANIFEST_BYTES * 2
    archive.write_bytes(header.tobuf(format=tarfile.USTAR_FORMAT))
    archive.chmod(0o600)
    with pytest.raises(BackupError, match="metadata_too_large"):
        with portable.unpack_backup(archive, tmp_path / "imports"):
            pytest.fail()


def test_multiple_small_pax_headers_obey_cumulative_metadata_budget(layout, tmp_path, monkeypatch):
    _, archive, _ = export(layout, tmp_path)
    header = tarfile.TarInfo.create_pax_global_header({"comment": "x" * 1000})
    damaged = archive.parent / "many-pax.tar"
    damaged.write_bytes(header * 4 + archive.read_bytes())
    damaged.chmod(0o600)
    monkeypatch.setattr(portable, "MAX_METADATA_BYTES", 2500)
    with pytest.raises(BackupError, match="metadata_too_large"):
        with portable.unpack_backup(damaged, tmp_path / "imports"):
            pytest.fail()


def test_exporter_long_unicode_paths_remain_supported(layout, tmp_path):
    config, _ = layout
    nested = config.data_dir / ("合成" * 40) / ("路径" * 40)
    nested.mkdir(parents=True, mode=0o700)
    file = nested / "记录.txt"
    file.write_text("Synthetic portable content")
    file.chmod(0o600)
    original, archive, _ = export(layout, tmp_path)
    with portable.unpack_backup(archive, tmp_path / "imports") as extracted:
        assert inspect_backup(extracted) == original
        assert (
            extracted / "data" / file.relative_to(config.data_dir)
        ).read_text() == file.read_text()


def test_global_pax_dictionary_is_rejected_before_payload_extraction(layout, tmp_path):
    _, archive, _ = export(layout, tmp_path)
    header = tarfile.TarInfo.create_pax_global_header(
        {f"synthetic.{number}": "value" for number in range(1000)}
    )
    damaged = archive.parent / "global-pax.tar"
    damaged.write_bytes(header + archive.read_bytes())
    damaged.chmod(0o600)
    with pytest.raises(BackupError, match="backup_archive_invalid"):
        with portable.unpack_backup(damaged, tmp_path / "imports"):
            pytest.fail()


def test_sequential_extraction_does_not_retain_all_tar_members(layout, tmp_path, monkeypatch):
    _, archive, _ = export(layout, tmp_path)
    original = portable._extensions
    retained = []

    def check(handle, member):
        retained.append(len(handle.members))
        return original(handle, member)

    monkeypatch.setattr(portable, "_extensions", check)
    with portable.unpack_backup(archive, tmp_path / "imports"):
        pass
    assert len(retained) > 10
    assert max(retained) <= 1


def test_consumer_restore_error_is_not_relabelled_as_archive_damage(layout, tmp_path):
    _, archive, _ = export(layout, tmp_path)
    failure = RuntimeError("synthetic_restore_failure")
    with pytest.raises(RuntimeError) as caught:
        with portable.unpack_backup(archive, tmp_path / "imports"):
            raise failure
    assert caught.value is failure


def test_failed_export_cleans_only_its_reservation(layout, tmp_path, monkeypatch):
    config, root = layout
    manifest = create_backup(config, root, revision="synthetic")
    parent = tmp_path / "exports"
    parent.mkdir(mode=0o700)
    output = parent / "snapshot.tar"
    sentinel = parent / "untouched"
    sentinel.write_text("synthetic")
    original = portable.store._read

    def fail(path, **kwargs):
        if Path(path).name == "synthetic-probe.private.json":
            raise OSError("synthetic-private-file")
        return original(path, **kwargs)

    monkeypatch.setattr(portable.store, "_read", fail)
    with pytest.raises(BackupError):
        portable.export_backup(root / manifest["backup_id"], output)
    assert not output.exists()
    assert set(parent.iterdir()) == {sentinel}


def test_archive_symlink_hardlink_and_public_file_rejected(layout, tmp_path):
    _, archive, _ = export(layout, tmp_path)
    link = archive.parent / "link.tar"
    link.symlink_to(archive)
    with pytest.raises(BackupError):
        with portable.unpack_backup(link, tmp_path / "imports"):
            pytest.fail()
    link.unlink()
    os.link(archive, link)
    with pytest.raises(BackupError):
        with portable.unpack_backup(archive, tmp_path / "imports"):
            pytest.fail()
    link.unlink()
    archive.chmod(0o644)
    with pytest.raises(BackupError):
        with portable.unpack_backup(archive, tmp_path / "imports"):
            pytest.fail()
