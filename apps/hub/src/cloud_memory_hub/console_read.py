"""Administrator console projections over existing Hub authority and scoped reads."""

import json
import os
import re
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from cloud_memory_hub.console_dashboard import dashboard
from cloud_memory_hub.privacy import CredentialFilter, PrivacyError
from cloud_memory_hub.read_contract import (
    READ_RESPONSE_BYTES,
    ReadItem,
    ReadOperation,
    ScopeRequest,
)
from cloud_memory_hub.remote_access import AccessError


class ConsolePageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    limit: int = Field(default=50, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=2**31 - 1)


class ConsoleReadReply(ScopeRequest):
    schema_version: Literal[1] = 1
    authority: Literal["administrator"] = "administrator"
    instance_id: str
    operation: ReadOperation
    content_trust: Literal["untrusted_history"] = "untrusted_history"
    items: tuple[ReadItem, ...] = Field(max_length=10)
    returned_count: int = Field(ge=0, le=10)
    next_offset: int | None = Field(default=None, ge=0)
    candidate_limit_reached: bool = False


class ConsoleRead:
    """Called only after require_admin; never registers or impersonates a device."""

    def __init__(self, access, ingest, reads, privacy: CredentialFilter):
        self.access = access
        self.ingest = ingest
        self.reads = reads
        self.privacy = privacy
        try:
            self.version = version("cloud-memory-hub")
        except PackageNotFoundError:
            self.version = None
        revision = os.environ.get("CMH_REVISION", "")
        self.build_revision = revision if re.fullmatch(r"[0-9a-f]{7,40}", revision) else None

    def request_privacy(self, session_credential=""):
        known = self.privacy.known_credentials
        try:
            return CredentialFilter(
                (*known, session_credential) if session_credential else known,
                known_credentials_source=self.privacy.known_credentials_source,
            )
        except PrivacyError:
            raise AccessError("console_unavailable", 503) from None

    @staticmethod
    def _project(db, scope_id):
        row = db.execute(
            "SELECT project_id AS scope_id,display_name,created_at FROM projects "
            "WHERE project_id=?",
            (scope_id,),
        ).fetchone()
        if row is None:
            raise AccessError("console_scope_not_found", 404)
        return dict(row)

    def _require_scope(self, scope_id):
        with self.access._connection() as db:
            return self._project(db, scope_id)

    async def require_scope(self, scope_id):
        try:
            validated = ScopeRequest(scope_id=scope_id)
        except ValueError:
            raise AccessError("invalid_request", 422) from None
        return await run_in_threadpool(self._require_scope, validated.scope_id)

    def _metadata(self, value, privacy):
        try:
            if len(json.dumps(value, ensure_ascii=False).encode()) > READ_RESPONSE_BYTES:
                raise AccessError("read_response_too_large", 503)
            if privacy.protect(value).value != value:
                raise AccessError("console_metadata_requires_review", 503)
            return value
        except PrivacyError:
            raise AccessError("console_metadata_requires_review", 503) from None

    def _bootstrap(self, request, privacy):
        with self.access._connection() as db:
            rows = db.execute(
                "SELECT project_id AS scope_id,display_name,created_at FROM projects "
                "WHERE project_id!='global' ORDER BY created_at,project_id LIMIT ? OFFSET ?",
                (request.limit + 1, request.offset),
            ).fetchall()
            global_scope = self._project(db, "global")
        return self._metadata(
            {
                "schema_version": 1,
                "product": "Cloud Memory Hub",
                "mode": "hub",
                "instance_id": self.access.instance_id,
                "version": self.version,
                "build_revision": self.build_revision,
                "features": ["scoped_console_reads", "scoped_console_ask"],
                "projects": [dict(row) for row in rows[: request.limit]],
                "next_offset": request.offset + request.limit
                if len(rows) > request.limit
                else None,
                "global_scope": {key: global_scope[key] for key in ("scope_id", "display_name")},
            },
            privacy,
        )

    async def bootstrap(self, request, *, session_credential=""):
        return await run_in_threadpool(
            self._bootstrap, request, self.request_privacy(session_credential)
        )

    def _summary(self, request, privacy):
        with self.ingest._database() as db:
            project = self._project(db, request.scope_id)
            total = db.execute(
                "SELECT COUNT(*) FROM cmh_sources WHERE scope_id=?", (request.scope_id,)
            ).fetchone()[0]
            sources = [
                dict(row)
                for row in db.execute(
                    "SELECT s.source_id,s.device_id,s.scope_id,s.adapter_id,s.display_name,"
                    "s.revision,s.status,d.display_name AS device_display_name,"
                    "d.status AS device_status FROM cmh_sources s "
                    "LEFT JOIN devices d ON s.device_id=d.device_id "
                    "WHERE s.scope_id=? ORDER BY s.source_id LIMIT ? OFFSET ?",
                    (request.scope_id, request.limit, request.offset),
                )
            ]
            if any(row["device_display_name"] is None for row in sources):
                raise AccessError("console_unavailable", 503)
        return self._metadata(
            {
                "schema_version": 1,
                "instance_id": self.access.instance_id,
                "scope": project,
                "sources": sources,
                "sources_total": total,
                "next_offset": request.offset + request.limit
                if request.offset + request.limit < total
                else None,
                "visible_memory_total": None,
                "visible_history_total": None,
                "count_state": "not_computed",
            },
            privacy,
        )

    async def summary(self, request, *, session_credential=""):
        return await run_in_threadpool(
            self._summary, request, self.request_privacy(session_credential)
        )

    async def dashboard(self, request, *, session_credential=""):
        return await run_in_threadpool(
            dashboard,
            self,
            request.scope_id,
            self.request_privacy(session_credential),
        )

    async def read(self, operation, request, *, session_credential=""):
        await self.require_scope(request.scope_id)
        privacy = self.request_privacy(session_credential)
        page = await self.reads.query(operation, request, privacy=privacy)
        result = self.reads.response(
            ConsoleReadReply,
            page,
            privacy,
            instance_id=self.access.instance_id,
            scope_id=request.scope_id,
            operation=operation,
            returned_count=len(page.items),
        )
        await self.require_scope(request.scope_id)
        return result
