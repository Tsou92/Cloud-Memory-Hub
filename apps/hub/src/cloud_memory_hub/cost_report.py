"""Estimated model spend for the administrator console.

The Core audit store already records one row per model call with its model
name, token counts and start time; this module turns those rows into an
estimate.

The number is an estimate, never a bill. Prices are list prices cached on the
date below (Anthropic, DeepSeek, OpenAI and Google first-party APIs), a model
the table does not know is reported as unpriced instead of guessed, and models
with peak/off-peak billing are split by the request start time. Operators can
override the table with ``CMH_MODEL_PRICES``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

PRICE_TABLE_DATE = "2026-11-17"
PRICE_ENV = "CMH_MODEL_PRICES"
AUDIT_DATABASE = "one-memory.sqlite3"

# DeepSeek bills peak at UTC 01:00-04:00 and 06:00-10:00, Monday to Friday.
# Chinese public holidays are off-peak upstream; without a holiday calendar
# those days are estimated at peak, which overstates rather than understates.
# Minutes since midnight, so the table needs no parsing at import time.
DEEPSEEK_PEAK_WINDOWS = ((60, 240), (360, 600))
WEEKDAYS_MON_FRI = (0, 1, 2, 3, 4)


def _entry(
    input_rate, output_rate, cache_rate, *, off_peak=None, peak_windows=None, peak_weekdays=None
):
    return {
        "input": float(input_rate),
        "output": float(output_rate),
        "cache_read": float(cache_rate),
        "off_peak": off_peak,
        "peak_windows": peak_windows,
        "peak_weekdays": peak_weekdays,
    }


def _deepseek(input_rate, output_rate, cache_rate):
    half = (input_rate / 2, output_rate / 2, cache_rate / 2)
    return _entry(
        input_rate,
        output_rate,
        cache_rate,
        off_peak=half,
        peak_windows=DEEPSEEK_PEAK_WINDOWS,
        peak_weekdays=WEEKDAYS_MON_FRI,
    )


# USD per million tokens: input, output, cache read.
BUILTIN_PRICES = {
    # Anthropic
    "claude-fable-5-2": _entry(8.0, 40.0, 0.20),
    "claude-fable-5-1": _entry(10.0, 50.0, 0.20),
    "claude-fable-5": _entry(10.0, 50.0, 0.20),
    "claude-opus-5-5": _entry(4.0, 20.0, 0.20),
    "claude-opus-5": _entry(5.0, 25.0, 0.20),
    "claude-opus-4-8": _entry(5.0, 25.0, 0.20),
    "claude-opus-4-7": _entry(5.0, 25.0, 0.20),
    "claude-opus-4-6": _entry(5.0, 25.0, 0.20),
    "claude-sonnet-5-5": _entry(2.0, 10.0, 0.10),
    "claude-sonnet-5": _entry(2.0, 10.0, 0.10),
    "claude-sonnet-4-6": _entry(3.0, 15.0, 0.30),
    "claude-haiku-5-5": _entry(0.10, 0.50, 0.01),
    "claude-haiku-4-5": _entry(1.0, 5.0, 0.10),
    # DeepSeek: peak rates doubled; the list below is the peak rate.
    "deepseek-flash": _deepseek(0.30, 1.20, 0.006),
    "deepseek-v4.1-flash": _deepseek(0.30, 1.20, 0.006),
    "deepseek-v4-flash": _deepseek(0.30, 1.20, 0.006),
    "deepseek-v4-pro": _deepseek(1.32, 3.96, 0.044),
    # OpenAI
    "gpt-5.5": _entry(5.0, 30.0, 0.50),
    "gpt-5.4": _entry(2.50, 15.0, 0.25),
    "gpt-5.4-mini": _entry(0.75, 4.50, 0.075),
    "gpt-5.4-nano": _entry(0.20, 1.25, 0.02),
    "gpt-5.3-codex": _entry(1.75, 14.0, 0.175),
    "gpt-5.2": _entry(1.75, 14.0, 0.175),
    "gpt-5.1": _entry(1.25, 10.0, 0.125),
    "gpt-5": _entry(1.25, 10.0, 0.125),
    "gpt-5-mini": _entry(0.25, 2.0, 0.025),
    "gpt-5-nano": _entry(0.05, 0.40, 0.005),
    "gpt-4.1": _entry(2.0, 8.0, 0.50),
    "gpt-4.1-mini": _entry(0.40, 1.60, 0.10),
    "gpt-4.1-nano": _entry(0.10, 0.40, 0.025),
    # Google Gemini (first tier; long prompts above 200K cost more)
    "gemini-3.8-flash": _entry(0.75, 3.75, 0.075),
    "gemini-3.6-flash": _entry(0.75, 3.75, 0.075),
    "gemini-3.1-pro-preview": _entry(2.0, 12.0, 0.20),
    "gemini-2.5-flash": _entry(0.30, 2.50, 0.03),
    "gemini-2.5-flash-lite": _entry(0.10, 0.40, 0.01),
}


def _minutes(value):
    try:
        hours, minutes = str(value).split(":")
        return int(hours) * 60 + int(minutes)
    except (AttributeError, TypeError, ValueError):
        return None


def _parse_windows(raw):
    windows = []
    for item in raw or ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = _minutes(item[0]), _minutes(item[1])
        if start is None or end is None or not 0 <= start < 1440 or not 0 <= end <= 1440:
            continue
        windows.append((start, end))
    return tuple(windows)


def _parse_entry(value):
    if not isinstance(value, dict):
        return None
    try:
        entry = _entry(value["input"], value["output"], value.get("cache_read", value["input"]))
    except (KeyError, TypeError, ValueError):
        return None
    if min(entry["input"], entry["output"], entry["cache_read"]) < 0:
        return None
    off_peak = value.get("off_peak")
    if isinstance(off_peak, dict):
        try:
            entry["off_peak"] = (
                float(off_peak["input"]),
                float(off_peak["output"]),
                float(off_peak.get("cache_read", off_peak["input"])),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if min(entry["off_peak"]) < 0:
            return None
        entry["peak_windows"] = _parse_windows(value.get("peak_windows_utc"))
        weekdays = value.get("peak_weekdays", list(WEEKDAYS_MON_FRI))
        try:
            entry["peak_weekdays"] = tuple(sorted({int(day) for day in weekdays}))
        except (TypeError, ValueError):
            return None
        if not entry["peak_windows"]:
            return None
    return entry


def _price_for(model, prices):
    """Return a price for the model name, tolerating a provider prefix.

    Only the last path segment is tried as a fallback; no fuzzy rewriting of
    version suffixes, because a wrong model match would silently misprice.
    """
    if model in prices:
        return prices[model]
    return prices.get(model.rsplit("/", 1)[-1])


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
    prices = {}
    for model, value in parsed.items():
        entry = _parse_entry(value) if isinstance(model, str) and model else None
        if entry is not None:
            prices[model] = entry
    if not prices:
        return BUILTIN_PRICES, "builtin_config_invalid"
    return prices, "configured"


def read_model_totals(database: Path):
    """Per-model, per-hour token totals, or None when the audit store cannot be read."""
    path = Path(database)
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT model, "
                "CAST(strftime('%w', started_at) AS INTEGER) AS weekday, "
                "CAST(strftime('%H', started_at) AS INTEGER) AS hour, "
                "COUNT(*), SUM(prompt_tokens), SUM(completion_tokens), "
                "SUM(cached_prompt_tokens), SUM(total_tokens) "
                "FROM model_request_audit "
                "GROUP BY model, weekday, hour "
                "ORDER BY SUM(total_tokens) DESC"
            ).fetchall()
    except sqlite3.Error:
        return None


def _is_peak(entry, weekday, hour):
    """Whether a request at this UTC weekday/hour pays the peak rate.

    A row without a parsable start time is treated as peak, which overstates
    rather than understates a provider whose peak is the expensive side.
    """
    if entry["off_peak"] is None:
        return True
    if weekday is None or hour is None:
        return True
    if weekday not in entry["peak_weekdays"]:
        return False
    minute = hour * 60
    for start, end in entry["peak_windows"]:
        if start <= minute < end or (end <= start and (minute >= start or minute < end)):
            return True
    return False


def _usd(prompt, completion, cached, rates):
    input_rate, output_rate, cache_rate = rates
    return (
        (prompt - cached) * input_rate + cached * cache_rate + completion * output_rate
    ) / 1_000_000


def estimate_cost(rows, prices):
    models = {}
    unpriced = set()
    for model, weekday, hour, requests, prompt, completion, cached, total_tokens in rows:
        # SQLite %w is 0=Sunday..6=Saturday; the price table uses 0=Monday.
        weekday = (weekday + 6) % 7 if isinstance(weekday, int) else None
        name = model if isinstance(model, str) else str(model)
        entry = models.setdefault(
            name,
            {
                "model": name,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_prompt_tokens": 0,
                "total_tokens": 0,
                "peak_requests": 0,
                "off_peak_requests": 0,
                "estimated_cost": 0.0,
            },
        )
        prompt_tokens = int(prompt or 0)
        completion_tokens = int(completion or 0)
        # Cache reads are recorded alongside prompt tokens without promising
        # which contains the other; clamp so they are never billed twice.
        cached_tokens = min(int(cached or 0), prompt_tokens)
        count = int(requests or 0)
        entry["requests"] += count
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        entry["cached_prompt_tokens"] += cached_tokens
        entry["total_tokens"] += int(total_tokens or 0)
        price = _price_for(name, prices)
        if price is None:
            unpriced.add(name)
            entry["estimated_cost"] = None
            continue
        if entry["estimated_cost"] is None:
            # A priced bucket after an unpriced one cannot happen: pricing is
            # per model, not per row. Keep the guard explicit anyway.
            entry["estimated_cost"] = 0.0
        peak = _is_peak(price, weekday, hour)
        if peak:
            entry["peak_requests"] += count
            rates = (price["input"], price["output"], price["cache_read"])
        else:
            entry["off_peak_requests"] += count
            rates = price["off_peak"]
        entry["estimated_cost"] += _usd(prompt_tokens, completion_tokens, cached_tokens, rates)

    ordered = sorted(models.values(), key=lambda item: item["total_tokens"], reverse=True)
    for entry in ordered:
        if entry["estimated_cost"] is not None:
            entry["estimated_cost"] = round(entry["estimated_cost"], 6)
    total = round(sum(entry["estimated_cost"] or 0.0 for entry in ordered), 6)
    priced_requests = sum(
        entry["requests"] for entry in ordered if entry["estimated_cost"] is not None
    )
    unpriced_requests = sum(
        entry["requests"] for entry in ordered if entry["estimated_cost"] is None
    )
    return {
        "total": total,
        "priced_requests": priced_requests,
        "unpriced_requests": unpriced_requests,
        "models": ordered,
        "unpriced_models": sorted(unpriced),
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
