"""Install and diagnose a downloaded Hub pairing without exposing its credential."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from cloud_memory_hub.agent_cli import failure
from cloud_memory_hub.agent_config import AgentConfigurationError
from cloud_memory_hub.client_service import (
    ClientServiceError,
    service_install,
    service_start,
    service_status,
    service_stop,
    service_uninstall,
)
from cloud_memory_hub.client_setup import (
    ClientSetupError,
    PairingBundle,
    client_status,
    import_bundle,
    import_pairing,
    install_automation,
    mcp_configuration,
)
from cloud_memory_hub.local_outbox import OutboxError
from cloud_memory_hub.privacy import PrivacyError, parse_json
from cloud_memory_hub.transport import TransportError


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="cloud-memory-setup")
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("import", help="校验并安装网页下载的配对文件")
    pairing.add_argument("--pairing-file", type=Path, required=True)
    pairing.add_argument("--client-root", type=Path)
    automation = commands.add_parser("automation", help="为当前项目安装自动采集与召回")
    automation.add_argument("--agent", choices=["claude", "codex"], required=True)
    automation.add_argument("--workspace", type=Path, required=True)
    automation.add_argument(
        "--config-file", type=Path, help="复用已有配对，省略时从标准输入接收配对"
    )
    status = commands.add_parser("status", help="查看队列，可选检查实际远程授权")
    status.add_argument("--config-file", type=Path, required=True)
    status.add_argument("--check-remote", action="store_true")
    mcp = commands.add_parser("mcp-config", help="输出此设备的 MCP 配置，不修改已有 Agent")
    mcp.add_argument("--config-file", type=Path, required=True)
    mcp.add_argument("--format", choices=["json", "codex"], default="json")
    service = commands.add_parser("service", help="管理 Mac 登录后运行的队列补传服务")
    service.add_argument("action", choices=["install", "start", "status", "stop", "uninstall"])
    service.add_argument("--config-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "import":
            result = asyncio.run(import_pairing(args.pairing_file, client_root=args.client_root))
        elif args.command == "automation":
            config_file = args.config_file
            if config_file is None:
                try:
                    raw = sys.stdin.buffer.read(4097)
                    if len(raw) > 4096:
                        raise ValueError
                    bundle = PairingBundle.model_validate(parse_json(raw))
                except (ValueError, PrivacyError):
                    raise ClientSetupError("pairing_file_invalid") from None
                imported = asyncio.run(import_bundle(bundle))
                config_file = Path(imported["config_file"])
            result = install_automation(config_file, agent=args.agent, workspace=args.workspace)
        elif args.command == "status":
            result = asyncio.run(client_status(args.config_file, check_remote=args.check_remote))
        elif args.command == "mcp-config":
            result = mcp_configuration(args.config_file)
            if args.format == "codex":
                server = result["mcpServers"]["cloud-memory-hub"]
                print("[mcp_servers.cloud-memory-hub]")
                print("command = " + json.dumps(server["command"], ensure_ascii=False))
                print("args = " + json.dumps(server["args"], ensure_ascii=False))
                return 0
        else:
            actions = {
                "install": service_install,
                "start": service_start,
                "status": service_status,
                "stop": service_stop,
                "uninstall": service_uninstall,
            }
            result = actions[args.action](args.config_file)
        print(json.dumps(result, ensure_ascii=False))
        if args.command == "status" and result["remote"]["status"] == "unavailable":
            return 3 if result["remote"]["retryable"] else 2
        if args.command == "status" and result["processing"]["status"] in {
            "partial",
            "unavailable",
        }:
            return 3 if any(item.get("retryable") for item in result["processing"]["events"]) else 2
        return 0
    except ClientSetupError as error:
        print(json.dumps({"error": str(error), "retryable": False}), file=sys.stderr)
        return 2
    except ClientServiceError as error:
        print(json.dumps({"error": error.code, "retryable": False}), file=sys.stderr)
        return 2
    except (AgentConfigurationError, OutboxError, TransportError, OSError, ValueError) as error:
        result = failure(error)
        print(json.dumps(result), file=sys.stderr)
        return 3 if result["retryable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
