"""Measure whether ordinary SDK retrieval is sufficient for governance planning.

Uses only synthetic records and an opt-in graph in a disposable child process.
No production graph setting, model profile, source configuration or data is changed.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus
from one_memory.hy_memory_bridge import HyMemoryBridge, builtin_retrieval_snapshot


async def inspect(root):
    bridge = HyMemoryBridge(
        profile=builtin_retrieval_snapshot(), api_key=None, data_dir=root / "data"
    )
    ids = {
        key: str(uuid4())
        for key in ("raw", "old", "head", "summary", "active", "archived", "foreign")
    }
    scope, foreign = "cmh:synthetic:project", "cmh:synthetic:other"
    try:
        sdk = await bridge._get_client()
        assert sdk._graph_store is not None and sdk._graph_store._available
        await bridge.import_sdk_raw_exact(
            content="Synthetic conversation used to verify lineage.",
            raw_id=ids["raw"],
            user_id=scope,
            agent_id="synthetic-agent",
            session_id="synthetic-session",
            source="synthetic-governance-links",
            memory_at=None,
            metadata={},
            request_id=str(uuid4()),
        )
        for key, options in (
            ("old", {"status": "superseded", "is_latest": False, "superseded_by": [ids["head"]]}),
            ("head", {"supersedes": [ids["old"]]}),
            ("summary", {"layer": "l3_summary"}),
        ):
            result = await asyncio.to_thread(
                sdk.import_memory_exact,
                "Synthetic " + key,
                memory_id=ids[key],
                user_id=scope,
                agent_id="synthetic-agent",
                **options,
            )
            assert result["success"]

        async def seed_internal_links():
            # The singular anchor is a real MemoryNode storage field used by
            # summary/identity writers; exact import has no parameter for it.
            assert await sdk._vector_store.update_payload(
                ids["summary"], {"source_raw_memory_id": ids["raw"]}
            )
            graph = sdk._graph_store
            await graph.ensure_vdbref(ids["head"], MemoryLayer.L2_FACT.value)
            for key, owner, status in (
                ("active", scope, MemoryStatus.ACTIVE),
                ("archived", scope, MemoryStatus.ARCHIVED),
                ("foreign", foreign, MemoryStatus.ACTIVE),
            ):
                node = MemoryNode(
                    node_id=ids[key],
                    content="Synthetic schema " + key,
                    user_id=owner,
                    agent_id="synthetic-agent",
                    session_id="synthetic-session",
                    layer=MemoryLayer.L6_SCHEMA,
                    status=status,
                )
                assert await graph.upsert_memory_node(node) == ids[key]
                assert await graph.add_derived_from(ids[key], ids["head"])
            summary = await sdk._vector_store.get_by_id(ids["summary"])
            all_versions = await sdk._vector_store.list_by_user(
                user_id=scope,
                with_vectors=False,
                allow_raw_layer=True,
                limit=100,
            )
            reverse = await graph.find_referencing_memories([ids["head"]], limit=100)
            strict_edges = await graph._run(
                "MATCH (m:Memory)-[:DERIVED_FROM]->(v:VdbRef) "
                "WHERE m.user_id=$scope RETURN m.node_id, v.node_id",
                {"scope": scope},
            )
            original = graph._run

            async def failing_query(*args, **kwargs):
                raise RuntimeError("synthetic_graph_query_failure")

            graph._run = failing_query
            try:
                masked = await graph.get_evidence_vdbrefs(ids["active"])
            finally:
                graph._run = original
            return summary, all_versions, reverse, strict_edges, masked

        summary, all_versions, reverse, strict_edges, masked = await asyncio.to_thread(
            lambda: sdk._loop_thread.run(seed_internal_links())
        )
        public_summary = await bridge.get_sdk_memory(ids["summary"])
        default_page = await bridge.list_sdk_memories(user_id=scope, limit=100)
        reverse_ids = {row["node_id"] for row in reverse}
        edge_ids = {row[0] for row in strict_edges}
        report = {
            "status": "observed_gaps",
            "sdk_version": version("hy-memory"),
            "real_sdk_and_graph": True,
            "external_model_called": False,
            "singular_raw_anchor_in_storage": summary.source_raw_memory_id == ids["raw"],
            "singular_raw_anchor_omitted_from_bridge": public_summary.source_raw_ids == (),
            "superseded_missing_from_default_page": ids["old"]
            not in {r.memory_id for r in default_page.memories},
            "storage_scan_contains_raw_and_old": {ids["raw"], ids["old"]}.issubset(
                {n.node_id for n in all_versions}
            ),
            "reverse_helper_omits_archived": ids["archived"] not in reverse_ids,
            "reverse_helper_crosses_user_scope": ids["foreign"] in reverse_ids,
            "scoped_storage_query_includes_archived": edge_ids == {ids["active"], ids["archived"]},
            "evidence_query_failure_returns_empty": masked == [],
            "product_governance_validated": False,
        }
        assert report["sdk_version"] == "2.0.5.dev3"
        assert all(
            value
            for key, value in report.items()
            if key
            not in {
                "status",
                "sdk_version",
                "external_model_called",
                "product_governance_validated",
            }
        ), "governance_link_probe_expectation_changed"
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        await bridge.close()


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "inspect":
        asyncio.run(inspect(Path(sys.argv[2])))
        return
    root = Path(tempfile.mkdtemp(prefix="cmh-sdk-governance-links-")).resolve()
    try:
        with (root / "probe.log").open("w") as log:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "inspect", str(root)],
                env={**os.environ, "ONE_MEMORY_GRAPH_STORE": "kuzu"},
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=90,
            )
        if result.returncode:
            raise RuntimeError("governance_links_probe_failed")
        print((root / "report.json").read_text())
    finally:
        print(f"Private synthetic evidence: {root}")


if __name__ == "__main__":
    main()
