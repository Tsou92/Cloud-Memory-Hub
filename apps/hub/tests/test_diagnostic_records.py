"""FR-11 / ADR-0042: exact physical grants, not inferred log provenance."""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from cloud_memory_hub.governance_diagnostic_records import (
    MAX_RECORD_BYTES,
    RecordGrant,
    RecordInspector,
    WriteRequest,
    inspect_record,
)
from hy_memory.utils import diagnostic_policy as policy

BODY = "Synthetic legacy private orchard detail."
EMIT = r"""
import json, logging, os, socket, sys
from pathlib import Path
def forbidden(*a, **k):
    raise AssertionError("network_forbidden")
socket.create_connection = forbidden
socket.socket.connect = forbidden
payload = json.load(sys.stdin)
root = Path(payload["root"])
os.environ["MEMORY_LOG_LLM_CONTENT"] = "true"
from hy_memory.utils import diagnostic_policy as policy, log_setup
from hy_memory.utils.pipeline_log_writer import PipelineLogWriter
assert not policy.strict_diagnostics_enabled()
writer = PipelineLogWriter(str(root / "pipeline"))
for row in payload["rows"]:
    writer.write_step(**row)
writer.close()
log_setup.setup_logging(log_dir=str(root / "sdk"), level=logging.INFO)
logger = logging.getLogger("hy_memory.client")
handler = next(h for h in logging.getLogger("hy_memory").handlers
               if isinstance(h, logging.FileHandler))
formatters = (log_setup.SingleLineFormatter(log_setup._LOG_FORMAT, log_setup._DATE_FORMAT),
              log_setup.JsonLineFormatter())
for formatter in formatters:
    handler.setFormatter(formatter)
    for row in payload["rows"]:
        log_setup.set_request_id(row["request_id"])
        logger.warning(row["prompt"])
handler.flush()
assert not policy.strict_diagnostics_enabled()
# Transition only after the real legacy writer was closed; never downgrade.
policy.enable_strict_diagnostics()
writer = PipelineLogWriter(str(root / "strict-pipeline"))
writer.write_step(**(payload["rows"][0] | {"step": "READ_RECALL_VEC"}))
writer.close()
logger.warning(payload["rows"][0]["prompt"],
               exc_info=(ValueError, ValueError("synthetic error"), None))
record = logging.LogRecord("hy_memory", logging.ERROR, "<synthetic>", 1, "ignored", (), None)
del record.created
handler.handle(record)  # Real native fallback formatter -> real file handler.
record = logging.LogRecord("hy_memory", logging.ERROR, policy.__file__, 1, "ignored", (), None)
handler.handle(record)
handler.flush()
for item in logging.getLogger("hy_memory").handlers:
    item.close()
"""


