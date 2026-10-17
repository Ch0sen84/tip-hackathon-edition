"""Phase B compatibility guarantee: the existing Radar / Ticker-Analysis /
Ticker-OHLC response CONTRACTS must be byte-for-byte unchanged by the new
ingestion layer being wired in underneath them. Proven two ways:

  1. The exact same live-mode scan produces an IDENTICAL response body
     whether the ingestion catalog write succeeds, fails outright, or is
     paused by the storage guard -- i.e. ingestion is provably unable to
     affect what the route returns.
  2. The response still contains exactly the key set Phase A/pre-Phase-B
     already documented (a literal snapshot of the expected shape).

Every monkeypatch below uses pytest's `monkeypatch` fixture (auto-reverted
at test teardown) rather than a direct `app_main.foo = ...` assignment --
app.main's module-level globals (_adapter, _store, _catalog) are
process-wide singletons shared by the WHOLE test session, so a direct
assignment here would permanently corrupt state (e.g. PRSX's seeded demo
history) for every test that runs afterward in the same pytest process.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient
from app.ingestion import IngestionCatalog
from app.storage import QualifyingDaysStore


def _resp(json_body):
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.text = ""
    resp.headers = {}
    resp.json.return_value = json_body
    return resp


def _mock_session_by_path():
    """Different real-shaped bodies per endpoint path -- a single
    `{"data": [...]}` canned response for every call (as a naive mock
    would do) breaks stock-state normalization, which expects a dict,
    not a list. Mirrors the real per-endpoint response shapes documented
    in live_uw_adapter.py."""
    flow_body = {"data": [
        {"type": "call", "total_premium": 2_000_000, "total_ask_side_prem": 1_800_000,
         "total_bid_side_prem": 200_000, "created_at": "2026-10-15T14:00:00Z"},
    ]}
    darkpool_body = {"data": [
        {"id": "dp1", "executed_at": "2026-10-15T14:00:00Z", "price": 450.0, "size": 1000, "premium": 450000.0},
    ]}
    stock_state_body = {"data": {"close": "450.10", "prev_close": "445.00"}}

    def side_effect(url, **kwargs):
        if "/stock-state" in url:
            return _resp(stock_state_body)
        if "/darkpool/" in url:
            return _resp(darkpool_body)
        if "/flow-alerts" in url:
            return _resp(flow_body)
        return _resp({"data": []})

    session = MagicMock()
    session.get.side_effect = side_effect
    return session


def _wire_live_adapter(app_main, monkeypatch, watchlist=("NVDA",)):
    session = _mock_session_by_path()
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=list(watchlist), client=client)
    # get_adapter() recomputes its own cache signature from
    # _settings_store/settings on every call and will silently rebuild
    # (discarding) any adapter instance assigned directly to
    # app_main._adapter whose signature doesn't match -- overriding
    # get_adapter() itself is the reliable way to pin a specific
    # LiveUWAdapter instance for one of these compatibility tests.
    # monkeypatch (not a bare assignment) so this reverts at teardown.
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter


def _fresh_memory_store(app_main, monkeypatch):
    """Points app_main.get_store() at a BRAND NEW, isolated in-memory
    store for the duration of one test -- monkeypatched, so the real
    shared `_store` (with PRSX's seeded demo history other tests in this
    session depend on) is restored automatically at teardown."""
    store = QualifyingDaysStore(db_path=":memory:")
    monkeypatch.setattr(app_main, "get_store", lambda: store)
    return store


def _fresh_catalog(app_main, monkeypatch):
    """Phase C: get_catalog()'s global `_catalog` singleton (unlike
    get_store()'s) is NOT reset by the app_main fixture -- it is lazily
    created once and then reused for the rest of the pytest session. That
    was harmless before Phase C (catalog WRITE success/failure never fed
    back into a response), but now that a read off this same catalog
    feeds the dark_pool_anomaly gate's inputs, a test comparing two scans
    of the SAME ticker must give each its own isolated, empty catalog --
    otherwise an EARLIER, unrelated test's cataloged NVDA rows (left over
    in the shared singleton) would leak into one run's baseline but not
    the other's, which is a test-isolation gap, not a real behavior
    difference."""
    catalog = IngestionCatalog(db_path=":memory:")
    monkeypatch.setattr(app_main, "get_catalog", lambda: catalog)
    return catalog


# W2-b (2026-10-16): Ticker Analysis now also discloses eval_freshness/
# last_evaluated_at/served_from_cache, same cache-first contract as
# /api/radar's.
EXPECTED_TICKER_ANALYSIS_KEYS = {
    "ticker", "state", "previous_state", "transition_rule", "confidence", "signal_type",
    "confidence_reason", "qualifies", "qualifying_days_count", "gates", "headline",
    "current_read", "evidence", "stock_state", "history", "raw",
    "eval_freshness", "last_evaluated_at", "served_from_cache",
}

EXPECTED_RADAR_KEYS = {
    "highest", "other", "scanned", "scan_scope", "last_scan_at", "served_from_cache",
    # D4/D5 fix (ux-nav-watchlist-reliability round 9): explicit total-
    # watchlist-size vs actually-evaluated-count disclosure -- see
    # app/main.py's _radar_impl docstring update for the real bug this
    # closes (an unevaluated ticker's cache-only "scanned" count was
    # being misread by the frontend as "N were actually evaluated").
    "watchlist_size", "evaluated_count",
}


def test_ticker_analysis_response_shape_matches_documented_contract(app_main, monkeypatch):
    _wire_live_adapter(app_main, monkeypatch)
    _fresh_memory_store(app_main, monkeypatch)
    client = TestClient(app_main.app)
    # W2-b: GET is now cache-first -- use the explicit POST refresh route
    # to exercise a real scan's response shape, same pattern as the
    # radar tests' S1/W2 fix above.
    resp = client.post("/api/ticker/NVDA/refresh")
    assert resp.status_code == 200
    assert set(resp.json().keys()) == EXPECTED_TICKER_ANALYSIS_KEYS


def test_ticker_analysis_response_identical_whether_ingestion_succeeds_or_is_broken(app_main, monkeypatch):
    """The SAME ticker, scanned twice against the exact same adapter
    data, each time starting from a FRESH isolated in-memory
    qualifying-days store (so both scans genuinely start from the same
    NEW lifecycle state -- reusing one store across both calls would
    legitimately change state/history on the second call, which is real
    pre-existing behavior unrelated to Phase B, and would make this
    comparison meaningless). The synthetic historical-percentile gate
    (signal_engine.synthetic_percentile_gate) is deterministic per
    ticker+premium, so holding the ticker and adapter data fixed makes
    the WHOLE response, not just a subset, deterministic across the two
    calls. One call's catalog write succeeds; the other is forced to
    raise -- response bodies must be byte-for-byte identical."""
    _wire_live_adapter(app_main, monkeypatch)
    client = TestClient(app_main.app)

    _fresh_memory_store(app_main, monkeypatch)
    _fresh_catalog(app_main, monkeypatch)  # Phase C: see _fresh_catalog's docstring
    # W2-b: GET is cache-first now -- use the explicit POST refresh route
    # so both calls genuinely exercise a real scan (which is this test's
    # whole point: comparing ok-vs-broken catalog WRITE behavior).
    resp_ok = client.post("/api/ticker/NVDA/refresh")
    assert resp_ok.status_code == 200
    body_ok = resp_ok.json()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated ingestion failure")

    # Phase C: get_catalog() is now ALSO used for a read-only historical
    # baseline lookup (app/core/dark_pool_baseline.compute_baseline_for_ticker,
    # via _catalog_dark_pool_baseline_best_effort) -- a plain MagicMock
    # with only the WRITE methods stubbed would auto-mock those two READ
    # methods too, returning Mock objects instead of the real empty-list/
    # empty-dict a never-cataloged ticker's catalog actually returns, and
    # the resulting diverging dark_pool_anomaly gate inputs would make
    # this test fail for a reason that has nothing to do with what it's
    # actually testing (WRITE robustness). Stub them to the same empty
    # shape the real, never-before-cataloged-for-NVDA catalog gives in
    # the "ok" run above, so only the WRITE path is genuinely broken here.
    monkeypatch.setattr(app_main, "get_catalog", lambda: MagicMock(
        catalog_options_flow_observations=_boom,
        catalog_darkpool_observations=_boom,
        catalog_signal_evaluation=_boom,
        list_coverage_status=lambda ticker: [],
        list_darkpool_observations_by_date=lambda ticker: {},
    ))
    _fresh_memory_store(app_main, monkeypatch)  # fresh, independent history again
    resp_broken = client.post("/api/ticker/NVDA/refresh")
    assert resp_broken.status_code == 200
    body_broken = resp_broken.json()

    # UX remediation item 4 (2026-10-16): `last_evaluated_at` is now a
    # genuine wall-clock timestamp captured fresh on every real live
    # fetch (fixing the "Current" + "Not yet evaluated" contradiction
    # bug -- see _ticker_analysis_impl's own comment) -- legitimately
    # differs by a few microseconds between these two separate POST
    # calls, same class of exclusion as `last_scan_at` elsewhere in this
    # file. Deliberately NOT read back from the catalog (which would
    # reintroduce exactly the write-failure-changes-the-response
    # coupling this test exists to forbid) -- excluded here, not
    # asserted equal, by design.
    body_ok.pop("last_evaluated_at", None)
    body_broken.pop("last_evaluated_at", None)
    assert body_ok == body_broken


def test_radar_response_shape_unaffected_when_ingestion_catalog_write_is_paused(app_main, monkeypatch):
    """The ingestion catalog's OWN storage guard (a real StorageGuard
    pointed at the ':memory:' catalog's directory) reporting critical
    must be swallowed by _catalog_scan_best_effort and never change the
    /api/radar response -- this is distinct from (and must not be
    confused with) the Phase A qualifying-days store's own guard, which
    is skipped entirely for a ':memory:' db (see test_storage_guard.py's
    `test_qualifying_days_store_skips_guard_for_in_memory_db`)."""
    _wire_live_adapter(app_main, monkeypatch)
    client = TestClient(app_main.app)

    _fresh_memory_store(app_main, monkeypatch)
    _fresh_catalog(app_main, monkeypatch)  # Phase C: see _fresh_catalog's docstring
    # W2 (cache-first watchlist) + S1 security fix: this test specifically
    # exercises a real scan's catalog-write-pause handling -- an ordinary
    # GET no longer scans live for a LiveUWAdapter, and GET /api/radar no
    # longer accepts a refresh parameter AT ALL (removed entirely as part
    # of the S1 fix: a GET must never have a side effect regardless of
    # query string). Both calls here must use the real POST
    # /api/radar/refresh action instead, exactly like the UI's "Refresh
    # Data" button does.
    resp_ok = client.post("/api/radar/refresh")
    assert resp_ok.status_code == 200
    assert set(resp_ok.json().keys()) == EXPECTED_RADAR_KEYS
    body_ok = resp_ok.json()

    def _boom(*args, **kwargs):
        from app.ingestion import StorageGuardPausedError
        from app.storage_guard import StorageGuardResult
        raise StorageGuardPausedError(
            StorageGuardResult(allowed=False, kind="on_demand", free_mb=1.0, threshold_mb=500.0,
                                mount_point="/fake")
        )

    # Phase C: see the equivalent comment in
    # test_ticker_analysis_response_identical_whether_ingestion_succeeds_or_is_broken
    # above -- the two new READ-only methods must be stubbed to the same
    # empty shape a real, never-cataloged ticker's catalog returns, or
    # this MagicMock would also accidentally change dark_pool_anomaly's
    # gate inputs for a reason unrelated to what this test checks.
    monkeypatch.setattr(app_main, "get_catalog", lambda: MagicMock(
        catalog_options_flow_observations=_boom,
        catalog_darkpool_observations=_boom,
        catalog_signal_evaluation=_boom,
        list_coverage_status=lambda ticker: [],
        list_darkpool_observations_by_date=lambda ticker: {},
    ))
    _fresh_memory_store(app_main, monkeypatch)
    resp_paused = client.post("/api/radar/refresh")
    assert resp_paused.status_code == 200
    body_paused = resp_paused.json()
    # `last_scan_at` is a genuine wall-clock timestamp recorded fresh on
    # every real scan (see SettingsStore.record_scan) -- legitimately
    # differs between any two calls regardless of Phase B; excluded from
    # the equality check below, which covers everything else.
    body_ok.pop("last_scan_at", None)
    body_paused.pop("last_scan_at", None)
    assert body_paused == body_ok
