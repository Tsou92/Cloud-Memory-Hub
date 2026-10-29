import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cloud_memory_hub.chain_inventory import (
    ROUND_LAYER,
    THREAD_LAYER,
    chain_id,
    observe_chain,
    require_chain_origins,
)
from cloud_memory_hub.governance_inventory import _node, observe_inventory
from cloud_memory_hub.governance_journal import GovernanceError
from cloud_memory_hub.governance_plan import plan_relations
from cloud_memory_hub.privacy import PrivacyError
from hy_memory.client import HyMemoryClient
from hy_memory.coding.chain_store import HyCodeMem
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import _joined_thread
from one_memory.memory_server.durable_queue import COMPLETED
from test_original_archive import config_for
from test_output_lineage import accepted, fixture
from test_remote_ingest import provision
from test_sdk_context_capture import edit_messages
from test_sdk_reconcile import sdk_request


def populate(base, owner="scope|"):
    path = Path(base).parent / "hy_code_chain"
    store = HyCodeMem(str(path), owner=owner)
    first, merged, second = [], [], []
    try:
        store.observe(HyMemoryClient._chain_text(edit_messages()), observed_round_ids=first)
        store.observe(
            HyMemoryClient._chain_text(edit_messages(new="value=3\n")), observed_round_ids=merged
        )
        store.observe(
            HyMemoryClient._chain_text(edit_messages(said="Second version", new="value=4\n")),
            observed_round_ids=second,
        )
        assert first == merged and second != first
        store.db.execute("UPDATE thread SET body='Synthetic complete body' WHERE owner=?", (owner,))
        store.db.commit()
    finally:
        store.db.close()
    return path / "hy_code.db", first[0], second[0]


def scan(base, *, maximum=100, edges=100, byte_limit=1024 * 1024):
    return observe_chain(base, "scope", maximum, edges, byte_limit)


def test_native_round_merge_versions_thread_body_and_foreign_filter(tmp_path):
    base = tmp_path / "coding.db"
    path, first, second = populate(base)
    populate(base, "foreign|")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE round SET adds='not-json' WHERE thread_id IN "
            "(SELECT id FROM thread WHERE owner='foreign|')"
        )
    nodes = scan(base)
    assert len(nodes) == 3
    first, second = chain_id("r", first), chain_id("r", second)
    assert nodes[first].layer == nodes[second].layer == ROUND_LAYER
    assert nodes[second].versions == {first}
    assert nodes[first].versions == frozenset()
    threads = [node for node in nodes.values() if node.layer == THREAD_LAYER]
    assert len(threads) == 1 and threads[0].parents == {first, second}
    assert json.loads(threads[0].content)["body"] == "Synthetic complete body"
    assert (
        sum(
            line.startswith("@@") for line in json.loads(nodes[first].content)["patch"].splitlines()
        )
        == 2
    )
    assert not nodes[first].parents
    with pytest.raises(GovernanceError, match="lineage_incomplete"):
        require_chain_origins(nodes)


@pytest.mark.parametrize(
    "damage,code",
    [
        ("UPDATE round SET adds='broken'", "invalid_json"),
        ("UPDATE round SET adds='[false]'", "record_invalid"),
        ("UPDATE round SET seq=1", "sequence_invalid"),
        ("UPDATE round SET seq='bad'", "record_invalid"),
        ("UPDATE thread SET n_rounds=99", "sequence_invalid"),
        ("DELETE FROM round", "sequence_invalid"),
        ("DELETE FROM thread", "orphan"),
        ("UPDATE thread SET owner='scope|other-workspace'", "owner_unsupported"),
        ("INSERT INTO repo_symbol VALUES('symbol','other.py',1,'')", "index_unscoped"),
        ("DROP INDEX ix_round_t", "index_invalid"),
        ("ALTER TABLE thread ADD COLUMN unknown_body TEXT", "schema_unknown"),
        ("UPDATE thread SET lo=100,hi=1", "record_invalid"),
        ("UPDATE round SET created_at=-1", "record_invalid"),
    ],
)
def test_native_store_damage_is_rejected_without_repair(tmp_path, damage, code):
    base = tmp_path / "coding.db"
    path, _, _ = populate(base)
    with sqlite3.connect(path) as db:
        db.execute(damage)
    # JSON errors are intentionally normalized by the existing outer inventory boundary.
    with pytest.raises((GovernanceError, PrivacyError), match=code):
        scan(base)
    if "DROP INDEX" in damage:
        with sqlite3.connect(path) as db:
            assert not db.execute("PRAGMA index_info(ix_round_t)").fetchall()


@pytest.mark.parametrize("limit", ["rows", "edges", "bytes"])
def test_chain_budget_counts_aggregate_thread_and_versions(tmp_path, limit):
    base = tmp_path / "coding.db"
    populate(base)
    options = {"rows": {"maximum": 2}, "edges": {"edges": 1}, "bytes": {"byte_limit": 10}}
    with pytest.raises(GovernanceError, match="scan_limit"):
        scan(base, **options[limit])


def test_absent_chain_does_not_create_storage(tmp_path):
    assert scan(tmp_path / "coding.db") == {}
    assert not (tmp_path / "hy_code_chain").exists()


