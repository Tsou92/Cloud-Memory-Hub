"""Administrator correction of a distilled memory inside one project scope.

The pinned SDK exposes an unscoped ``update(memory_id, content)``. Calling it
directly from the console would let a request edit a record belonging to
another project, so every correction re-reads the record and requires its
``user_id`` to equal this scope's namespace before writing. A record that is
not in scope answers exactly like a missing one.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_memory_hub.remote_access import AccessError


class CorrectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    scope_id: str
    record_id: str = Field(strict=True, pattern=r"^[A-Za-z0-9_:-]{1,200}$")
    content: str = Field(strict=True, min_length=1, max_length=16384, repr=False)

    @field_validator("scope_id")
    @classmethod
    def scope(cls, value):
        from cloud_memory_hub.events import canonical_uuid

        return "global" if value == "global" else canonical_uuid(value)

    @field_validator("content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty_content")
        return value


async def correct_memory(console, request):
    """Replace the body of one active memory, after proving it is in scope."""
    await console.require_scope(request.scope_id)
    namespace = f"cmh:{console.access.instance_id}:{request.scope_id}"
    bridge = console.reads.bridge
    try:
        current = await bridge.get_sdk_memory(request.record_id)
    except Exception:
        raise AccessError("correct_unavailable", 503) from None
    if current is None or current.user_id != namespace:
        raise AccessError("record_not_found", 404)
    try:
        await bridge.update(request.record_id, request.content)
    except Exception:
        raise AccessError("correct_unavailable", 503) from None
    # Re-read so the caller sees what was actually stored, not what was sent.
    try:
        updated = await bridge.get_sdk_memory(request.record_id)
    except Exception:
        raise AccessError("correct_unavailable", 503) from None
    if updated is None or updated.user_id != namespace:
        raise AccessError("correct_unavailable", 503)
    return {
        "schema_version": 1,
        "scope_id": request.scope_id,
        "record_id": request.record_id,
        "content": updated.content,
        "state": "corrected",
    }
