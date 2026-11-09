"""NAS device authority only; local host/source grants remain upstream.

See ADR-0004 and ADR-0017. The intake extension now archives filtered source
events alongside receipts; device credentials remain hashed. Callers must
recheck authorization before publishing long work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

Permission = Literal["events:write", "memory:read", "history:read", "sync:read"]
MAX_REVISION = 2**53 - 1
TOKEN_PATTERN = re.compile(r"cmhdev_v1_([0-9a-f-]{36})_([A-Za-z0-9_-]{43})\Z")


class AccessError(Exception):
    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class NamedResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    display_name: str = Field(min_length=1, max_length=80, strict=True)

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if value != value.strip() or not value.isprintable():
            raise ValueError("invalid_display_name")
        return value


class ScopeGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope_id: str
    permissions: tuple[Permission, ...] = Field(min_length=1, max_length=4)

    @field_validator("scope_id")
    @classmethod
    def valid_scope(cls, value: str) -> str:
        if value != "global":
            parsed = UUID(value)
            if parsed.version != 4 or str(parsed) != value:
                raise ValueError("invalid_scope")
        return value

    @field_validator("permissions")
    @classmethod
    def distinct_permissions(cls, value: tuple[Permission, ...]) -> tuple[Permission, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate_permission")
        return tuple(sorted(value))


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(strict=True, ge=1, le=MAX_REVISION)


class GrantRequest(RevisionRequest):
    grants: tuple[ScopeGrant, ...] = Field(max_length=256)

    @field_validator("grants")
    @classmethod
    def distinct_scopes(cls, value: tuple[ScopeGrant, ...]) -> tuple[ScopeGrant, ...]:
        if len({grant.scope_id for grant in value}) != len(value):
            raise ValueError("duplicate_scope")
        return value


class IssuedCredential(BaseModel):
    model_config = ConfigDict(frozen=True)
    device: dict
    credential: SecretStr


class DeviceAccessStore:
    def __init__(self, path: Path, *, require_existing: bool = False) -> None:
        self.path = path.absolute()
        try:
            if not require_existing:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._check_private(self.path.parent, directory=True)
                descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            self._check_private(self.path.parent, directory=True)
            self._check_private(self.path)
        except OSError:
            raise AccessError("access_store_unavailable", 503) from None
        with self._connection(writing=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if require_existing:
                    raise AccessError("access_store_unrecognized", 503)
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise AccessError("access_store_unrecognized", 503)
                for statement in (
                    "CREATE TABLE instance (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "instance_id TEXT NOT NULL)",
                    "CREATE TABLE projects (project_id TEXT PRIMARY KEY, "
                    "display_name TEXT NOT NULL, created_at TEXT NOT NULL)",
                    "CREATE TABLE devices (device_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
                    "credential_hash TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), "
                    "status TEXT NOT NULL CHECK(status IN ('active','revoked')), "
                    "created_at TEXT NOT NULL)",
                    "CREATE TABLE grants (device_id TEXT NOT NULL REFERENCES devices(device_id), "
                    "scope_id TEXT NOT NULL REFERENCES projects(project_id), "
                    "permissions TEXT NOT NULL, PRIMARY KEY(device_id,scope_id))",
                ):
                    db.execute(statement)
                db.execute("INSERT INTO instance VALUES (1, ?)", (str(uuid4()),))
                db.execute(
                    "INSERT INTO projects VALUES ('global', 'Global preferences', ?)", (_now(),)
                )
                db.execute("PRAGMA user_version=1")
                version = 1
            elif version not in {1, 2, 3}:
                raise AccessError("access_store_version_unsupported", 503)
            # Validate existing schema too; never recreate a partly damaged store.
            row = db.execute("SELECT instance_id FROM instance WHERE id=1").fetchone()
            try:
                valid_instance = row is not None and str(UUID(row[0])) == row[0]
            except (ValueError, TypeError, AttributeError):
                valid_instance = False
            if not valid_instance:
                raise AccessError("access_store_unrecognized", 503)
            self.instance_id = row[0]
            self.schema_version = version
            if version in {2, 3}:
                binding = db.execute("SELECT ledger_id FROM cmh_governance_binding").fetchall()
                if len(binding) != 1:
                    raise AccessError("access_store_unrecognized", 503)
                db.execute(
                    "SELECT scope_id, checkpoint, signature FROM cmh_governance_floors LIMIT 0"
                )
            db.execute("SELECT device_id, credential_hash, revision, status FROM devices LIMIT 0")
            db.execute("SELECT project_id, display_name, created_at FROM projects LIMIT 0")
            db.execute("SELECT device_id, scope_id, permissions FROM grants LIMIT 0")
            if (
                not db.execute("SELECT 1 FROM projects WHERE project_id='global'").fetchone()
                or db.execute("PRAGMA foreign_key_check").fetchone()
            ):
                raise AccessError("access_store_unrecognized", 503)

    @staticmethod
    def _check_private(path: Path, *, directory: bool = False) -> None:
        info = path.lstat()
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (
            not correct_type
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or not directory
            and info.st_nlink != 1
        ):
            raise AccessError("access_store_unsafe", 503)

    @contextmanager
    def _connection(self, *, writing: bool = False):
        try:
            self._check_private(self.path.parent, directory=True)
            self._check_private(self.path)
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE" if writing else "BEGIN")
                try:
                    if hasattr(self, "instance_id"):
                        version = db.execute("PRAGMA user_version").fetchone()[0]
                        instance = db.execute(
                            "SELECT instance_id FROM instance WHERE id=1"
                        ).fetchone()
                        if (
                            version != self.schema_version
                            or instance is None
                            or instance[0] != self.instance_id
                        ):
                            raise AccessError("access_store_changed", 503)
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            raise AccessError("access_store_unavailable", 503) from None

    def create_project(self, request: NamedResource) -> dict:
        project = {
            "project_id": str(uuid4()),
            "display_name": request.display_name,
            "created_at": _now(),
        }
        with self._connection(writing=True) as db:
            db.execute(
                "INSERT INTO projects VALUES (:project_id, :display_name, :created_at)", project
            )
        return project

    def list_projects(self) -> list[dict]:
        with self._connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM projects WHERE project_id!='global' "
                    "ORDER BY created_at, project_id"
                )
            ]

    def create_device(self, request: NamedResource) -> IssuedCredential:
        device_id = str(uuid4())
        credential = _new_credential(device_id)
        with self._connection(writing=True) as db:
            db.execute(
                "INSERT INTO devices VALUES (?, ?, ?, 1, 'active', ?)",
                (device_id, request.display_name, _digest(credential), _now()),
            )
            receipt = self._receipt(db, device_id)
        return IssuedCredential(device=receipt, credential=SecretStr(credential))

    def list_devices(self) -> list[dict]:
        with self._connection() as db:
            return [
                self._receipt(db, row[0])
                for row in db.execute(
                    "SELECT device_id FROM devices ORDER BY created_at, device_id"
                )
            ]

    def replace_grants(self, device_id: str, request: GrantRequest) -> dict:
        with self._connection(writing=True) as db:
            self._require_revision(db, device_id, request.expected_revision)
            for grant in request.grants:
                if not db.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (grant.scope_id,)
                ).fetchone():
                    raise AccessError("unknown_scope", 422)
            db.execute("DELETE FROM grants WHERE device_id=?", (device_id,))
            db.executemany(
                "INSERT INTO grants VALUES (?, ?, ?)",
                [
                    (device_id, grant.scope_id, json.dumps(grant.permissions))
                    for grant in request.grants
                ],
            )
            db.execute("UPDATE devices SET revision=revision+1 WHERE device_id=?", (device_id,))
            return self._receipt(db, device_id)

    def rotate(self, device_id: str, request: RevisionRequest) -> IssuedCredential:
        credential = _new_credential(device_id)
        with self._connection(writing=True) as db:
            self._require_revision(db, device_id, request.expected_revision)
            db.execute(
                "UPDATE devices SET credential_hash=?, revision=revision+1 WHERE device_id=?",
                (_digest(credential), device_id),
            )
            receipt = self._receipt(db, device_id)
        return IssuedCredential(device=receipt, credential=SecretStr(credential))

    def revoke(self, device_id: str, request: RevisionRequest) -> dict:
        with self._connection(writing=True) as db:
            self._require_revision(db, device_id, request.expected_revision)
            db.execute(
                "UPDATE devices SET status='revoked', revision=revision+1 WHERE device_id=?",
                (device_id,),
            )
            db.execute("DELETE FROM grants WHERE device_id=?", (device_id,))
            return self._receipt(db, device_id)

    def authenticate(self, credential: str) -> dict:
        with self._connection() as db:
            return self._authenticate(db, credential)

    def require_permission(self, credential: str, scope_id: str, permission: Permission) -> dict:
        with self._connection() as db:
            receipt = self._authenticate(db, credential)
            if not any(
                grant["scope_id"] == scope_id and permission in grant["permissions"]
                for grant in receipt["grants"]
            ):
                raise AccessError("scope_not_allowed", 403)
            return receipt

    def _authenticate(self, db: sqlite3.Connection, credential: str) -> dict:
        match = TOKEN_PATTERN.fullmatch(credential)
        if match is None:
            raise AccessError("invalid_device_credential", 401)
        row = db.execute(
            "SELECT credential_hash, status FROM devices WHERE device_id=?", (match[1],)
        ).fetchone()
        if row and (not isinstance(row[0], str) or not re.fullmatch(r"[0-9a-f]{64}", row[0])):
            raise AccessError("access_store_unrecognized", 503)
        valid = hmac.compare_digest(_digest(credential), row[0] if row else "0" * 64)
        if not valid or row is None or row[1] != "active":
            raise AccessError("invalid_device_credential", 401)
        return self._receipt(db, match[1])

    @staticmethod
    def _require_revision(db: sqlite3.Connection, device_id: str, revision: int) -> None:
        row = db.execute(
            "SELECT revision, status FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            raise AccessError("device_not_found", 404)
        if row[1] != "active":
            raise AccessError("device_revoked", 409)
        if row[0] != revision or row[0] >= MAX_REVISION:
            raise AccessError("revision_conflict", 409)

    @staticmethod
    def _receipt(db: sqlite3.Connection, device_id: str) -> dict:
        row = db.execute(
            "SELECT device_id, display_name, revision, status, created_at "
            "FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        try:
            grants = [
                ScopeGrant(scope_id=grant[0], permissions=json.loads(grant[1])).model_dump(
                    mode="json"
                )
                for grant in db.execute(
                    "SELECT scope_id, permissions FROM grants WHERE device_id=? ORDER BY scope_id",
                    (device_id,),
                )
            ]
        except (ValidationError, ValueError, TypeError):
            raise AccessError("access_store_unrecognized", 503) from None
        return {**dict(row), "grants": grants}


def _new_credential(device_id: str) -> str:
    return f"cmhdev_v1_{device_id}_{secrets.token_urlsafe(32)}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
