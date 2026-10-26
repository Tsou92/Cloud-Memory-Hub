import os
import select
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from cloud_memory_hub.__main__ import initialize
from cloud_memory_hub.storage_lease import LOCK_NAME, StorageLease, StorageLeaseError


def private(root):
    root.mkdir(mode=0o700)
    return root


def test_partial_acquisition_rolls_back_and_keeps_stable_empty_lock_files(tmp_path):
    first, last = private(tmp_path / "a"), private(tmp_path / "z")
    with StorageLease(last):
        with pytest.raises(StorageLeaseError, match="^storage_in_use$"):
            with StorageLease(last, first):
                pytest.fail("overlapping storage was admitted")
        with StorageLease(first) as lease:
            assert all(not os.get_inheritable(fd) for fd in lease._descriptors)
            identity = (first / LOCK_NAME).stat().st_ino
    with StorageLease(first, last):
        assert (first / LOCK_NAME).stat().st_ino == identity
        assert (first / LOCK_NAME).read_bytes() == b""
        assert (first / LOCK_NAME).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "permissions", "fifo", "nonempty"])
def test_unsafe_existing_lock_is_rejected_without_replacing_it(tmp_path, damage):
    root = private(tmp_path / "data")
    lock = root / LOCK_NAME
    other = tmp_path / "untouched"
    other.write_bytes(b"synthetic unrelated contents")
    other.chmod(0o600)
    if damage == "symlink":
        lock.symlink_to(other)
    elif damage == "hardlink":
        os.link(other, lock)
    elif damage == "fifo":
        os.mkfifo(lock, 0o600)
    else:
        lock.touch(mode=0o600)
        if damage == "permissions":
            lock.chmod(0o644)
        else:
            lock.write_bytes(b"unexpected contents")
    before = lock.lstat()
    with pytest.raises(StorageLeaseError):
        with StorageLease(root):
            pytest.fail("unsafe lock admitted")
    assert lock.lstat().st_ino == before.st_ino
    assert other.read_bytes() == b"synthetic unrelated contents"


def test_real_process_holds_all_roots_and_os_releases_after_kill(tmp_path):
    roots = [private(tmp_path / name) for name in ("data", "config", "governance")]
    unrelated = private(tmp_path / "independent")
    script = """
import sys
from pathlib import Path
from cloud_memory_hub.storage_lease import StorageLease
with StorageLease(*(Path(path) for path in sys.argv[1:])):
    print('ready', flush=True)
    sys.stdin.buffer.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, *map(str, roots)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline() == b"ready\n"
        for root in roots:
            with pytest.raises(StorageLeaseError, match="^storage_in_use$"):
                with StorageLease(root):
                    pytest.fail("live process was ignored")
        with StorageLease(unrelated):
            pass
        child.kill()
        child.wait(timeout=10)
        with StorageLease(*roots):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def test_actual_serve_blocks_second_port_and_migration_until_process_exits(tmp_path):
    config, governance = tmp_path / "config", tmp_path / "governance"
    initialize(config, governance_dir=governance)
    credentials = {name: (config / name).read_bytes() for name in ("admin-token", "encryption-key")}
    port = free_port()
    environment = {
        **os.environ,
        "CMH_DATA_DIR": str(tmp_path / "data"),
        "CMH_CONFIG_DIR": str(config),
        "CMH_GOVERNANCE_DIR": str(governance),
        "CMH_PORT": str(port),
        "CMH_PUBLIC_ORIGIN": f"http://localhost:{port}",
    }
    command = [sys.executable, "-m", "cloud_memory_hub"]
    with (tmp_path / "serve.log").open("w") as log:
        process = subprocess.Popen([*command, "serve"], env=environment, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert process.poll() is None, "synthetic Hub exited before readiness"
                try:
                    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as reply:
                        assert reply.status == 200
                        break
                except (OSError, URLError):
                    time.sleep(0.1)
            else:
                pytest.fail("synthetic Hub did not become ready")
            for args, overrides in (
                (["serve"], {"CMH_PORT": str(free_port())}),
                (["migrate-governance", "--writers-stopped"], {}),
            ):
                result = subprocess.run(
                    [*command, *args],
                    env={**environment, **overrides},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert result.returncode == 2
                assert result.stderr.strip() == "cloud-memory-hub: storage_in_use"
            process.kill()
            process.wait(timeout=10)
            result = subprocess.run(
                [*command, "migrate-governance", "--writers-stopped"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            assert "binding verified" in result.stdout
            assert all((config / name).read_bytes() == value for name, value in credentials.items())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
