"""ADR-0062: bounded GET-only processing receipts for a local metadata sample."""

import asyncio
from datetime import UTC, datetime

import httpx
from one_memory.http_client import create_async_client

from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.transport import REQUEST_DEADLINE, TransportError, _identity, _request

PROCESSING_STATES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "unknown",
        "dead",
        "cancelled",
        "waiting_for_permission",
        "waiting_for_model",
    }
)


def _timestamp(value):
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC).isoformat()


def _receipt_metadata(receipt, connection, event):
    _identity(receipt, connection)
    try:
        canonical_uuid(receipt.get("receipt_id"))
        if (
            receipt.get("status") != "accepted"
            or receipt.get("device_id") != connection.device_id
            or receipt.get("event_id") != event.event_id
            or receipt.get("payload_digest") != event.payload_digest
            or (event.receipt_id is not None and receipt["receipt_id"] != event.receipt_id)
            or receipt.get("processing_state") not in PROCESSING_STATES
        ):
            raise ValueError
        return {
            "remote_status": "accepted",
            "receipt_id": receipt["receipt_id"],
            "processing_state": receipt["processing_state"],
            "server_enqueued_at": _timestamp(receipt.get("server_enqueued_at")),
            "processing_updated_at": _timestamp(receipt.get("processing_updated_at")),
        }
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise TransportError("remote_receipt_mismatch") from None


def _failure(error):
    return {"remote_status": "unavailable", "error": error.code, "retryable": error.retryable}


async def read_processing_sample(connection, events, *, transport=None):
    """The caller has checked authorization and loaded at most five validated rows."""
    if len(events) > 5:
        raise TransportError("processing_sample_invalid")
    slots = asyncio.Semaphore(2)
    deadline = asyncio.get_running_loop().time() + REQUEST_DEADLINE
    async with create_async_client(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(10, connect=5),
        transport=transport,
    ) as client:

        async def check(event):
            result = {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "local_status": event.status,
            }
            try:
                async with asyncio.timeout_at(deadline), slots:
                    receipt = await _request(client, connection, f"/hub/v1/events/{event.event_id}")
                    result.update(_receipt_metadata(receipt, connection, event))
            except TimeoutError:
                result.update(_failure(TransportError("transport_unavailable", retryable=True)))
            except TransportError as error:
                if error.code == "event_not_found":
                    if event.status == "queued":
                        result["remote_status"] = "no_receipt"
                    else:
                        result.update(_failure(TransportError("remote_receipt_missing")))
                else:
                    result.update(_failure(error))
            return result

        checked = await asyncio.gather(*(check(event) for event in events))
    failures = sum(item["remote_status"] == "unavailable" for item in checked)

    def latest(field, *, completed=False):
        values = [
            item[field]
            for item in checked
            if item.get(field) is not None
            and (not completed or item.get("processing_state") == "completed")
        ]
        return max(values, key=datetime.fromisoformat) if values else None

    return {
        "status": "unavailable"
        if failures and failures == len(checked)
        else "partial"
        if failures
        else "checked",
        "sampling": "latest_local_sequence_for_configured_source",
        "limit": 5,
        "checked_at": datetime.now(UTC).isoformat(),
        "events": checked,
        "latest_known_enqueued_at_in_sample": latest("server_enqueued_at"),
        "latest_known_completed_update_at_in_sample": latest(
            "processing_updated_at", completed=True
        ),
    }
