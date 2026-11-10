"""ADR-0065: one-use, project-scoped enrollment in the existing access database."""

from __future__ import annotations

import base64
import hmac
import re
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.remote_access import (
    AccessError,
    GrantRequest,
    NamedResource,
    _digest,
)
from cloud_memory_hub.remote_ingest import SourceRegistration
from cloud_memory_hub.transport import RemoteConnection

PAIRING_TTL_SECONDS = 600
PAIRING_PERMISSIONS = ("events:write", "memory:read", "history:read")
PAIRING_ADAPTER = "cloud-memory-hub.agent"
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class PairingRequest(NamedResource):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    scope_id: str = Field(strict=True)

    @field_validator("scope_id")
    @classmethod
    def project_scope(cls, value):
        # Global preferences require separate authorization, never implicit enrollment.
        return canonical_uuid(value)


class PairingClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    pairing_id: str = Field(strict=True)
    invitation_secret: SecretStr = Field(repr=False)
    claimant_secret: SecretStr = Field(repr=False)

    @field_validator("pairing_id")
    @classmethod
    def identity(cls, value):
        return canonical_uuid(value)

    @field_validator("invitation_secret", "claimant_secret", mode="before")
    @classmethod
    def random_secret(cls, value):
        if not isinstance(value, str) or _SECRET.fullmatch(value) is None:
            raise ValueError("invalid_pairing_secret")
        decoded = base64.urlsafe_b64decode(value + "=")
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
            raise ValueError("invalid_pairing_secret")
        return value


class PairingRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class IssuedPairing(BaseModel):
    model_config = ConfigDict(frozen=True)
    pairing: dict
    invitation_secret: SecretStr = Field(repr=False)


def _time(now):
    return int((now or datetime.now(UTC)).timestamp())


def _iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def _check_schema(db):
    rows = db.execute("SELECT id, version FROM cmh_pairing_schema").fetchall()
    if [tuple(row) for row in rows] != [(1, 1)]:
        raise AccessError("pairing_store_version_unsupported", 503)
    db.execute(
        "SELECT pairing_id, invitation_hash, scope_id, display_name, created_at, expires_at, "
        "state, claimant_hash, device_id, source_id, claimed_at FROM cmh_pairings LIMIT 0"
    )


def cancel_restored_pairings(db):
    """Restore never revives an old invitation or a previously recoverable claim."""
    names = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('cmh_pairing_schema','cmh_pairings')"
        )
    }
    if not names:
        return
    if names != {"cmh_pairing_schema", "cmh_pairings"}:
        raise AccessError("pairing_store_unrecognized", 503)
    _check_schema(db)
    db.execute("UPDATE cmh_pairings SET state='cancelled'")


