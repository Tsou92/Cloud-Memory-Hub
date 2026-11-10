"""FR-03/07/10/12: scoped administrator QA with real stores and synthetic Messages."""

import asyncio
import json
import socket
from uuid import uuid4

import httpx
import pytest
from cloud_memory_hub.secret_store import EncryptedSecretStore
from one_memory.api import _model_profile_store, _sdk_library_bridge, _secret_store
from one_memory.model_profiles import (
    ModelCapabilities,
    ModelProfileInput,
    ModelProfilePatch,
    ProviderKind,
)
from pydantic import SecretStr
from test_chat_graph import produce
from test_governance_execution import EXECUTE, preview
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_ingest import provision

ASK = "/hub/v1/console/ask"
STREAM = "/hub/v1/console/ask-stream"
QUESTION = "What Synthetic fact should I remember?"


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("console_qa_test_must_not_open_a_socket")

    monkeypatch.setattr(socket.socket, "connect", denied)


def activate_model(app):
    profiles, secrets = _model_profile_store(app.core), _secret_store(app.core)
    assert isinstance(secrets, EncryptedSecretStore)
    credential = "synthetic-" + uuid4().hex
    profile = profiles.create(
        ModelProfileInput(
            name="Synthetic console QA",
            provider_kind=ProviderKind.ANTHROPIC,
            base_url="https://synthetic.invalid",
            model="synthetic-messages-model",
            api_key=SecretStr(credential),
            disable_thinking=True,
        ),
        secrets,
    )
    profiles.record_capabilities(
        profile.profile_id,
        expected_revision=profile.revision,
        capabilities=ModelCapabilities(
            endpoint_reachable=True, model_available=True, embedding_available=True
        ),
        secrets=secrets,
    )
    active = profiles.activate(profile.profile_id, secrets)
    assert (
        profiles.resolve_snapshot_secret(active.profile_id, active.revision, secrets) == credential
    )
    assert credential.encode() not in secrets.path.read_bytes()
    return active, credential


def model_input(request):
    assert request.method == "POST" and request.url.path == "/v1/messages"
    payload = json.loads(request.content)
    assert payload["thinking"] == {"type": "disabled"}
    assert "JSON" in payload["system"][0]["text"]
    return json.loads(payload["messages"][0]["content"][0]["text"])


def model_reply(citation, *, text=None):
    return httpx.Response(
        200,
        json={
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": text
                    if text is not None
                    else json.dumps(
                        {"answer": "合成回答：根据本项目记忆。", "citations": [citation]}
                    ),
                }
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    )


def sse_events(reply):
    assert reply.status_code == 200, reply.text
    assert reply.headers["content-type"].startswith("text/event-stream")
    assert reply.headers["cache-control"] == "no-store"
    return [json.loads(line[5:]) for line in reply.text.splitlines() if line.startswith("data:")]


async def ask(web, headers, scope, *, path=ASK, question=QUESTION):
    return await web.post(path, headers=headers, json={"scope_id": scope, "question": question})


