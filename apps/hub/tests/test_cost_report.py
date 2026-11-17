"""Cost estimation is an estimate with named gaps, never a guessed bill."""

import sqlite3
from types import SimpleNamespace

import pytest
from cloud_memory_hub.console_api import usage_routes
from cloud_memory_hub.cost_report import (
    BUILTIN_PRICES,
    build_cost_report,
    estimate_cost,
    load_prices,
)
from cloud_memory_hub.remote_access import AccessError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient


def _database(path, rows):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE model_request_audit ("
        "model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, "
        "cached_prompt_tokens INTEGER, total_tokens INTEGER)"
    )
    connection.executemany(
        "INSERT INTO model_request_audit VALUES (?,?,?,?,?)",
        rows,
    )
    connection.commit()
    connection.close()


def test_prices_known_models_and_names_unknown_ones(tmp_path):
    database = tmp_path / "one-memory.sqlite3"
    _database(
        database,
        [
            ("claude-opus-5-5", 1_000_000, 1_000_000, 0, 2_000_000),
            ("custom-gateway-model", 10, 1, 0, 11),
        ],
    )
    report = build_cost_report(tmp_path, environ={})
    assert report["available"] is True
    assert report["currency"] == "USD"
    assert report["estimated"] is True
    assert report["total"] == 24.0
    assert report["priced_requests"] == 1
    assert report["unpriced_requests"] == 1
    assert report["unpriced_models"] == ["custom-gateway-model"]
    priced = {item["model"]: item for item in report["models"]}
    assert priced["claude-opus-5-5"]["estimated_cost"] == 24.0
    assert priced["custom-gateway-model"]["estimated_cost"] is None


def test_cache_reads_are_not_billed_twice():
    total = estimate_cost(
        [("claude-opus-5-5", 1, 1_000_000, 0, 500_000, 1_000_000)], BUILTIN_PRICES
    )
    # 500k fresh input at $4/M plus 500k cache reads at $0.20/M.
    assert total["total"] == 2.1


def test_cached_tokens_are_clamped_to_prompt_tokens():
    total = estimate_cost([("claude-opus-5-5", 1, 100, 0, 1_000_000, 100)], BUILTIN_PRICES)
    assert total["total"] == round(100 * 0.20 / 1_000_000, 6)


def test_provider_prefixed_model_names_use_the_builtin_price():
    total = estimate_cost(
        [("anthropic/claude-opus-5-5", 1, 1_000_000, 0, 0, 1_000_000)], BUILTIN_PRICES
    )
    assert total["total"] == 4.0


def test_configuration_overrides_the_builtin_table(tmp_path):
    prices, source = load_prices(
        {"CMH_MODEL_PRICES": '{"claude-opus-5-5":{"input":1,"output":2,"cache_read":0.5}}'}
    )
    assert source == "configured"
    assert prices["claude-opus-5-5"] == (1.0, 2.0, 0.5)
    database = tmp_path / "one-memory.sqlite3"
    _database(database, [("claude-opus-5-5", 1_000_000, 1_000_000, 0, 2_000_000)])
    report = build_cost_report(
        tmp_path,
        environ={"CMH_MODEL_PRICES": '{"claude-opus-5-5":{"input":1,"output":2,"cache_read":0.5}}'},
    )
    assert report["prices_source"] == "configured"
    assert report["total"] == 3.0


@pytest.mark.parametrize("raw", ["not json", "[]", '{"m":{"input":-1,"output":1}}', "{}"])
def test_broken_configuration_falls_back_to_the_builtin_table(raw):
    prices, source = load_prices({"CMH_MODEL_PRICES": raw})
    assert prices is BUILTIN_PRICES and source == "builtin_config_invalid"


def test_missing_audit_store_is_reported_not_zeroed(tmp_path):
    report = build_cost_report(tmp_path, environ={})
    assert report["available"] is False
    assert report["reason"] == "audit_store_missing"
    assert "total" not in report


def _client(core):
    class Admin:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                scope.setdefault("state", {})["cmh_admin"] = True
            await self.app(scope, receive, send)

    def access_error(_request: Request, error: AccessError):
        return JSONResponse({"detail": error.code}, status_code=error.status)

    return TestClient(
        Starlette(
            routes=usage_routes(core),
            middleware=[Middleware(Admin)],
            exception_handlers={AccessError: access_error},
        )
    )


def _core(tmp_path, report=None):
    class Store:
        def report(self):
            return report or {
                "request_count": 2,
                "succeeded": 2,
                "failed": 0,
                "in_flight": 0,
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            }

    return SimpleNamespace(
        state=SimpleNamespace(
            model_request_audit_store=Store(),
            settings=SimpleNamespace(data_dir=tmp_path),
        )
    )


def test_usage_endpoint_adds_cost_without_touching_the_existing_fields(tmp_path):
    _database(
        tmp_path / "one-memory.sqlite3",
        [("claude-opus-5-5", 1_000_000, 1_000_000, 0, 2_000_000)],
    )
    client = _client(_core(tmp_path))
    response = client.get("/hub/v1/console/usage")
    assert response.status_code == 200
    body = response.json()
    assert body["request_count"] == 2
    assert body["billing"] == "provider_statement_is_authoritative"
    assert body["cost"]["available"] is True
    assert body["cost"]["total"] == 24.0
    assert client.get("/hub/v1/console/usage?x=1").status_code == 422


def test_usage_endpoint_without_settings_still_answers(tmp_path):
    core = _core(tmp_path)
    core.state.settings = None
    client = _client(core)
    body = client.get("/hub/v1/console/usage").json()
    assert body["cost"] == {
        "schema_version": 1,
        "available": False,
        "reason": "settings_missing",
    }