class PairingStore:
    def __init__(self, ingest, public_origin):
        self.ingest, self.access = ingest, ingest.access
        # Reuse the exact origin contract used by the installed client, with an
        # inert validation-only identity. No request Host or forwarded header is read.
        identity = self.access.instance_id
        try:
            self.public_origin = RemoteConnection(
                origin=public_origin,
                instance_id=identity,
                device_id=identity,
                credential=f"cmhdev_v1_{identity}_{'A' * 43}",
            ).origin
        except (ValueError, httpx.InvalidURL):
            raise AccessError("pairing_origin_invalid", 503) from None
        with self.access._connection(writing=True) as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('cmh_pairing_schema','cmh_pairings')"
                )
            }
            if not names:
                db.execute(
                    "CREATE TABLE cmh_pairing_schema (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "version INTEGER NOT NULL)"
                )
                db.execute("""CREATE TABLE cmh_pairings (
                    pairing_id TEXT PRIMARY KEY,
                    invitation_hash TEXT NOT NULL,
                    scope_id TEXT NOT NULL REFERENCES projects(project_id),
                    display_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','claimed','cancelled')),
                    claimant_hash TEXT,
                    device_id TEXT REFERENCES devices(device_id),
                    source_id TEXT REFERENCES cmh_sources(source_id),
                    claimed_at INTEGER,
                    CHECK(state!='pending' OR (claimant_hash IS NULL AND device_id IS NULL
                        AND source_id IS NULL AND claimed_at IS NULL)),
                    CHECK(state!='claimed' OR (claimant_hash IS NOT NULL AND device_id IS NOT NULL
                        AND source_id IS NOT NULL AND claimed_at IS NOT NULL))
                )""")
                db.execute("INSERT INTO cmh_pairing_schema VALUES (1,1)")
            elif names != {"cmh_pairing_schema", "cmh_pairings"}:
                raise AccessError("pairing_store_unrecognized", 503)
            _check_schema(db)

    def create(self, request: PairingRequest, *, now=None):
        created_at = _time(now)
        secret = secrets.token_urlsafe(32)
        pairing_id = str(uuid4())
        with self.ingest._database(writing=True) as db:
            _check_schema(db)
            if not db.execute(
                "SELECT 1 FROM projects WHERE project_id=?", (request.scope_id,)
            ).fetchone():
                raise AccessError("unknown_scope", 422)
            db.execute(
                "INSERT INTO cmh_pairings VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, "
                "NULL, NULL)",
                (
                    pairing_id,
                    _digest(secret),
                    request.scope_id,
                    request.display_name,
                    created_at,
                    created_at + PAIRING_TTL_SECONDS,
                ),
            )
        return IssuedPairing(
            pairing={
                "pairing_id": pairing_id,
                "origin": self.public_origin,
                "expires_at": _iso(created_at + PAIRING_TTL_SECONDS),
            },
            invitation_secret=SecretStr(secret),
        )

    def get(self, pairing_id, *, now=None):
        with self.access._connection() as db:
            _check_schema(db)
            return self._receipt(self._row(db, pairing_id), _time(now))

    def revoke(self, pairing_id, *, now=None):
        with self.access._connection(writing=True) as db:
            _check_schema(db)
            self._row(db, pairing_id)
            db.execute(
                "UPDATE cmh_pairings SET state='cancelled' WHERE pairing_id=?", (pairing_id,)
            )
            return self._receipt(self._row(db, pairing_id), _time(now))

    def claim(self, request: PairingClaim, *, now=None):
        invitation = request.invitation_secret.get_secret_value()
        claimant = request.claimant_secret.get_secret_value()
        with self.ingest._database(writing=True) as db:
            _check_schema(db)
            row = db.execute(
                "SELECT * FROM cmh_pairings WHERE pairing_id=?", (request.pairing_id,)
            ).fetchone()
            if not hmac.compare_digest(
                _digest(invitation), row["invitation_hash"] if row else "0" * 64
            ):
                raise AccessError("pairing_invalid_or_expired", 401)
            if row is None or row["state"] == "cancelled":
                raise AccessError("pairing_invalid_or_expired", 401)
            if row["state"] == "claimed":
                if not hmac.compare_digest(_digest(claimant), row["claimant_hash"]):
                    raise AccessError("pairing_already_claimed", 409)
                return self._recover(db, row, claimant)
            timestamp = _time(now)
            if row["state"] != "pending" or row["expires_at"] <= timestamp:
                raise AccessError("pairing_invalid_or_expired", 401)
            device_id = str(uuid4())
            credential = f"cmhdev_v1_{device_id}_{claimant}"
            self.access._create_device(
                db, NamedResource(display_name=row["display_name"]), device_id, credential
            )
            self.access._replace_grants(
                db,
                device_id,
                GrantRequest(
                    expected_revision=1,
                    grants=[{"scope_id": row["scope_id"], "permissions": PAIRING_PERMISSIONS}],
                ),
            )
            source = self.ingest._create_source(
                db,
                SourceRegistration(
                    display_name="Cloud Memory Hub Agent",
                    device_id=device_id,
                    scope_id=row["scope_id"],
                    adapter_id=PAIRING_ADAPTER,
                ),
            )
            db.execute(
                "UPDATE cmh_pairings SET state='claimed', claimant_hash=?, device_id=?, "
                "source_id=?, claimed_at=? WHERE pairing_id=?",
                (_digest(claimant), device_id, source["source_id"], timestamp, request.pairing_id),
            )
            return self._recover(db, self._row(db, request.pairing_id), claimant)

    def _recover(self, db, row, claimant):
        credential = f"cmhdev_v1_{row['device_id']}_{claimant}"
        try:
            device = self.access._authenticate(db, credential)
        except AccessError as error:
            if error.code != "invalid_device_credential":
                raise
            raise AccessError("pairing_result_unavailable", 409) from None
        permitted = any(
            grant["scope_id"] == row["scope_id"]
            and set(PAIRING_PERMISSIONS).issubset(grant["permissions"])
            for grant in device["grants"]
        )
        if not permitted or not self.ingest._source_allowed(
            db, device, row["source_id"], row["scope_id"]
        ):
            raise AccessError("pairing_result_unavailable", 409)
        return {
            "schema_version": 1,
            "kind": "cloud-memory-hub-pairing",
            "origin": self.public_origin,
            "instance_id": self.access.instance_id,
            "device_id": row["device_id"],
            "scope_id": row["scope_id"],
            "source_id": row["source_id"],
        }

    @staticmethod
    def _row(db, pairing_id):
        row = db.execute("SELECT * FROM cmh_pairings WHERE pairing_id=?", (pairing_id,)).fetchone()
        if row is None:
            raise AccessError("pairing_not_found", 404)
        return row

    @staticmethod
    def _receipt(row, now):
        return {
            "pairing_id": row["pairing_id"],
            "scope_id": row["scope_id"],
            "display_name": row["display_name"],
            "state": "expired"
            if row["state"] == "pending" and row["expires_at"] <= now
            else row["state"],
            "expires_at": _iso(row["expires_at"]),
            "claimed_at": _iso(row["claimed_at"]),
            "device_id": row["device_id"],
            "source_id": row["source_id"],
        }
