import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.read_contract import GetRequest, ListRequest, SearchRequest
from cloud_memory_hub.remote_access import GrantRequest
from httpx import ASGITransport, AsyncClient
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import HySdkMemoryRecord
from test_remote_ingest import config_for, provision


async def grant_reads(
    client, admin, raw, *, permissions=("memory:read", "history:read"), revision=2
):
    response = await client.put(
        f"/hub/v1/devices/{raw['device_id']}/grants",
        headers=admin,
        json={
            "expected_revision": revision,
            "grants": [
                {"scope_id": raw["scope_id"], "permissions": list(permissions)},
                {"scope_id": "global", "permissions": list(permissions)},
            ],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_real_sdk_scopes_lifecycle_provenance_and_restart(tmp_path):
    config = config_for(tmp_path)
    identifiers = {
        key: str(uuid4())
        for key in (
            "current",
            "other_agent",
            "other_project",
            "global",
            "local",
            "archived",
            "old",
            "raw",
            "foreign_raw",
            "global_raw",
        )
    }

    async def scenario():
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                scope = raw["scope_id"]
                namespace = f"cmh:{raw['instance_id']}:{scope}"
                bridge = _sdk_library_bridge(app.core)
                sdk = await bridge._get_client()
                for name in (
                    "current",
                    "other_agent",
                    "other_project",
                    "global",
                    "local",
                    "archived",
                    "old",
                ):
                    owner = {
                        "other_project": f"cmh:{raw['instance_id']}:{uuid4()}",
                        "global": f"cmh:{raw['instance_id']}:global",
                        "local": "local",
                    }.get(name, namespace)
                    result = await asyncio.to_thread(
                        sdk.import_memory_exact,
                        "项目决定使用蓝色设计方案。" + name,
                        memory_id=identifiers[name],
                        user_id=owner,
                        agent_id="agent-b" if name == "other_agent" else "agent-a",
                        session_id=raw["conversation_id"],
                        source="cmh:" + raw["source_id"],
                        layer="l2_fact",
                        status="archived" if name == "archived" else "active",
                        is_latest=name != "old",
                        request_id=str(uuid4()),
                        metadata={"source_raw_memory_ids": [identifiers["raw"]]},
                    )
                    assert result["success"] is True
                for name in ("raw", "foreign_raw", "global_raw"):
                    owner = (
                        namespace
                        if name == "raw"
                        else (
                            f"cmh:{raw['instance_id']}:global" if name == "global_raw" else "local"
                        )
                    )
                    await bridge.import_sdk_raw_exact(
                        content="原始决定使用蓝色设计方案。",
                        raw_id=identifiers[name],
                        user_id=owner,
                        agent_id="agent-a",
                        session_id=raw["conversation_id"],
                        source="cmh:" + raw["source_id"],
                        memory_at=None,
                        metadata={},
                        request_id=str(uuid4()),
                    )

                async def read(path, **body):
                    return await web.post(
                        "/hub/v1/" + path, headers=headers, json={"scope_id": scope, **body}
                    )

                listing = await read("memories/list")
                assert listing.status_code == 200, listing.text
                assert {i["record_id"] for i in listing.json()["items"]} == {
                    identifiers["current"],
                    identifiers["other_agent"],
                }
                found = await read("memories/search", query="项目决定使用蓝色设计方案。")
                assert found.status_code == 200, found.text
                ids = {i["record_id"] for i in found.json()["items"]}
                assert ids and ids <= {identifiers["current"], identifiers["other_agent"]}
                assert found.json()["content_trust"] == "untrusted_history"
                for name in ("other_project", "global", "local", "archived", "old"):
                    denied = await read("memories/get", record_id=identifiers[name])
                    assert denied.status_code == 404
                    assert denied.json() == {"detail": "record_not_found"}
                current = await read("memories/get", record_id=identifiers["current"])
                assert current.status_code == 200
                assert current.json()["items"][0]["source_raw_ids"] == [identifiers["raw"]]
                raw_reply = await read("history/get", record_id=identifiers["raw"])
                assert raw_reply.status_code == 200
                assert raw_reply.json()["items"][0]["session_id"] == raw["conversation_id"]
                assert (
                    await read("history/get", record_id=identifiers["foreign_raw"])
                ).status_code == 404
                for path, expected in (("memories/list", "global"), ("history/list", "global_raw")):
                    global_result = await read(path, scope_id="global")
                    assert [i["record_id"] for i in global_result.json()["items"]] == [
                        identifiers[expected]
                    ]
                page_ids, offset = [], 0
                while offset is not None:
                    page = (await read("memories/list", limit=1, offset=offset)).json()
                    page_ids.extend(i["record_id"] for i in page["items"])
                    assert page["next_offset"] in (None, offset + 1)
                    offset = page["next_offset"]
                assert set(page_ids) == {identifiers["current"], identifiers["other_agent"]}
                assert len(page_ids) == 2
                missing_scope = await read("memories/list", scope_id=str(uuid4()))
                assert missing_scope.status_code == 403
                await grant_reads(web, admin, raw, permissions=("memory:read",), revision=3)
                assert (await read("history/get", record_id=identifiers["raw"])).status_code == 403
                assert (
                    await read("memories/get", record_id=identifiers["current"])
                ).status_code == 200
        # Reopen the same SDK directory and device authority; no in-memory result cache.
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                response = await web.post(
                    "/hub/v1/memories/get",
                    headers=headers,
                    json={"scope_id": scope, "record_id": identifiers["current"]},
                )
                assert response.status_code == 200
                assert response.json()["items"][0]["content"].endswith("current")

    asyncio.run(scenario())


def test_read_revocation_during_sdk_work_and_query_privacy(tmp_path):
    async def scenario():
        config = config_for(tmp_path)
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                reads = app.core.state.cmh_reads
                calls = []
                entered, release = asyncio.Event(), asyncio.Event()

                async def search(query, **kwargs):
                    calls.append((query, kwargs))
                    entered.set()
                    await release.wait()
                    return ()

                reads.bridge = lambda: SimpleNamespace(search_memories=search)
                secret = uuid4().hex
                bad = await web.post(
                    "/hub/v1/memories/search",
                    headers=headers,
                    json={"scope_id": raw["scope_id"], "query": "password=" + secret},
                )
                assert bad.status_code == 422 and secret not in bad.text and not calls
                pending = asyncio.create_task(
                    web.post(
                        "/hub/v1/memories/search",
                        headers=headers,
                        json={"scope_id": raw["scope_id"], "query": "蓝色设计"},
                    )
                )
                await asyncio.wait_for(entered.wait(), 3)
                # Remove and then restore the same permission. Revision still invalidates the read.
                store = reads.access
                store.replace_grants(raw["device_id"], GrantRequest(expected_revision=3, grants=()))
                await grant_reads(web, admin, raw, revision=4)
                release.set()
                result = await pending
                assert result.status_code == 409
                assert result.json() == {"detail": "read_authorization_changed"}
                assert calls[0][1]["user_id"] == f"cmh:{raw['instance_id']}:{raw['scope_id']}"

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["revoke", "rotate", "permission"])
def test_read_denied_after_current_authority_changes(tmp_path, change):
    async def scenario():
        config = config_for(tmp_path)
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                reads = app.core.state.cmh_reads

                async def listing(**kwargs):
                    if change == "permission":
                        reads.access.replace_grants(
                            raw["device_id"], GrantRequest(expected_revision=3, grants=())
                        )
                    else:
                        result = await web.post(
                            f"/hub/v1/devices/{raw['device_id']}/{change}",
                            headers=admin,
                            json={"expected_revision": 3},
                        )
                        assert result.status_code == 200
                    return SimpleNamespace(memories=(), total=0)

                reads.bridge = lambda: SimpleNamespace(list_sdk_memories=listing)
                result = await web.post(
                    "/hub/v1/memories/list", headers=headers, json={"scope_id": raw["scope_id"]}
                )
                assert result.status_code == (403 if change == "permission" else 401)
                assert "items" not in result.json()

    asyncio.run(scenario())


def test_text_blocks_filter_before_slicing_and_sdk_failures_do_not_echo(tmp_path):
    async def scenario():
        config = config_for(tmp_path)
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                reads = app.core.state.cmh_reads
                secret = uuid4().hex
                record = HySdkMemoryRecord(
                    memory_id=str(uuid4()),
                    user_id=f"cmh:{raw['instance_id']}:{raw['scope_id']}",
                    agent_id="test",
                    content="x" * 4085 + " password=" + secret + "\n" + "y" * 100,
                    layer="l2_fact",
                    status="active",
                    is_latest=True,
                )

                async def get(_):
                    return record

                reads.bridge = lambda: SimpleNamespace(get_sdk_memory=get)
                body = {"scope_id": raw["scope_id"], "record_id": record.memory_id}
                first = await web.post("/hub/v1/memories/get", headers=headers, json=body)
                assert first.status_code == 200 and secret not in first.text
                item = first.json()["items"][0]
                assert item["next_content_offset"] == 4096
                second = await web.post(
                    "/hub/v1/memories/get", headers=headers, json={**body, "content_offset": 4096}
                )
                assert secret not in second.text
                assert secret not in item["content"] + second.json()["items"][0]["content"]

                async def broken(_):
                    raise ValueError(secret)

                reads.bridge = lambda: SimpleNamespace(get_sdk_memory=broken)
                failed = await web.post("/hub/v1/memories/get", headers=headers, json=body)
                assert failed.status_code == 503 and secret not in failed.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "model, value",
    [
        (ListRequest, {"scope_id": "*"}),
        (ListRequest, {"scope_id": "global", "limit": True}),
        (ListRequest, {"scope_id": "global", "limit": 11}),
        (GetRequest, {"scope_id": "global", "record_id": "../../other"}),
        (SearchRequest, {"scope_id": "global", "query": " "}),
        (SearchRequest, {"scope_id": "global", "query": "x", "user_id": "local"}),
    ],
)
def test_read_contract_rejects_scope_injection_and_unbounded_requests(model, value):
    with pytest.raises(ValueError):
        model.model_validate(value)


