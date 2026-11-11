"""Provision the first project's invitation through the installed Hub's local API.

Run on stdin in the Hub container. The caller must capture stdout privately.
"""

import fcntl
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cloud_memory_hub.config import read_private_file
from cloud_memory_hub.events import canonical_uuid
from cloud_memory_hub.onboarding.connect import parse_link, valid_origin

API = "http://127.0.0.1:8800/hub/v1/"
MAX_RESPONSE_BYTES = 64 * 1024


class InstallError(Exception):
    """An error code safe to show without revealing the API response."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise InstallError("first_device_redirect_refused")


@contextmanager
def first_device_lock():
    descriptor = os.open(
        "/tmp/.cmh-first-device.lock",
        os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise InstallError("first_device_lock_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("first_device_already_running") from None
        yield
    finally:
        os.close(descriptor)


def provision(project_name, device_name, origin, admin_token):
    valid_origin(origin)
    if not origin.startswith("https://"):
        raise InstallError("first_device_https_required")
    if any(
        not (1 <= len(name) <= 80 and name.strip() == name and name.isprintable())
        for name in (project_name, device_name)
    ):
        raise InstallError("first_device_name_invalid")

    opener = build_opener(ProxyHandler({}), NoRedirect())
    headers = {
        "Authorization": "Bearer " + admin_token,
        "Content-Type": "application/json",
        "Host": urlsplit(origin).netloc,
    }

    def call(path, body=None):
        request = Request(
            API + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        with opener.open(request, timeout=10) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != (200 if body is None else 201) or len(data) > MAX_RESPONSE_BYTES:
                raise InstallError("first_device_response_invalid")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise InstallError("first_device_response_invalid")
        return value

    devices = call("devices").get("devices")
    projects = call("projects").get("projects")
    if not isinstance(devices, list) or not isinstance(projects, list):
        raise InstallError("first_device_response_invalid")
    if devices:
        raise InstallError("first_device_already_configured")
    if projects:
        if (
            len(projects) != 1
            or not isinstance(projects[0], dict)
            or projects[0].get("display_name") != project_name
        ):
            raise InstallError("first_device_project_conflict")
        project = projects[0]
    else:
        project = call("projects", {"display_name": project_name})
    project_id = canonical_uuid(project["project_id"])
    invitation = call("pairings", {"scope_id": project_id, "display_name": device_name})
    if invitation["origin"] != origin:
        raise InstallError("first_device_origin_mismatch")
    fragment = urlencode(
        {"pairing_id": invitation["pairing_id"], "secret": invitation["invitation_secret"]}
    )
    link = origin + "/hub/connect#" + fragment
    parse_link(link)
    return {"link": link, "project_id": project_id}


def main():
    try:
        if len(sys.argv) != 3:
            raise InstallError("first_device_arguments_invalid")
        origin = os.environ["CMH_PUBLIC_ORIGIN"]
        config_dir = Path(os.environ.get("CMH_CONFIG_DIR", "/config"))
        token = read_private_file(config_dir / "admin-token").decode("ascii").strip()
        with first_device_lock():
            result = provision(sys.argv[1], sys.argv[2], origin, token)
        print(json.dumps(result))
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("first_device_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