@pytest.fixture(scope="module")
def native_records(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-diagnostic-records")
    instance = str(uuid4())
    request = "cmh2:" + ":".join((instance, str(uuid4()), str(uuid4()), str(uuid4())))
    other = "cmh2:" + ":".join((instance, str(uuid4()), str(uuid4()), str(uuid4())))
    user = "cmh:" + instance + ":" + str(uuid4())
    body = (
        BODY + " 中文\u2028line \\n\r\n" + f"2026-11-10 01:02:03 [INFO] [{other}] hy_memory: fake"
    )
    rows = [
        dict(
            request_id=request,
            user_id=user,
            agent_id="synthetic-agent",
            session_id="synthetic-session",
            step="EXTRACT",
            prompt=body,
            response=body,
            parsed=json.dumps({"reason": BODY, "count": 4}),
            memory_ids=[str(uuid4())],
            elapsed_ms=3,
            total_tokens=9,
        ),
        dict(
            request_id=other,
            user_id="cmh:" + instance + ":" + str(uuid4()),
            agent_id="synthetic-other-agent",
            session_id="synthetic-other-session",
            step="EXTRACT",
            prompt=BODY,
            response=BODY,
            parsed=json.dumps({"reason": BODY}),
            elapsed_ms=2,
            total_tokens=5,
        ),
    ]
    completed = subprocess.run(
        [sys.executable, "-c", EMIT],
        input=json.dumps({"root": str(root), "rows": rows}),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr

    def read(directory):
        files = list(directory.rglob("*.log"))
        assert len(files) == 1
        data = files[0].read_bytes()
        assert data.endswith(b"\n")
        return [line + b"\n" for line in data.split(b"\n")[:-1]]

    pipeline, sdk, strict = (
        read(root / "pipeline"),
        read(root / "sdk"),
        read(root / "strict-pipeline"),
    )
    assert len(pipeline) == 2 and len(sdk) == 7 and len(strict) == 1
    assert all(BODY.encode() in row for row in (*pipeline, *sdk[:4]))
    assert all(BODY.encode() not in row for row in (*strict, *sdk[4:]))
    return {
        "request": WriteRequest(request, user),
        "other_id": other,
        "pipeline": pipeline[0],
        "other_pipeline": pipeline[1],
        "text": sdk[0],
        "other_text": sdk[1],
        "json": sdk[2],
        "other_json": sdk[3],
        "strict_pipeline": strict[0],
        "strict_sdk": sdk[4],
        "fallback": sdk[5],
        "source": sdk[6],
    }


def inspect(native, key, *, mode="propose", grants=(), **kwargs):
    return inspect_record(
        native[key],
        kind="pipeline" if "pipeline" in key else "sdk",
        requests=(native["request"],),
        mode=mode,
        grants=grants,
        **kwargs,
    )


@pytest.mark.parametrize(
    "key,format",
    [("pipeline", "pipeline-jsonl-v1"), ("text", "sdk-text-v1"), ("json", "sdk-jsonl-v1")],
)
def test_native_legacy_requires_exact_record_grant(native_records, key, format):
    native = native_records
    candidate = inspect(native, key)
    assert candidate.action == "candidate" and candidate.reason is None
    assert candidate.grant == RecordGrant(
        format, native["request"].request_id, sha256(native[key]).hexdigest(), len(native[key])
    )
    assert inspect(native, key, mode="authorized").reason == "diagnostic_record_grant_missing"
    allowed = inspect(native, key, mode="authorized", grants=(candidate.grant,))
    assert allowed.action == "remove" and allowed.grant == candidate.grant
    # Identical copies have the same physical identity, independent of file path.
    assert inspect(native, key, mode="authorized", grants=(candidate.grant,)) == allowed
    assert BODY not in json.dumps(asdict(allowed))
    with pytest.raises(FrozenInstanceError):
        allowed.action = "keep"


@pytest.mark.parametrize("key", ["other_pipeline", "other_text", "other_json"])
def test_different_request_does_not_prove_absence_or_authorize_deletion(native_records, key):
    assert inspect(native_records, key).reason == "diagnostic_record_coverage_unproven"


@pytest.mark.parametrize("key", ["strict_pipeline", "strict_sdk", "fallback"])
def test_real_native_strict_metadata_is_kept_without_grants(native_records, key):
    assert inspect(native_records, key, mode="authorized").action == "keep"
    kind = "pipeline" if "pipeline" in key else "sdk"
    assert (
        inspect_record(native_records[key], kind=kind, requests=(), mode="authorized").action
        == "keep"
    )


def test_strict_code_location_requires_native_trusted_inventory(native_records, monkeypatch):
    monkeypatch.setattr(policy, "_KNOWN_SOURCES", {})
    assert inspect(native_records, "source").action == "blocked"
    monkeypatch.setattr(
        policy, "_KNOWN_SOURCES", {policy.__file__: "hy_memory/utils/diagnostic_policy.py"}
    )
    assert inspect(native_records, "source").action == "keep"


def encoded(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


@pytest.mark.parametrize("mutation", ["body", "timestamp", "whitespace"])
def test_changed_same_request_record_needs_new_grant(native_records, mutation):
    candidate = inspect(native_records, "pipeline")
    value = json.loads(native_records["pipeline"])
    if mutation == "body":
        value["content"] += " Changed."
    elif mutation == "timestamp":
        value["timestamp"] = "2020-01-01T00:00:00.000"
    raw = encoded(value) if mutation != "whitespace" else b" " + native_records["pipeline"]
    decision = inspect_record(
        raw,
        kind="pipeline",
        requests=(native_records["request"],),
        mode="authorized",
        grants=(candidate.grant,),
    )
    assert decision.reason == "diagnostic_record_grant_missing"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"namespace": "other"}, "scope_conflict"),
        ({"user_id": "other"}, "scope_conflict"),
        ({"step": "READ_RECALL_VEC"}, "coverage_unproven"),
    ],
)
def test_scope_conflict_and_read_do_not_become_whole_write_candidates(
    native_records, change, reason
):
    raw = encoded(json.loads(native_records["pipeline"]) | change)
    assert (
        inspect_record(
            raw, kind="pipeline", requests=(native_records["request"],), mode="propose"
        ).reason
        == "diagnostic_record_" + reason
    )


@pytest.mark.parametrize(
    "change",
    [
        {"content": BODY},
        {"namespace": BODY},
        {"request_id": BODY},
        {"extra": BODY},
        {"tokens": True},
        {"step": "READ_" + BODY},
        {"parsed": '{"diagnostic_policy":"strict-v1","content_omitted":true,"reason":"body"}'},
        {"parsed": '{"diagnostic_policy":"strict-v1","content_omitted":true,"count":true}'},
        {"parsed": '{"diagnostic_policy":"strict-v1","content_omitted":true,"count":1,"count":2}'},
    ],
)
def test_strict_pipeline_marker_never_excuses_free_values_or_extra_fields(native_records, change):
    raw = encoded(json.loads(native_records["strict_pipeline"]) | change)
    assert (
        inspect_record(
            raw, kind="pipeline", requests=(native_records["request"],), mode="propose"
        ).action
        == "blocked"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"msg": BODY},
        {"extra": BODY},
        {"logger": "hy_memory." + BODY},
        {"source": BODY},
        {"error_type": BODY},
        {"request_id": BODY},
        {"line": True},
        {"safe_stack": [BODY]},
        {"content_omitted": 1},
    ],
)
def test_strict_sdk_schema_is_closed(native_records, change):
    raw = encoded(json.loads(native_records["strict_sdk"]) | change)
    assert (
        inspect_record(
            raw, kind="sdk", requests=(native_records["request"],), mode="propose"
        ).action
        == "blocked"
    )


