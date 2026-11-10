"""Filtered local network delivery, separate from Core's model-work queue."""

import hashlib
import os
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
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
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError, parse_json

OUTBOX_VERSION = 2
TERMINAL_REJECTION_CODE = "governance_content_suppressed"
_REJECTIONS_SCHEMA = """CREATE TABLE delivery_rejections (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    payload_digest TEXT NOT NULL,
    code TEXT NOT NULL CHECK(code='governance_content_suppressed')
)"""


class OutboxError(Exception):
    pass


@dataclass(frozen=True)
class PendingEvent:
    sequence: int
    event_id: str
    payload_digest: str
    payload: str = field(repr=False)


@dataclass(frozen=True)
class EventMetadata:
    sequence: int
    event_id: str
    payload_digest: str
    status: str
    receipt_id: str | None


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
                """)
                db.execute(_REJECTIONS_SCHEMA)
                db.execute(f"PRAGMA user_version={OUTBOX_VERSION}")
                db.execute("INSERT INTO binding VALUES (1, ?, ?)", (instance_id, device_id))
        except FileExistsError:
            raise OutboxError("outbox_already_exists") from None
        except (sqlite3.Error, OSError):
            # Preserve partial state for diagnosis; never overwrite or delete it.
            raise OutboxError("outbox_initialization_failed") from None
        return cls(path, instance_id=instance_id, device_id=device_id, privacy=privacy)

    @classmethod
    def upgrade(cls, path: Path, *, instance_id: str, device_id: str, backup_file: Path) -> dict:
        """Explicit local migration; old data remains in a consistent private backup."""
        try:
            canonical_uuid(instance_id)
            canonical_uuid(device_id)
        except (ValueError, TypeError, AttributeError):
            raise OutboxError("invalid_binding") from None
        path, backup_file = path.absolute(), backup_file.absolute()
        try:
            _private(path.parent, directory=True)
            _private(path)
            with (
                closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)) as db,
                db,
            ):
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                binding = db.execute(
                    "SELECT instance_id,device_id FROM binding WHERE id=1"
                ).fetchone()
                if binding != (instance_id, device_id):
                    raise OutboxError("outbox_binding_mismatch")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {1, OUTBOX_VERSION}:
                    raise OutboxError("unsupported_outbox_version")
                cls._check_base_schema(db)
                if version == OUTBOX_VERSION:
                    cls._check_rejections(db)
                    return {"status": "already_current", "schema_version": OUTBOX_VERSION}
                _private(backup_file.parent, directory=True)
                cls._backup_locked(path, backup_file)
                db.execute(_REJECTIONS_SCHEMA)
                cls._check_rejections(db)
                db.execute(f"PRAGMA user_version={OUTBOX_VERSION}")
            return {"status": "upgraded", "schema_version": OUTBOX_VERSION}
        except FileExistsError:
            raise OutboxError("outbox_backup_exists") from None
        except (sqlite3.Error, OSError):
            raise OutboxError("outbox_upgrade_failed") from None

    @staticmethod
    def _backup_locked(path: Path, backup: Path) -> None:
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        # A distinct reader can back up while the migration connection excludes
        # other writers. backup() on that write connection can wait on itself.
        started = time.monotonic()

        def progress(status, remaining, total):
            if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                if time.monotonic() - started > 30:
                    raise OutboxError("outbox_backup_busy")

        with (
            closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)) as source,
            closing(sqlite3.connect(backup.as_uri() + "?mode=rw", uri=True, timeout=10)) as target,
            target,
        ):
            source.backup(target, pages=256, progress=progress, sleep=0.05)
            if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise OutboxError("outbox_backup_invalid")
        _private(backup)
        with backup.open("rb") as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(backup.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _check_base_schema(db):
        # Resolving required columns detects damaged schemas without loading any
        # event content. Never report an upgrade after merely trusting version 1.
        db.execute(
            "SELECT sequence,event_id,payload,payload_digest,status,receipt_id FROM events LIMIT 0"
        )
        db.execute("SELECT code,count FROM rejected LIMIT 0")

    @staticmethod
    def _check_rejections(db):
        invalid = db.execute(
            "SELECT 1 FROM delivery_rejections AS r LEFT JOIN events AS e "
            "ON e.event_id=r.event_id WHERE e.event_id IS NULL OR e.status!='queued' "
            "OR e.receipt_id IS NOT NULL OR r.payload_digest!=e.payload_digest "
            "OR r.code!=? LIMIT 1",
            (TERMINAL_REJECTION_CODE,),
        ).fetchone()
        if invalid is not None:
            raise OutboxError("outbox_rejection_corrupted")

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
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version == 1:
                        raise OutboxError("outbox_upgrade_required")
                    if version != OUTBOX_VERSION:
                        raise OutboxError("unsupported_outbox_version")
                    if binding is None or tuple(binding) != (self.instance_id, self.device_id):
                        raise OutboxError("outbox_binding_mismatch")
                    self._check_rejections(db)
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
            refused = db.execute(
                "SELECT code FROM delivery_rejections WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if refused is not None:
                return {
                    "event_id": event.event_id,
                    "status": "rejected",
                    "receipt_id": None,
                    "error": refused[0],
                    "retryable": False,
                }
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
                "SELECT * FROM events WHERE status='queued' AND NOT EXISTS "
                "(SELECT 1 FROM delivery_rejections "
                "WHERE delivery_rejections.event_id=events.event_id) "
                "ORDER BY sequence LIMIT ?",
                (limit,),
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

    def recent_metadata(self, *, scope_id: str, source_id: str, limit: int = 5):
        """Read a bounded, source-scoped sample; never expose or modify event content."""
        try:
            if scope_id != "global":
                canonical_uuid(scope_id)
            canonical_uuid(source_id)
            if type(limit) is not int or not 1 <= limit <= 5:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise OutboxError("invalid_metadata_query") from None
        with self._connection() as db:
            rows = db.execute(
                "SELECT sequence,event_id,payload_digest,status,receipt_id,payload "
                "FROM events WHERE json_extract(payload,'$.scope_id')=? "
                "AND json_extract(payload,'$.source_id')=? AND NOT EXISTS "
                "(SELECT 1 FROM delivery_rejections AS r WHERE r.event_id=events.event_id) "
                "ORDER BY sequence DESC LIMIT ?",
                (scope_id, source_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            try:
                # Hydrate only selected rows to validate the authoritative binding and
                # digest. SQLite's first-key JSON lookup alone cannot prove either.
                event = EventEnvelope.model_validate(parse_json(row["payload"]))
                require_current_filter(event, self.privacy)
                if (
                    (event.instance_id, event.device_id) != (self.instance_id, self.device_id)
                    or (event.scope_id, event.source_id) != (scope_id, source_id)
                    or event.event_id != row["event_id"]
                    or event.canonical_payload() != row["payload"]
                    or event.payload_digest() != row["payload_digest"]
                    or type(row["sequence"]) is not int
                    or row["sequence"] < 1
                    or row["status"] not in {"accepted", "queued"}
                ):
                    raise ValueError
                if row["status"] == "accepted":
                    canonical_uuid(row["receipt_id"])
                elif row["receipt_id"] is not None:
                    raise ValueError
            except (
                ValidationError,
                PrivacyError,
                EventError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                raise OutboxError("outbox_metadata_corrupted") from None
            result.append(
                EventMetadata(
                    row["sequence"],
                    row["event_id"],
                    row["payload_digest"],
                    row["status"],
                    row["receipt_id"],
                )
            )
        return tuple(result)

    def reject_delivery(
        self, *, instance_id: str, device_id: str, event_id: str, payload_digest: str, code: str
    ) -> None:
        if (instance_id, device_id) != (self.instance_id, self.device_id):
            raise OutboxError("receipt_binding_mismatch")
        if code != TERMINAL_REJECTION_CODE:
            raise OutboxError("invalid_rejection_code")
        try:
            canonical_uuid(event_id)
        except (ValueError, TypeError, AttributeError):
            raise OutboxError("invalid_rejection_identity") from None
        with self._connection(writing=True) as db:
            row = db.execute(
                "SELECT payload_digest,status,receipt_id,payload FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if (
                row is None
                or row[0] != payload_digest
                or row[1] != "queued"
                or row[2] is not None
                or not isinstance(row[3], str)
                or hashlib.sha256(row[3].encode()).hexdigest() != payload_digest
            ):
                raise OutboxError("rejection_conflict")
            db.execute(
                "INSERT OR IGNORE INTO delivery_rejections VALUES (?,?,?)",
                (event_id, payload_digest, code),
            )

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
            if db.execute(
                "SELECT 1 FROM delivery_rejections WHERE event_id=?", (event_id,)
            ).fetchone():
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
            terminal = dict(
                db.execute("SELECT code,COUNT(*) FROM delivery_rejections GROUP BY code")
            )
            for code, count in terminal.items():
                rejected[code] = rejected.get(code, 0) + count
        return {
            "queued": counts.get("queued", 0) - sum(terminal.values()),
            "accepted": counts.get("accepted", 0),
            "rejected": rejected,
        }
