"""Measure pinned SDK deletion/restore semantics using private synthetic data."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from hy_memory.snapshot import restore_local_data_plane, snapshot_local_data_plane
from one_memory.hy_memory_bridge import HyMemoryBridge, builtin_retrieval_snapshot

OLD = "CMH_SYNTHETIC_OLD: The project previously used a blue design."
NEW = "CMH_SYNTHETIC_NEW: The project now uses a green design."
RAW = "CMH_SYNTHETIC_RAW: Discuss changing the design from blue to green."


async def phase(root, action):
    state = json.loads((root / "state.json").read_text())
    bridge = HyMemoryBridge(
        profile=builtin_retrieval_snapshot(), api_key=None, data_dir=root / "data"
    )
    try:
        sdk = await bridge._get_client()
        if action == "seed":
            for key, content, options in (
                (
                    "old_id",
                    OLD,
                    {
                        "status": "superseded",
                        "is_latest": False,
                        "superseded_by": [state["new_id"]],
                    },
                ),
                ("new_id", NEW, {"supersedes": [state["old_id"]]}),
                ("foreign_id", "Unrelated synthetic project", {"user_id": state["foreign_scope"]}),
            ):
                result = await asyncio.to_thread(
                    sdk.import_memory_exact,
                    content,
                    memory_id=state[key],
                    agent_id="synthetic-agent",
                    metadata={"source_raw_memory_ids": [state["raw_id"]]},
                    **{"user_id": state["scope"], **options},
                )
                assert result["success"]
            await bridge.import_sdk_raw_exact(
                content=RAW,
                raw_id=state["raw_id"],
                user_id=state["scope"],
                agent_id="synthetic-agent",
                session_id="synthetic-session",
                source="synthetic-governance-fixture",
                memory_at=None,
                metadata={},
                request_id=str(uuid4()),
            )
            old = await bridge.get_sdk_memory(state["old_id"])
            assert old.status == "superseded" and not old.is_latest
            result = {"seeded": True}
        elif action == "delete":
            receipt = await bridge.delete(state["new_id"])
            assert receipt["success"] and await bridge.get_sdk_memory(state["new_id"]) is None
            old = await bridge.get_sdk_memory(state["old_id"])
            raw = await bridge.get_sdk_raw(state["raw_id"])
            history = await asyncio.to_thread(sdk.history, state["new_id"])
            result = {
                "sdk_delete_succeeded": True,
                "old_version_reactivated": old.status == "active" and old.is_latest,
                "raw_remains": raw is not None and raw.content == RAW,
                "deleted_text_in_history": NEW in json.dumps(history),
            }
            assert all(result.values()), "pinned_sdk_delete_semantics_changed"
            replacement = await asyncio.to_thread(
                sdk.import_memory_exact,
                NEW,
                memory_id=state["reimport_id"],
                user_id=state["scope"],
                agent_id="synthetic-agent",
                metadata={"source_raw_memory_ids": [state["raw_id"]]},
            )
            assert replacement["success"]
            recreated = await bridge.get_sdk_memory(state["reimport_id"])
            result["same_content_new_id_reimported"] = recreated.content == NEW
        elif action == "restored":
            head = await bridge.get_sdk_memory(state["new_id"])
            result = {
                "deleted_head_restored": head is not None and head.content == NEW,
                "restored_head_active": head.status == "active" and head.is_latest,
            }
            assert all(result.values()), "pinned_sdk_restore_semantics_changed"
        else:
            raise ValueError("unknown_probe_phase")
        foreign = await bridge.get_sdk_memory(state["foreign_id"])
        assert foreign is not None and foreign.user_id == state["foreign_scope"]
        result["foreign_scope_preserved"] = True
        (root / f"{action}-report.json").write_text(json.dumps(result, indent=2))
    finally:
        await bridge.close()


def probe():
    os.umask(0o077)
    sdk_version = version("hy-memory")
    assert sdk_version == "2.0.5.dev3", "unreviewed_sdk_version"
    root = Path(tempfile.mkdtemp(prefix="cmh-sdk-governance-")).resolve()
    state = {
        key: str(uuid4()) for key in ("old_id", "new_id", "raw_id", "foreign_id", "reimport_id")
    }
    state.update(scope="cmh:synthetic:project", foreign_scope="cmh:synthetic:foreign")
    (root / "state.json").write_text(json.dumps(state))

    def run(action):
        with (root / f"{action}.log").open("w") as log:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), action, str(root)],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=90,
            )
        if result.returncode:
            raise RuntimeError(f"governance_probe_{action}_failed")
        return json.loads((root / f"{action}-report.json").read_text())

    try:
        run("seed")
        # A child has exited: no SDK client/runtime can still write this directory.
        snapshot = snapshot_local_data_plane(
            source_root=root / "data", target_root=root / "snapshot", writers_stopped=True
        )
        deleted = run("delete")
        restored = restore_local_data_plane(
            snapshot_root=root / "snapshot",
            target_root=root / "data",
            protective_root=root / "after-delete",
            writers_stopped=True,
        )
        observed = run("restored")
        report = {
            "status": "observed_gaps",
            "sdk_version": sdk_version,
            "real_sdk": True,
            "external_model_called": False,
            **deleted,
            **observed,
            "snapshot_files": len(snapshot.files),
            "restored_files": restored.files_restored,
            "product_governance_validated": False,
        }
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        print(f"Private synthetic evidence: {root}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        probe()
    else:
        os.umask(0o077)
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1]))
