"""Portable private backup archives; extraction never writes to running volumes."""

import json
import os
import shutil
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from cloud_memory_hub import backup_store as store
from cloud_memory_hub.storage_lease import StorageLease

DEFAULT_MAX_BYTES = 16 * 1024**3
MAX_METADATA_BYTES = 64 * 1024**2


class _BoundedReader:
    """Bound tar's eager GNU/PAX metadata reads as well as extracted file bytes."""

    def __init__(self, source):
        self.source = source
        self.metadata = True
        self.metadata_bytes = 0

    def read(self, size):
        if size < 0 or size > store.MAX_MANIFEST_BYTES + 1024:
            raise store.BackupError("backup_archive_metadata_too_large")
        if self.metadata and self.metadata_bytes + size > MAX_METADATA_BYTES:
            raise store.BackupError("backup_archive_metadata_too_large")
        content = self.source.read(size)
        if self.metadata:
            self.metadata_bytes += len(content)
        return content

    @contextmanager
    def payload(self):
        self.metadata = False
        try:
            yield
        finally:
            self.metadata = True

    def seek(self, *args):
        return self.source.seek(*args)

    def tell(self):
        return self.source.tell()


def _header(name, *, size=0, directory=False):
    entry = tarfile.TarInfo(name)
    entry.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    entry.mode = 0o700 if directory else 0o600
    entry.size = size
    return entry


def export_backup(backup_path: Path, output: Path) -> dict:
    """Export a verified version into a new, non-overwriting uncompressed tar."""
    output = output.absolute()
    store._plain_path(output.parent, directory=True, private=True)
    if output.is_relative_to(backup_path.absolute()):
        raise store.BackupError("backup_export_target_invalid")
    temporary = output.parent / ("." + output.name + ".partial-" + uuid4().hex)
    reservation = None
    published = False
    try:
        with StorageLease(backup_path.parent):
            manifest = store.inspect_backup(backup_path)
            directories = set(manifest["directories"])
            descriptor = os.open(
                output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            reservation = os.fstat(descriptor)
            os.close(descriptor)
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    # The first member gives a bounded identity before extraction.
                    for name in [
                        store.MANIFEST,
                        *manifest["directories"],
                        *[row["path"] for row in manifest["files"]],
                    ]:
                        if name in directories:
                            archive.addfile(_header(name, directory=True))
                        else:
                            with store._read(backup_path / name) as source:
                                archive.addfile(
                                    _header(name, size=os.fstat(source.fileno()).st_size), source
                                )
                stream.flush()
                os.fsync(stream.fileno())
            if store.inspect_backup(backup_path) != manifest:
                raise store.BackupError("backup_changed")
            current = output.lstat()
            if (current.st_dev, current.st_ino) != (reservation.st_dev, reservation.st_ino):
                raise store.BackupError("backup_export_target_changed")
            os.replace(temporary, output)
            published = True
            store._fsync_directory(output.parent)
            return {
                "backup_id": manifest["backup_id"],
                "format": "cmh-backup-tar-v1",
                "bytes": output.stat().st_size,
                "sha256": store._hash(output),
            }
    except store.BackupError:
        raise
    except FileExistsError:
        raise store.BackupError("backup_export_exists") from None
    except Exception:
        raise store.BackupError("backup_export_failed") from None
    finally:
        temporary.unlink(missing_ok=True)
        if reservation is not None and not published:
            try:
                current = output.lstat()
                if (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino):
                    output.unlink()
            except FileNotFoundError:
                pass


def _extensions(archive, member):
    # Our exporter only emits local long-path/large-size PAX extensions. Global
    # values are copied into every TarInfo by tarfile and can amplify memory.
    if archive.pax_headers or set(member.pax_headers) - {"path", "size"}:
        raise store.BackupError("backup_archive_invalid")


def _read_manifest(archive):
    first = archive.next()
    if (
        first is None
        or first.name != store.MANIFEST
        or not first.isreg()
        or not 0 < first.size <= store.MAX_MANIFEST_BYTES
    ):
        raise store.BackupError("backup_archive_invalid")
    _extensions(archive, first)
    stream = archive.extractfile(first)
    if stream is None:
        raise store.BackupError("backup_archive_invalid")
    with stream, archive.fileobj.payload():
        content = stream.read(store.MAX_MANIFEST_BYTES + 1)
    if len(content) != first.size:
        raise store.BackupError("backup_archive_invalid")
    value = json.loads(content, object_pairs_hook=store._unique_object)
    identity = value.get("backup_id") if isinstance(value, dict) else None
    if not isinstance(identity, str) or not store._ID.fullmatch(identity):
        raise store.BackupError("backup_archive_invalid")
    archive.members.clear()
    return identity, content


def _extract(archive, target, manifest_bytes, *, max_bytes):
    descriptor = os.open(target / store.MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(manifest_bytes)
    total = len(manifest_bytes)
    seen = {store.MANIFEST}
    while (member := archive.next()) is not None:
        _extensions(archive, member)
        relative = store._relative(member.name)
        if (
            relative.parts[0] not in {"data", "config", "governance"}
            or member.name in seen
            or len(seen) >= store.MAX_FILES * 2
            or not (member.isdir() or member.isreg())
            or member.size < 0
            or (member.isdir() and member.size != 0)
        ):
            raise store.BackupError("backup_archive_invalid")
        seen.add(member.name)
        total += member.size
        if total > max_bytes:
            raise store.BackupError("backup_archive_too_large")
        destination = target / relative
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if member.isdir():
            destination.mkdir(mode=0o700, exist_ok=True)
        else:
            incoming = archive.extractfile(member)
            if incoming is None:
                raise store.BackupError("backup_archive_invalid")
            descriptor = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with incoming, os.fdopen(descriptor, "wb") as output, archive.fileobj.payload():
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            if destination.stat().st_size != member.size:
                raise store.BackupError("backup_archive_invalid")
        # Extraction is sequential and never resolves links or member names.
        # Python 3.12 otherwise retains all parsed TarInfo objects until close.
        archive.members.clear()


@contextmanager
def unpack_backup(archive_path: Path, work_dir: Path, *, max_bytes=DEFAULT_MAX_BYTES):
    """Yield a fully verified private copy, never an arbitrary extraction target.

    The archive and any protective restore version outlive this temporary copy.
    The caller must keep work_dir separate from its three running volume roots.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**4:
        raise store.BackupError("backup_archive_limit_invalid")
    store._plain_path(archive_path, directory=False, private=True)
    store._root(work_dir, create=True)
    with tempfile.TemporaryDirectory(prefix=".archive-import-", dir=work_dir) as directory:
        try:
            with store._read(archive_path) as source:
                # Public v1 is deliberately uncompressed; no decompression bomb or
                # archive metadata may create a link, executable, or device node.
                with tarfile.open(fileobj=_BoundedReader(source), mode="r:") as archive:
                    identity, content = _read_manifest(archive)
                    if len(content) > max_bytes:
                        raise store.BackupError("backup_archive_too_large")
                    target = Path(directory) / identity
                    target.mkdir(mode=0o700)
                    _extract(archive, target, content, max_bytes=max_bytes)
            store.inspect_backup(target)
        except store.BackupError:
            raise
        except Exception:
            raise store.BackupError("backup_archive_invalid") from None
        # Restore errors belong to the restore caller and must retain their codes.
        yield target
