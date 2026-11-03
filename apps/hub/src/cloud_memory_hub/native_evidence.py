"""Strict shared MemoryNode evidence parsing without lossy normalization."""

from dataclasses import fields

from hy_memory.coding.record_origin import verified_origin
from hy_memory.models.memory import MemoryNode

from cloud_memory_hub.sdk_reconcile import _ids

_FIELDS = {item.name for item in fields(MemoryNode)} - {"embedding"}
_ORIGIN = {"origin_request_id", "origin_digest"}


def validate_native_record(item, kind, request=None, *, error="lineage_memory"):
    if not isinstance(item, dict) or set(item) != {"kind", "id", "record"}:
        raise ValueError(error + "_record_invalid")
    record = item["record"]
    if (
        item["kind"] != kind
        or not isinstance(record, dict)
        or set(record) not in (_FIELDS, _FIELDS - _ORIGIN)
        or item["id"] != record.get("node_id")
    ):
        raise ValueError(error + "_record_invalid")
    _ids([item["id"]])
    # Reuse the native schema; reject lossy coercions rather than normalize evidence.
    try:
        normalized = MemoryNode.from_dict(record).to_dict()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ValueError(error + "_record_invalid") from exc
    normalized.pop("embedding")
    for name in _ORIGIN - record.keys():
        normalized.pop(name)
    if normalized != record:
        raise ValueError(error + "_record_invalid")
    verified_origin(kind, record)
    if request is not None:
        if record["user_id"] != request.user_id:
            raise ValueError(error + "_scope_mismatch")
        if record.get("origin_request_id") == request.request_id:
            raise ValueError(error + "_self_reference")

    return record