def test_formatted_search_is_hydrated_and_cannot_bypass_scope_or_lifecycle(tmp_path):
    async def scenario():
        config = config_for(tmp_path)
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                canonical = HySdkMemoryRecord(
                    memory_id=str(uuid4()),
                    content="当前权威内容",
                    user_id=f"cmh:{raw['instance_id']}:{raw['scope_id']}",
                    agent_id="a",
                    layer="l2_fact",
                    status="active",
                    is_latest=True,
                )
                other = canonical.model_copy(update={"memory_id": str(uuid4()), "user_id": "local"})
                archived = canonical.model_copy(
                    update={"memory_id": str(uuid4()), "status": "archived"}
                )
                records = {r.memory_id: r for r in (other, archived, canonical)}

                async def search(*args, **kwargs):
                    return (
                        SimpleNamespace(
                            memory_id=canonical.memory_id,
                            member_ids=tuple(records),
                            formatted_content="untrusted stale aggregate",
                        ),
                    )

                async def get(record_id):
                    return records[record_id]

                app.core.state.cmh_reads.bridge = lambda: SimpleNamespace(
                    search_memories=search, get_sdk_memory=get
                )
                result = await web.post(
                    "/hub/v1/memories/search",
                    headers=headers,
                    json={"scope_id": raw["scope_id"], "query": "内容"},
                )
                assert result.status_code == 200
                assert [r["record_id"] for r in result.json()["items"]] == [canonical.memory_id]
                assert result.json()["items"][0]["content"] == canonical.content
                assert "aggregate" not in result.text

    asyncio.run(scenario())


