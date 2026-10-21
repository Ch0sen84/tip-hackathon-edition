"""Round 13 item 2 regression test (ux-nav-watchlist-reliability round 13).

Real bug: on a ticker's FIRST zero-gate evaluation specifically, the
history entry returned by the IMMEDIATE POST /api/ticker/{ticker}/refresh
response carried no `transition_rule` field at all, while the very next
cache-only GET /api/ticker/{ticker} (same underlying catalog row) DID
carry it -- because _ticker_analysis_impl's history-enrichment loop used
to run `if served_from_cache and callable(rule_reader)`, gating the
catalog-backed enrichment on the wrong path entirely. The frontend's
shared isZeroGateTransitionReason-based helpers (historyEntryGloss, etc.)
then correctly fell through to the generic STATE_GLOSS["NEW"] wording for
the immediate-refresh response specifically, until the user's next reload.

This test drives the REAL app (TestClient, mocked UW session returning
empty flow/darkpool data so the scan genuinely produces zero qualifying
gates -- never a hand-built fixture standing in for the route) through
both call paths and asserts the newest history entry's `transition_rule`
is identical and present in BOTH responses.
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


def test_immediate_refresh_and_subsequent_cached_get_agree_on_transition_rule_for_first_zero_gate_eval(
    app_main, monkeypatch,
):
    ticker = "ARTNA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    # Empty flow alerts / darkpool trades / stock state -> genuinely zero
    # qualifying gates on this scan (a real first zero-gate evaluation,
    # not a hand-constructed fixture standing in for one).
    mock_session.get.return_value = MagicMock(
        status_code=200, ok=True, text="{}", headers={},
        json=MagicMock(return_value={"data": []}),
    )

    client = TestClient(app_main.app)

    # (a) The immediate POST /refresh response -- no reload involved.
    refresh_resp = client.post(f"/api/ticker/{ticker}/refresh", json={"confirm": True})
    assert refresh_resp.status_code == 200, refresh_resp.text
    refresh_body = refresh_resp.json()
    assert refresh_body["served_from_cache"] is False
    assert refresh_body["qualifying_days_count"] == 0

    refresh_history = refresh_body["history"]
    assert refresh_history, "expected at least one real history entry after a live refresh"
    newest_from_refresh = refresh_history[-1]
    # This is the real bug: before this fix, this key was silently
    # missing from the response entirely on the immediate-refresh path.
    assert "transition_rule" in newest_from_refresh, (
        "the IMMEDIATE refresh response's newest history entry is missing transition_rule -- "
        "exactly the round 13 item 2 regression (history-enrichment gated on served_from_cache)"
    )
    # Live Mode with no session baselines yet: nothing independent could be
    # evaluated, so the scan is held (signal-integrity remediation).
    assert newest_from_refresh["transition_rule"] == "held_insufficient_evidence", (
        f"expected the first zero-gate evaluation's real reason string, got "
        f"{newest_from_refresh['transition_rule']!r}"
    )

    # (b) The SAME scan's data via a cache-only GET (simulating F5).
    get_resp = client.get(f"/api/ticker/{ticker}")
    assert get_resp.status_code == 200, get_resp.text
    get_body = get_resp.json()
    assert get_body["served_from_cache"] is True
    get_history = get_body["history"]
    newest_from_get = get_history[-1]
    assert newest_from_get["transition_rule"] == "held_insufficient_evidence"

    # The actual regression this test exists to catch: both paths must
    # produce the IDENTICAL shape for the newest entry, not merely the
    # same final value by coincidence.
    assert newest_from_refresh["transition_rule"] == newest_from_get["transition_rule"]
    # No qualifying evidence => no qualification episode opens (state None),
    # never a "NEW" label (signal-integrity remediation).
    assert newest_from_refresh["state"] is None and newest_from_get["state"] is None

    assert mock_session.get.call_count > 0  # the one real fetch this test deliberately triggers


def test_second_zero_gate_evaluation_also_gets_enriched_on_the_immediate_refresh_path(app_main, monkeypatch):
    """The L1 any_gate_passed guard's SECOND zero-gate reason
    (no_gate_passed_remains_new, a later re-evaluation of an
    already-NEW ticker that again found zero gates) must also be
    correctly enriched on the immediate-refresh response, not just the
    very first evaluation."""
    ticker = "MSEX"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    mock_session.get.return_value = MagicMock(
        status_code=200, ok=True, text="{}", headers={},
        json=MagicMock(return_value={"data": []}),
    )
    client = TestClient(app_main.app)

    first = client.post(f"/api/ticker/{ticker}/refresh", json={"confirm": True}).json()
    assert first["transition_rule"] == "held_insufficient_evidence"
    assert first["state"] is None

    # A second scan without baselines still opens nothing.
    second = client.post(f"/api/ticker/{ticker}/refresh", json={"confirm": True}).json()
    assert second["transition_rule"] == "held_insufficient_evidence", (
        f"expected the insufficient-evidence reason, got {second['transition_rule']!r}"
    )
    assert second["state"] is None
    newest = second["history"][-1]
    assert newest["transition_rule"] == "held_insufficient_evidence", (
        "the SECOND zero-gate reason's immediate-refresh history entry must also carry "
        "its real transition_rule, not just the first evaluation's"
    )
