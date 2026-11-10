"""ENG-01: verify patched SDK origins inside the isolated Core pytest process."""

from importlib import import_module
from pathlib import Path

UPSTREAM_ROOT = Path(__file__).resolve().parents[1] / ".local/upstream/hy-memory"
MODULE_FILES = {
    "hy_memory": "hy_memory/__init__.py",
    "hy_memory.agent.anthropic_messages": "hy_memory/agent/anthropic_messages.py",
}


def verify_source(upstream: Path) -> None:
    for name, relative in MODULE_FILES.items():
        try:
            module = import_module(name)
        except ImportError:
            raise RuntimeError(f"core_sdk_source_unavailable:{name}") from None
        origin = getattr(module, "__file__", None)
        expected = upstream / relative
        if not isinstance(origin, str) or not expected.is_file():
            raise RuntimeError(f"core_sdk_source_mismatch:{name}")
        if Path(origin).resolve() != expected.resolve():
            raise RuntimeError(f"core_sdk_source_mismatch:{name}")


def pytest_sessionstart(session):
    import pytest

    try:
        verify_source(UPSTREAM_ROOT)
    except RuntimeError as error:
        raise pytest.UsageError(str(error)) from None
    print("Core SDK source binding verified: prepared patched checkout.")
