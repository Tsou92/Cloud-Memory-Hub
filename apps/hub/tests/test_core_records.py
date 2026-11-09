"""ADR-0047: native Core bytes, pure closed classification and lossless framing."""

import importlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest
from hy_memory.utils import diagnostic_policy as policy

BODY = "Synthetic Core diagnostic prose."
EMIT = r"""
import json, logging, socket, sys
from pathlib import Path
def forbidden(*args, **kwargs):
    raise AssertionError("network_forbidden")
socket.create_connection = forbidden
socket.socket.connect = forbidden
payload = json.load(sys.stdin)
root, mode = Path(payload["root"]).resolve(), payload["mode"]
from hy_memory.utils import diagnostic_policy as policy
from one_memory.diagnostic_log import DiagnosticLog
from one_memory.log_file import start_logging_to_file
assert not policy.strict_diagnostics_enabled()
if mode == "strict":
    policy.enable_strict_diagnostics()
destination = start_logging_to_file(root)
handler = next(h for h in logging.getLogger().handlers
               if getattr(h, "baseFilename", None) == str(destination.resolve()))
logger = logging.getLogger("one_memory.synthetic")
body = "Synthetic Core diagnostic prose."
if mode == "large":
    logger.warning("X" * (4 * 1024 * 1024))
elif mode == "embedded":
    record = logging.LogRecord("one_memory.synthetic", logging.INFO,
                               "<synthetic>", 1, "omitted", (), None)
    record.created = 0
    strict = policy.StrictDiagnosticFormatter().format(record)
    logger.warning("before\n2026-11-10 01:02:03 ERROR fake\n" + strict + "\nafter")
else:
    logger.warning(body + " 中文\u2028section\r\nsecond physical line")
    try:
        raise ValueError(body)
    except ValueError:
        logger.exception(body)
    if mode == "strict":
        record = logging.LogRecord("one_memory.synthetic", logging.ERROR,
                                   "<synthetic>", 1, body, (), None)
        del record.created
        handler.handle(record)
    log = DiagnosticLog(root)
    log.write(level="error", component="core.database", event="database.error",
              outcome="failed", trace_id="00000000-0000-0000-0000-000000000001",
              details={"reason": json.dumps({"nested": body}), "count": 3},
              error_type="OperationalError", duration_ms=7)
    assert log.close()
handler.flush()
logging.getLogger().removeHandler(handler)
handler.close()
"""


def emit_native(root, mode):
    result = subprocess.run(
        [sys.executable, "-c", EMIT],
        input=json.dumps({"root": str(root), "mode": mode}),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        "core-log": (root / "logs/core.log").read_bytes(),
        **(
            {"core-diagnostic": (root / "logs/diagnostic-core.jsonl").read_bytes()}
            if mode in {"legacy", "strict"}
            else {}
        ),
    }


def records():
    return importlib.import_module("cloud_memory_hub.governance_core_records")


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    root = tmp_path_factory.mktemp("core-native-records")
    return {mode: emit_native(root / mode, mode) for mode in ("legacy", "strict", "embedded")}


def lines(raw):
    assert raw.endswith(b"\n")
    return tuple(item + b"\n" for item in raw.split(b"\n")[:-1])


def frame(raw, domain, *, chunks=None, **kwargs):
    framer = records().CoreFramer(domain, **kwargs)
    result = []
    for chunk in (raw,) if chunks is None else chunks:
        result.extend(framer.feed(chunk))
    result.extend(framer.finish())
    assert b"".join(item.raw for item in result) == raw
    offset = 0
    for item in result:
        assert item.domain == domain and item.offset == offset
        offset += len(item.raw)
    return tuple(result)


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_actual_strict_core_files_are_kept_byte_for_byte(native, domain):
    raw = native["strict"][domain]
    assert BODY.encode() not in raw
    assert all(records().classify_core_line(line, domain=domain) == "strict" for line in lines(raw))
    result = frame(raw, domain)
    assert tuple(item.raw for item in result) == lines(raw)
    assert all(item.kind == "strict" for item in result)


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_actual_legacy_core_prose_is_one_maximal_raw_block(native, domain):
    raw = native["legacy"][domain]
    assert BODY.encode() in raw
    result = frame(raw, domain)
    assert len(result) == 1 and result[0].kind == "raw" and result[0].raw == raw
    assert BODY not in repr(result[0])
    with pytest.raises(FrozenInstanceError):
        result[0].offset = 1


def test_native_multiline_message_contains_strict_fragment_without_event_inference(native):
    raw = native["embedded"]["core-log"]
    result = frame(raw, "core-log")
    assert [item.kind for item in result] == ["raw", "strict", "raw"]
    assert b"2026-11-10 01:02:03 ERROR fake\n" in result[0].raw
    assert result[2].raw == b"after\n"


