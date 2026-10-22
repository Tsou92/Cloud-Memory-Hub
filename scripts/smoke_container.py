"""Exercise Compose using only newly created, randomly named test resources."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def smoke() -> None:
    project = "cmh-smoke-" + uuid.uuid4().hex[:12]
    environment = {
        **os.environ,
        "CMH_PORT": "0",
        "CMH_PUBLIC_ORIGIN": "http://localhost:8800",
        "DOCKER_DEFAULT_PLATFORM": "linux/amd64",
    }
    compose = ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "-p", project]

    def run(args, **kwargs):
        return subprocess.run(
            [*compose, *args],
            cwd=ROOT,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
            timeout=90,
            **kwargs,
        ).stdout

    def ready():
        for _ in range(60):
            state = run(["ps", "--format", "json", "hub"])
            if state:
                service = json.loads(state)
                if service.get("Health") == "healthy":
                    return
                if service.get("State") in {"dead", "exited"}:
                    break
            time.sleep(1)
        raise RuntimeError("Disposable container did not become healthy")

    try:
        run(["run", "--rm", "setup"])
        run(["up", "-d", "--no-build", "hub"])
        ready()
        payload = (ROOT / "scripts/probe_runtime.py").read_text()
        for phase in ("before", "after"):
            if phase == "after":
                run(["restart", "hub"])
                ready()
            print(
                run(["exec", "-T", "hub", "/opt/cmh/venv/bin/python", "-", phase], input=payload),
                end="",
            )
    except subprocess.CalledProcessError as error:
        # Docker/API output might contain user data; only report safe command metadata.
        command = " ".join(error.cmd[len(compose) :])
        raise RuntimeError(
            f"Container smoke command failed (exit {error.returncode}): {command}"
        ) from None
    finally:
        # No user-supplied project name is accepted: only this run's resources are removed.
        run(["down", "--volumes", "--remove-orphans"])
    print("Compose initialization, authentication and restart persistence passed")


if __name__ == "__main__":
    smoke()