def test_concurrent_sdk_reads_have_a_bounded_admission_queue(tmp_path):
    async def scenario():
        config = config_for(tmp_path)
        app = create_hub(config)
        async with app.core.router.lifespan_context(app.core):
            async with AsyncClient(
                transport=ASGITransport(app), base_url=config.public_origin
            ) as web:
                admin, headers, token, raw = await provision(web, config)
                await grant_reads(web, admin, raw)
                entered, release = asyncio.Event(), asyncio.Event()
                count = 0

                async def listing(**kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        entered.set()
                    await release.wait()
                    return SimpleNamespace(memories=(), total=0)

                app.core.state.cmh_reads.bridge = lambda: SimpleNamespace(list_sdk_memories=listing)

                async def call():
                    return await web.post(
                        "/hub/v1/memories/list", headers=headers, json={"scope_id": raw["scope_id"]}
                    )

                requests = [asyncio.create_task(call()) for _ in range(2)]
                await asyncio.wait_for(entered.wait(), 3)
                busy = await call()
                assert busy.status_code == 503 and busy.json() == {"detail": "read_busy"}
                assert count == 2
                release.set()
                assert all(result.status_code == 200 for result in await asyncio.gather(*requests))
                assert (await call()).status_code == 200

    asyncio.run(scenario())
