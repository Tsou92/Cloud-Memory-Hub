"""Plaintext local cache of the last successful read replies.

The connector talks to the Hub over HTTP, so a NAS that is rebooting, asleep or
off the network leaves every read unavailable. This cache keeps the last reply
per read request and serves it only when the transport itself failed with a
retryable error; a 403/404/422 still reaches the caller so revocation and
authorization changes are never masked by stale data.

The file lives next to the outbox in the private state directory and is
plaintext. Cached replies carry the same warning as live history: they are
untrusted data, and each served reply is marked with `served_from_cache` and
`cached_at` so the agent can say how old it is.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

MAX_ENTRIES = 5000
MAX_AGE_SECONDS = 30 * 24 * 3600


class ReadCache:
    def __init__(self, path: Path, *, instance_id: str, device_id: str):
        self.path = Path(path)
        self.instance_id = instance_id
        self.device_id = device_id
        self._prepare()

    def _prepare(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS read_cache ("
                    "cache_key TEXT PRIMARY KEY,"
                    "instance_id TEXT NOT NULL,"
                    "device_id TEXT NOT NULL,"
                    "scope_id TEXT NOT NULL,"
                    "operation TEXT NOT NULL,"
                    "request_json TEXT NOT NULL,"
                    "reply_json TEXT NOT NULL,"
                    "cached_at INTEGER NOT NULL)"
                )
                connection.execute("PRAGMA user_version=1")
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error):
            # A cache that cannot be created only disables offline reads.
            pass

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _key(self, scope_id, operation, request):
        payload = json.dumps(
            {
                "instance_id": self.instance_id,
                "device_id": self.device_id,
                "scope_id": scope_id,
                "operation": operation,
                "request": request,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, scope_id, operation, request):
        """Return the cached reply for this exact request, or None."""
        key = self._key(scope_id, operation, request)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT reply_json,cached_at FROM read_cache WHERE cache_key=?", (key,)
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if row is None:
            return None
        reply, cached_at = row
        if time.time() - int(cached_at) > MAX_AGE_SECONDS:
            return None
        try:
            value = json.loads(reply)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        value["served_from_cache"] = True
        value["cached_at"] = datetime.fromtimestamp(int(cached_at), UTC).isoformat()
        return value

    def put(self, scope_id, operation, request, reply):
        """Store one successful reply. Failures are ignored: reads still work."""
        if not isinstance(reply, dict):
            return
        key = self._key(scope_id, operation, request)
        now = int(time.time())
        try:
            encoded = json.dumps(reply, ensure_ascii=False, separators=(",", ":"))
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO read_cache VALUES (?,?,?,?,?,?,?,?)",
                    (
                        key,
                        self.instance_id,
                        self.device_id,
                        scope_id,
                        operation,
                        json.dumps(request, sort_keys=True, ensure_ascii=False),
                        encoded,
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM read_cache WHERE cached_at<?", (now - MAX_AGE_SECONDS,)
                )
                count = connection.execute("SELECT COUNT(*) FROM read_cache").fetchone()[0]
                if count > MAX_ENTRIES:
                    connection.execute(
                        "DELETE FROM read_cache WHERE cache_key IN ("
                        "SELECT cache_key FROM read_cache ORDER BY cached_at ASC LIMIT ?)",
                        (count - MAX_ENTRIES,),
                    )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass
