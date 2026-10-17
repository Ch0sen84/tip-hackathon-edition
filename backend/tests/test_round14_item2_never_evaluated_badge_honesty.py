"""Round 14 item 2 regression test (ux-nav-watchlist-reliability): the
real bug was in _not_evaluated_ticker_analysis_response (backend/app/
main.py) -- `state` used to fall back to the literal string "NEW"
whenever QualifyingDaysStore genuinely had no real lifecycle record for
a ticker either. "NEW" is a real signal_engine lifecycle state implying
an evaluation actually happened; the frontend's STATE_GLOSS["NEW"]
("just started qualifying") then rendered directly alongside this same
response's own "Not yet evaluated" headline/freshness text -- a plain
contradiction for a ticker that was never scanned at all.

This test proves TWO genuinely distinct cases stay distinct at the API
level (the frontend fix, covered separately in frontend/test_round14_
mixed_six_ticker.mjs, trusts these exact fields):
  1. A ticker with NO cataloged evaluation AND no real QualifyingDaysStore
     record either -- genuinely never evaluated. `state` must be honestly
     null/None, never faked to "NEW", and `not_evaluated` must be True.
  2. A ticker with no cataloged evaluation but a REAL prior
     QualifyingDaysStore state (it was evaluated at some point; only
     this specific cache-miss path has nothing fresh cataloged) -- that
     real state must still be reported honestly, and `not_evaluated`
     must be False (this is NOT a never-evaluated case).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter


def _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist):
    mock_session = MagicMock()
    adapter = LiveUWAdapter(api_key="fake-key", base_url="https://api.unusualwhales.com",
                             client_api_id="fake", watchlist=list(watchlist))
    adapter.client.session = mock_session
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter, mock_session


def test_genuinely_never_evaluated_ticker_reports_honest_null_state(app_main, monkeypatch):
    """No cataloged evaluation, no QualifyingDaysStore record at all --
    `state` must be None (never a faked "NEW"), `not_evaluated` True."""
    ticker = "NFLX"
    app_main._settings_store.set_watchlist([ticker])
    _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] is None, f"expected honest null state, got {body['state']!r}"
    assert body["not_evaluated"] is True
    assert body["headline"] == "Not yet evaluated"
    assert body["eval_freshness"] == "unevaluated"
    assert body["last_evaluated_at"] is None


def test_ticker_with_real_prior_state_but_no_fresh_catalog_entry_keeps_real_state(app_main, monkeypatch):
    """QualifyingDaysStore already has a REAL prior lifecycle state for
    this ticker (it genuinely was evaluated before), even though nothing
    is cataloged for this specific cache-miss lookup -- that real state
    must be reported honestly and must NOT be mislabeled not_evaluated."""
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    app_main.get_store().record_scan(ticker, any_gate_passed=True, state="BUILDING")
    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "BUILDING", f"expected the real prior state, got {body['state']!r}"
    assert body["not_evaluated"] is False