def test_native_four_mebibyte_legacy_message_is_supported(tmp_path):
    raw = emit_native(tmp_path, "large")["core-log"]
    assert len(raw) > 4 * 1024 * 1024
    result = frame(raw, "core-log", chunks=(raw[i : i + 65536] for i in range(0, len(raw), 65536)))
    assert len(result) == 1 and result[0].kind == "raw"


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
@pytest.mark.parametrize("size", (1, 7, 127, 65536))
def test_chunk_boundaries_do_not_change_any_frame(native, domain, size):
    strict = native["strict"][domain]
    raw = b"\xff\x00\r\n" + native["legacy"][domain] + strict + b"tail\r\xe2\x80\xa8"
    expected = frame(raw, domain)
    actual = frame(raw, domain, chunks=(raw[i : i + size] for i in range(0, len(raw), size)))
    assert actual == expected
    assert actual[-1].kind == "raw" and actual[-1].raw == b"tail\r\xe2\x80\xa8"


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_every_single_split_including_utf8_and_newline_is_equivalent(native, domain):
    raw = b"before\xe2\x80\xa8\n" + lines(native["strict"][domain])[0] + b"after"
    expected = frame(raw, domain)
    for split in range(len(raw) + 1):
        assert frame(raw, domain, chunks=(raw[:split], b"", raw[split:])) == expected


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_unterminated_strict_looking_tail_is_raw(native, domain):
    strict = lines(native["strict"][domain])[0]
    raw = b"before\n" + strict[:-1]
    result = frame(raw, domain)
    assert len(result) == 1 and result[0].kind == "raw"
    assert records().classify_core_line(strict[:-1], domain=domain) == "raw"


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_strict_shape_is_domain_specific(native, domain):
    other = "core-log" if domain == "core-diagnostic" else "core-diagnostic"
    assert records().classify_core_line(lines(native["strict"][other])[0], domain=domain) == "raw"


@pytest.mark.parametrize(
    "change",
    (
        {"schema_version": True},
        {"content_omitted": 1},
        {"component": BODY},
        {"event": BODY},
        {"service": BODY},
        {"request_id": BODY},
        {"trace_id": "CMH-free-string"},
        {"duration_ms": True},
        {"duration_ms": 3.0},
        {"error_type": BODY},
        {"details": {"reason": BODY}},
        {"details": {"source": BODY}},
        {"details": {"owner": BODY}},
        {"details": {"stats": {"reason": BODY}}},
        {"safe_stack": ["arbitrary/path.py:7"]},
        {"extra": BODY},
        {"timestamp": "invalid"},
        {"timestamp": "2026-11-10T12:13:14"},
        {"timestamp": "2026-11-10T12:13:14Z"},
        {"timestamp": None},
    ),
)
def test_forged_core_metadata_never_becomes_a_strict_separator(native, change):
    value = json.loads(lines(native["strict"]["core-diagnostic"])[0])
    raw = json.dumps(value | change, ensure_ascii=False).encode() + b"\n"
    assert records().classify_core_line(raw, domain="core-diagnostic") == "raw"
    assert len(frame(b"before\n" + raw + b"after\n", "core-diagnostic")) == 1


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_duplicate_keys_nonfinite_and_invalid_framing_remain_raw(native, domain):
    strict = lines(native["strict"][domain])[0]
    cases = (
        strict[:-2] + b',"content_omitted":true}\n',
        strict[:-2] + b',"extra":NaN}\n',
        strict[:-2] + b',"extra":Infinity}\n',
        strict.replace(b"\n", b"\r\n"),
        b"\xff" + strict,
        b"\n" + strict,
        b"[]\n",
        b"true\n",
        b'{"diagnostic_policy":"strict-v1","content_omitted":true}\n',
    )
    assert all(records().classify_core_line(raw, domain=domain) == "raw" for raw in cases)


def test_core_timestamp_precheck_never_calls_projector_clock(native, monkeypatch):
    class ClockForbidden:
        fromisoformat = datetime.fromisoformat

        @staticmethod
        def now(*args, **kwargs):
            raise AssertionError("clock_forbidden")

    monkeypatch.setattr(policy, "datetime", ClockForbidden)
    raw = lines(native["strict"]["core-diagnostic"])[0]
    assert records().classify_core_line(raw, domain="core-diagnostic") == "strict"
    value = json.loads(raw)
    for timestamp in (None, "invalid", "2026-11-10", "2026-11-10T12:13:14"):
        candidate = json.dumps(value | {"timestamp": timestamp}).encode() + b"\n"
        assert records().classify_core_line(candidate, domain="core-diagnostic") == "raw"