@pytest.mark.parametrize(
    "transform",
    [
        lambda row: row[:-1],
        lambda row: row + b"\n",
        lambda row: b"\xff" + row,
        lambda row: row.replace(b'"tokens": 9', b'"tokens": NaN'),
        lambda row: row.replace(b'"tokens": 9', b'"tokens": 1e999'),
        lambda row: row.replace(b'"tokens": 9', b'"tokens": 9, "tokens": 9'),
        lambda row: row.replace(b'"elapsed_ms": 3.0', b'"elapsed_ms": false'),
    ],
)
def test_invalid_physical_records_block_before_any_grant(native_records, transform):
    original = native_records["pipeline"]
    row = transform(original)
    assert row != original
    assert (
        inspect_record(
            row, kind="pipeline", requests=(native_records["request"],), mode="propose"
        ).reason
        == "diagnostic_record_invalid"
    )


def test_binary_lf_unicode_and_body_fake_headers_do_not_change_identity(native_records):
    assert b"\xe2\x80\xa8" in native_records["pipeline"]
    assert len(native_records["pipeline"].decode().splitlines()) > 1
    assert (
        inspect(native_records, "pipeline").grant.request_id == native_records["request"].request_id
    )
    assert inspect(native_records, "text").grant.request_id == native_records["request"].request_id


def test_budget_and_config_rejection_never_stringify_unknown_objects(native_records):
    class Unknown:
        def __str__(self):
            raise AssertionError("unexpected str")

        def __repr__(self):
            raise AssertionError("unexpected repr")

    inspector = RecordInspector((native_records["request"],), mode="propose", max_record_bytes=10)
    assert (
        inspector.inspect(native_records["pipeline"], kind="pipeline").reason
        == "diagnostic_record_budget_exceeded"
    )
    assert inspector.inspect(Unknown(), kind="pipeline").reason == "diagnostic_record_invalid"
    for kwargs in (
        {"mode": Unknown()},
        {"mode": "propose", "max_record_bytes": MAX_RECORD_BYTES + 1},
        {"mode": "propose", "grants": (Unknown(),)},
    ):
        with pytest.raises(ValueError, match="^diagnostic_record_selection_invalid$"):
            RecordInspector((native_records["request"],), **kwargs)


def test_record_grant_never_replaces_the_request_selection(native_records):
    grant = inspect(native_records, "pipeline").grant
    with pytest.raises(ValueError, match="^diagnostic_record_selection_invalid$"):
        RecordInspector((), mode="authorized", grants=(grant,))
    with pytest.raises(ValueError, match="^diagnostic_record_selection_invalid$"):
        replace(grant, format="unknown")
    with pytest.raises(ValueError, match="^diagnostic_record_selection_invalid$"):
        WriteRequest(str(uuid4()), native_records["request"].user_id)


@pytest.mark.parametrize(
    "change", [{"format": "sdk-jsonl-v1"}, {"sha256": "0" * 64}, {"byte_length": 1}]
)
def test_every_grant_field_participates_in_exact_match(native_records, change):
    grant = inspect(native_records, "pipeline").grant
    assert (
        inspect(
            native_records, "pipeline", mode="authorized", grants=(replace(grant, **change),)
        ).reason
        == "diagnostic_record_grant_missing"
    )


@pytest.mark.parametrize("step", ["GET", "LIST", "GET_USER_PROFILE", "GET_BASIC_INFO"])
def test_native_nonprefixed_read_steps_never_become_write_candidates(native_records, step):
    raw = encoded(json.loads(native_records["pipeline"]) | {"step": step})
    assert (
        inspect_record(
            raw, kind="pipeline", requests=(native_records["request"],), mode="propose"
        ).reason
        == "diagnostic_record_coverage_unproven"
    )


def test_write_reconciliation_search_query_keeps_exact_grant_path(native_records):
    raw = encoded(json.loads(native_records["pipeline"]) | {"step": "SEARCH_QUERY"})
    selection = {"kind": "pipeline", "requests": (native_records["request"],)}
    candidate = inspect_record(raw, mode="propose", **selection)
    assert candidate.action == "candidate"
    allowed = inspect_record(raw, mode="authorized", grants=(candidate.grant,), **selection)
    assert allowed.action == "remove"


def test_classifier_has_no_filesystem_side_effects(native_records, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected filesystem access")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert inspect(native_records, "pipeline").action == "candidate"
    assert inspect(native_records, "strict_pipeline").action == "keep"
