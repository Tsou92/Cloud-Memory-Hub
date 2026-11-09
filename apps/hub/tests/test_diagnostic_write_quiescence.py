"""FR-10/FR-11, ADR-0040: the installed probe's real HTTP/native-I/O scenario.

No subprocess result stands in for assertions: pytest awaits the same scenario
directly against its isolated Hub fixture. Models are synthetic and hard-guarded.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from test_original_archive import config_for
from test_output_lineage import fixture
from test_remote_ingest import provision

_PATH = Path(__file__).resolve().parents[3] / "scripts" / "probe_diagnostic_quiescence.py"
_SPEC = importlib.util.spec_from_file_location("diagnostic_quiescence_probe", _PATH)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("sink", ["jsonl", "trace"])
@pytest.mark.parametrize("interruption", ["none", "cancel", "timeout"])
def test_http_purge_waits_for_native_diagnostics_without_resurrection(tmp_path, sink, interruption):
    config = config_for(tmp_path)
    delay = probe.DelayedDiagnostic(sink)

    async def scenario():
        with probe.instrumentation(config, delay):
            async with fixture(config) as (app, web):
                try:
                    admin, headers, _, template = await provision(web, config)
                    await probe.verify_case(
                        app,
                        web,
                        config,
                        admin,
                        headers,
                        template,
                        delay,
                        interruption=interruption,
                    )
                finally:
                    delay.release.set()

    asyncio.run(scenario())
