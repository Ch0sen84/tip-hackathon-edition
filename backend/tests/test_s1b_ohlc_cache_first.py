"""S1-b security fix regression tests (this round's merge-blocking item).

Real finding (Claude Code's Windows Docker Desktop QA pass against
bbac0cf): GET /api/ticker/{ticker}/ohlc performed 3 real, unauthenticated,
cross-site-triggerable UW calls (get_ohlc, get_flow_alerts,
get_darkpool_trades) and wrote 3 kinds of observations on every plain GET
-- the exact same bug class as the already-fixed S1 (GET /api/radar) and
W2-b (GET /api/ticker/{ticker}), just missed in that round's audit. Fix:
the GET route is now strictly cache-first/read-only for a real
LiveUWAdapter (see _ticker_ohlc_impl in app/main.py); the only way to
trigger a real OHLC fetch is the new, authenticated, concurrency-locked
POST /api/ticker/{ticker}/ohlc/refresh.
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


def _mock_ok_response(payload):
    return MagicMock(status_code=200, ok=True, text="{}", headers={}, json=MagicMock(return_value=payload))


def test_get_ohlc_plain_makes_zero_uw_calls(app_main, monkeypatch):
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}/ohlc")
    assert resp.status_code == 200
    assert mock_session.get.call_count == 0, (
        "GET /api/ticker/{ticker}/ohlc made a real UW call -- this is exactly the S1-b regression."
    )
    body = resp.json()
    assert body["served_from_cache"] is True
    assert body["unavailable"] is True  # nothing cataloged yet -- honest, not fabricated
    assert body["candles"] == []


def test_get_ohlc_with_range_query_params_still_makes_zero_uw_calls(app_main, monkeypatch):
    """Every OHLC GET variation (with/without query params) must make
    zero UW calls -- not just the bare-defaults case."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    for qs in ["", "?range=1M", "?range=1Y", "?range=1D", "?timeframe=1d&limit=30", "?refresh=true"]:
        resp = client.get(f"/api/ticker/{ticker}/ohlc{qs}")
        assert resp.status_code in (200, 400), f"{qs} -> {resp.status_code}: {resp.text}"
    assert mock_session.get.call_count == 0, (
        f"A GET variation made {mock_session.get.call_count} real UW call(s) -- refresh=true in the "
        "query string must be inert (no `refresh` parameter exists on the GET route at all)."
    )


