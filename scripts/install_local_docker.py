"""Deploy the verified Linux release to a local Docker engine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

INSTALL_LABEL = "io.cloud-memory-hub.install-id"
PROJECT_LABEL = "com.docker.compose.project"
REVISION_LABEL = "org.opencontainers.image.revision"
VOLUMES = ("hub_data", "hub_config", "hub_governance", "hub_backups")
MAX_COMPOSE = 64 * 1024
MAX_IMAGE_DESCRIPTOR = 2 * 1024 * 1024
DIGEST = r"sha256:[a-f0-9]{64}"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


class LocalInstallError(ValueError):
    """A fixed diagnostic safe to display to the user."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def require(condition, code):
    if not condition:
        raise LocalInstallError(code)


def _workspace(home, project):
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", project), "local_project_invalid")
    base = Path(home).resolve(strict=True)
    path = base / ".cloud-memory-hub" / "local-server" / project
    require(path.resolve() == path, "local_install_path_unsafe")
    return path


@contextmanager
def _lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(directory.resolve() == directory, "local_install_path_unsafe")
    lock = directory / ".install-lock"
    require(not lock.is_symlink(), "local_install_path_unsafe")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(lock, flags, 0o600), "r+b") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "local_install_path_unsafe")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LocalInstallError("local_install_busy") from None
        yield


class Docker:
    def __init__(self):
        executable = shutil.which("docker")
        require(executable, "local_docker_unavailable")
        self.command = [executable]
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_DEFAULT_PLATFORM",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            }
            and not key.startswith(("COMPOSE_", "CMH_"))
        }
        context = self.run("context", "show").strip()
        require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", context), "local_docker_context_invalid"
        )
        host = self.run(
            "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}', context
        ).strip()
        require(
            host.startswith(("unix:///", "npipe:////./pipe/"))
            and not any(character.isspace() for character in host),
            "local_docker_context_remote",
        )
        self.command.extend(("--context", context))
        self.context = context
        require(
            self.run("info", "--format", "{{.OSType}}", error="local_docker_unavailable").strip()
            == "linux",
            "local_docker_needs_linux",
        )
        self.run("compose", "version", error="local_docker_compose_unavailable")

    def run(self, *arguments, body=None, timeout=30, error="local_docker_failed"):
        try:
            result = subprocess.run(
                [*self.command, *arguments],
                input=body,
                stdin=subprocess.DEVNULL if body is None else None,
                capture_output=True,
                env=self.env,
                timeout=timeout,
                check=False,
            )
            require(result.returncode == 0, error)
            return result.stdout.decode("utf-8")
        except (OSError, UnicodeError, subprocess.SubprocessError):
            raise LocalInstallError(error) from None


def _compose(docker, directory, project, *args, **kwargs):
    return docker.run(
        "compose",
        "--project-directory",
        str(directory),
        "--env-file",
        str(directory / ".env"),
        "-f",
        str(directory / "compose.yaml"),
        "-f",
        str(directory / "compose.override.yaml"),
        "-p",
        project,
        *args,
        **kwargs,
    )


def _read(path, maximum):
    require(
        not path.is_symlink() and path.is_file() and path.stat().st_size <= maximum,
        "local_install_config_invalid",
    )
    return path.read_bytes()


def _write(path, content):
    if os.path.lexists(path):
        require(_read(path, MAX_COMPOSE) == content, "local_install_config_changed")
        return
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(directory, args, context):
    path = directory / ".cmh-install.json"
    if not os.path.lexists(path):
        return None
    try:
        state = json.loads(_read(path, 4096))
        require(
            state["tag"] == args.tag
            and state["port"] == args.port
            and state["context"] == context
            and re.fullmatch(r"[a-f0-9]{32}", state["owner"])
            and re.fullmatch(r"[a-f0-9]{40}", state["commit"])
            and re.fullmatch(r"[a-f0-9]{64}", state["compose_sha256"])
            and 2 <= len(state["image_ids"]) <= 3
            and all(re.fullmatch(DIGEST, value) for value in state["image_ids"]),
            "local_install_intent_conflict",
        )
    except (TypeError, KeyError, ValueError) as error:
        if isinstance(error, LocalInstallError):
            raise
        raise LocalInstallError("local_install_config_invalid") from None
    return state


