"""Explicit connector CLI. Network use requires send/watch and a private binding."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from cloud_memory_hub.capture_store import CaptureError
from cloud_memory_hub.claude_hook import run_hook_process
from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.events import EventError
from cloud_memory_hub.local_outbox import LocalOutbox, OutboxError
from cloud_memory_hub.privacy import MAX_CONTENT_BYTES, CredentialFilter, PrivacyError, parse_json
from cloud_memory_hub.read_contract import READ_OPERATIONS
from cloud_memory_hub.source_capture import SourceConfiguration, collect_source, initialize_capture
from cloud_memory_hub.transport import (
    RemoteConnection,
    TransportError,
    read_remote,
    send_pending,
    watch_delivery,
)


def open_configuration(args):
    connection = RemoteConnection.from_file(args.connection_file) if args.connection_file else None
    if args.command in {"send", "watch", *READ_OPERATIONS} and connection is None:
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
    return connection, dict(
        instance_id=instance_id, device_id=device_id, privacy=CredentialFilter(known)
    )


def open_outbox(args):
    connection, options = open_configuration(args)
    if args.state_dir is None:
        raise TransportError("state_directory_required")
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
    parser.add_argument(
        "command",
        choices=[
            "init",
            "upgrade-outbox",
            "enqueue",
            "status",
            "send",
            "watch",
            "capture-init",
            "collect",
            "collect-watch",
            "claude-hook",
            *READ_OPERATIONS,
        ],
    )
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--instance-id")
    parser.add_argument("--device-id")
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--known-credentials-file", type=Path)
    parser.add_argument("--source-file", type=Path)
    parser.add_argument("--backup-file", type=Path)
    parser.add_argument("--include-global", action="store_true")
    args = parser.parse_args()
    if (args.command == "upgrade-outbox") != (args.backup_file is not None):
        parser.error("--backup-file is required only for upgrade-outbox")
    if args.command == "claude-hook":
        return run_hook_process(args, open_configuration)
    try:
        if args.command == "upgrade-outbox":
            _, options = open_configuration(args)
            if args.state_dir is None:
                raise TransportError("state_directory_required")
            result = LocalOutbox.upgrade(
                args.state_dir / "outbox.sqlite3",
                instance_id=options["instance_id"],
                device_id=options["device_id"],
                backup_file=args.backup_file,
            )
            print(json.dumps(result))
            return 0
        if args.command in READ_OPERATIONS:
            connection, options = open_configuration(args)
            data = sys.stdin.buffer.read(64 * 1024 + 1)
            if len(data) > 64 * 1024:
                raise TransportError("read_request_too_large")
            reply = asyncio.run(
                read_remote(connection, args.command, parse_json(data), privacy=options["privacy"])
            )
            print(reply.model_dump_json())
            return 0
        connection, outbox = open_outbox(args)
        if args.command == "capture-init":
            result = initialize_capture(outbox, SourceConfiguration.from_file(args.source_file))
        elif args.command == "collect":
            result = asyncio.run(
                collect_source(outbox, SourceConfiguration.from_file(args.source_file))
            )
        elif args.command == "collect-watch":

            async def collect_forever():
                while True:
                    _, queue = open_outbox(args)
                    source = SourceConfiguration.from_file(args.source_file)
                    state = await collect_source(queue, source)
                    print(json.dumps(state), flush=True)
                    await asyncio.sleep(2)

            asyncio.run(collect_forever())
            return 0
        elif args.command == "enqueue":
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
    except (PrivacyError, EventError, OutboxError, CaptureError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print('{"error":"local_configuration_unavailable"}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
