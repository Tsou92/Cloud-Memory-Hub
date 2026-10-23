"""Explicit isolated local queue CLI. No Agent discovery or network side effects."""

import argparse
import json
import os
import sys
from pathlib import Path

from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.events import EventError
from cloud_memory_hub.local_outbox import LocalOutbox, OutboxError
from cloud_memory_hub.privacy import MAX_CONTENT_BYTES, CredentialFilter, PrivacyError, parse_json


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="cloud-memory-connector")
    parser.add_argument("command", choices=["init", "enqueue", "status"])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--known-credentials-file", type=Path)
    args = parser.parse_args()
    try:
        known = ()
        if args.known_credentials_file:
            data = read_private_file(args.known_credentials_file)
            if len(data) > 4096:
                raise PrivacyError("known_credentials_too_large")
            values = parse_json(data)
            if not isinstance(values, list):
                raise PrivacyError("invalid_known_credentials")
            known = tuple(values)
        options = dict(
            instance_id=args.instance_id, device_id=args.device_id, privacy=CredentialFilter(known)
        )
        path = args.state_dir / "outbox.sqlite3"
        outbox = (
            LocalOutbox.initialize(path, **options)
            if args.command == "init"
            else LocalOutbox(path, **options)
        )
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
        else:
            result = outbox.status()
        print(json.dumps(result))
        return 0
    except (PrivacyError, EventError, OutboxError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print('{"error":"local_configuration_unavailable"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
