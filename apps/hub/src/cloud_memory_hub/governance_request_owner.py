"""Pure metadata proof of a Hub queue job's registered source ownership."""

from dataclasses import dataclass

from cloud_memory_hub.events import canonical_uuid, split_request_identity
from cloud_memory_hub.governance_journal import GovernanceError


@dataclass(frozen=True)
class RequestIdentity:
    job_id: str
    request_id: str
    instance_id: str
    source_id: str
    device_id: str
    scope_id: str
    event_id: str
    birth_id: str | None = None


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("invalid_identifier")
    return canonical_uuid(value)


def verify_request_owner(job, source, *, instance_id, scope_id) -> RequestIdentity:
    """Validate supplied metadata only; revoked sources still own their history.

    The caller supplies rows from its existing read snapshot. This does not
    authenticate an upload, inspect payloads, or establish any SDK effect.
    """
    try:
        instance_id = _uuid(instance_id)
        if scope_id != "global":
            scope_id = _uuid(scope_id)
        job_id = _uuid(job["job_id"])
        source_id = _uuid(source["source_id"])
        device_id = _uuid(source["device_id"])
        if source["scope_id"] != scope_id or job["source_key"] != "cmh:" + source_id:
            raise ValueError("source_owner_mismatch")
        request_id = job["idempotency_key"]
        actual_instance, actual_device, event_id, birth_id = split_request_identity(request_id)
        if (actual_instance, actual_device) != (instance_id, device_id):
            raise ValueError("request_owner_mismatch")
        return RequestIdentity(
            job_id, request_id, instance_id, source_id, device_id, scope_id, event_id, birth_id
        )
    except (ValueError, TypeError, KeyError, IndexError):
        raise GovernanceError("governance_request_owner_unproven") from None