def _check_resources(docker, project, owner):
    """Reject resources with this Compose project or its conventional names."""
    label = f"label={PROJECT_LABEL}={project}"
    for kind, names in (
        ("container", {f"{project}-hub-1", f"{project}_hub_1"}),
        ("volume", {f"{project}_{name}" for name in VOLUMES}),
        ("network", {f"{project}_default"}),
    ):
        list_args = (
            ("ps", "-a", "--format", "{{.Names}}")
            if kind == "container"
            else (kind, "ls", "--format", "{{.Name}}")
        )
        matches = set(docker.run(*list_args, "--filter", label).splitlines())
        matches.update(names.intersection(docker.run(*list_args).splitlines()))
        for resource in matches:
            require(owner, "local_docker_project_conflict")
            labels = ".Config.Labels" if kind == "container" else ".Labels"
            actual = docker.run(
                kind, "inspect", "--format", f'{{{{index {labels} "{INSTALL_LABEL}"}}}}', resource
            ).strip()
            require(actual == owner, "local_docker_project_conflict")


def _compose_from_archive(archive, tag):
    name = f"cloud-memory-hub-{tag}/compose.yaml"
    content = None
    try:
        with tarfile.open(archive, mode="r|gz") as source:
            for member in source:
                if member.name != name:
                    continue
                require(
                    content is None and member.isfile() and 0 < member.size <= MAX_COMPOSE,
                    "local_compose_archive_invalid",
                )
                with source.extractfile(member) as stream:
                    content = stream.read()
    except (OSError, tarfile.TarError):
        raise LocalInstallError("local_compose_archive_invalid") from None
    require(content, "local_compose_archive_invalid")
    return content


def _image_json(archive, member_name):
    member = archive.getmember(member_name)
    require(
        member.isfile() and 0 < member.size <= MAX_IMAGE_DESCRIPTOR,
        "local_image_archive_invalid",
    )
    with archive.extractfile(member) as stream:
        encoded = stream.read()
    document = json.loads(encoded)
    require(
        isinstance(document, dict) and document.get("schemaVersion") == 2,
        "local_image_archive_invalid",
    )
    return document, encoded


def _image_document(archive, descriptor):
    digest = descriptor["digest"]
    require(re.fullmatch(DIGEST, digest), "local_image_archive_invalid")
    document, encoded = _image_json(archive, "blobs/" + digest.replace(":", "/", 1))
    require(
        digest == "sha256:" + hashlib.sha256(encoded).hexdigest()
        and descriptor["size"] == len(encoded)
        and document.get("mediaType") == descriptor["mediaType"],
        "local_image_archive_invalid",
    )
    return document


def _platform_manifest_id(path, tag, config_digest):
    """Bind containerd's platform manifest ID to the verified image configuration."""
    ref = f"cloud-memory-hub:{tag}"
    refs = {ref, f"docker.io/library/{ref}"}
    try:
        with tarfile.open(path, "r:gz") as archive:
            try:
                index, _ = _image_json(archive, "index.json")
            except KeyError:
                # Classic Docker save archives use the config digest as image ID.
                return None
            candidates = []
            for descriptor in index["manifests"]:
                annotations = descriptor.get("annotations", {})
                if any(
                    annotations.get(key) in refs
                    for key in ("io.containerd.image.name", "org.opencontainers.image.ref.name")
                ):
                    candidates.append(descriptor)
            if not candidates:
                return None
            require(len(candidates) == 1, "local_image_archive_invalid")
            descriptor = candidates[0]
            document = _image_document(archive, descriptor)
            if descriptor["mediaType"] == OCI_INDEX:
                candidates = [
                    entry
                    for entry in document["manifests"]
                    if entry.get("platform", {}).get("os") == "linux"
                    and entry.get("platform", {}).get("architecture") == "amd64"
                ]
                require(len(candidates) == 1, "local_image_archive_invalid")
                descriptor = candidates[0]
                document = _image_document(archive, descriptor)
            require(
                descriptor["mediaType"] == OCI_MANIFEST
                and document["config"]["digest"] == config_digest,
                "local_image_archive_invalid",
            )
            return descriptor["digest"]
    except LocalInstallError:
        raise
    except (AttributeError, KeyError, TypeError, OSError, ValueError, tarfile.TarError):
        raise LocalInstallError("local_image_archive_invalid") from None


