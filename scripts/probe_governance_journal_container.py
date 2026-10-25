"""Candidate journal on an isolated Docker volume, including process death.

The module is streamed from this worktree into a disposable container. This is
filesystem/runtime evidence, not a claim that the running Hub has adopted it.
"""

import base64
import os
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("CMH_IMAGE", "cloud-memory-hub:dev-234244b")
BOOTSTRAP = """
import base64,hashlib,json,os,shutil,sys
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4
os.umask(0o077)
candidate=base64.b64decode(CANDIDATE)
module=Path('/data/governance_candidate.py')
if PHASE=='prepare':
    with module.open('xb') as out:
        out.write(candidate)
assert hashlib.sha256(module.read_bytes()).digest()==hashlib.sha256(candidate).digest()
sys.path.insert(0,'/data')
from governance_candidate import GovernanceJournal,GovernanceError,Intent,Checkpoint
root=Path('/data/authority')
state_file=Path('/data/state.json')
if PHASE=='prepare':
    state={'instance_id':str(uuid4()),'scope_id':str(uuid4()),'actor_id':str(uuid4()),'key':os.urandom(32).hex()}
    state_file.write_text(json.dumps(state))
    j=GovernanceJournal.initialize(root/'journal.sqlite3',instance_id=state['instance_id'],key=bytes.fromhex(state['key']))
else:
    state=json.loads(state_file.read_text())
    j=GovernanceJournal(root/'journal.sqlite3',instance_id=state['instance_id'],key=bytes.fromhex(state['key']))
def operation(action):
    return Intent(
        operation_id=str(uuid4()),actor_id=state['actor_id'],scope_id=state['scope_id'],
        action=action,targets=(j.target(state['scope_id'],'raw','synthetic record'),)
    )
if PHASE=='prepare':
    first=operation('forget')
    seen=j.append(first)
    assert j.append(first)==seen
    shutil.copy2(j.path,root/'before.sqlite3')
    print('Private journal initialized and idempotent intent committed')
elif PHASE=='crash':
    original=j._connection
    @contextmanager
    def crashing(**kwargs):
        with original(**kwargs) as db:
            def trace(statement):
                if statement.startswith('INSERT INTO heads'):
                    os._exit(23)
            db.set_trace_callback(trace)
            yield db
    j._connection=crashing
    j.append(operation('purge'))
    raise AssertionError('process_did_not_exit')
elif PHASE=='recover':
    assert j.head(state['scope_id']).sequence==1
    assert len(j.changes(state['scope_id'])['entries'])==1
    seen=j.append(operation('purge'))
    assert seen.sequence==2
    shutil.copy2(root/'before.sqlite3',j.path)
    j=GovernanceJournal(j.path,instance_id=state['instance_id'],key=bytes.fromhex(state['key']))
    try:
        j.changes(state['scope_id'],after=seen)
    except GovernanceError as error:
        assert str(error)=='governance_watermark_ahead'
    else:
        raise AssertionError('rollback_not_detected')
    fork=j.append(operation('forget'))
    assert fork.sequence==seen.sequence and fork.digest!=seen.digest
    try:
        j.changes(state['scope_id'],after=seen)
    except GovernanceError as error:
        assert str(error)=='governance_watermark_diverged'
    else:
        raise AssertionError('fork_not_detected')
    assert j.path.stat().st_mode & 0o777 == 0o600
    print(json.dumps({'status':'passed','process_crash_atomic':True,'old_copy_rejected':True,'same_height_fork_rejected':True,'candidate_module':True,'hub_integration':False}))
else:
    raise AssertionError('unknown_phase')
"""


def probe():
    candidate = base64.b64encode(
        (ROOT / "apps/hub/src/cloud_memory_hub/governance_journal.py").read_bytes()
    ).decode()
    volume = "cmh-journal-probe-" + uuid4().hex[:12]
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    containers = []
    try:
        for phase in ("prepare", "crash", "recover"):
            container = volume + "-" + phase
            containers.append(container)
            script = f"CANDIDATE={candidate!r}\nPHASE={phase!r}\n" + BOOTSTRAP
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    container,
                    "-i",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--tmpfs",
                    "/tmp",
                    "--mount",
                    f"type=volume,source={volume},target=/data",
                    "--entrypoint",
                    "/opt/cmh/venv/bin/python",
                    IMAGE,
                    "-",
                ],
                input=script,
                capture_output=True,
                text=True,
                timeout=45,
            )
            expected = 23 if phase == "crash" else 0
            if result.returncode != expected:
                raise RuntimeError(f"candidate_journal_{phase}_failed_{result.returncode}")
            print(result.stdout, end="", flush=True)
    finally:
        # Also remove this probe's container if the client timed out before --rm ran.
        for container in containers:
            subprocess.run(
                ["docker", "rm", "-f", container], capture_output=True, check=False, timeout=30
            )
        subprocess.run(
            ["docker", "volume", "rm", volume], check=True, capture_output=True, timeout=30
        )


if __name__ == "__main__":
    probe()