def test_get_ohlc_cross_site_sec_fetch_header_still_makes_zero_calls(app_main, monkeypatch):
    """Same attack vector as the S1 test: a foreign page's no-cors
    fetch/<img> request. GET has no require_auth dependency (by design,
    a plain page-load read) and must still never call UW."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    resp = client.get(
        f"/api/ticker/{ticker}/ohlc?refresh=true",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 200
    assert mock_session.get.call_count == 0


def test_unauthenticated_network_mode_caller_cannot_initiate_ohlc_collection(app_main, monkeypatch):
    """An unauthenticated caller in Network Mode (no X-TIP-Auth token)
    cannot trigger OHLC collection via the plain GET route."""
    import app.main as app_module
    monkeypatch.setattr(app_module, "LOCAL_MODE_ACTIVE", False)
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.get(f"/api/ticker/{ticker}/ohlc")
    # S2-5: in Network Mode every /api read needs the token too; either way the GET can never cost a
    # real UW call.
    assert resp.status_code == 401
    assert mock_session.get.call_count == 0
    # The POST refresh route DOES require auth -- this is the actual
    # boundary that matters for this item.
    resp2 = client.post(f"/api/ticker/{ticker}/ohlc/refresh")
    assert resp2.status_code == 401
    assert mock_session.get.call_count == 0


def test_ticker_analysis_f5_back_forward_make_zero_uw_calls_for_ohlc_too(app_main, monkeypatch):
    """Extends the existing W2-b exact-zero test to cover the OHLC route
    alongside the detail route -- a real session opens Ticker Analysis
    (which calls both routes), hits F5, navigates away and Back."""
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    for _ in range(4):  # open, F5, Back, direct-link
        r1 = client.get(f"/api/ticker/{ticker}")
        r2 = client.get(f"/api/ticker/{ticker}/ohlc")
        assert r1.status_code == 200 and r2.status_code == 200
    assert mock_session.get.call_count == 0


def test_post_ohlc_refresh_actually_collects_and_persists_real_observations(app_main, monkeypatch):
    """Proof point: the protected POST path still genuinely collects and
    persists real observations when invoked."""
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    mock_session.get.return_value = _mock_ok_response({"data": []})
    client = TestClient(app_main.app)
    resp = client.post(f"/api/ticker/{ticker}/ohlc/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["served_from_cache"] is False
    assert mock_session.get.call_count > 0

    # A subsequent plain GET must read back the just-written cache and
    # make zero additional UW calls.
    calls_after_refresh = mock_session.get.call_count
    resp2 = client.get(f"/api/ticker/{ticker}/ohlc")
    assert resp2.status_code == 200
    assert resp2.json()["served_from_cache"] is True
    assert mock_session.get.call_count == calls_after_refresh


def test_post_ohlc_refresh_requires_auth(app_main, monkeypatch):
    import app.main as app_module
    monkeypatch.setattr(app_module, "LOCAL_MODE_ACTIVE", False)
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.post("/api/ticker/AMD/ohlc/refresh")
    assert resp.status_code == 401


def test_post_ohlc_refresh_cross_origin_is_rejected(app_main, monkeypatch):
    """The sole mutating path remains properly origin-checked."""
    client = TestClient(app_main.app)
    resp = client.post("/api/ticker/AMD/ohlc/refresh", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_post_ohlc_refresh_duplicate_concurrent_is_rejected_cleanly(app_main, monkeypatch):
    import app.main as app_module
    app_module._ticker_ohlc_refresh_lock.acquire()
    try:
        client = TestClient(app_main.app)
        resp = client.post("/api/ticker/AMD/ohlc/refresh")
        assert resp.status_code == 409
    finally:
        app_module._ticker_ohlc_refresh_lock.release()


def test_empty_cache_chart_behavior_is_honest_no_auto_fetch_no_fabricated_data(app_main, monkeypatch):
    ticker = "MSFT"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}/ohlc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["candles"] == []
    assert body["markers"] == []
    assert body["unavailable"] is True
    assert body["reason"] is not None and "Refresh" in body["reason"]
    assert body["candle_count"] == 0
    assert mock_session.get.call_count == 0


def test_existing_cached_chart_ranges_still_render_correctly_after_refresh(app_main, monkeypatch):
    """After a real refresh writes candles, a plain cache-only GET for
    the same (ticker, timeframe) must render them back correctly."""
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])

    candles = [
        {"t": "2026-10-01", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000},
        {"t": "2026-10-02", "o": 100.5, "h": 102.0, "l": 100.0, "c": 101.0, "v": 1200},
    ]
    monkeypatch.setattr(
        type(adapter), "get_ohlc",
        lambda self, ticker, timeframe="1d", limit=180, window=None: {
            "candles": candles, "source": "live", "negative_volume_bars_clamped": 0,
            "unavailable": False, "reason": None,
        },
    )
    mock_session.get.return_value = _mock_ok_response({"data": []})

    client = TestClient(app_main.app)
    refresh_resp = client.post(f"/api/ticker/{ticker}/ohlc/refresh")
    assert refresh_resp.status_code == 200, refresh_resp.text

    cached_resp = client.get(f"/api/ticker/{ticker}/ohlc")
    assert cached_resp.status_code == 200
    body = cached_resp.json()
    assert body["served_from_cache"] is True
    assert body["unavailable"] is False
    assert [c["t"] for c in body["candles"]] == ["2026-10-01", "2026-10-02"]
    assert body["candles"][0]["c"] == 100.5
    assert body["cached_at"] is not None


def test_no_other_get_route_accepts_a_refresh_style_query_param_including_ohlc():
    """Structural re-audit, extended to cover the just-added OHLC route
    explicitly -- same allowlist/pattern as the S1 structural audit."""
    import inspect

    from app.main import app

    suspicious_names = {"refresh", "run", "trigger", "force", "scan", "execute"}
    offenders = []
    for route in app.router.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            sig = inspect.signature(endpoint)
        except (TypeError, ValueError):
            continue
        for name in sig.parameters:
            if name.lower() in suspicious_names:
                offenders.append((route.path, name))
    assert offenders == []
    # Explicit positive check that the OHLC GET route is in the audited set at all.
    paths = {route.path for route in app.router.routes if "GET" in (getattr(route, "methods", None) or set())}
    assert "/api/ticker/{ticker}/ohlc" in paths