@pytest.mark.parametrize("damage", ["context", "round", "unavailable"])
def test_real_sdk_chain_proofs_http_scope_direction_and_restart(tmp_path, monkeypatch, damage):
    config = config_for(tmp_path)

    async def scenario():
        async with fixture(config) as (app, web):
            admin, headers, _, event = await provision(web, config)
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()

            async def no_semantic_output(*args, **kwargs):
                # Only semantic extraction is substituted. Native SDK chain and outcomes run.
                return dict(
                    success=True,
                    scene="productivity",
                    request_id=kwargs["request_id"],
                    experience_ids=[],
                    fact_ids=[],
                    memory_id=None,
                    history_request_ids=[],
                )

            async def writer(**kwargs):
                return SimpleNamespace(write=no_semantic_output)

            monkeypatch.setattr(sdk, "_get_coding_writer", writer)
            jobs, rounds = [], []
            queue = app.core.state.memory_write_queue
            for index in range(3):
                current = {
                    **event,
                    "event_id": str(uuid4()),
                    "record_id": str(uuid4()),
                    "content": {
                        "messages": [
                            {"role": message.role, "text": message.content}
                            for message in edit_messages(
                                said=f"Synthetic version {index}",
                                path="independent.py" if index == 2 else "synthetic.py",
                                new=f"value={index + 2}\n",
                            )
                        ]
                    },
                }
                job = await accepted(web, headers, current)
                frozen = queue.payload_for(job)
                request = sdk_request(frozen)
                result = await _joined_thread(
                    sdk.add,
                    [turn.model_dump() for turn in request.turns],
                    scene="coding",
                    user_id=request.user_id,
                    agent_id=request.agent_id,
                    session_id=request.session_id,
                    source=request.source_id,
                    request_id=request.request_id,
                )
                assert result["success"] and result["chain_context"]["round_ids"]
                await app.core.state.cmh_lineage.observe(job, frozen)
                queue.settle(job, state=COMPLETED)
                jobs.append(job)
                rounds.append(chain_id("r", result["chain_context"]["round_ids"][0]))
            assert queue.clear_terminal_payloads() == 3
            namespace = request.user_id
        async with fixture(config) as (app, web):
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            assert sdk._chain_store is None
            nodes = await observe_inventory(bridge, namespace, app.core.state.cmh_lineage)
            assert sdk._chain_store is None
            for job, identity in zip(jobs, rounds, strict=True):
                assert nodes[identity].parents == {"cmhe:" + job}
            selected = plan_relations(nodes, ("cmhe:" + jobs[0],))["record_ids"]
            assert rounds[0] in selected and rounds[1] in selected and rounds[2] not in selected
            assert "cmhe:" + jobs[1] not in selected and "cmhe:" + jobs[2] not in selected
            thread = next(
                node
                for node in nodes.values()
                if node.layer == THREAD_LAYER and rounds[0] in node.parents
            )
            assert thread.record_id in selected
            complete = plan_relations(nodes, (thread.record_id,))["record_ids"]
            assert {"cmhe:" + jobs[0], "cmhe:" + jobs[1]} <= set(complete)
            response = await web.post(
                "/hub/v1/governance/preview",
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "purge",
                    "record_ids": [thread.record_id],
                },
            )
            assert response.status_code == 200, response.text
            assert not response.json()["execution_available"]
            assert {item["record_id"] for item in response.json()["items"]} == set(complete)
            foreign = await web.post(
                "/hub/v1/governance/preview",
                headers=admin,
                json={"scope_id": "global", "action": "purge", "record_ids": [thread.record_id]},
            )
            assert foreign.status_code == 404 and "items" not in foreign.json()
            with pytest.raises(GovernanceError, match="lineage_incomplete"):
                await observe_inventory(bridge, namespace)
            if damage == "round":
                path = Path(sdk._config.coding.db_path).parent / "hy_code_chain" / "hy_code.db"
                with sqlite3.connect(path) as db:
                    native = int(rounds[0].rsplit(":", 1)[1])
                    owner_thread = db.execute(
                        "SELECT thread_id FROM round WHERE id=?", (native,)
                    ).fetchone()[0]
                    db.execute("DELETE FROM round WHERE id=?", (native,))
                    db.execute("UPDATE round SET seq=seq-1 WHERE thread_id=?", (owner_thread,))
                    db.execute("UPDATE thread SET n_rounds=n_rounds-1 WHERE id=?", (owner_thread,))
            else:
                with app.core.state.cmh_ingest._database(writing=True) as db:
                    if damage == "context":
                        db.execute("DELETE FROM cmh_event_contexts WHERE receipt_id=?", (jobs[0],))
                    else:
                        saved = db.execute(
                            "SELECT context_json FROM cmh_event_contexts WHERE receipt_id=?",
                            (jobs[0],),
                        ).fetchone()[0]
                        value = json.loads(saved)
                        value.update(chain_status="unavailable", chain_round_ids=[])
                        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
                        db.execute(
                            "UPDATE cmh_event_contexts SET context_json=?,context_digest=? "
                            "WHERE receipt_id=?",
                            (encoded, hashlib.sha256(encoded.encode()).hexdigest(), jobs[0]),
                        )
            response = await web.post(
                "/hub/v1/governance/preview",
                headers=admin,
                json={
                    "scope_id": event["scope_id"],
                    "action": "purge",
                    "record_ids": [thread.record_id],
                },
            )
            assert response.status_code == (409 if damage == "round" else 503)
            assert (
                response.json()["detail"]
                == {
                    "context": "governance_chain_lineage_incomplete",
                    "round": "governance_reference_unresolved",
                    "unavailable": "governance_chain_capture_incomplete",
                }[damage]
            )

    asyncio.run(scenario())


def test_ordinary_rows_cannot_impersonate_chain_records():
    with pytest.raises(GovernanceError, match="record_identity_mismatch"):
        _node({"node_id": "cmhc:r:1", "user_id": "scope"}, "scope")
