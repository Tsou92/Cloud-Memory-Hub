"""Atomic source progress in the existing outbox; no unfiltered body is stored."""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import stat
from contextlib import contextmanager
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from one_memory.imports.adapters import SourceCursor
from pydantic import ValidationError

from cloud_memory_hub.events import prepare_event
from cloud_memory_hub.privacy import CredentialFilter


class CaptureError(Exception):
    pass


def read_capture_file(path, *, limit):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise CaptureError("unsafe_capture_file")
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise CaptureError("capture_file_too_large")
            return value
    except OSError:
        raise CaptureError("capture_file_unavailable") from None


@contextmanager
def capture_lock(outbox):
    # Validate the private state directory before creating any sibling file.
    with outbox._connection():
        pass
    descriptor = None
    try:
        descriptor = os.open(
            outbox.path.parent / "capture.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise CaptureError("unsafe_capture_lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CaptureError("capture_already_running") from None
        yield
    except OSError:
        raise CaptureError("capture_lock_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class CaptureStore:
    def __init__(self, outbox, source_id, binding_digest, root_identity, *, initialize=False):
        self.outbox = outbox
        self.source_id = source_id
        self.binding_digest = binding_digest
        self.root_identity = root_identity
        self.changed_times = {}
        self.marker = f"cmh-capture-v1:{outbox.instance_id}:{outbox.device_id}".encode()
        with outbox._connection(writing=initialize) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_schema'"
            ).fetchone()
            if not exists and not initialize:
                raise CaptureError("capture_not_initialized")
            key_path = outbox.path.parent / "capture-key"
            if not exists and initialize:
                # A failed initialization can leave a key, never overwrite it.
                try:
                    descriptor = os.open(
                        key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
                    )
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(Fernet.generate_key())
                        stream.flush()
                        os.fsync(stream.fileno())
            self.key = read_capture_file(key_path, limit=44)
            try:
                self.cipher = Fernet(self.key)
                self.mac_key = base64.urlsafe_b64decode(self.key)
            except ValueError:
                raise CaptureError("capture_key_invalid") from None
            self.privacy = CredentialFilter(
                (*outbox.privacy.known_credentials, self.key.decode("ascii"))
            )
            if not exists:
                # execute(), not executescript(): preserve the enclosing transaction.
                for statement in (
                    "CREATE TABLE capture_schema "
                    "(version INTEGER NOT NULL, key_check BLOB NOT NULL)",
                    "CREATE TABLE capture_sources (source_id TEXT PRIMARY KEY, "
                    "binding_digest TEXT NOT NULL, root_identity TEXT NOT NULL)",
                    "CREATE TABLE capture_cursors "
                    "(source_id TEXT NOT NULL, file_key TEXT NOT NULL, "
                    "sealed BLOB NOT NULL, PRIMARY KEY(source_id,file_key))",
                    "CREATE TABLE capture_records "
                    "(source_id TEXT NOT NULL, native_key TEXT NOT NULL, "
                    "content_digest TEXT NOT NULL, revision INTEGER NOT NULL, "
                    "event_id TEXT NOT NULL, "
                    "PRIMARY KEY(source_id,native_key))",
                ):
                    db.execute(statement)
                db.execute(
                    "INSERT INTO capture_schema VALUES (1,?)", (self.cipher.encrypt(self.marker),)
                )
            rows = db.execute("SELECT version,key_check FROM capture_schema").fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                raise CaptureError("unsupported_capture_version")
            if self._decrypt(rows[0][1]) != self.marker:
                raise CaptureError("capture_key_mismatch")
            if initialize:
                db.execute(
                    "INSERT OR IGNORE INTO capture_sources VALUES (?,?,?)",
                    (source_id, binding_digest, root_identity),
                )
            self._binding(db)

    def _decrypt(self, value):
        try:
            return self.cipher.decrypt(value)
        except (InvalidToken, TypeError):
            raise CaptureError("capture_state_corrupted") from None

    def _binding(self, db):
        row = db.execute(
            "SELECT binding_digest,root_identity FROM capture_sources WHERE source_id=?",
            (self.source_id,),
        ).fetchone()
        if row is None or tuple(row) != (self.binding_digest, self.root_identity):
            raise CaptureError("capture_binding_mismatch")

    def _identity(self, kind, value):
        message = json.dumps([self.marker.decode(), self.source_id, kind, value]).encode()
        return hmac.new(self.mac_key, message, hashlib.sha256).hexdigest()

    def cursors(self):
        result = {}
        with self.outbox._connection() as db:
            self._binding(db)
            for row in db.execute(
                "SELECT file_key,sealed FROM capture_cursors WHERE source_id=?", (self.source_id,)
            ):
                try:
                    payload = json.loads(self._decrypt(row[1]))
                    cursor = SourceCursor.model_validate(payload["cursor"])
                    if payload["source_id"] != self.source_id or cursor.file_key != row[0]:
                        raise ValueError()
                    changed_ns = payload["changed_ns"]
                    if type(changed_ns) is not int or changed_ns < 0:
                        raise ValueError()
                except (ValueError, KeyError, TypeError, ValidationError):
                    raise CaptureError("capture_state_corrupted") from None
                result[cursor.file_key] = cursor
                self.changed_times[cursor.file_key] = changed_ns
        return result

    def _save_cursor(self, db, cursor):
        payload = json.dumps(
            {
                "source_id": self.source_id,
                "cursor": cursor.model_dump(mode="json"),
                "changed_ns": self.changed_times[cursor.file_key],
            }
        ).encode()
        db.execute(
            "INSERT INTO capture_cursors VALUES (?,?,?) ON CONFLICT(source_id,file_key) "
            "DO UPDATE SET sealed=excluded.sealed",
            (self.source_id, cursor.file_key, self.cipher.encrypt(payload)),
        )

    def advance(self, cursor):
        with self.outbox._connection(writing=True) as db:
            self._binding(db)
            self._save_cursor(db, cursor)

    def record(self, evidence, cursor, *, scope_id, content):
        # Filter before computing any persistent digest. Raw native identities
        # (including upstream's text-derived fallback) only enter a keyed MAC.
        filtered = self.privacy.protect(content).value
        semantic = {
            "content": filtered,
            "occurred_at": evidence.created_at.isoformat() if evidence.created_at else None,
        }
        digest = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        native_key = self._identity("record", evidence.idempotency_key)
        record_id = str(UUID(bytes=bytes.fromhex(native_key)[:16], version=4))
        conversation_key = self._identity(
            "conversation", [evidence.workspace, evidence.conversation_id]
        )
        conversation_id = str(UUID(bytes=bytes.fromhex(conversation_key)[:16], version=4))
        with self.outbox._connection(writing=True) as db:
            self._binding(db)
            previous = db.execute(
                "SELECT content_digest,revision FROM capture_records "
                "WHERE source_id=? AND native_key=?",
                (self.source_id, native_key),
            ).fetchone()
            changed = previous is None or previous[0] != digest
            if changed:
                event = prepare_event(
                    {
                        "event_id": str(uuid4()),
                        "instance_id": self.outbox.instance_id,
                        "device_id": self.outbox.device_id,
                        "source_id": self.source_id,
                        "scope_id": scope_id,
                        "conversation_id": conversation_id,
                        "record_id": record_id,
                        "revision": 1 if previous is None else previous[1] + 1,
                        "kind": "conversation_turn",
                        "occurred_at": evidence.created_at,
                        "content": filtered,
                    },
                    self.outbox.privacy,
                )
                self.outbox._insert_event(db, event)
                db.execute(
                    "INSERT INTO capture_records VALUES (?,?,?,?,?) "
                    "ON CONFLICT(source_id,native_key) DO UPDATE SET "
                    "content_digest=excluded.content_digest,revision=excluded.revision,"
                    "event_id=excluded.event_id",
                    (self.source_id, native_key, digest, event.revision, event.event_id),
                )
            self._save_cursor(db, cursor)
        return changed