def _compose_files(directory, state, compose_bytes):
    tag, port, owner = state["tag"], state["port"], state["owner"]
    env = f"CMH_IMAGE=cloud-memory-hub:{tag}\nCMH_PORT={port}\nCMH_PUBLIC_ORIGIN=http://127.0.0.1:{port}\n"
    labels = f'    labels:\n      {INSTALL_LABEL}: "{owner}"\n'
    override = (
        "services:\n"
        f"  setup:\n    platform: linux/amd64\n{labels}"
        f"  hub:\n    platform: linux/amd64\n{labels}"
        "volumes:\n"
        + "".join(f"  {name}:\n{labels}" for name in VOLUMES)
        + f"networks:\n  default:\n{labels}"
    )
    require(
        hashlib.sha256(compose_bytes).hexdigest() == state["compose_sha256"],
        "local_install_config_changed",
    )
    _write(directory / "compose.yaml", compose_bytes)
    _write(directory / "compose.override.yaml", override.encode())
    _write(directory / ".env", env.encode())


def _image_available(docker, state):
    ref = f"cloud-memory-hub:{state['tag']}"
    if not docker.run("image", "ls", "--no-trunc", "--quiet", ref).strip():
        return False
    identity = docker.run(
        "image",
        "inspect",
        "--format",
        "{{.Id}} {{.Os}}/{{.Architecture}} " + f'{{{{index .Config.Labels "{REVISION_LABEL}"}}}}',
        ref,
    ).split()
    require(
        len(identity) == 3
        and identity[0] in state["image_ids"]
        and identity[1:] == ["linux/amd64", state["commit"]],
        "local_docker_image_conflict",
    )
    return True


def _install_image(docker, directory, args, fetcher, previous):
    with tempfile.TemporaryDirectory(prefix="cmh-release-") as temporary:
        assets = (
            Path(temporary) if args.assets_dir is None else args.assets_dir.resolve(strict=True)
        )
        print("正在准备并校验官方镜像与 Compose 配置…", flush=True)
        try:
            metadata = (
                fetcher.fetch_release(args.tag, assets)
                if args.assets_dir is None
                else fetcher.validate_release(args.tag, assets)
            )
        except (OSError, ValueError, RuntimeError):
            raise LocalInstallError("local_release_unavailable") from None
        _, _, source, image = fetcher.release_files(args.tag)
        compose_bytes = _compose_from_archive(assets / source, args.tag)
        receipt = metadata["image"]
        image_ids = [receipt["image_id"], receipt["config_digest"]]
        manifest = _platform_manifest_id(assets / image, args.tag, receipt["config_digest"])
        if manifest is not None and manifest not in image_ids:
            image_ids.append(manifest)
        state = {
            "tag": args.tag,
            "port": args.port,
            "context": docker.context,
            "owner": previous["owner"] if previous else uuid4().hex,
            "commit": metadata["commit"],
            "image_ids": image_ids,
            "compose_sha256": hashlib.sha256(compose_bytes).hexdigest(),
        }
        require(previous is None or previous == state, "local_install_intent_conflict")
        if not _image_available(docker, state):
            print("正在将校验通过的镜像导入本机 Docker…", flush=True)
            docker.run("image", "load", "--input", str(assets / image), timeout=300)
            require(_image_available(docker, state), "local_docker_image_missing")
        _write(directory / ".cmh-install.json", json.dumps(state, sort_keys=True).encode())
        _compose_files(directory, state, compose_bytes)
        return state