def test_anthropic_answers_only_cite_the_selected_real_project(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            groups = [await produce(app, web, config, monkeypatch) for _ in range(2)]
            _, credential = activate_model(app)
            admin = {"Authorization": "Bearer " + config.admin_token}
            expected, requests, chosen = set(), [], []

            def handler(request):
                value = model_input(request)
                assert request.headers["x-api-key"] == credential
                identities = {memory["memory_id"] for memory in value["memories"]}
                assert identities and identities <= expected
                chosen.append(value["memories"][0]["memory_id"])
                requests.append(value)
                return model_reply(chosen[-1])

            app.core.state.cmh_questions.transport = httpx.MockTransport(handler)
            for group in groups:
                expected = {result["memory_ids"][0] for result in group[2]}
                for path in (ASK, STREAM):
                    reply = await ask(web, admin, group[4]["scope_id"], path=path)
                    assert reply.status_code == 200, reply.text
                    if path == STREAM:
                        events = sse_events(reply)
                        assert [event["type"] for event in events] == ["recall", "delta", "final"]
                        value = events[-1]
                        assert events[0]["memories"] == value["memories"]
                        assert events[1]["text"] == value["answer"]
                    else:
                        value = reply.json()
                        assert reply.headers["cache-control"] == "no-store"
                    assert value["mode"] == "generated"
                    assert "本项目记忆" in value["answer"]
                    assert [memory["memory_id"] for memory in value["memories"]] == [chosen[-1]]
                    assert credential not in reply.text and config.admin_token not in reply.text
            assert len(requests) == 4

    asyncio.run(scenario())


def test_unconfigured_extract_empty_global_and_real_forget_are_distinct(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, results, _, event, _ = await produce(app, web, config, monkeypatch)
            admin, _, _, empty = await provision(web, config)
            assert _model_profile_store(app.core).active_identity() is None

            def no_model(request):
                pytest.fail("empty or unconfigured QA must not send a model request")

            app.core.state.cmh_questions.transport = httpx.MockTransport(no_model)
            scope = event["scope_id"]
            before = await ask(web, admin, scope)
            assert before.status_code == 200 and before.json()["mode"] == "extractive", before.text
            expected = {result["memory_ids"][0] for result in results}
            assert {memory["memory_id"] for memory in before.json()["memories"]} <= expected
            assert before.json()["memories"]
            for empty_scope in (empty["scope_id"], "global"):
                reply = await ask(web, admin, empty_scope)
                assert reply.status_code == 200 and reply.json()["mode"] == "empty"
                assert reply.json()["memories"] == []
            unknown = await ask(web, admin, str(uuid4()))
            assert unknown.status_code == 404
            assert unknown.json() == {"detail": "console_scope_not_found"}

            original = "cmhe:" + jobs[0]
            view = await preview(web, admin, scope, [original])
            forgotten = await web.post(
                EXECUTE,
                headers=admin,
                json={
                    "scope_id": scope,
                    "action": "forget",
                    "record_ids": [original],
                    "expected_view": view["view_digest"],
                    "operation_id": str(uuid4()),
                },
            )
            assert forgotten.status_code == 200 and forgotten.json()["state"] == "completed"
            after = await ask(web, admin, scope)
            assert after.status_code == 200 and after.json()["mode"] == "empty"
            assert after.json()["memories"] == []
            assert all(memory["content"] not in after.text for memory in before.json()["memories"])

    asyncio.run(scenario())


def test_ask_requires_administrator_and_cookie_origin_for_both_routes(tmp_path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, device, _, event = await provision(web, config)
            scope = event["scope_id"]
            for path in (ASK, STREAM):
                for headers in ({}, device):
                    denied = await ask(web, headers, scope, path=path)
                    assert denied.status_code == 403
                    assert denied.json() == {"detail": "administrator_required"}
            code, _ = app.sessions.issue_exchange()
            web.cookies.set("cmh_session", app.sessions.consume_exchange(code))
            for path in (ASK, STREAM):
                missing = await ask(web, {}, scope, path=path)
                assert missing.status_code == 403 and missing.json() == {
                    "detail": "origin_required"
                }
                foreign = await ask(web, {"Origin": "https://foreign.invalid"}, scope, path=path)
                assert foreign.status_code == 403
                assert foreign.json() == {"detail": "origin_not_allowed"}
                allowed = await ask(web, {"Origin": config.public_origin}, scope, path=path)
                assert allowed.status_code == 200
                value = sse_events(allowed)[-1] if path == STREAM else allowed.json()
                assert value["mode"] == "empty"
            assert (await ask(web, admin, scope)).status_code == 200

    asyncio.run(scenario())


def test_question_credentials_are_rejected_before_model_egress(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            _, credential = activate_model(app)
            calls = []

            def handler(request):
                value = model_input(request)
                calls.append(value)
                return model_reply(value["memories"][0]["memory_id"])

            app.core.state.cmh_questions.transport = httpx.MockTransport(handler)
            code, _ = app.sessions.issue_exchange()
            cookie = app.sessions.consume_exchange(code)
            web.cookies.set("cmh_session", cookie)
            headers = {"Origin": config.public_origin}
            assert (await ask(web, headers, event["scope_id"])).json()["mode"] == "generated"
            for question in (config.admin_token, cookie, "api_key=" + credential):
                reply = await ask(web, headers, event["scope_id"], question=question)
                assert reply.status_code == 422
                assert reply.json() == {"detail": "query_requires_refiltering"}
                assert question not in reply.text
            assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("json", "memory_answer_invalid_model_output"),
        ("citation", "memory_answer_invalid_citation"),
        ("network", "memory_answer_model_unavailable"),
    ],
)
def test_model_failures_have_fixed_errors_and_no_streamed_answer(
    tmp_path, monkeypatch, failure, expected
):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            _, credential = activate_model(app)
            admin = {"Authorization": "Bearer " + config.admin_token}

            def handler(request):
                model_input(request)
                if failure == "network":
                    raise httpx.RemoteProtocolError("Synthetic private failure " + credential)
                if failure == "json":
                    return model_reply("unused", text="Synthetic invalid body " + credential)
                return model_reply("outside-project-" + uuid4().hex)

            app.core.state.cmh_questions.transport = httpx.MockTransport(handler)
            sync = await ask(web, admin, event["scope_id"])
            assert sync.status_code == 503 and sync.json() == {"detail": expected}
            streamed = await ask(web, admin, event["scope_id"], path=STREAM)
            assert sse_events(streamed) == [{"type": "error", "code": expected}]
            assert credential not in sync.text + streamed.text

    asyncio.run(scenario())


@pytest.mark.parametrize("change,path", [("profile", ASK), ("record", STREAM)])
def test_model_completion_rechecks_real_profile_or_memory(tmp_path, monkeypatch, change, path):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            profile, _ = activate_model(app)
            admin = {"Authorization": "Bearer " + config.admin_token}
            changed = []

            async def handler(request):
                identity = model_input(request)["memories"][0]["memory_id"]
                if change == "profile":
                    updated = _model_profile_store(app.core).update(
                        profile.profile_id,
                        ModelProfilePatch(
                            expected_revision=profile.revision,
                            name="Synthetic changed configuration",
                        ),
                        _secret_store(app.core),
                    )
                    assert updated.revision != profile.revision
                else:
                    bridge = _sdk_library_bridge(app.core)
                    await bridge.update(identity, "Synthetic fact changed during answer.")
                    assert (await bridge.get_sdk_memory(identity)).content.endswith(
                        "during answer."
                    )
                changed.append(identity)
                return model_reply(identity)

            app.core.state.cmh_questions.transport = httpx.MockTransport(handler)
            reply = await asyncio.wait_for(
                ask(web, admin, event["scope_id"], path=path), timeout=30
            )
            expected = "memory_answer_generation_not_authorized"
            if path == STREAM:
                assert sse_events(reply) == [{"type": "error", "code": expected}]
            else:
                assert reply.status_code == 503 and reply.json() == {"detail": expected}
            assert len(changed) == 1

    asyncio.run(scenario())


def test_stream_emits_nothing_until_the_model_answer_has_been_validated(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            _, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            activate_model(app)
            started, release = asyncio.Event(), asyncio.Event()
            bodies = []

            async def handler(request):
                identity = model_input(request)["memories"][0]["memory_id"]
                started.set()
                await release.wait()
                return model_reply(identity)

            async def observed(scope, receive, send):
                async def capture(message):
                    if message["type"] == "http.response.body" and message.get("body"):
                        bodies.append(message["body"])
                    await send(message)

                await app(scope, receive, capture)

            app.core.state.cmh_questions.transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(observed), base_url=config.public_origin
            ) as client:
                task = asyncio.create_task(
                    ask(
                        client,
                        {"Authorization": "Bearer " + config.admin_token},
                        event["scope_id"],
                        path=STREAM,
                    )
                )
                try:
                    await asyncio.wait_for(started.wait(), timeout=15)
                    assert not bodies and not task.done()
                finally:
                    release.set()
                    reply = await asyncio.wait_for(task, timeout=15)
                events = sse_events(reply)
                assert [item["type"] for item in events] == ["recall", "delta", "final"]
                assert events[1]["text"] == events[2]["answer"]
                assert events[0]["memories"] == events[2]["memories"]

    asyncio.run(scenario())


def test_stream_has_no_later_answer_chunks_after_a_completed_forget(tmp_path, monkeypatch):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            jobs, _, _, _, event, _ = await produce(app, web, config, monkeypatch)
            activate_model(app)
            admin = {"Authorization": "Bearer " + config.admin_token}
            scope_id, original = event["scope_id"], "cmhe:" + jobs[0]
            view = await preview(web, admin, scope_id, [original])
            sent, later, forgotten = [], [], []

            def handler(request):
                return model_reply(model_input(request)["memories"][0]["memory_id"])

            async def observed(scope, receive, send):
                async def capture(message):
                    body = message.get("body")
                    if message["type"] != "http.response.body" or not body:
                        return await send(message)
                    sent.append(body)
                    if forgotten:
                        later.append(body)
                    await send(message)
                    if len(sent) == 1:
                        reply = await web.post(
                            EXECUTE,
                            headers=admin,
                            json={
                                "scope_id": scope_id,
                                "action": "forget",
                                "record_ids": [original],
                                "expected_view": view["view_digest"],
                                "operation_id": str(uuid4()),
                            },
                        )
                        assert reply.status_code == 200, reply.text
                        assert reply.json()["state"] == "completed"
                        forgotten.append(reply.json()["state"])

                await app(scope, receive, capture)

            app.core.state.cmh_questions.transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(observed), base_url=config.public_origin
            ) as client:
                reply = await asyncio.wait_for(
                    ask(client, admin, scope_id, path=STREAM), timeout=30
                )
            assert forgotten == ["completed"]
            assert [item["type"] for item in sse_events(reply)] == ["recall", "delta", "final"]
            assert not later, "a completed forget must not be followed by old answer chunks"
            after = await ask(web, admin, scope_id)
            assert after.status_code == 200 and after.json()["mode"] == "empty"

    asyncio.run(scenario())
