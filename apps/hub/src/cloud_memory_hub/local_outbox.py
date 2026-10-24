"""Filtered local network delivery, separate from Core's model-work queue."""

import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from cloud_memory_hub.events import (
    EventEnvelope,
    EventError,
    canonical_uuid,
    prepare_event,
    require_current_filter,
)
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError


class OutboxError(Exception):
    pass


@dataclass(frozen=True)
class PendingEvent:
    sequence: int
    event_id: str
    payload_digest: str
    payload: str = field(repr=False)


def _private(path: Path, *, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not kind
        or info.st_mode & 0o077
        or info.st_uid != os.getuid()
        or (not directory and info.st_nlink != 1)
    ):
        raise OutboxError("unsafe_outbox_path")


class LocalOutbox:
    def __init__(
        self,
        path: Path,
        *,
        instance_id: str,
        device_id: str,
        privacy: CredentialFilter | None = None,
    ):
        self.path = path.absolute()
        try:
            self.instance_id = canonical_uuid(instance_id)
            self.device_id = canonical_uuid(device_id)
        except (ValueError, TypeError, AttributeError):
            raise OutboxError("invalid_binding") from None
        self.privacy = privacy or CredentialFilter()
        # Normal opens require a valid existing database, including after restart.
        with self._connection():
            pass

    @classmethod
    def initialize(
        cls,
        path: Path,
        *,
        instance_id: str,
        device_id: str,
        privacy: CredentialFilter | None = None,
    ):
        try:
            canonical_uuid(instance_id)
            canonical_uuid(device_id)
        except (ValueError, TypeError, AttributeError):
            raise OutboxError("invalid_binding") from None
        path = path.absolute()
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _private(path.parent, directory=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            with sqlite3.connect(path) as db:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE binding (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        instance_id TEXT NOT NULL, device_id TEXT NOT NULL
                    );
                    CREATE TABLE events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'queued'
                            CHECK(status IN ('queued','accepted')),
                        receipt_id TEXT
                    );
                    CREATE INDEX pending_events ON events(status,sequence);
                    CREATE TABLE rejected (code TEXT PRIMARY KEY, count INTEGER NOT NULL);
                    PRAGMA user_version=1;
                """)
                db.execute("INSERT INTO binding VALUES (1, ?, ?)", (instance_id, device_id))
        except FileExistsError:
            raise OutboxError("outbox_already_exists") from None
        except (sqlite3.Error, OSError):
            # Preserve partial state for diagnosis; never overwrite or delete it.
            raise OutboxError("outbox_initialization_failed") from None
        return cls(path, instance_id=instance_id, device_id=device_id, privacy=privacy)

    @contextmanager
    def _connection(self, *, writing=False):
        try:
            _private(self.path.parent, directory=True)
            _private(self.path)
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
                try:
                    binding = db.execute(
                        "SELECT instance_id, device_id FROM binding WHERE id=1"
                    ).fetchone()
                    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise OutboxError("unsupported_outbox_version")
                    if binding is None or tuple(binding) != (self.instance_id, self.device_id):
                        raise OutboxError("outbox_binding_mismatch")
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            raise OutboxError("outbox_unavailable") from None

    def enqueue(self, raw: object) -> dict:
        try:
            return self._enqueue(raw)
        except PrivacyError:
            self.record_rejection("privacy_rejected")
            raise
        except EventError:
            self.record_rejection("invalid_event")
            raise
        except OutboxError as error:
            if str(error) == "event_identity_conflict":
                self.record_rejection("identity_conflict")
            raise

    def _enqueue(self, raw: object) -> dict:
        event = prepare_event(raw, self.privacy)
        with self._connection(writing=True) as db:
            return self._insert_event(db, event)

    def _insert_event(self, db: sqlite3.Connection, event: EventEnvelope) -> dict:
        """Insert inside our caller's transaction, also used for atomic capture cursors."""
        if event.instance_id != self.instance_id or event.device_id != self.device_id:
            raise OutboxError("event_binding_mismatch")
        require_current_filter(event, self.privacy)
        payload, digest = event.canonical_payload(), event.payload_digest()
        row = db.execute(
            "SELECT payload_digest, status, receipt_id FROM events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        if row is not None:
            if row[0] != digest:
                raise OutboxError("event_identity_conflict")
            return {"event_id": event.event_id, "status": row[1], "receipt_id": row[2]}
        db.execute(
            "INSERT INTO events(event_id,payload,payload_digest) VALUES (?,?,?)",
            (event.event_id, payload, digest),
        )
        return {"event_id": event.event_id, "status": "queued", "receipt_id": None}

    def pending(self, *, limit: int = 32) -> tuple[PendingEvent, ...]:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise OutboxError("invalid_batch_limit")
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE status='queued' ORDER BY sequence LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            if (
                not isinstance(row["payload"], str)
                or hashlib.sha256(row["payload"].encode()).hexdigest() != row["payload_digest"]
            ):
                raise OutboxError("outbox_payload_corrupted")
            try:
                event = EventEnvelope.model_validate_json(row["payload"])
                require_current_filter(event, self.privacy)
            except (ValidationError, EventError, ValueError, TypeError):
                raise OutboxError("outbox_payload_requires_review") from None
            if (
                event.instance_id != self.instance_id
                or event.device_id != self.device_id
                or event.event_id != row["event_id"]
            ):
                raise OutboxError("outbox_payload_corrupted")
            result.append(
                PendingEvent(
                    row["sequence"], row["event_id"], row["payload_digest"], row["payload"]
                )
            )
        return tuple(result)

    def acknowledge(
        self,
        *,
        instance_id: str,
        device_id: str,
        event_id: str,
        payload_digest: str,
        receipt_id: str,
    ) -> None:
        if (instance_id, device_id) != (self.instance_id, self.device_id):
            raise OutboxError("receipt_binding_mismatch")
        try:
            canonical_uuid(event_id)
            canonical_uuid(receipt_id)
        except (ValueError, TypeError, AttributeError):
            raise OutboxError("invalid_receipt") from None
        with self._connection(writing=True) as db:
            row = db.execute(
                "SELECT payload_digest, receipt_id FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is None or row[0] != payload_digest or row[1] not in (None, receipt_id):
                raise OutboxError("receipt_conflict")
            db.execute(
                "UPDATE events SET status='accepted', receipt_id=? WHERE event_id=?",
                (receipt_id, event_id),
            )

    def record_rejection(self, code: str) -> None:
        # Deliberately narrow: callers cannot turn exception text into a log.
        if code not in {"privacy_rejected", "invalid_event", "identity_conflict"}:
            raise OutboxError("invalid_rejection_code")
        with self._connection(writing=True) as db:
            db.execute(
                "INSERT INTO rejected VALUES (?,1) ON CONFLICT(code) DO UPDATE SET count=count+1",
                (code,),
            )

    def status(self) -> dict:
        with self._connection() as db:
            counts = {
                row[0]: row[1]
                for row in db.execute("SELECT status, COUNT(*) FROM events GROUP BY status")
            }
            rejected = dict(db.execute("SELECT code, count FROM rejected ORDER BY code"))
        return {
            "queued": counts.get("queued", 0),
            "accepted": counts.get("accepted", 0),
            "rejected": rejected,
        }
