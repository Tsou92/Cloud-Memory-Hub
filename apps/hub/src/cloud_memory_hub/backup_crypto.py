"""Authenticated encryption for exported backup archives.

The internal /backups versions stay in their existing directory format. This
module protects the user-facing `.cmh.tar` export: a scrypt-derived AES-256-GCM
stream with per-chunk authentication, so large archives do not need to be held
in memory.
"""

import hashlib
import io
import os
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cloud_memory_hub import backup_store as store

MAGIC = b"CMHBK1"
VERSION = 1
SALT_BYTES = 16
NONCE_BYTES = 8
CHUNK_SIZE = 1024 * 1024
KDF_N = 2**15
KDF_R = 8
KDF_P = 1
MAX_PASSPHRASE_BYTES = 1024
FORMAT = "scrypt-aes-256-gcm-v1"
# The decrypted tar may exceed the content limit only by tar metadata.
DECRYPT_OVERHEAD = 256 * 1024 * 1024


def _header(salt, nonce):
    return MAGIC + bytes([VERSION]) + salt + nonce + CHUNK_SIZE.to_bytes(4, "big")


def _key(passphrase, salt):
    return hashlib.scrypt(
        passphrase,
        salt=salt,
        n=KDF_N,
        r=KDF_R,
        p=KDF_P,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )


def _passphrase(value):
    if not isinstance(value, bytes) or not 1 <= len(value) <= MAX_PASSPHRASE_BYTES:
        raise store.BackupError("backup_passphrase_invalid")
    return value


def is_encrypted(path):
    with store._read(Path(path)) as stream:
        return stream.read(len(MAGIC)) == MAGIC


class EncryptedWriter:
    """File-like sink used by tarfile; encrypts in authenticated chunks."""

    def __init__(self, stream, passphrase):
        self.stream = stream
        self.salt = secrets.token_bytes(SALT_BYTES)
        self.base_nonce = secrets.token_bytes(NONCE_BYTES)
        self.header = _header(self.salt, self.base_nonce)
        self.aead = AESGCM(_key(_passphrase(passphrase), self.salt))
        self.counter = 0
        self.buffer = bytearray()
        self.written = 0
        stream.write(self.header)

    def write(self, data):
        data = bytes(data)
        self.buffer.extend(data)
        self.written += len(data)
        while len(self.buffer) >= CHUNK_SIZE:
            chunk = bytes(self.buffer[:CHUNK_SIZE])
            del self.buffer[:CHUNK_SIZE]
            self._chunk(chunk, final=False)
        return len(data)

    def _chunk(self, data, *, final):
        if self.counter >= 2**32:
            raise store.BackupError("backup_archive_too_large")
        counter = self.counter.to_bytes(4, "big")
        aad = self.header + counter + (b"\x01" if final else b"\x00")
        cipher = self.aead.encrypt(self.base_nonce + counter, data, aad)
        self.stream.write(len(data).to_bytes(4, "big") + cipher)
        self.counter += 1

    def finalize(self):
        if self.buffer:
            self._chunk(bytes(self.buffer), final=False)
            self.buffer.clear()
        self._chunk(b"", final=True)
        self.stream.flush()

    def flush(self):
        self.stream.flush()

    def tell(self):
        return self.written

    def seekable(self):
        return False


def encrypt_bytes(data, passphrase):
    """Small-input helper used by tests and tooling."""
    output = io.BytesIO()
    writer = EncryptedWriter(output, passphrase)
    writer.write(data)
    writer.finalize()
    return output.getvalue()


@contextmanager
def decrypted_archive(path, work_dir, passphrase, *, max_bytes):
    """Decrypt into a private temporary tar, yielding its path, then remove it."""
    passphrase = _passphrase(passphrase)
    path = Path(path).absolute()
    store._plain_path(path, directory=False, private=True)
    root = store._root(Path(work_dir), create=True)
    directory = Path(tempfile.mkdtemp(prefix=".decrypt-", dir=root))
    target = directory / "archive.tar"
    try:
        with store._read(path) as source:
            magic = source.read(len(MAGIC))
            if magic != MAGIC:
                raise store.BackupError("backup_archive_not_encrypted")
            version = source.read(1)
            if version != bytes([VERSION]):
                raise store.BackupError("backup_archive_encryption_unsupported")
            salt = source.read(SALT_BYTES)
            nonce = source.read(NONCE_BYTES)
            size_field = source.read(4)
            if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES or len(size_field) != 4:
                raise store.BackupError("backup_archive_encrypted_invalid")
            if int.from_bytes(size_field, "big") != CHUNK_SIZE:
                raise store.BackupError("backup_archive_encryption_unsupported")
            header = MAGIC + version + salt + nonce + size_field
            aead = AESGCM(_key(passphrase, salt))
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                counter = 0
                total = 0
                while True:
                    length_field = source.read(4)
                    if len(length_field) != 4:
                        raise store.BackupError("backup_archive_encrypted_invalid")
                    length = int.from_bytes(length_field, "big")
                    if length > CHUNK_SIZE:
                        raise store.BackupError("backup_archive_encrypted_invalid")
                    cipher = source.read(length + 16)
                    if len(cipher) != length + 16:
                        raise store.BackupError("backup_archive_encrypted_invalid")
                    counter_field = counter.to_bytes(4, "big")
                    final = length == 0
                    aad = header + counter_field + (b"\x01" if final else b"\x00")
                    try:
                        plain = aead.decrypt(nonce + counter_field, cipher, aad)
                    except InvalidTag:
                        raise store.BackupError("backup_archive_passphrase_invalid") from None
                    if final:
                        if source.read(1) != b"":
                            raise store.BackupError("backup_archive_encrypted_invalid")
                        break
                    total += len(plain)
                    if total > max_bytes + DECRYPT_OVERHEAD:
                        raise store.BackupError("backup_archive_too_large")
                    output.write(plain)
                    counter += 1
                    if counter >= 2**32:
                        raise store.BackupError("backup_archive_encrypted_invalid")
                output.flush()
                os.fsync(output.fileno())
        yield target
    except store.BackupError:
        raise
    except Exception:
        raise store.BackupError("backup_archive_encrypted_invalid") from None
    finally:
        shutil.rmtree(directory, ignore_errors=True)
