import io
import tarfile
from types import SimpleNamespace

import pytest
from cloud_memory_hub import backup_store
from cloud_memory_hub.backup_archive import DEFAULT_MAX_BYTES, export_backup, unpack_backup
from cloud_memory_hub.backup_crypto import (
    CHUNK_SIZE,
    MAGIC,
    decrypted_archive,
    encrypt_bytes,
    is_encrypted,
)
from cloud_memory_hub.backup_maintenance import _source
from test_backup_store import layout as base_layout


@pytest.fixture
def layout(tmp_path):
    return base_layout.__wrapped__(tmp_path)


def _tar(payload):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("file.txt")
        info.size = len(payload)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _private(path, value):
    path.write_bytes(value)
    path.chmod(0o600)
    return path


def _decrypt(path, work, passphrase):
    with decrypted_archive(path, work, passphrase, max_bytes=DEFAULT_MAX_BYTES) as archive:
        return archive.read_bytes()


def test_round_trip(tmp_path):
    data = _tar(b"hello encrypted backup")
    encrypted = encrypt_bytes(data, b"correct horse battery staple")
    assert encrypted.startswith(MAGIC)
    source = _private(tmp_path / "archive.cmh.tar.enc", encrypted)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    assert _decrypt(source, work, b"correct horse battery staple") == data


def test_plain_tar_is_not_detected_as_encrypted(tmp_path):
    source = _private(tmp_path / "plain.cmh.tar", _tar(b"plain"))
    assert is_encrypted(source) is False


def test_missing_archive_is_invalid(tmp_path):
    with pytest.raises(backup_store.BackupError) as error:
        is_encrypted(tmp_path / "missing.cmh.tar")
    assert error.value.code == "backup_archive_invalid"


def test_wrong_passphrase_is_rejected(tmp_path):
    source = _private(tmp_path / "archive.enc", encrypt_bytes(_tar(b"secret"), b"right"))
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    with pytest.raises(backup_store.BackupError) as error:
        _decrypt(source, work, b"wrong")
    assert error.value.code == "backup_archive_passphrase_invalid"


def test_tampered_chunk_is_rejected(tmp_path):
    encrypted = bytearray(encrypt_bytes(_tar(b"secret"), b"passphrase"))
    encrypted[50] ^= 1
    source = _private(tmp_path / "archive.enc", bytes(encrypted))
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    with pytest.raises(backup_store.BackupError) as error:
        _decrypt(source, work, b"passphrase")
    assert error.value.code == "backup_archive_passphrase_invalid"


def test_truncated_stream_is_rejected(tmp_path):
    encrypted = encrypt_bytes(_tar(b"secret"), b"passphrase")
    source = _private(tmp_path / "archive.enc", encrypted[:-10])
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    with pytest.raises(backup_store.BackupError) as error:
        _decrypt(source, work, b"passphrase")
    assert error.value.code == "backup_archive_encrypted_invalid"


def test_chunk_boundary_round_trip(tmp_path):
    data = b"x" * (CHUNK_SIZE + 5)
    source = _private(tmp_path / "archive.enc", encrypt_bytes(data, b"passphrase"))
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    assert _decrypt(source, work, b"passphrase") == data


def test_exported_backup_round_trips_through_unpack(layout, tmp_path):
    config, root = layout
    manifest = backup_store.create_backup(config, root, revision="synthetic-encrypted")
    output = tmp_path / "export"
    output.mkdir(mode=0o700)
    target = output / "private.cmh.tar.enc"
    result = export_backup(root / manifest["backup_id"], target, passphrase=b"passphrase")
    assert result["format"] == "cmh-backup-tar-v1-encrypted"
    assert result["encryption"] == "scrypt-aes-256-gcm-v1"
    assert is_encrypted(target)
    with decrypted_archive(target, output, b"passphrase", max_bytes=DEFAULT_MAX_BYTES) as archive:
        with unpack_backup(archive, output, max_bytes=DEFAULT_MAX_BYTES) as version:
            assert backup_store.inspect_backup(version)["backup_id"] == manifest["backup_id"]


def test_archive_source_requires_and_reads_the_passphrase_file(layout, tmp_path):
    config, root = layout
    manifest = backup_store.create_backup(config, root, revision="synthetic-cli")
    output = tmp_path / "export"
    output.mkdir(mode=0o700)
    target = output / "private.cmh.tar.enc"
    export_backup(root / manifest["backup_id"], target, passphrase=b"passphrase")
    passphrase_file = output / "passphrase"
    passphrase_file.write_bytes(b"passphrase\n")
    passphrase_file.chmod(0o600)
    args = SimpleNamespace(
        archive=target,
        backup_dir=output,
        max_bytes=DEFAULT_MAX_BYTES,
        passphrase_file=passphrase_file,
        version=None,
    )
    with _source(args) as version:
        assert backup_store.inspect_backup(version)["backup_id"] == manifest["backup_id"]
    args.passphrase_file = None
    with pytest.raises(backup_store.BackupError) as error:
        with _source(args):
            pass
    assert error.value.code == "backup_passphrase_required"