def test_closed_nested_metadata_and_trusted_code_locations_are_supported(native, monkeypatch):
    trusted = "one_memory/diagnostic_log.py"
    monkeypatch.setattr(policy, "_KNOWN_SOURCES", {"/synthetic/code.py": trusted})
    value = json.loads(lines(native["strict"]["core-diagnostic"])[0])
    value.update(
        safe_stack=[trusted + ":17"],
        details={"counts": {"count": 2, "timed_out": False}, "status": "failed"},
        duration_ms=0,
    )
    raw = json.dumps(value).encode() + b"\n"
    assert records().classify_core_line(raw, domain="core-diagnostic") == "strict"
    sdk = json.loads(lines(native["strict"]["core-log"])[0])
    sdk.update(source=trusted, safe_stack=[trusted + ":17"])
    sdk_raw = json.dumps(sdk).encode() + b"\n"
    assert records().classify_core_line(sdk_raw, domain="core-log") == "strict"
    monkeypatch.setattr(policy, "_KNOWN_SOURCES", {})
    assert records().classify_core_line(raw, domain="core-diagnostic") == "raw"
    assert records().classify_core_line(sdk_raw, domain="core-log") == "raw"


@pytest.mark.parametrize("domain", ("core-log", "core-diagnostic"))
def test_excessive_json_structure_and_single_line_parse_budget_remain_raw(native, domain):
    strict = lines(native["strict"][domain])[0]
    cases = (
        b"[" * 40 + b"0" + b"]" * 40 + b"\n",
        b"[" + b"0," * 20_001 + b"0]\n",
        b" " * (2 * 1024 * 1024) + strict,
    )
    for raw in cases:
        assert records().classify_core_line(raw, domain=domain) == "raw"
        result = frame(raw, domain)
        assert len(result) == 1 and result[0].kind == "raw"


def test_raw_budget_failure_poisoning_prevents_any_successful_continuation(native):
    module = records()
    strict = lines(native["strict"]["core-log"])[0]
    framer = module.CoreFramer("core-log", max_raw_block_bytes=8)
    assert framer.feed(b"1234") == ()
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_budget_exceeded$"):
        framer.feed(b"56789\n" + strict)
    for call in (lambda: framer.feed(strict), framer.finish):
        with pytest.raises(module.CoreRecordError, match="^diagnostic_core_framer_failed$"):
            call()


def test_failure_does_not_return_an_earlier_frame_from_the_same_feed(native):
    module = records()
    framer = module.CoreFramer("core-log", max_raw_block_bytes=2)
    raw = lines(native["strict"]["core-log"])[0] + b"xxx\n"
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_budget_exceeded$"):
        framer.feed(raw)
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_framer_failed$"):
        framer.finish()


def test_exact_budget_empty_input_and_long_line_raw_mode(native):
    assert frame(b"", "core-log") == ()
    assert frame(b"abcd", "core-log", max_raw_block_bytes=4)[0].raw == b"abcd"
    strict = lines(native["strict"]["core-log"])[0]
    result = frame(strict, "core-log", max_line_bytes=1)
    assert len(result) == 1 and result[0].kind == "raw"


def test_finish_budget_failure_is_permanent():
    module = records()
    framer = module.CoreFramer("core-log", max_raw_block_bytes=3)
    assert framer.feed(b"a\nbc") == ()
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_budget_exceeded$"):
        framer.finish()
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_framer_failed$"):
        framer.finish()


@pytest.mark.parametrize("domain", (None, "sdk", "core", 1))
def test_invalid_domain_has_only_a_fixed_error(domain):
    module = records()
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_invalid$"):
        module.CoreFramer(domain)
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_invalid$"):
        module.classify_core_line(b"body", domain=domain)


@pytest.mark.parametrize("name", ("max_line_bytes", "max_raw_block_bytes"))
@pytest.mark.parametrize("value", (0, -1, True, 1.0, "2", 33 * 1024 * 1024))
def test_invalid_budget_has_only_a_fixed_error(name, value):
    module = records()
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_invalid$"):
        module.CoreFramer("core-log", **{name: value})


def test_invalid_chunk_poisons_framer_and_finished_framer_rejects_reuse():
    module = records()
    framer = module.CoreFramer("core-log")
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_invalid$"):
        framer.feed(bytearray(b"synthetic"))
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_framer_failed$"):
        framer.finish()
    with pytest.raises(module.CoreRecordError, match="^diagnostic_core_invalid$"):
        module.classify_core_line("synthetic", domain="core-log")
    closed = module.CoreFramer("core-log")
    assert closed.finish() == ()
    for call in (lambda: closed.feed(b""), closed.finish):
        with pytest.raises(module.CoreRecordError, match="^diagnostic_core_framer_closed$"):
            call()
