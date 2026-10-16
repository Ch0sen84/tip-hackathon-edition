"""UX remediation item 2: Demo/Live watchlist freshness contamination.

Real finding (Claude Code's Windows Docker Desktop QA pass): a previous
Demo-mode scan timestamp leaked into Live Mode's displayed watchlist
freshness -- with zero real evaluations, My Watchlist falsely showed
e.g. "4 tickers scanned"/"last scanned 1m ago". Root cause: SettingsStore
had ONE shared `last_scan_at`/`last_scan_count` pair, written on every
real live_fetch in _radar_impl -- including every ordinary Demo Mode
page load (Demo Mode's adapter is never live, so live_fetch is always
True there). Switching to Live Mode's cache-only GET /api/radar then
read that same shared, Demo-written value with no awareness of which
mode actually wrote it.

Fix: last-scan telemetry is now scoped per mode (SettingsStore.
record_scan(..., mode=...) / get_last_scan(mode)) -- see settings_store.py
and app/main.py's _radar_impl for the full writeup.
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


def test_demo_mode_scan_timestamp_never_leaks_into_live_mode_cache_read(app_main, monkeypatch):
    """The exact real-world reproduction: Demo Mode scans a watchlist
    (writing a real, fresh last-scan timestamp), the user then switches
    to Live Mode with zero real evaluations ever performed -- Live
    Mode's cache-only GET /api/radar must report last_scan_at=None /
    last_scan_count=None, never the Demo Mode value."""
    tickers = ["FLUX", "ZYNC"]
    app_main._settings_store.set_watchlist(tickers)

    # Demo Mode: a plain GET /api/radar is always a real "fetch" for the
    # synthetic adapter (no live cost) -- this legitimately writes a
    # fresh Demo-scoped last-scan record.
    client = TestClient(app_main.app)
    demo_resp = client.get("/api/radar")
    assert demo_resp.status_code == 200
    demo_body = demo_resp.json()
    assert demo_body["last_scan_at"] is not None
    # Demo Mode's universe is the fixed set of fixture scenarios, not the
    # user's watchlist (watchlist scoping only applies to Live Mode) --
    # not asserting an exact count here, just that a real Demo scan ran.
    assert demo_body["scanned"] > 0

    # Now switch to a real LiveUWAdapter with ZERO cataloged evaluations
    # and ZERO live refreshes ever performed. The cache-only GET must
    # not show the Demo scan's timestamp/count.
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    live_resp = client.get("/api/radar")
    assert live_resp.status_code == 200
    live_body = live_resp.json()
    assert live_body["last_scan_at"] is None, (
        f"Live Mode's last_scan_at leaked a Demo Mode value: {live_body['last_scan_at']!r} -- "
        "this is exactly the item 2 regression."
    )
    assert mock_session.get.call_count == 0  # still cache-only, zero live cost


def test_live_mode_scan_after_real_refresh_shows_its_own_timestamp_not_demo(app_main, monkeypatch):
    """The inverse check: once Live Mode performs a REAL refresh, its
    own timestamp must be visible -- and a later Demo Mode read must
    never show the Live Mode value either (separation is bidirectional).
    """
    tickers = ["AMD"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    client = TestClient(app_main.app)
    refresh_resp = client.post("/api/radar/refresh")
    assert refresh_resp.status_code == 200
    live_scan_at = refresh_resp.json()["last_scan_at"]
    assert live_scan_at is not None

    # Switch back to Demo Mode (no live adapter wired) -- Demo's own
    # scoped field is independent and must not echo the Live timestamp
    # just recorded above (it will show its OWN just-now Demo scan
    # instead, since a Demo GET always performs a "fetch").
    monkeypatch.undo()
    demo_resp = client.get("/api/radar")
    assert demo_resp.status_code == 200
    # The two values are allowed to be numerically close in a fast test
    # run, so assert via the store directly instead of wall-clock drift.
    last_scan = app_main._settings_store.get_all()
    assert last_scan["last_scan_at_live"] == live_scan_at
    assert last_scan["last_scan_at_demo"] is not None
    assert last_scan["last_scan_at_demo"] != last_scan["last_scan_at_live"]


def test_cache_only_live_read_never_writes_a_new_timestamp_w2a_still_holds(app_main, monkeypatch):
    """Re-verification of the pre-existing W2-a invariant after this
    fix: a cache-only Live Mode read must never overwrite the persisted
    last_scan_at_live, even when called repeatedly."""
    tickers = ["AMD"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    client = TestClient(app_main.app)
    client.post("/api/radar/refresh")
    recorded = app_main._settings_store.get_all()["last_scan_at_live"]
    calls_after_refresh = mock_session.get.call_count

    for _ in range(3):
        client.get("/api/radar")
    still = app_main._settings_store.get_all()["last_scan_at_live"]
    assert still == recorded, "A cache-only GET must never overwrite last_scan_at_live."
    assert mock_session.get.call_count == calls_after_refresh, "The three GETs above must cost zero additional UW calls."


def test_unevaluated_live_watchlist_symbol_is_shown_not_hidden(app_main, monkeypatch):
    """Required by item 2: a watchlist ticker with no real evaluation
    yet must still appear in the response (not be silently dropped),
    rendered as honestly unevaluated."""
    tickers = ["NFLX"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200
    body = resp.json()
    all_rows = body["highest"] + body["other"]
    assert any(r["ticker"] == "NFLX" for r in all_rows), "Unevaluated watchlist ticker was hidden, not shown."
    row = next(r for r in all_rows if r["ticker"] == "NFLX")
    assert row["eval_freshness"] == "unevaluated"
    assert row["last_evaluated_at"] is None
