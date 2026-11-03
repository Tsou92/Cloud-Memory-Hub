"""Installed version ancestry using the existing Hub history probe transport."""

import asyncio
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_chat_graph import phase
from probe_original_archive import environment


def main():
    if len(sys.argv) == 3:
        assert sys.argv[1] in {"seed", "read", "old-read"}
        asyncio.run(phase(Path(sys.argv[2]), sys.argv[1], memory_reconcile=True))
        return
    with TemporaryDirectory(prefix="cmh-memory-graph-") as temporary:
        root = Path(temporary)
        subprocess.run(
            [sys.executable, "-m", "cloud_memory_hub", "init"],
            env=environment(root),
            check=True,
            capture_output=True,
        )
        for index, mode in enumerate(("seed", "read", "read")):
            with (root / f"phase-{index}.log").open("w") as log:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
                    stdout=log,
                    stderr=log,
                    timeout=120,
                )
            if result.returncode:
                raise RuntimeError("memory_graph_probe_failed_" + mode)
    print("Memory version ancestry, scoped reads and preview persisted across processes.")


if __name__ == "__main__":
    main()
