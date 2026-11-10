"""Project-bound administrator Q&A composed from the existing SDK answerer."""

import asyncio
import json
import unicodedata
from types import SimpleNamespace

from one_memory.api import _model_profile_store, _secret_store, _sse
from one_memory.hy_memory_bridge import HySdkMemoryRecord
from one_memory.memory_qa import MemoryAnswerError
from one_memory.sdk_memory_qa import SdkAskMemory, SdkMemoryAnswerer
from pydantic import Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.routing import Route

from cloud_memory_hub.privacy import PrivacyError
from cloud_memory_hub.read_contract import (
    READ_RESPONSE_BYTES,
    GetRequest,
    ScopeRequest,
    SearchRequest,
)
from cloud_memory_hub.remote_access import AccessError
from cloud_memory_hub.remote_api import read_body, require_admin, response


class AskRequest(ScopeRequest):
    question: str = Field(strict=True, min_length=1, max_length=4096, repr=False)

    @field_validator("question")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("empty_question")
        return unicodedata.normalize("NFKC", value)


def _normalize_text(value):
    """Match the credential filter's Unicode form without mistaking it for redaction."""
    if isinstance(value, str):
        return unicodedata.normalize("NFKC", value)
    if isinstance(value, list):
        return [_normalize_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_text(item) for key, item in value.items()}
    return value


class ScopedAnswerBridge:
    """Expose only bounded, privacy-filtered Hub memory projections to the SDK QA."""

    def __init__(self, console, scope_id, cookie):
        self.console, self.scope_id, self.cookie = console, scope_id, cookie
        self.namespace = f"cmh:{console.access.instance_id}:{scope_id}"

    async def search_memories(self, question, *, user_id, agent_id, limit):
        if user_id != self.namespace or agent_id != "*":
            raise MemoryAnswerError("memory_answer_generation_not_authorized")
        result = await self.console.read(
            "memory-search",
            SearchRequest(scope_id=self.scope_id, query=question, limit=min(limit, 6)),
            session_credential=self.cookie,
        )
        return tuple(SimpleNamespace(memory_id=item["record_id"]) for item in result["items"])

    async def get_sdk_memory(self, memory_id):
        try:
            result = await self.console.read(
                "memory-get",
                GetRequest(scope_id=self.scope_id, record_id=memory_id),
                session_credential=self.cookie,
            )
        except AccessError as error:
            if error.code == "record_not_found":
                return None
            raise
        item = result["items"][0]
        return HySdkMemoryRecord(
            memory_id=item["record_id"],
            content=item["content"],
            layer=item["layer"] or "unknown",
            status="active",
            is_latest=True,
            user_id=self.namespace,
            agent_id=item["agent_id"],
            session_id=item["session_id"],
            source=item["source"],
            source_raw_ids=tuple(item["source_raw_ids"]),
            memory_at=item["memory_at"],
        )


class ConsoleQuestions:
    def __init__(self, core, console):
        self.core, self.console = core, console
        self.slots = asyncio.Semaphore(2)
        self.transport = None  # Explicit injection for isolated HTTP contract tests.

    async def answer(self, request, cookie):
        await self.console.require_scope(request.scope_id)
        privacy = self.console.request_privacy(cookie)
        try:
            if (
                privacy.protect({"question": request.question}).value["question"]
                != request.question
            ):
                raise AccessError("query_requires_refiltering", 422)
        except PrivacyError:
            raise AccessError("query_requires_refiltering", 422) from None
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise AccessError("read_busy", 503) from None
        try:
            async with self.core.state.cmh_sdk_admission.operation():
                await run_in_threadpool(self.core.state.cmh_governance.require_ready)
                return await self._answer(request, cookie, privacy)
        except AccessError:
            raise
        except MemoryAnswerError as error:
            known = {
                "memory_answer_model_unavailable",
                "memory_answer_model_failed",
                "memory_answer_model_secret_unavailable",
                "memory_answer_invalid_model_output",
                "memory_answer_invalid_citation",
                "memory_answer_context_changed",
                "memory_answer_generation_not_authorized",
            }
            code = str(error) if str(error) in known else "memory_answer_failed"
            raise AccessError(code, 503) from None
        except Exception:
            raise AccessError("memory_answer_failed", 503) from None
        finally:
            self.slots.release()

    async def _answer(self, request, cookie, privacy):
        bridge = ScopedAnswerBridge(self.console, request.scope_id, cookie)
        profiles = _model_profile_store(self.core)

        async def authorize(profile, question, grounding):
            async def recheck():
                await self.console.require_scope(request.scope_id)
                await run_in_threadpool(self.core.state.cmh_governance.require_ready)
                identity = await run_in_threadpool(profiles.active_identity)
                if identity != (profile.profile_id, profile.revision):
                    raise MemoryAnswerError("memory_answer_generation_not_authorized")
                for memory in grounding:
                    current = await bridge.get_sdk_memory(memory.memory_id)
                    if (
                        current is None
                        or SdkAskMemory(
                            memory_id=current.memory_id,
                            content=current.content,
                            layer=current.layer,
                            status=current.status,
                            source=current.source,
                            gmt_created=current.gmt_created,
                        )
                        != memory
                    ):
                        raise MemoryAnswerError("memory_answer_context_changed")

            await recheck()
            return recheck

        answerer = SdkMemoryAnswerer(
            bridge=bridge,
            profiles=profiles,
            secrets=_secret_store(self.core),
            authorize_generation=authorize,
            transport=self.transport,
        )
        result = await answerer.answer(request.question, user_id=bridge.namespace)
        value = result.model_dump(mode="json")
        if len(json.dumps(value, ensure_ascii=False).encode()) > READ_RESPONSE_BYTES:
            raise AccessError("read_response_too_large", 503)
        normalized = _normalize_text(value)
        if privacy.protect(normalized).value != normalized:
            raise MemoryAnswerError("memory_answer_invalid_model_output")
        return value


def console_question_routes(questions):
    async def prepare(request):
        require_admin(request)
        body = await read_body(request, AskRequest)
        try:
            await questions.console.require_scope(body.scope_id)
        except AccessError:
            raise
        except Exception:
            raise AccessError("console_unavailable", 503) from None
        return body, request.cookies.get("cmh_session", "")

    async def ask(request):
        body, cookie = await prepare(request)
        return response(await questions.answer(body, cookie))

    async def ask_stream(request):
        body, cookie = await prepare(request)

        async def events():
            try:
                result = await questions.answer(body, cookie)
                # Only expose a validated final answer and citations, including
                # for providers whose token stream cannot validate citations yet.
                yield "".join(
                    (
                        _sse({"type": "recall", "memories": result["memories"]}),
                        _sse({"type": "delta", "text": result["answer"]}),
                        _sse({"type": "final", **result}),
                    )
                )
            except AccessError as error:
                yield _sse({"type": "error", "code": error.code})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return [
        Route("/hub/v1/console/ask", ask, methods=["POST"]),
        Route("/hub/v1/console/ask-stream", ask_stream, methods=["POST"]),
    ]
