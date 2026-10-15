"""Regression tests for Chad's real Live-Mode screener-field bugs (batch 2,
items #7/#8/#10/#11-ish numeric side): /api/radar/discover must normalize
real UW /api/screener/stocks rows, not assume the app's own earlier
(incorrect) field-name guesses. Uses hand-built, clearly-synthetic row
dicts SHAPED like the real schema Chad observed -- never a real UW
response or real licensed market data.
"""
from fastapi.testclient import TestClient

from app.main import _normalize_discovery_candidate


class _FakeAdapter:
    """Drop-in adapter stand-in so these tests exercise the REAL
    /api/radar/discover route (normalization + ranking) without any
    network call."""

    def __init__(self, rows):
        self._rows = rows

    def discover_candidates(self, filters=None):
        return {
            "candidates": self._rows,
            "snapshot_at": "2026-10-15T12:00:00+00:00",
            "screener_requests_used": 1,
            "source": "fake",
            "unavailable": False,
            "reason": None,
        }

    # unused by this route, present only so get_adapter()'s callers don't explode
    def get_universe(self):
        return []


def _client_with_rows(app_main, rows, monkeypatch):
    fake = _FakeAdapter(rows)
    monkeypatch.setattr(app_main, "get_adapter", lambda: fake)
    return TestClient(app_main.app)


# --- _normalize_discovery_candidate: unit-level ----------------------------

def test_normalize_maps_one_day_perc_not_change():
    row = {"ticker": "NVDA", "one_day_perc": 3.25, "net_premium": 1_000_000}
    out = _normalize_discovery_candidate(row)
    assert out["change_percent"] == 3.25


def test_normalize_falls_back_to_change_when_one_day_perc_absent():
    row = {"ticker": "NVDA", "change": 1.5, "net_premium": 1_000_000}
    out = _normalize_discovery_candidate(row)
    assert out["change_percent"] == 1.5


def test_normalize_coerces_numeric_strings():
    row = {
        "ticker": "SPY", "net_premium": "-1800000", "call_volume": "12345",
        "put_volume": "6789", "iv_rank": "20.6659", "repeated_hits": "3",
        "one_day_perc": "-0.4",
    }
    out = _normalize_discovery_candidate(row)
    assert out["net_premium"] == -1_800_000.0
    assert out["call_volume"] == 12345.0
    assert out["put_volume"] == 6789.0
    assert out["iv_rank"] == 20.6659
    assert out["repeated_hits"] == 3.0
    assert out["change_percent"] == -0.4


def test_normalize_handles_missing_optional_fields_without_crashing():
    row = {"ticker": "OBSCURE"}
    out = _normalize_discovery_candidate(row)
    assert out["net_premium"] is None
    assert out["call_volume"] is None
    assert out["change_percent"] is None
    assert out["is_index"] is False


def test_normalize_carries_source_date_separately_from_app_fetch_time():
    row = {"ticker": "SPY", "date": "2026-10-14", "net_premium": 1}
    out = _normalize_discovery_candidate(row)
    assert out["source_date"] == "2026-10-14"


def test_normalize_flags_index_instruments():
    row = {"ticker": "SPXW", "is_index": True, "net_premium": 1}
    out = _normalize_discovery_candidate(row)
    assert out["is_index"] is True


def test_normalize_non_index_defaults_false():
    row = {"ticker": "NVDA", "net_premium": 1}
    out = _normalize_discovery_candidate(row)
    assert out["is_index"] is False


# --- /api/radar/discover endpoint: integration through normalization -----

def test_endpoint_marks_index_instrument_evaluate_unsupported(app_main, monkeypatch):
    rows = [
        {"ticker": "SPXW", "is_index": True, "net_premium": "5000000", "one_day_perc": "0.1", "date": "2026-10-14"},
        {"ticker": "NVDA", "is_index": False, "net_premium": "3000000", "one_day_perc": "1.2", "date": "2026-10-14"},
    ]
    client = _client_with_rows(app_main, rows, monkeypatch)
    res = client.post("/api/radar/discover", json={})
    assert res.status_code == 200
    body = res.json()
    by_ticker = {c["ticker"]: c for c in body["candidates"]}
    assert by_ticker["SPXW"]["is_index"] is True
    assert by_ticker["SPXW"]["evaluate_supported"] is False
    assert by_ticker["NVDA"]["is_index"] is False
    assert by_ticker["NVDA"]["evaluate_supported"] is True


def test_endpoint_numeric_strings_dont_break_ranking(app_main, monkeypatch):
    rows = [
        {"ticker": "A", "net_premium": "1000000", "repeated_hits": "1"},
        {"ticker": "B", "net_premium": "-5000000", "repeated_hits": "0"},  # larger magnitude, negative
    ]
    client = _client_with_rows(app_main, rows, monkeypatch)
    res = client.post("/api/radar/discover", json={})
    body = res.json()
    assert [c["ticker"] for c in body["candidates"]] == ["B", "A"]


def test_endpoint_preserves_real_negative_net_premium_value(app_main, monkeypatch):
    """Chad's exact real-world case: a $5M min_premium filter result still
    legitimately carrying a NEGATIVE net_premium -- this app must never
    silently flip/clamp that sign, just normalize its type."""
    rows = [{"ticker": "SPY", "net_premium": "-1800000", "one_day_perc": "-0.3"}]
    client = _client_with_rows(app_main, rows, monkeypatch)
    res = client.post("/api/radar/discover", json={"min_premium": 5_000_000})
    body = res.json()
    assert body["candidates"][0]["net_premium"] == -1_800_000.0
