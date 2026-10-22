"""Implement MemHub's existing SecretStore protocol for a headless NAS.

Only encrypted model credentials live here. The key stays in a separate mounted
configuration directory; it is not part of memory data or content exports.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from one_memory.secrets import SecretStoreUnavailableError


class EncryptedSecretStore:
    def __init__(self, path: Path, key: bytes) -> None:
        self.path = path
        self.cipher = Fernet(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        with self._connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS credentials "
                "(reference TEXT PRIMARY KEY, ciphertext BLOB NOT NULL)"
            )
            # Detect the wrong recovery key before any model request can run.
            for (encrypted,) in connection.execute("SELECT ciphertext FROM credentials"):
                self._decrypt(encrypted)

    @contextmanager
    def _connection(self):
        try:
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
        except sqlite3.Error:
            raise SecretStoreUnavailableError() from None

    def _decrypt(self, encrypted: bytes) -> str:
        try:
            return self.cipher.decrypt(encrypted).decode("utf-8")
        except (InvalidToken, UnicodeError):
            raise SecretStoreUnavailableError() from None

    def put(self, secret_ref: str, value: str) -> None:
        if not value:
            raise ValueError("Secret value cannot be empty")
        encrypted = self.cipher.encrypt(value.encode("utf-8"))
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO credentials VALUES (?, ?) ON CONFLICT(reference) "
                "DO UPDATE SET ciphertext=excluded.ciphertext",
                (secret_ref, encrypted),
            )

    def resolve(self, secret_ref: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM credentials WHERE reference=?", (secret_ref,)
            ).fetchone()
        return self._decrypt(row[0]) if row else None

    def has(self, secret_ref: str) -> bool:
        return self.resolve(secret_ref) is not None

    def delete(self, secret_ref: str) -> None:
        self.delete_many((secret_ref,))

    def delete_many(self, secret_refs: tuple[str, ...]) -> None:
        with self._connection() as connection:
            connection.executemany(
                "DELETE FROM credentials WHERE reference=?",
                [(reference,) for reference in secret_refs],
            )
