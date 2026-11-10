"""Process exclusion for cooperating Hub CLI runtimes and maintenance commands."""

import fcntl
import os
import stat
from pathlib import Path

from cloud_memory_hub.remote_access import AccessError, DeviceAccessStore

LOCK_NAME = ".cmh-storage.lock"
RESTORE_MARKER = ".cmh-restore-incomplete.json"


class StorageLeaseError(RuntimeError):
    """Fixed public codes without filesystem or credential details."""


def require_restore_complete(*directories: Path) -> None:
    """Any marker, including an unreadable or dangling one, fences startup."""
    for directory in directories:
        try:
            (directory / RESTORE_MARKER).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise StorageLeaseError("restore_incomplete") from None
        raise StorageLeaseError("restore_incomplete")


class StorageLease:
    def __init__(self, *directories: Path):
        self.directories = directories
        self._descriptors: list[int] = []

    def __enter__(self):
        if self._descriptors:
            raise StorageLeaseError("storage_lease_already_held")
        try:
            for directory in self.directories:
                DeviceAccessStore._check_private(directory, directory=True)
            # Normalize only after rejecting symlinks at the supplied roots.
            roots = sorted({directory.resolve(strict=True) for directory in self.directories})
            if not roots:
                raise StorageLeaseError("storage_lease_roots_required")
            for root in roots:
                self._acquire(root / LOCK_NAME)
            return self
        except BaseException as error:
            self._close()
            if isinstance(error, (OSError, AccessError)):
                raise StorageLeaseError("storage_lease_unavailable") from None
            raise

    def _acquire(self, path):
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600
        )
        self._descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_size != 0
        ):
            raise StorageLeaseError("storage_lease_unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StorageLeaseError("storage_in_use") from None
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise StorageLeaseError("storage_lease_changed")
        DeviceAccessStore._check_private(path)

    def _close(self):
        while self._descriptors:
            os.close(self._descriptors.pop())

    def __exit__(self, *exc):
        self._close()
