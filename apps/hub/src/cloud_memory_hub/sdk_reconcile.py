"""Recover durable queue receipts from proven SDK outcomes, never by resending."""

import logging
import re
from importlib.metadata import version

from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_processing import MemoryTaskRunner, _imported_as_said
from one_memory.memory_server.durable_queue import COMPLETED, DurableSdkReceipt
from one_memory.memory_tasks import NON_RETRYABLE_POST_WRITE_CODES

from cloud_memory_hub.productivity_read import record_id
from cloud_memory_hub.sdk_productivity import productivity_references

BATCH_SIZE = 32
MAX_REFERENCES = 512
_ID = re.compile(r"[A-Za-z0-9_:-]{1,200}\Z")
_LOG = logging.getLogger(__name__)


def _ids(value):
    if not isinstance(value, list) or len(value) > MAX_REFERENCES:
        raise ValueError("sdk_outcome_references_invalid")
    if any(not isinstance(item, str) or _ID.fullmatch(item) is None for item in value):
        raise ValueError("sdk_outcome_references_invalid")
    return tuple(dict.fromkeys(value))


def _result(outcome, request, operation):
    if not isinstance(outcome, dict) or outcome.get("status") != "succeeded":
        raise ValueError("sdk_outcome_not_proven")
    for key, expected in (
        ("request_id", request.request_id),
        ("user_id", request.user_id),
        ("agent_id", request.agent_id),
        ("session_id", request.session_id),
        ("operation", operation),
    ):
        if outcome.get(key) != expected:
            raise ValueError("sdk_outcome_identity_mismatch")
    result = outcome.get("result")
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or result.get("request_id") != request.request_id
    ):
        raise ValueError("sdk_outcome_result_invalid")
    return result


async def proven_receipt(bridge, job_id, request, operation):
    receipt, _ = await proven_outputs(bridge, job_id, request, operation)
    return receipt


async def proven_outputs(bridge, job_id, request, operation):
    result = _result(await bridge.get_sdk_write_outcome(request.request_id), request, operation)
    if result.get("scene") == "productivity" and operation == "add":
        experiences, facts = _ids(result.get("experience_ids")), _ids(result.get("fact_ids"))
        if (
            len(experiences) + len(facts) > MAX_REFERENCES
            or result.get("memory_id") != (experiences[0] if experiences else None)
            or result.get("memory_ids") not in (None, [])
        ):
            raise ValueError("sdk_outcome_references_invalid")
        sdk = await bridge._get_client()
        rows = await _joined_thread(
            productivity_references,
            sdk._config.coding.db_path,
            experiences,
            facts,
            variant=result.get("productivity_variant"),
        )
        links = []
        for (variant, kind, identity), (user, agent, session) in rows.items():
            if (user, agent) != (request.user_id, request.agent_id):
                raise ValueError("sdk_outcome_reference_scope_mismatch")
            if kind == "f" and session != request.session_id:
                raise ValueError("sdk_outcome_reference_session_mismatch")
            links.append(("memory", record_id(variant, kind, identity)))
        return DurableSdkReceipt(job_id, (), tuple(dict.fromkeys((*experiences, *facts)))), tuple(
            sorted(links)
        )
    if result.get("scene") != "chat":
        raise ValueError("sdk_outcome_scene_unknown")
    if result.get("experience_ids") not in (None, []) or result.get("fact_ids") not in (None, []):
        raise ValueError("sdk_outcome_references_invalid")
    single = result.get("memory_id")
    memories = _ids(result.get("memory_ids", []))
    if any(
        isinstance(identity, str) and identity.startswith(("cmhp:", "cmhe:"))
        for identity in (single, *memories)
    ):
        raise ValueError("sdk_outcome_reserved_identity")
    raw_ids = ()
    if operation == "add":
        raw_ids = _ids([single])
        if single in memories or len(memories) + 1 > MAX_REFERENCES:
            raise ValueError("sdk_outcome_references_invalid")
        raw = await bridge.get_sdk_raw(single)
        if (
            raw is None
            or raw.raw_id != single
            or (raw.user_id, raw.agent_id, raw.session_id)
            != (request.user_id, request.agent_id, request.session_id)
        ):
            raise ValueError("sdk_outcome_raw_unproven")
    else:
        # add_extracted has no L1 raw; its singular ID is the first semantic output.
        if not memories or single != memories[0]:
            raise ValueError("sdk_outcome_import_unproven")
    for identity in memories:
        memory = await bridge.get_sdk_memory(identity)
        if (
            memory is None
            or memory.memory_id != identity
            or (memory.user_id, memory.agent_id) != (request.user_id, request.agent_id)
            or memory.layer.lower().startswith("l1_")
        ):
            raise ValueError("sdk_outcome_memory_unproven")
    return DurableSdkReceipt(job_id, raw_ids, memories), tuple(
        sorted(
            [
                *(("raw", identity) for identity in raw_ids),
                *(("memory", identity) for identity in memories),
            ]
        )
    )


class SdkReconciler:
    def __init__(self, queue, bridge, governance, *, observe_outputs=None):
        self.queue = queue
        self.bridge = bridge
        self.governance = governance
        self.observe_outputs = observe_outputs
        self._cursor = None
        self._running = False

    def _commit(self, queue, job, frozen, receipt):
        self.governance.require_ready()
        return queue.resolve_unknown(
            job.job_id,
            found=True,
            sdk_receipt=receipt,
            expected_request=frozen,
            expected_updated_at=job.updated_at,
        )

    async def run(self):
        if self._running:
            return 0
        self._running = True
        completed = 0
        try:
            queue = self.queue()
            if not queue.sdk_authority or version("hy-memory") != "2.0.5.dev3":
                return 0
            await _joined_thread(self.governance.require_ready)
            jobs = await _joined_thread(
                queue.unknown_jobs,
                limit=BATCH_SIZE,
                after_cursor=self._cursor,
                exclude_error_codes=NON_RETRYABLE_POST_WRITE_CODES,
            )
            if not jobs:
                self._cursor = None
                return 0
            for job in jobs:
                # Advance even on an unreadable row. Wrap on the next exhausted page.
                self._cursor = (job.updated_at, job.job_id)
                try:
                    bridge = self.bridge()
                    async with bridge.exclusive_access():
                        await _joined_thread(self.governance.require_ready)
                        frozen = await _joined_thread(queue.payload_for, job.job_id)
                        if frozen is None or not frozen.input_records:
                            continue
                        if frozen.idempotency_key != job.request_id:
                            continue
                        request = MemoryTaskRunner.request_for(job.request_id, frozen.input_records)
                        if any(
                            (row.user_id, row.source_id, row.conversation_id)
                            != (request.user_id, request.source_id, request.session_id)
                            for row in frozen.input_records
                        ):
                            continue
                        operation = (
                            "add_extracted"
                            if all(_imported_as_said(row) for row in frozen.input_records)
                            else "add"
                        )
                        receipt = await proven_receipt(bridge, job.job_id, request, operation)
                        if self.observe_outputs is not None:
                            await self.observe_outputs(job.job_id, frozen)

                        if (
                            await _joined_thread(self._commit, queue, job, frozen, receipt)
                            == COMPLETED
                        ):
                            completed += 1
                except Exception:
                    # Provider/SQLite errors can include credentials or content.
                    _LOG.debug("sdk_reconcile_deferred")
            return completed
        except Exception:
            _LOG.warning("sdk_reconcile_unavailable")
            return completed
        finally:
            self._running = False
