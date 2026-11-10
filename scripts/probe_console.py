"""FR-01/03/07/12: real administrator HTTP over disposable SDK read fixtures.

Run only through smoke_container: prepare before the existing stopped SDK seed,
seed while stopped, then before/after/revoked against the installed server. No
model is activated and no external model request is needed for these checks.
"""

import asyncio
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE = "http://localhost:8800"
ROOT = Path("/data")
STATE = ROOT / "console-smoke.json"
READ_STATE = ROOT / "read-smoke.json"
READ_CONNECTION = ROOT / "read-connection.json"
INGEST_STATE = ROOT / "remote-ingest-smoke.json"
ADMIN_TOKEN = Path("/config/admin-token")
PREFIX = "/hub/v1/console"
CONTENT = "合成控制台第二项目：决定使用绿色设计方案。"
QUESTION = "决定使用设计方案"
MAX_REPLY_BYTES = 256 * 1024


def request(path, token, method="GET", body=None, *, stream=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    call = Request(
        BASE + path,
        method=method,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        reply = urlopen(call, timeout=30)
    except HTTPError as error:
        reply = error
    with reply:
        if path.startswith("/hub/"):
            assert reply.headers.get("Cache-Control") == "no-store", "console_cache_policy"
        raw = reply.read(MAX_REPLY_BYTES + 1)
        assert len(raw) <= MAX_REPLY_BYTES, "console_reply_budget"
        assert not token or token.encode() not in raw, "console_credential_echo"
        if stream and reply.status == 200:
            assert reply.headers.get_content_type() == "text/event-stream", "console_sse_type"
            events = []
            for frame in raw.decode().split("\n\n"):
                if frame.strip():
                    assert frame.startswith("data: "), "console_sse_frame"
                    events.append(json.loads(frame[6:]))
            return reply.status, events
        return reply.status, json.loads(raw)


def prepare(admin, read_state):
    assert not STATE.exists(), "console_fixture_already_exists"
    state = {"instance_id": read_state["instance_id"], "memory_id": str(uuid4())}
    for field, name in (
        ("scope_id", "Synthetic second console project"),
        ("empty_scope_id", "Synthetic empty console project"),
    ):
        code, project = request("/hub/v1/projects", admin, "POST", {"display_name": name})
        assert code == 201, "console_fixture_project"
        state[field] = project["project_id"]
    STATE.touch(mode=0o600, exist_ok=False)
    STATE.write_text(json.dumps(state))


async def seed(state):
    # Reuse the real SDK and the same stopped-writer process as probe_remote_read.
    from one_memory.hy_memory_bridge import HyMemoryBridge, builtin_retrieval_snapshot

    bridge = HyMemoryBridge(profile=builtin_retrieval_snapshot(), api_key=None, data_dir=ROOT)
    try:
        sdk = await bridge._get_client()
        result = await asyncio.to_thread(
            sdk.import_memory_exact,
            CONTENT,
            memory_id=state["memory_id"],
            user_id=f"cmh:{state['instance_id']}:{state['scope_id']}",
            agent_id="synthetic-console-agent",
            session_id=str(uuid4()),
            source="synthetic-console-fixture",
            request_id=str(uuid4()),
            metadata={},
        )
        assert result["success"] is True, "console_fixture_seed"
    finally:
        await bridge.close()


def read(admin, scope_id, operation="memory-list", **fields):
    return request(PREFIX + "/read/" + operation, admin, "POST", {"scope_id": scope_id, **fields})


def seeded_memories(state, read_state):
    # Compare against known seed text, not another response from the same server.
    blue = unicodedata.normalize("NFKC", "合成验收：决定使用蓝色设计方案。")
    return (
        (read_state["scope_id"], read_state["memory_id"], blue),
        (state["scope_id"], state["memory_id"], unicodedata.normalize("NFKC", CONTENT)),
        ("global", read_state["foreign_id"], blue),
    )


def check_bootstrap(admin, state, read_state):
    scopes, offset = set(), 0
    for _ in range(30):
        code, page = request(f"{PREFIX}/bootstrap?limit=1&offset={offset}", admin)
        assert code == 200 and page["schema_version"] == 1, "console_bootstrap"
        assert page["product"] == "Cloud Memory Hub" and page["mode"] == "hub"
        assert page["instance_id"] == state["instance_id"] == read_state["instance_id"]
        assert {"scoped_console_reads", "scoped_console_ask"} <= set(page["features"])
        assert page["global_scope"]["scope_id"] == "global", "console_global_selector"
        revision = os.environ.get("CMH_REVISION", "")
        expected_revision = revision if re.fullmatch(r"[0-9a-f]{7,40}", revision) else None
        assert page["build_revision"] == expected_revision, "console_build_identity"
        assert isinstance(page["version"], str) and page["version"], "console_version"
        ids = {item["scope_id"] for item in page["projects"]}
        assert not scopes.intersection(ids) and "global" not in ids, "console_project_page"
        scopes.update(ids)
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + 1, "console_project_cursor"
        offset = page["next_offset"]
    else:
        raise AssertionError("console_project_page_budget")
    assert {state["scope_id"], state["empty_scope_id"], read_state["scope_id"]} <= scopes


def check_reads(admin, state, read_state):
    expected = seeded_memories(state, read_state)
    for scope_id, memory_id, content in expected:
        code, page = read(admin, scope_id)
        assert code == 200 and page["scope_id"] == scope_id, "console_scope_echo"
        assert page["authority"] == "administrator" and "device_id" not in page
        assert page["content_trust"] == "untrusted_history", "console_content_trust"
        assert page["returned_count"] == 1 and page["next_offset"] is None
        assert [item["record_id"] for item in page["items"]] == [memory_id]
        assert page["items"][0]["content"] == content, "console_known_project_content"
        code, exact = read(admin, scope_id, "memory-get", record_id=memory_id)
        assert code == 200 and exact["items"] == page["items"], "console_exact_memory"
        code, found = read(admin, scope_id, "memory-search", query=QUESTION)
        assert code == 200 and [item["record_id"] for item in found["items"]] == [memory_id]
        assert found["items"][0]["content"] == content, "console_known_search_content"
        for foreign_scope, foreign_id, _ in expected:
            if foreign_scope != scope_id:
                code, error = read(admin, scope_id, "memory-get", record_id=foreign_id)
                assert (code, error) == (404, {"detail": "record_not_found"})
        code, summary = request(PREFIX + "/summary", admin, "POST", {"scope_id": scope_id})
        assert code == 200 and summary["scope"]["scope_id"] == scope_id
        assert summary["visible_memory_total"] is summary["visible_history_total"] is None
        assert summary["count_state"] == "not_computed", "console_unknown_total"
    code, history = read(admin, read_state["scope_id"], "history-list")
    assert code == 200 and {item["record_id"] for item in history["items"]} == {
        read_state["raw_id"]
    }
    assert history["items"][0]["content"] == unicodedata.normalize(
        "NFKC", "合成对话：设计使用蓝色，下步检查布局。"
    ), "console_known_history_content"
    assert read(admin, state["scope_id"], "history-get", record_id=read_state["raw_id"])[0] == 404
    code, empty = read(admin, state["empty_scope_id"])
    assert code == 200 and empty["items"] == [] and empty["returned_count"] == 0
    for operation in ("memory-list", "history-list"):
        code, error = read(admin, str(uuid4()), operation)
        assert (code, error) == (404, {"detail": "console_scope_not_found"})


def check_source_summary(admin, phase):
    # The existing intake probe registers and later revokes this synthetic source.
    ingest = json.loads(INGEST_STATE.read_text())
    code, listing = request("/hub/v1/sources", admin)
    assert code == 200, "console_source_listing"
    source = next(item for item in listing["sources"] if item["source_id"] == ingest["source_id"])
    code, summary = request(PREFIX + "/summary", admin, "POST", {"scope_id": source["scope_id"]})
    assert code == 200 and summary["sources_total"] == 1, "console_source_total"
    assert summary["next_offset"] is None and len(summary["sources"]) == 1
    actual = summary["sources"][0]
    assert actual["source_id"] == source["source_id"] and actual["device_id"] == ingest["device_id"]
    assert actual["status"] == ("active" if phase == "before" else "revoked")
    assert actual["device_display_name"] == "Synthetic ingest device"


def check_answers(admin, state, read_state):
    code, profiles = request("/api/v1/model-profiles", admin)
    assert code == 200 and not any(item["is_active"] for item in profiles), "console_no_model"
    for scope_id, memory_id, content in (
        *seeded_memories(state, read_state),
        (state["empty_scope_id"], None, None),
    ):
        body = {"scope_id": scope_id, "question": QUESTION}
        code, answer = request(PREFIX + "/ask", admin, "POST", body)
        assert code == 200 and answer["mode"] == ("extractive" if memory_id else "empty")
        expected_ids = [memory_id] if memory_id else []
        assert [item["memory_id"] for item in answer["memories"]] == expected_ids
        if memory_id:
            assert answer["answer"] == content, "console_known_answer_content"
            assert answer["memories"][0]["content"] == content, "console_known_citation_content"
            code, detail = read(admin, scope_id, "memory-get", record_id=memory_id)
            assert code == 200 and answer["answer"] == detail["items"][0]["content"]
        code, events = request(PREFIX + "/ask-stream", admin, "POST", body, stream=True)
        assert code == 200 and [item["type"] for item in events] == ["recall", "delta", "final"]
        assert events[0]["memories"] == answer["memories"] and events[1]["text"] == answer["answer"]
        assert events[2] == {"type": "final", **answer}, "console_final_citations"


def probe(phase):
    assert phase in {"prepare", "seed", "before", "after", "revoked"}, "console_phase"
    # This sentinel is created only in the disposable smoke flow, before SDK seeds.
    read_state = json.loads(READ_STATE.read_text())
    admin = ADMIN_TOKEN.read_text().strip()
    if phase == "prepare":
        prepare(admin, read_state)
        return
    state = json.loads(STATE.read_text())
    assert state["instance_id"] == read_state["instance_id"], "console_fixture_identity"
    if phase == "seed":
        asyncio.run(seed(state))
        print("Synthetic second console project seeded while the Hub was stopped")
        return
    device = json.loads(READ_CONNECTION.read_text())["credential"]
    for credential in ("", device):
        for path, method, body in (
            (PREFIX + "/bootstrap", "GET", None),
            (PREFIX + "/summary", "POST", {"scope_id": state["scope_id"]}),
            (PREFIX + "/ask", "POST", {"scope_id": state["scope_id"], "question": QUESTION}),
        ):
            assert request(path, credential, method, body) == (
                403,
                {"detail": "administrator_required"},
            ), "console_administrator_required"
    check_bootstrap(admin, state, read_state)
    check_reads(admin, state, read_state)
    check_source_summary(admin, phase)
    check_answers(admin, state, read_state)
    print(
        f"Administrator console scopes, registered sources and cited extractive QA passed ({phase})"
    )


if __name__ == "__main__":
    try:
        probe(sys.argv[1])
    except Exception:
        # Never dump HTTP response bodies, credentials, or local fixture contents.
        raise SystemExit("Administrator console container probe failed") from None
