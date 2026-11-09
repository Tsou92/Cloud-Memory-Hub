"""Installed queue lifecycle contract; synthetic supervisor, no SDK/model inference.

This standalone probe imports runtime packages only. It verifies persisted
claim/settle and the complete prepare-to-observer handle against Hub admission;
it does not validate model quality or pending-job governance suppression.
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cloud_memory_hub.sdk_admission import SdkAdmission
from one_memory.memory_server.contracts import BatchIdentity, WriteRequest
from one_memory.memory_server.durable_queue import (
    COMPLETED,
    PENDING,
    DurableWriteDrain,
    DurableWriteQueue,
    execute_job,
)


def submit(queue, identity):
    return queue.submit(
        WriteRequest(
            internal_session_id="synthetic-quiescence-session",
            source_id="synthetic-quiescence-source",
            batch_identity=BatchIdentity(
                internal_session_id="synthetic-quiescence-session", first_round=1, last_round=1
            ),
            evidence_ids=["synthetic-quiescence-evidence"],
            idempotency_key=identity,
        )
    )


async def bounded(awaitable):
    return await asyncio.wait_for(asyncio.shield(awaitable), 8)


async def probe(path, mode, phase):
    queue = DurableWriteQueue(path)
    first = submit(queue, "synthetic-first")
    gate = SdkAdmission()
    blocked, release = asyncio.Event(), asyncio.Event()
    requested, governed, finish_governance = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def barrier(current, stage):
        if current and stage == phase:
            blocked.set()
            await release.wait()
        async with gate.operation():
            pass

    class SyntheticSupervisor:
        sdk_authority = staticmethod(lambda: True)

        async def process_sdk_job(self, records, *, request_id, before_hy_invocation):
            await barrier(request_id == first.request_id, "prepare")
            await before_hy_invocation()
            return SimpleNamespace(raw_ids=("synthetic-raw",), memories=())

    async def observe(job_id, request):
        await barrier(job_id == first.job_id, "observer")
        observed.append(job_id)

    async def execute(job_id, owner):
        await execute_job(
            job_id,
            queue=queue,
            supervisor=SyntheticSupervisor(),
            evidence=SimpleNamespace(get=lambda identity: SimpleNamespace(evidence_id=identity)),
            owner=owner,
            observe_sdk_outputs=observe,
        )

    drain = DurableWriteDrain(
        queue,
        execute=lambda job_id: execute(job_id, None),
        execute_owned=execute,
        max_concurrent_sources=1,
    )

    async def govern():
        requested.set()
        async with drain.quiesce(), gate.exclusive():
            governed.set()
            await finish_governance.wait()

    runner = asyncio.create_task(drain.run_one() if mode == "one" else drain.run_forever())
    maintenance = following_runner = None
    try:
        await bounded(blocked.wait())
        maintenance = asyncio.create_task(govern())
        await bounded(requested.wait())
        assert not governed.is_set(), "governance_preceded_worker_completion"
        async with gate.operation():
            pass
        second = submit(queue, "synthetic-second")
        release.set()
        await bounded(governed.wait())
        assert observed == [first.job_id], "observer_not_joined_before_governance"
        assert queue.get(first.request_id).state == COMPLETED, "worker_did_not_complete"
        assert queue.get(second.request_id).state == PENDING, "claim_not_paused"
        if mode == "one":
            following_runner = asyncio.create_task(drain.run_one())
            for _ in range(3):
                await asyncio.sleep(0)
            assert not following_runner.done(), "run_one_bypassed_quiescence"
        finish_governance.set()
        await bounded(maintenance)
        async with asyncio.timeout(8):
            while queue.get(second.request_id).state != COMPLETED:
                await asyncio.sleep(0.01)
        assert observed == [first.job_id, second.job_id], "following_work_was_not_observed"
    finally:
        release.set()
        finish_governance.set()
        drain.stop()
        if maintenance is not None:
            await bounded(maintenance)
        if following_runner is not None:
            await bounded(following_runner)
        await bounded(runner)


async def main():
    with TemporaryDirectory(prefix="cmh-drain-quiescence-") as temporary:
        for mode in ("one", "forever"):
            for phase in ("prepare", "observer"):
                await probe(Path(temporary) / f"{mode}-{phase}.sqlite3", mode, phase)
    print(
        "Installed drain quiescence passed: run_one/run_forever wait through prepare and "
        "observer, then resume pending work (synthetic supervisor; no model invocation)."
    )


if __name__ == "__main__":
    asyncio.run(main())
