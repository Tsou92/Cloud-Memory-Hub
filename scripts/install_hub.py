#!/usr/bin/env python3
"""Install a fresh NAS Hub and connect the Agent in the current project."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
CONNECTOR = ROOT / "apps/hub/src/cloud_memory_hub/onboarding/connect.py"
DEFAULT_TAG = "v0.1.4"
STABLE_TAG = re.compile(r"v(?:0|[1-9][0-9]{0,7})(?:\.(?:0|[1-9][0-9]{0,7})){2}")
SSH_TARGET = re.compile(
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?(?:[A-Za-z0-9][A-Za-z0-9_.-]*|\[[0-9A-Fa-f:]+\])"
)
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
READY_PREFIX = "CMH_NAS_READY\t"


class InstallError(Exception):
    """A fixed diagnostic that can be shown without exposing a pairing link."""


class ReleaseDownloadError(InstallError):
    pass


def load_connector():
    spec = importlib.util.spec_from_file_location("cmh_first_install_connector", CONNECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checked_name(value):
    if not value.strip() or len(value) > 80 or any(ord(char) < 32 for char in value):
        raise InstallError("display_name_invalid")
    return value


def check_arguments(args, connector):
    if not SSH_TARGET.fullmatch(args.nas):
        raise InstallError("ssh_target_invalid")
    if not STABLE_TAG.fullmatch(args.tag) or not SAFE_NAME.fullmatch(args.compose_project):
        raise InstallError("release_or_compose_name_invalid")
    if not 1024 <= args.port <= 65535 or not (
        args.serve_port == 443 or 1024 <= args.serve_port <= 65535
    ):
        raise InstallError("port_invalid")
    if args.ssh_port is not None and not 1 <= args.ssh_port <= 65535:
        raise InstallError("ssh_port_invalid")
    if args.origin != "AUTO" and not connector.valid_origin(args.origin).startswith("https://"):
        raise InstallError("public_https_required")
    path = PurePosixPath(args.install_dir)
    if path in (PurePosixPath("."), PurePosixPath("/")) or ".." in path.parts:
        raise InstallError("nas_install_directory_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in args.install_dir):
        raise InstallError("nas_install_directory_invalid")
    if args.tailscale_container and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.tailscale_container
    ):
        raise InstallError("tailscale_container_invalid")


def ssh_command(args, remote_command):
    command = ["ssh", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-T"]
    if args.ssh_port is not None:
        command.extend(["-p", str(args.ssh_port)])
    return [*command, args.nas, remote_command]


def remote_script(args, remote_command, script, *, private=False):
    result = subprocess.run(
        ssh_command(args, remote_command),
        input=(ROOT / "scripts" / script).read_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if private else None,
        check=False,
    )
    if result.returncode:
        if not private and result.returncode == 42:
            raise ReleaseDownloadError("nas_release_download_failed")
        if private:
            code = result.stderr.decode("utf-8", "replace").strip().splitlines()
            code = code[-1] if code else "nas_pairing_failed"
            if code in {"first_device_already_configured", "first_device_project_conflict"}:
                raise InstallError(code)
            raise InstallError("nas_pairing_failed")
        raise InstallError("nas_install_incomplete")
    return result.stdout


def install_nas(args, connector, staging=""):
    remote = shlex.join(
        [
            "bash",
            "-s",
            "--",
            args.tag,
            args.install_dir,
            args.compose_project,
            str(args.port),
            args.origin,
            str(args.serve_port),
            args.tailscale_container,
            staging,
        ]
    )
    output = remote_script(args, remote, "install_nas.sh").decode("utf-8", "strict")
    lines = [line for line in output.splitlines() if line.startswith(READY_PREFIX)]
    if len(lines) != 1:
        raise InstallError("nas_install_result_missing")
    fields = lines[0].split("\t")
    if len(fields) != 5:
        raise InstallError("nas_install_result_invalid")
    _, origin, directory, compose_project, access = fields
    origin = connector.valid_origin(origin)
    path = PurePosixPath(directory)
    if (
        not origin.startswith("https://")
        or (args.origin != "AUTO" and origin != connector.valid_origin(args.origin))
        or not path.is_absolute()
        or path == PurePosixPath("/")
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in directory)
        or compose_project != args.compose_project
        or access not in {"docker", "sudo"}
    ):
        raise InstallError("nas_install_result_invalid")
    return origin, directory, access


def load_release_fetcher():
    spec = importlib.util.spec_from_file_location(
        "cmh_install_release_fetch", ROOT / "scripts/install_release_fetch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_from_computer(args, connector, directory):
    fetcher = load_release_fetcher()
    try:
        fetcher.validate_release(args.tag, directory)
    except (OSError, ValueError, RuntimeError):
        raise InstallError("local_release_verification_failed") from None
    names = fetcher.release_files(args.tag)
    stage_command = (
        'umask 077; cmh_dir=$(mktemp -d "$HOME/.cmh-first-release.XXXXXXXX") '
        '&& chmod 700 "$cmh_dir" && printf "%s\\n" "$cmh_dir"'
    )
    stage_result = subprocess.run(
        ssh_command(args, stage_command), capture_output=True, check=False
    )
    staging = stage_result.stdout.decode("utf-8", "strict").strip()
    if stage_result.returncode or not re.fullmatch(
        r"/[^\x00-\x1f]+/\.cmh-first-release\.[A-Za-z0-9]{8}", staging
    ):
        raise InstallError("nas_release_staging_failed")
    try:
        for name in names:
            destination = shlex.quote(staging + "/" + name)
            with (directory / name).open("rb") as source:
                copied = subprocess.run(
                    ssh_command(
                        args,
                        "umask 077; set -C; cat > " + destination + " && chmod 600 " + destination,
                    ),
                    stdin=source,
                    check=False,
                )
            if copied.returncode:
                raise InstallError("nas_release_transfer_failed")
        return install_nas(args, connector, staging)
    finally:
        paths = [staging + "/" + name for name in names]
        cleanup = (
            shlex.join(["rm", "-f", "--", *paths]) + " && " + shlex.join(["rmdir", "--", staging])
        )
        subprocess.run(
            ssh_command(args, cleanup),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def deploy(args, connector):
    if args.assets_dir is not None:
        return install_from_computer(args, connector, args.assets_dir.resolve(strict=True))
    try:
        return install_nas(args, connector)
    except ReleaseDownloadError:
        print("NAS 下载暂不可用，改由这台电脑下载官方制品并传给 NAS。", flush=True)
        with tempfile.TemporaryDirectory(prefix="cmh-install-release-") as temporary:
            directory = Path(temporary)
            try:
                load_release_fetcher().fetch_release(args.tag, directory)
            except (OSError, ValueError, RuntimeError):
                raise InstallError("release_download_unavailable") from None
            return install_from_computer(args, connector, directory)


def issue_pairing(args, directory, access, project, device, connector, origin):
    command = ["env", "-u", "DOCKER_CONTEXT", "-u", "DOCKER_HOST", "-u", "COMPOSE_PROFILES"]
    command.extend(["sudo", "-n", "docker"] if access == "sudo" else ["docker"])
    command.extend(
        [
            "--context",
            "default",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.override.yaml",
            "--env-file",
            ".env",
            "--project-name",
            args.compose_project,
            "exec",
            "-T",
            "hub",
            "/opt/cmh/venv/bin/python",
            "-",
            project,
            device,
        ]
    )
    remote = "cd " + shlex.quote(directory) + " && " + shlex.join(command)
    payload = remote_script(args, remote, "install_first_device.py", private=True)
    return validate_pairing(payload, connector, origin)


def validate_pairing(payload, connector, origin):
    try:
        if len(payload) > 4096:
            raise ValueError
        result = connector.parse_json(payload)
        if set(result) != {"link", "project_id"}:
            raise ValueError
        if str(UUID(result["project_id"])) != result["project_id"]:
            raise ValueError
        if connector.parse_link(result["link"])[0] != origin:
            raise ValueError
    except (ValueError, TypeError, AttributeError, connector.ConnectError):
        raise InstallError("nas_pairing_result_invalid") from None
    return result


def install_client(args, connector, home, workspace, origin, directory, access):
    # Refuse to spend an invitation until the device can reach the final HTTPS origin.
    network = connector.Remote(origin, network=args.network)
    if network.network_status["action_required"]:
        print(connector.network_guidance(origin, args.agent), file=sys.stderr)
        raise InstallError("network_action_required")
    project = checked_name(args.project or workspace.name)
    device = checked_name(args.device or socket.gethostname().split(".")[0] or "电脑")
    identity = [
        args.nas,
        args.ssh_port,
        directory,
        args.compose_project,
        origin,
        args.agent,
        str(workspace),
        project,
        device,
    ]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    state_dir = connector.ensure_dir(home / ".cloud-memory-hub/first-install" / key, private=True)
    pending = state_dir / "pairing.json"
    with connector.exclusive_lock(state_dir):
        payload = connector.read_file(pending, private=True)
        if payload is None:
            pairing = issue_pairing(args, directory, access, project, device, connector, origin)
            connector.atomic_write(pending, connector.json_bytes(pairing))
        else:
            pairing = validate_pairing(payload, connector, origin)
        connector.CLIENT_RELEASE_TAG = args.tag
        host_home = home if args.home is not None else None
        release_assets = args.assets_dir
        if release_assets is not None:
            release_assets = release_assets.resolve(strict=True)
            mac_archive = release_assets / f"cloud-memory-hub-client-{args.tag}-macos-arm64.tar.gz"
            if not mac_archive.is_file():
                release_assets = None
        try:
            return connector.connect(
                pairing["link"],
                args.agent,
                home=host_home,
                workspace=workspace,
                network=args.network,
                expected_scope_id=pairing["project_id"],
                release_assets=release_assets,
            )
        except connector.ConnectError as error:
            if str(error) not in {"pairing_expired", "authorization_failed"}:
                raise
            # The server refuses issuing another device if an earlier claim succeeded.
            pairing = issue_pairing(args, directory, access, project, device, connector, origin)
            connector.atomic_write(pending, connector.json_bytes(pairing))
            return connector.connect(
                pairing["link"],
                args.agent,
                home=host_home,
                workspace=workspace,
                network=args.network,
                expected_scope_id=pairing["project_id"],
                release_assets=release_assets,
            )


def show_client_result(result, agent):
    if result["status"] != "configured":
        print("NAS 已就绪。电脑端连接还需完成网络检查。", file=sys.stderr)
        return 3
    automation = result.get("automation", {})
    if agent == "generic":
        print("已生成通用 MCP 配置，请在客户端导入：", result["private_state"])
        return 3
    if automation.get("status") != "configured":
        print("MCP 和 Skill 已配置。当前宿主的逐轮自动共享尚未启用。")
        return 3
    if automation.get("host_review_required"):
        print("MCP、Skill 和项目钩子已安装。请在 Codex 的 Review hooks 信任本项目两条钩子。")
        print("确认后在同一项目正常聊天即可自动共享和接续。")
        return 3
    print("MCP、Skill 和项目钩子已安装。请在这个目录打开 Claude Code，正常聊天即可。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="首次安装 NAS Hub 并接入电脑当前项目。")
    parser.add_argument("--nas", required=True, help="已有 SSH 别名，或 用户名@NAS地址")
    parser.add_argument("--ssh-port", type=int, help="SSH 端口，默认沿用本机 SSH 配置")
    parser.add_argument(
        "--agent", choices=("claude", "codex", "opencode", "hermes", "generic"), required=True
    )
    parser.add_argument("--workspace", type=Path, help="共享的项目目录，默认当前目录")
    parser.add_argument("--project", help="共享项目名，默认为当前目录名")
    parser.add_argument("--device", help="设备名称，默认为电脑名称")
    parser.add_argument("--tag", default=DEFAULT_TAG, help="固定的 GitHub 正式发行标签")
    parser.add_argument("--assets-dir", type=Path, help="使用已下载的官方发行附件目录")
    parser.add_argument("--install-dir", default="cloud-memory-hub-install", help="NAS 新安装目录")
    parser.add_argument("--compose-project", default="cloud-memory-hub", help="NAS Compose 项目名")
    parser.add_argument("--port", type=int, default=8800, help="NAS 回环端口")
    parser.add_argument("--origin", default="AUTO", help="已有 HTTPS 入口，默认用 Tailscale Serve")
    parser.add_argument("--serve-port", type=int, default=8443, help="Tailscale 私网 HTTPS 端口")
    parser.add_argument(
        "--tailscale-container", default="", help="Tailscale 装在 Docker 时的容器名"
    )
    parser.add_argument("--network", choices=("auto", "direct", "environment"), default="auto")
    parser.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    os.umask(0o077)
    connector = load_connector()
    try:
        check_arguments(args, connector)
        home = (args.home or Path.home()).resolve(strict=True)
        connector.paths_for(
            args.agent, home, connector.agent_environment(home if args.home else None)
        )
        workspace = args.workspace or Path.cwd()
        if args.workspace is None and workspace == home:
            workspace = connector.ensure_dir(home / "Cloud Memory Hub", private=True)
        workspace = connector.automation_workspace(workspace, home)
        print("正在通过 SSH 安装 NAS 服务…", flush=True)
        origin, directory, access = deploy(args, connector)
        print("管理页面：" + origin + "/hub", flush=True)
        print("正在创建项目并连接这台电脑…", flush=True)
        result = install_client(args, connector, home, workspace, origin, directory, access)
        print("共享项目目录：" + str(workspace), flush=True)
        return show_client_result(result, args.agent)
    except (InstallError, connector.ConnectError) as error:
        print(f"安装尚未完成（{error}）。处理提示后可重跑同一命令。", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, subprocess.SubprocessError):
        print("安装尚未完成。请检查 SSH、网络和运行环境后重试。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
