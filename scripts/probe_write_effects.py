"""Installed native write observations; synthetic extraction and local embedding.

The real SDK, Chroma, history, outcome cache and Hub lifecycle run in three
separate processes. This verifies observation/replay, never governance authority
or complete side-effect coverage. No test modules or external models are used.
Sibling dependency: probe_original_archive.py (environment only).
"""

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch
from uuid import uuid4

import hy_memory.pipelines.writer as native_writer
from cloud_memory_hub.app import create_hub
from cloud_memory_hub.config import HubConfig
from hy_memory.agent.llm_provider import LLMProvider
from hy_memory.agent.mem_agent import AgentResult, MemAgent
from one_memory.api import _sdk_library_bridge
from one_memory.hy_memory_bridge import LocalHyEmbedService, _joined_thread
from probe_original_archive import environment

CONTENT = "Synthetic effect observation describes a blue orchard gate beside a wooden bridge."


class SyntheticEmbedder:
    def encode(self, texts):
        return [[1.0] + [0.0] * 511 for _ in texts]


def installed_modules():
    for name in (
        "hy_memory.core.write_effects",
        "hy_memory.data.vector_store_chroma",
        "one_memory.hy_memory_bridge",
        "cloud_memory_hub.app",
    ):
        path = Path(importlib.import_module(name).__file__).resolve()
        assert "site-packages" in path.parts, "probe_requires_installed_packages"


def verify_observation(observed, method, request, ids):
    assert observed["schema_version"] == 1
    assert observed["observation_only"] is True
    assert observed["all_effects_proven"] is False
    assert observed["snapshot_sealed"] is True
    assert observed["status"] in {"recorded", "partial", "unsupported"}
    assert observed["request"] == {**request, "operation": method}
    assert observed["domains"]["diagnostic_files"] == "not_observed"
    attempted = {
        entry["selector"]["node_id"]
        for entry in observed["attempts"]
        if entry["domain"] == "chroma"
    }
    assert set(ids) <= attempted, "native_output_missing_from_observation"
    assert all(entry["state"] in {"returned", "uncertain"} for entry in observed["attempts"])
    assert CONTENT not in json.dumps(observed), "observation_copied_content"


async def phase(root, mode):
    os.environ.update(environment(root))
    extractions, model_attempts = [], []

    async def no_model(*args, **kwargs):
        model_attempts.append(True)
        raise AssertionError("external_model_forbidden_in_probe")

    async def extract(self, **kwargs):
        assert mode == "seed", "cached_replay_invoked_model"
        extractions.append(True)
        return AgentResult(success=True, extracted_info={"memory": [{"content": CONTENT}]})

    with (
        patch.object(MemAgent, "process_add", extract),
        patch.object(native_writer, "_RECONCILE_ENABLED", False),
        patch.object(LLMProvider, "complete", no_model),
        patch.object(LLMProvider, "complete_messages", no_model),
        patch.object(LocalHyEmbedService, "_shared_embedder", SyntheticEmbedder()),
    ):
        app = create_hub(HubConfig.from_environment())
        async with app.core.router.lifespan_context(app.core):
            await asyncio.sleep(0)
            app.core.state.memory_write_drain.stop()
            await app.core.state.memory_write_drain_task
            bridge = _sdk_library_bridge(app.core)
            sdk = await bridge._get_client()
            state_path = root / "observations.json"
            if mode == "seed":
                rows = []
                for method in ("add", "add_extracted"):
                    request = {
                        "request_id": str(uuid4()),
                        "user_id": "synthetic-observation-" + method,
                        "agent_id": "synthetic-agent",
                        "session_id": "synthetic-session",
                    }
                    options = {"scene": "chat"} if method == "add" else {}
                    result = await _joined_thread(
                        getattr(sdk, method), CONTENT, **request, **options
                    )
                    assert result["success"], "native_write_failed"
                    ids = set(result.get("memory_ids", [])) | {result["memory_id"]}
                    assert None not in ids and ids, "native_write_empty"
                    observed = result["write_effects"]
                    verify_observation(observed, method, request, ids)
                    # Verify actual persisted history, not just the new field.
                    with sqlite3.connect(sdk._config.history.db_path) as db:
                        history_ids = {
                            row[0]
                            for row in db.execute("SELECT DISTINCT memory_id FROM memory_history")
                            if row[0] in ids
                        }
                    history_attempts = {
                        entry["selector"]["memory_id"]
                        for entry in observed["attempts"]
                        if entry["domain"] == "history"
                    }
                    # Fixed upstream add records the raw in history;
                    # add_extracted does not create native history rows.
                    if method == "add":
                        assert history_ids and history_ids <= history_attempts
                    else:
                        assert not history_ids and not history_attempts
                    saved = await bridge.get_sdk_write_outcome(request["request_id"])
                    assert saved["status"] == "succeeded"
                    assert saved["result"]["write_effects"] == observed
                    rows.append(
                        {
                            "method": method,
                            "request": request,
                            "ids": sorted(ids),
                            "value": observed,
                        }
                    )
                assert len(extractions) == 1
                assert not model_attempts
                assert set(rows[0]["ids"]).isdisjoint(rows[1]["ids"])
                with state_path.open("x") as output:
                    json.dump(rows, output)
                state_path.chmod(0o600)
                return
            for row in json.loads(state_path.read_text()):
                method, request = row["method"], row["request"]
                saved = await bridge.get_sdk_write_outcome(request["request_id"])
                assert saved["status"] == "succeeded"
                assert saved["result"]["write_effects"] == row["value"]
                verify_observation(row["value"], method, request, row["ids"])
                for identity in row["ids"]:
                    node = await bridge.get_sdk_memory(identity)
                    if node is None:
                        node = await bridge.get_sdk_raw(identity)
                    assert node is not None, "persisted_output_missing"
                replay = await _joined_thread(
                    getattr(sdk, method),
                    CONTENT,
                    **request,
                    **({"scene": "chat"} if method == "add" else {}),
                )
                assert replay["success"] and replay["replayed"]
                assert replay["write_effects"] == row["value"]
            assert not extractions
            assert not model_attempts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--phase", choices=("seed", "read"))
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.installed:
        installed_modules()
    if args.phase:
        if args.root is None:
            parser.error("--phase requires --root")
        asyncio.run(phase(args.root, args.phase))
        return
    if args.root:
        parser.error("--root requires --phase")
    root = Path(mkdtemp(prefix="cmh-write-effects-"))
    try:
        with (root / "init.log").open("w") as log:
            subprocess.run(
                [sys.executable, "-m", "cloud_memory_hub", "init"],
                env=environment(root),
                stdout=log,
                stderr=log,
                check=True,
                timeout=60,
            )
        for index, mode in enumerate(("seed", "read", "read")):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--phase",
                mode,
                "--root",
                str(root),
            ]
            if args.installed:
                command.append("--installed")
            with (root / f"phase-{index}.log").open("w") as log:
                subprocess.run(command, stdout=log, stderr=log, timeout=120, check=True)
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError(
            f"write_effect_probe_failed; private evidence retained at {root}"
        ) from None
    else:
        shutil.rmtree(root)
    print(
        "Native mutation observation, persisted history and replay passed across three processes."
    )


if __name__ == "__main__":
    main()
