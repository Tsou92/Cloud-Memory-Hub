"""Estimated model spend for the administrator console.

The Core audit store already records one row per model call with its model
name and token counts; this module turns those rows into an estimate.

The number is an estimate, never a bill. Prices are first-party Anthropic API
rates cached on the date below, and a model the table does not know is reported
as unpriced instead of being guessed, so a custom gateway name cannot silently
produce a wrong figure. Operators can override the table with the
``CMH_MODEL_PRICES`` environment variable.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

PRICE_TABLE_DATE = "2026-11-03"
PRICE_ENV = "CMH_MODEL_PRICES"

# USD per million tokens: (input, output, cache read).
BUILTIN_PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.20),
    "claude-fable-5": (10.0, 50.0, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.20),
    "claude-opus-4-8": (5.0, 25.0, 0.20),
    "claude-opus-4-7": (5.0, 25.0, 0.20),
    "claude-opus-4-6": (5.0, 25.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.10),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}

AUDIT_DATABASE = "one-memory.sqlite3"


def _price_for(model: str, prices: dict[str, tuple[float, float, float]]):
    """Return a price for the model name, tolerating a provider prefix.

    Only the last path segment is tried as a fallback; no fuzzy rewriting of
    version suffixes, because a wrong model match would silently misprice.
    """
    if model in prices:
        return prices[model]
    tail = model.rsplit("/", 1)[-1]
    return prices.get(tail)


def load_prices(environ=None):
    """Return (prices, source). A broken override falls back to the built-in table."""
    environment = os.environ if environ is None else environ
    raw = environment.get(PRICE_ENV)
    if not raw or not raw.strip():
        return BUILTIN_PRICES, "builtin"
    try:
        parsed = json.loads(raw)
    except ValueError:
        return BUILTIN_PRICES, "builtin_config_invalid"
    if not isinstance(parsed, dict):
        return BUILTIN_PRICES, "builtin_config_invalid"
    prices: dict[str, tuple[float, float, float]] = {}
    for model, value in parsed.items():
        if not isinstance(model, str) or not model:
            continue
        if not isinstance(value, dict):
            continue
        try:
            input_rate = float(value["input"])
            output_rate = float(value["output"])
            cache_rate = float(value.get("cache_read", input_rate))
        except (KeyError, TypeError, ValueError):
            continue
        if min(input_rate, output_rate, cache_rate) < 0:
            continue
        prices[model] = (input_rate, output_rate, cache_rate)
    if not prices:
        return BUILTIN_PRICES, "builtin_config_invalid"
    return prices, "configured"


def read_model_totals(database: Path):
    """Per-model token totals, or None when the audit store cannot be read."""
    path = Path(database)
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
                "SUM(cached_prompt_tokens), SUM(total_tokens) "
                "FROM model_request_audit GROUP BY model ORDER BY SUM(total_tokens) DESC"
            ).fetchall()
    except sqlite3.Error:
        return None


def estimate_cost(rows, prices):
    total = 0.0
    priced_requests = 0
    unpriced_requests = 0
    models = []
    for model, requests, prompt, completion, cached, total_tokens in rows:
        prompt_tokens = int(prompt or 0)
        completion_tokens = int(completion or 0)
        cached_tokens = int(cached or 0)
        # The audit store records cache reads alongside prompt tokens without
        # promising which contains the other; clamp so they are never billed twice.
        cached_tokens = min(cached_tokens, prompt_tokens)
        entry = {
            "model": model if isinstance(model, str) else str(model),
            "requests": int(requests or 0),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_prompt_tokens": cached_tokens,
            "total_tokens": int(total_tokens or 0),
        }
        price = _price_for(entry["model"], prices)
        if price is None:
            entry["estimated_cost"] = None
            unpriced_requests += entry["requests"]
        else:
            input_rate, output_rate, cache_rate = price
            usd = (
                (prompt_tokens - cached_tokens) * input_rate
                + cached_tokens * cache_rate
                + completion_tokens * output_rate
            ) / 1_000_000
            entry["estimated_cost"] = round(usd, 6)
            total += usd
            priced_requests += entry["requests"]
        models.append(entry)
    return {
        "total": round(total, 6),
        "priced_requests": priced_requests,
        "unpriced_requests": unpriced_requests,
        "models": models,
        "unpriced_models": [item["model"] for item in models if item["estimated_cost"] is None],
    }


def build_cost_report(data_dir: Path, environ=None):
    """Cost section for /hub/v1/console/usage. Content-free and read-only."""
    prices, source = load_prices(environ)
    base = {
        "schema_version": 1,
        "currency": "USD",
        "estimated": True,
        "prices_source": source,
        "price_table_date": PRICE_TABLE_DATE,
    }
    rows = read_model_totals(Path(data_dir) / AUDIT_DATABASE)
    if rows is None:
        return {**base, "available": False, "reason": "audit_store_missing"}
    return {**base, "available": True, **estimate_cost(rows, prices)}
