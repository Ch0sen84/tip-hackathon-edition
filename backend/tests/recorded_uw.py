"""Loader for the recorded real-UW fixture (fixtures/recorded_uw/).

Tests that need it call `require()`, which skips when the file is absent
(it is real third-party market data and is NOT distributed publicly -- see
that directory's README). Public end-to-end coverage uses the synthetic
fixture (SYNTHETIC_FIXTURE) instead."""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "recorded_uw" / "uw_capture_2026-10-15_17.json.gz"
# public, fully synthetic replay fixture (tools/build_synthetic_uw_fixture.py)
SYNTHETIC_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic_uw" / "synthetic_capture.json.gz"


def available() -> bool:
    return FIXTURE.exists()


@lru_cache(maxsize=1)
def load() -> dict:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def require() -> dict:
    if not available():
        pytest.skip("recorded UW fixture not present (fixtures/recorded_uw/)")
    return load()


def scan(ticker: str, retrieved_prefix: str) -> dict:
    """One captured Live evaluation, rows in UW wire format."""
    data = require()
    matches = [s for s in data["scans"] if s["ticker"] == ticker and s["retrieved_at"].startswith(retrieved_prefix)]
    assert matches, f"no recorded scan for {ticker} at {retrieved_prefix}"
    s = matches[0]
    return {
        "ticker": ticker,
        "retrieved_at": s["retrieved_at"],
        "flow_alerts": [dict(data["flow_alerts"][i]) for i in s["flow_alert_ids"]],
        "darkpool": [dict(data["darkpool"][k]) for k in s["darkpool_keys"]],
        "stock_state": dict(s["stock_state"]) if s["stock_state"] else None,
        "captured_engine": s["captured_engine"],
    }


def scans() -> list:
    return [scan(s["ticker"], s["retrieved_at"]) for s in require()["scans"]]
