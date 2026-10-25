"""Exercise installed-image migration and quarantine in disposable Docker volumes."""

import json
import os
import subprocess
import time
from uuid import uuid4

PREPARE = """
import json,os
from pathlib import Path
from cloud_memory_hub.remote_access import DeviceAccessStore, NamedResource, GrantRequest
a=DeviceAccessStore(Path('/config/remote-access.sqlite3'))
d=a.create_device(NamedResource(display_name='Synthetic migration device'))
token=d.credential.get_secret_value()
a.replace_grants(d.device['device_id'], GrantRequest(expected_revision=1,
    grants=[{'scope_id':'global','permissions':['memory:read']}]))
descriptor=os.open('/config/migration-probe.json',os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(descriptor,'w') as f:
    json.dump({'instance':a.instance_id,'credential':token,'receipt':a.authenticate(token)},f)
Path('/data/migration-marker').write_text('synthetic migration marker')
"""

VERIFY = """
import json
from pathlib import Path
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import open_authority
a=open_authority(HubConfig.from_environment())
state=json.loads(Path('/config/migration-probe.json').read_text())
assert a.access.schema_version==2 and a.access.instance_id==state['instance']
assert a.access.authenticate(state['credential'])==state['receipt']
assert Path('/data/migration-marker').read_text()=='synthetic migration marker'
a.require_ready()
"""

FAULT = """
import json,os,sys,urllib.request,urllib.error
from pathlib import Path
from uuid import uuid4
from cloud_memory_hub.config import HubConfig
from cloud_memory_hub.governance_authority import open_authority
from cloud_memory_hub.governance_journal import Intent
config=HubConfig.from_environment()
path=config.governance_dir/'journal.sqlite3'
mode=sys.argv[1]
if mode=='missing':
    os.rename(path, path.with_suffix('.probe-copy'))
elif mode=='restore':
    assert not path.exists()
    os.rename(path.with_suffix('.probe-copy'),path)
    sys.exit(0)
elif mode=='pending':
    a=open_authority(config)
    a.append(Intent(operation_id=str(uuid4()),actor_id=str(uuid4()),scope_id='global',
        action='purge',targets=(a.journal.target('global','raw','synthetic migration record'),)))
else:
    raise AssertionError('unknown phase')
for route in ['/healthz','/api/v2/memory-library','/hub/v1/devices']:
    req=urllib.request.Request('http://127.0.0.1:8800'+route,
        headers={'Host':'localhost:8800','Authorization':'Bearer '+config.admin_token})
    try:
        urllib.request.urlopen(req,timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code==503
        assert json.loads(error.read())=={'detail':'memory_authority_unavailable'}
    else:
        raise AssertionError('faulted service remained available')
"""


def main():
    image = os.environ.get("CMH_IMAGE", "cloud-memory-hub:development")
    previous = os.environ.get("CMH_PREVIOUS_IMAGE", "cloud-memory-hub:dev-234244b")
    prefix = "cmh-governance-runtime-" + uuid4().hex[:12]
    container = prefix + "-hub"
    volumes = []
    transient_names = []
    stage = "prepare"

    def docker(*args, payload=None, expected=0, reason=None):
        result = subprocess.run(
            ["docker", *args], input=payload, capture_output=True, text=True, timeout=90
        )
        if result.returncode != expected or reason is not None and reason not in result.stderr:
            # Do not expose credentials, container logs or private filesystem contents.
            raise RuntimeError(f"governance probe {stage}: unexpected exit {result.returncode}")
        return result.stdout

    def flags(governance=True):
        mount_args = [
            part for name in ("data", "config") for part in ("-v", f"{prefix}-{name}:/{name}")
        ]
        if governance:
            mount_args += ["-v", f"{prefix}-governance:/governance"]
        return [
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp",
            "--memory",
            "3g",
            "--cpus",
            "2",
            *mount_args,
        ]

    def one_shot(selected, args, *, code=None, expected=0, governance=True, reason=None):
        name = prefix + "-task-" + uuid4().hex[:6]
        transient_names.append(name)
        entry = ["--entrypoint", "/opt/cmh/venv/bin/python"] if code else []
        return docker(
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            *flags(governance),
            *entry,
            selected,
            *args,
            payload=code,
            expected=expected,
            reason=reason,
        )

    def start(selected, *, governance=True):
        docker("run", "-d", "--name", container, *flags(governance), selected)

    def wait_state(healthy, reason=None):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", "--format", "{{json .State}}", container))
            if healthy and state.get("Health", {}).get("Status") == "healthy":
                return
            if state["Status"] == "exited":
                if not healthy and state["ExitCode"] != 0:
                    logs = subprocess.run(
                        ["docker", "logs", container], capture_output=True, text=True, timeout=15
                    )
                    if reason is not None and reason not in logs.stderr:
                        raise RuntimeError(f"governance probe {stage}: unexpected failure reason")
                    return
                break
            time.sleep(1)
        raise RuntimeError(f"governance probe {stage}: unexpected service state")

    def execute(code, *args):
        return docker("exec", "-i", container, "/opt/cmh/venv/bin/python", "-", *args, payload=code)

    try:
        for suffix in ("data", "config", "governance"):
            name = prefix + "-" + suffix
            docker("volume", "create", name)
            volumes.append(name)
        one_shot(previous, ["init"], governance=False)
        start(previous, governance=False)
        wait_state(True)
        execute(PREPARE)
        docker("stop", container)
        docker("rm", container)
        stage = "migration required"
        one_shot(image, ["serve"], expected=1, reason="governance_migration_required")
        one_shot(image, ["migrate-governance"], expected=2, reason="--writers-stopped")
        stage = "explicit migration"
        one_shot(image, ["migrate-governance", "--writers-stopped"])
        one_shot(image, ["-"], code=VERIFY)
        one_shot(image, ["migrate-governance", "--writers-stopped"])
        stage = "old image rejected"
        one_shot(
            previous,
            ["serve"],
            expected=1,
            governance=False,
            reason="access_store_version_unsupported",
        )
        stage = "new service"
        start(image)
        wait_state(True)
        execute(VERIFY)
        stage = "missing journal"
        execute(FAULT, "missing")
        docker("restart", container)
        wait_state(False, reason="governance_store_unavailable")
        stage = "trusted copy restored"
        one_shot(image, ["-", "restore"], code=FAULT)
        docker("start", container)
        wait_state(True)
        execute(VERIFY)
        stage = "pending governance quarantine"
        execute(FAULT, "pending")
        docker("restart", container)
        wait_state(False, reason="governance_execution_pending")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "installed_images": True,
                    "device_identity_preserved": True,
                    "explicit_migration": True,
                    "old_image_rejected": True,
                    "missing_journal_quarantined": True,
                    "trusted_copy_restored": True,
                    "pending_intent_quarantined": True,
                    "physical_deletion_validated": False,
                }
            )
        )
    finally:
        # Names are generated here; no existing project/container/volume is accepted.
        for name in [container, *transient_names]:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        for name in reversed(volumes):
            docker("volume", "rm", name)


if __name__ == "__main__":
    main()
