"""ADR-0063: bounded administrator controls over the existing backup runtime."""

import asyncio

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.routing import Route

from cloud_memory_hub import backup_catalog, backup_runtime
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_api import read_body, require_admin, response


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    interval_minutes: int = Field(ge=1, le=525600)
    keep: int = Field(ge=1, le=365)


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


async def _control(action, *args, **kwargs):
    try:
        return await asyncio.to_thread(action, *args, **kwargs)
    except backup_runtime.BackupRuntimeError as error:
        if error.code == "backup_request_pending":
            raise AccessError("backup_request_pending", 409) from None
        raise AccessError("backup_control_unavailable", 503) from None
    except Exception:
        # File and OS errors can include private paths and serialized contents.
        raise AccessError("backup_control_unavailable", 503) from None


def backup_routes(config):
    def authorize(request):
        require_admin(request)
        if request.query_params:
            raise AccessError("invalid_request", 422)

    async def status(request: Request):
        authorize(request)
        return response(await _control(backup_runtime.status, config.backup_dir))

    async def versions(request: Request):
        authorize(request)
        try:
            summaries = await asyncio.to_thread(backup_catalog.list_catalog, config.backup_dir)
            return response({"versions": summaries})
        except backup_catalog.BackupCatalogError as error:
            if error.code in {
                "backup_storage_in_use",
                "backup_catalog_limit",
                "backup_catalog_timeout",
                "backup_catalog_invalid",
            }:
                raise AccessError(error.code, 503) from None
            raise AccessError("backup_versions_unavailable", 503) from None
        except Exception:
            raise AccessError("backup_versions_unavailable", 503) from None

    async def schedule(request: Request):
        authorize(request)
        body = await read_body(request, ScheduleRequest, max_bytes=4096)
        return response(
            await _control(backup_runtime.configure, config.backup_dir, **body.model_dump())
        )

    async def request_backup(request: Request):
        authorize(request)
        await read_body(request, BackupRequest, max_bytes=4096)
        queued = await _control(backup_runtime.request_backup, config.backup_dir)
        return response({"status": "queued", **queued}, 202)

    return [
        Route("/hub/v1/backups/status", status, methods=["GET"]),
        Route("/hub/v1/backups/versions", versions, methods=["GET"]),
        Route("/hub/v1/backups/schedule", schedule, methods=["PUT"]),
        Route("/hub/v1/backups/request", request_backup, methods=["POST"]),
    ]