def _container(docker, directory, project, state):
    identity = _compose(docker, directory, project, "ps", "-a", "-q", "hub").strip()
    if not identity:
        return None
    require(re.fullmatch(r"[a-f0-9]{12,64}", identity), "local_docker_project_conflict")
    try:
        document = json.loads(docker.run("container", "inspect", identity))[0]
        labels = document["Config"]["Labels"]
        binding = document["HostConfig"]["PortBindings"]["8800/tcp"]
        require(
            labels.get(INSTALL_LABEL) == state["owner"]
            and document["Image"] in state["image_ids"]
            and binding == [{"HostIp": "127.0.0.1", "HostPort": str(state["port"])}],
            "local_docker_project_conflict",
        )
        return identity, (document["State"].get("Health") or {}).get("Status")
    except (TypeError, IndexError, KeyError, json.JSONDecodeError):
        raise LocalInstallError("local_docker_inspection_invalid") from None


def _wait_healthy(docker, directory, project, state, origin):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        container = _container(docker, directory, project, state)
        if container is not None and container[1] == "healthy":
            try:
                with opener.open(origin + "/healthz", timeout=3) as response:
                    if response.geturl() == origin + "/healthz" and response.status == 200:
                        return
            except OSError:
                pass
        time.sleep(2)
    raise LocalInstallError("local_docker_health_timeout")


def _check_port(port):
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise LocalInstallError("local_port_busy") from None


def deploy_local(args, *, home, fetcher):
    """Return the loopback URL after verified startup, without creating a pairing."""
    require(1024 <= args.port <= 65535, "local_port_invalid")
    directory = _workspace(home, args.compose_project)
    docker = Docker()
    origin = f"http://127.0.0.1:{args.port}"
    with _lock(directory):
        previous = _read_state(directory, args, docker.context)
        _check_resources(docker, args.compose_project, previous["owner"] if previous else None)
        if previous is None:
            _check_port(args.port)
        compose_path = directory / "compose.yaml"
        if previous and os.path.lexists(compose_path) and _image_available(docker, previous):
            state = previous
            _compose_files(directory, state, _read(compose_path, MAX_COMPOSE))
        else:
            state = _install_image(docker, directory, args, fetcher, previous)
        _check_resources(docker, args.compose_project, state["owner"])
        _compose(docker, directory, args.compose_project, "config", "--quiet")
        if _container(docker, directory, args.compose_project, state) is None:
            setup = ("run", "--rm", "--no-deps", "-T", "--pull", "never")
            try:
                _compose(docker, directory, args.compose_project, *setup, "setup", timeout=60)
            except LocalInstallError:
                _compose(
                    docker,
                    directory,
                    args.compose_project,
                    *setup,
                    "--entrypoint",
                    "/opt/cmh/venv/bin/python",
                    "setup",
                    "-c",
                    "from cloud_memory_hub.config import HubConfig; HubConfig.from_environment()",
                    timeout=30,
                    error="local_docker_init_failed",
                )
        print("正在启动并检查本机 Docker 服务…", flush=True)
        _compose(
            docker,
            directory,
            args.compose_project,
            "up",
            "-d",
            "--no-build",
            "--no-recreate",
            "--no-deps",
            "--pull",
            "never",
            "hub",
            timeout=60,
            error="local_docker_start_failed",
        )
        _wait_healthy(docker, directory, args.compose_project, state, origin)
    return origin


def local_admin_entry(args, *, home, script: bytes, setup_only=False):
    """Read private first-use information from the exact running local Hub."""
    directory = _workspace(home, args.compose_project)
    require(directory.is_dir(), "local_install_not_found")
    docker = Docker()
    with _lock(directory):
        state = _read_state(directory, args, docker.context)
        require(state, "local_install_not_found")
        _compose_files(directory, state, _read(directory / "compose.yaml", MAX_COMPOSE))
        container = _container(docker, directory, args.compose_project, state)
        require(container is not None and container[1] == "healthy", "local_docker_not_ready")
        extra = ("--setup-only",) if setup_only else ()
        result = docker.run(
            "exec",
            "-i",
            container[0],
            "/opt/cmh/venv/bin/python",
            "-",
            *extra,
            body=script,
            timeout=15,
            error="local_admin_entry_unavailable",
        )
        return result.encode("utf-8")
