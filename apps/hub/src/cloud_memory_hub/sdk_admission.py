"""A process-local SDK barrier, not a durable-queue or deletion coordinator."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from one_memory.hy_memory_bridge import HyMemoryBridgeError


class SdkAdmission:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: set[asyncio.Task] = set()
        self._owner: asyncio.Task | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    def _task(self) -> asyncio.Task:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if loop is not self._loop:
            raise HyMemoryBridgeError("hy_memory_admission_loop_mismatch")
        task = asyncio.current_task()
        if task is None:
            raise HyMemoryBridgeError("hy_memory_admission_task_required")
        return task

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[None]:
        task = self._task()
        if task in self._active or task is self._owner:
            yield
            return
        if self._owner is not None:
            raise HyMemoryBridgeError("hy_memory_maintenance_in_progress")
        self._active.add(task)
        self._idle.clear()
        try:
            yield
        finally:
            # No await: repeated cancellation must not interrupt accounting.
            self._active.remove(task)
            if not self._active:
                self._idle.set()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Stop new SDK entries and await admitted Tasks, without cancelling them."""
        task = self._task()
        if task in self._active:
            raise HyMemoryBridgeError("hy_memory_admission_upgrade_forbidden")
        if self._owner is not None:
            raise HyMemoryBridgeError("hy_memory_maintenance_in_progress")
        self._owner = task
        try:
            await self._idle.wait()
            yield
        finally:
            self._owner = None
