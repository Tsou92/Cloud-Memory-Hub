"""Explicit connector CLI. Network use requires send/watch and a private binding."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.events import EventError
from cloud_memory_hub.local_outbox import LocalOutbox, OutboxError
from cloud_memory_hub.privacy import MAX_CONTENT_BYTES, CredentialFilter, PrivacyError, parse_json
from cloud_memory_hub.transport import (
    RemoteConnection,
    TransportError,
    send_pending,
    watch_delivery,
)


def open_outbox(args):
    connection = RemoteConnection.from_file(args.connection_file) if args.connection_file else None
    if args.command in {"send", "watch"} and connection is None:
        raise TransportError("connection_configuration_required")
    instance_id, device_id = args.instance_id, args.device_id
    known = ()
    if connection is not None:
        if (instance_id is not None and instance_id != connection.instance_id) or (
            device_id is not None and device_id != connection.device_id
        ):
            raise TransportError("connection_binding_mismatch")
        instance_id, device_id = connection.instance_id, connection.device_id
    if instance_id is None or device_id is None:
        raise TransportError("local_identity_required")
    if args.known_credentials_file:
        data = read_private_file(args.known_credentials_file)
        if len(data) > 4096:
            raise PrivacyError("known_credentials_too_large")
        values = parse_json(data)
        if not isinstance(values, list):
            raise PrivacyError("invalid_known_credentials")
        known = tuple(values)
    if connection is not None:
        known = (*known, connection.credential.get_secret_value())
    options = dict(instance_id=instance_id, device_id=device_id, privacy=CredentialFilter(known))
    path = args.state_dir / "outbox.sqlite3"
    outbox = (
        LocalOutbox.initialize(path, **options)
        if args.command == "init"
        else LocalOutbox(path, **options)
    )
    return connection, outbox


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="cloud-memory-connector")
    parser.add_argument("command", choices=["init", "enqueue", "status", "send", "watch"])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--device-id")
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--known-credentials-file", type=Path)
    args = parser.parse_args()
    try:
        connection, outbox = open_outbox(args)
        if args.command == "enqueue":
            data = sys.stdin.buffer.read(MAX_CONTENT_BYTES + 4097)
            try:
                if len(data) > MAX_CONTENT_BYTES + 4096:
                    raise PrivacyError("event_too_large")
                raw = parse_json(data)
            except PrivacyError:
                outbox.record_rejection("privacy_rejected")
                raise
            result = outbox.enqueue(raw)
        elif args.command == "send":
            accepted = asyncio.run(send_pending(connection, outbox))
            result = {"accepted": accepted, "queued": outbox.status()["queued"]}
        elif args.command == "watch":

            async def attempt():
                current, queue = open_outbox(args)
                return await send_pending(current, queue)

            asyncio.run(watch_delivery(attempt, lambda state: print(json.dumps(state), flush=True)))
            return 0
        else:
            result = outbox.status()
        print(json.dumps(result))
        return 0
    except TransportError as error:
        print(json.dumps({"error": error.code, "retryable": error.retryable}), file=sys.stderr)
        return 3 if error.retryable else 2
    except (PrivacyError, EventError, OutboxError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print('{"error":"local_configuration_unavailable"}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
