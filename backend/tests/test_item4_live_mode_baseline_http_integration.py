"""Item 4 (pre-Phase-D follow-up): END-TO-END integration verification --
not unit tests, not calling internal functions directly. Starts the real
FastAPI app (via TestClient, the project's established pattern) and
proves through REAL HTTP routes that a Live Radar/Ticker-Analysis scan
for a ticker with pre-seeded `darkpool_observations` +
`darkpool_coverage_status` catalog rows genuinely produces a
dark_pool_anomaly gate result reflecting that seeded history.

ISOLATION (no real UW calls for this item): `app.adapters.live_uw_adapter
.UWRestClient` is monkeypatched to a fake, in-process class with the
exact method surface LiveUWAdapter.__init__ actually calls (no network
I/O anywhere) -- the route under test is otherwise the REAL,
un-mocked main.py code path: get_adapter() -> LiveUWAdapter(...) ->
_run_scan() -> _catalog_dark_pool_baseline_best_effort() ->
dark_pool_baseline.compute_baseline_for_ticker() ->
scan_service.evaluate_ticker_scan() -> signal_engine.evaluate_gates().
TIP_DB_PATH is isolated to a per-test tmp_path file (never the real
deployed db).

WHAT THIS PROVES (or would have reported as a gap, per Chad's explicit
instruction to report wiring gaps honestly rather than hide them):
the dark_pool_anomaly gate the real HTTP response discloses for this
ticker is backed by the SEEDED catalog history (passed=True, a real
ratio/z-score derived from the 60 seeded historical sessions), not a
silent fallback to the bootstrap/"no baseline" path that would occur
if a live scan's today-side darkpool_trades (deliberately returned
empty by the fake client, simulating "today's live fetch returned
nothing new yet") were used instead of the catalog. Both /api/ticker/
{ticker} (Ticker Analysis) and /api/radar (Market Radar) are checked
for mutually consistent gate/baseline information, per item 4(b).
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.core import trading_calendar as tc
from app.dark_pool_pagination import COMPLETE

TICKER = "ZQXW"

# Round 12 (ux-nav-watchlist-reliability) fix: a FIXED, deterministic date
# known to be a real NYSE trading session (not a weekend, not a market
# holiday) per trading_calendar.py's own is_trading_day() -- replaces this
# module's prior wall-clock "today" assumption, which genuinely failed on
# any real weekend/holiday. 2026-06-15 is a Monday; verified a real
# trading day as of this fix via `trading_calendar.is_trading_day(...)`.
ANCHOR_TRADING_DATE = dt.date(2026, 6, 15)


class _FakeUWRestClient:
    """Duck-types UWRestClient's constructor + the three methods
    LiveUWAdapter.get_flow_alerts/get_darkpool_trades/get_stock_state
    call. Zero network I/O -- every method returns a canned, UW-shaped
    dict synchronously. `base_url`/`api_key` attributes are read by
    LiveUWAdapter.health()/elsewhere, so they're set for realism even
    though this test doesn't exercise health()."""

    def __init__(self, api_key: str, base_url: str = "", client_api_id: str = "", **kwargs):
        self.api_key = api_key
        self.base_url = base_url
        self.client_api_id = client_api_id
        self.calls = []

    def flow_alerts(self, ticker: str, **kwargs) -> dict:
        self.calls.append(("flow_alerts", ticker))
        # Deliberately NO options-flow evidence this scan -- isolates the
        # test to the dark-pool baseline wiring specifically (a single
        # real category passing caps at "medium"/BUILDING per the
        # existing 2-independent-category rule, which this test does
        # NOT touch or need to re-verify).
        return {"data": []}

    def darkpool_ticker(self, ticker: str, **kwargs) -> dict:
        self.calls.append(("darkpool_ticker", ticker))
        # Today's LIVE on-demand fetch returns nothing new -- if the
        # route were (wrongly) using THIS instead of the cataloged
        # baseline for the dark_pool_anomaly gate, it would bootstrap to
        # "no data" every time, which is exactly the silent-fallback gap
        # this test is designed to catch.
        return {"data": []}

    def stock_state(self, ticker: str, **kwargs) -> dict:
        self.calls.append(("stock_state", ticker))
        return {"data": {"ticker": ticker, "last_price": 101.5, "change_percent": 0.4}}


def _nth_trading_day_before(anchor: dt.date, n: int) -> dt.date:
    d = anchor
    found = 0
    while found < n:
        d -= dt.timedelta(days=1)
        if tc.is_trading_day(d):
            found += 1
    return d


def _seed_strong_dark_pool_baseline(catalog, ticker: str) -> dict:
    """Seeds 60 'complete' historical sessions (STRONG tier) plus TODAY's
    own 'complete' session, via the REAL IngestionCatalog write methods
    (never raw SQL) -- exactly what a real multi-week Phase B/C
    deployment would have accumulated. Every observation (today's and
    every historical day's) is placed EXACTLY at that day's real
    session_open_et instant, so the time-of-day elapsed-window
    comparison (dark_pool_baseline.cumulative_through_elapsed) matches
    deterministically regardless of what wall-clock time this test
    actually runs at (elapsed = now - today_open is always >= 0, and
    every seeded observation's timestamp == its own session_open <=
    session_open + elapsed for any elapsed >= 0 -- see module docstring
    of dark_pool_baseline.py for why same-elapsed-window comparison is
    the whole point of this design).

    Today is marked 'complete' explicitly (not left to the real-time
    in-progress check) so this test is not flaky around market hours.

    Round 12 (ux-nav-watchlist-reliability) fix: "today" here used to be
    the real wall-clock date (dt.datetime.now(...).date()), which made
    this whole test genuinely fail on any real weekend/market holiday
    (confirmed failing on an actual Saturday, 2026-10-17, via
    AssertionError on the old `assert tc.is_trading_day(today)` line) --
    this module's seeded-history/HTTP-integration coverage has nothing
    to do with which day it happens to be run on. Anchored instead to a
    FIXED, deterministic date (ANCHOR_TRADING_DATE below) already known
    to be a real NYSE trading day per this same trading_calendar module
    (not a market holiday, not a weekend) -- the
    `assert tc.is_trading_day(...)` safety check is intentionally KEPT
    (not removed/weakened), it now just guards the fixed constant
    instead of the wall clock, so a future change to the calendar module
    that ever invalidated this specific date would still be caught
    loudly rather than silently.
    """
    today = ANCHOR_TRADING_DATE
    assert tc.is_trading_day(today), (
        f"test assumption violated: the fixed anchor date {today} is not a real "
        "NYSE trading day per trading_calendar.py -- pick a different anchor date "
        "for this test (must be a non-weekend, non-holiday session)"
    )
    today_open = tc.session_start_utc(today)

    historical_dates = [_nth_trading_day_before(today, n) for n in range(1, 61)]
    for i, d in enumerate(historical_dates):
        hist_open = tc.session_start_utc(d)
        premium = 10_000.0 + (i % 5) * 100.0  # small realistic variance, never zero-stdev
        catalog.catalog_darkpool_observations(
            ticker, [{"executed_at": hist_open.isoformat(), "premium": premium, "price": 50.0, "size": 10}],
        )
        catalog.record_coverage_status(ticker, d.isoformat(), COMPLETE, pages_walked=3, session_start_verified=True)

    # Today: a real anomaly -- 8x every historical session's same-elapsed
    # cumulative, strong tier, should clear both the ratio and percentile
    # thresholds in dark_pool_baseline.compute_baseline.
    catalog.catalog_darkpool_observations(
        ticker, [{"executed_at": today_open.isoformat(), "premium": 80_000.0, "price": 50.0, "size": 100}],
    )
    catalog.record_coverage_status(ticker, today.isoformat(), COMPLETE, pages_walked=3, session_start_verified=True)
    return {"today": today, "today_open": today_open}


@pytest.fixture
def live_mode_seeded(app_main, tmp_path, monkeypatch):
    """Isolated Live Mode app: fake UWRestClient (no network), isolated
    file-backed catalog db (never the real deployed one), ticker
    watchlisted and mode flipped to 'live' directly on the settings
    store (bypassing the key-verification-gated HTTP route, which is an
    unrelated auth feature -- this test is about scan-gate wiring, not
    credential verification).

    Round 12 (ux-nav-watchlist-reliability) fix: the fixture now also
    freezes `app.core.dark_pool_baseline`'s own notion of "now" to a
    fixed instant on ANCHOR_TRADING_DATE. This is necessary (not
    optional) once "today" in the seeded-history helper above stopped
    being the real wall-clock date: `dark_pool_baseline.
    compute_baseline_for_ticker()` -- called by the REAL, un-mocked
    `_catalog_dark_pool_baseline_best_effort()` scan path this test
    exercises -- independently defaults `as_of_utc` to the real wall
    clock (`datetime.now(timezone.utc)`) whenever it isn't passed one
    explicitly, which main.py's call site never does. Without this
    freeze, the live scan's own "today" (the real wall-clock date) and
    this fixture's seeded "today" (the fixed anchor date) would
    silently diverge, breaking the baseline match this test depends on
    -- this is a test-only monkeypatch of a module-level `datetime`
    symbol, not a change to any application file's logic.
    """
    import app.adapters.live_uw_adapter as live_uw_mod
    import app.core.dark_pool_baseline as dark_pool_baseline_mod
    from app import main as _main

    monkeypatch.setattr(live_uw_mod, "UWRestClient", _FakeUWRestClient)

    # A fixed instant 2 hours after ANCHOR_TRADING_DATE's real session
    # open -- gives a genuine, non-zero elapsed window for the
    # time-of-day cumulative comparison, exactly like a real mid-session
    # live scan would see.
    frozen_now = tc.session_start_utc(ANCHOR_TRADING_DATE) + dt.timedelta(hours=2)

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_now.replace(tzinfo=None)
            return frozen_now.astimezone(tz)

    monkeypatch.setattr(dark_pool_baseline_mod, "datetime", _FrozenDatetime)

    isolated_db = str(tmp_path / "item4_isolated.db")
    monkeypatch.setattr(_main.settings, "db_path", isolated_db)
    _main._store = None
    _main._catalog = None
    _main._adapter = None
    _main._adapter_sig = None

    _main._settings_store.set_mode("live")
    _main._settings_store.set_watchlist([TICKER])
    monkeypatch.setattr(_main.settings, "uw_api_key", "fake-test-key-not-real")

    catalog = _main.get_catalog()
    seed_info = _seed_strong_dark_pool_baseline(catalog, TICKER)
    return {"main": _main, "seed_info": seed_info}


class TestLiveRadarGateGenuinelyReflectsSeededCatalogHistory:
    def test_ticker_analysis_http_response_shows_baseline_backed_dark_pool_anomaly(self, live_mode_seeded):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        # W2-b: GET is cache-first now -- use the explicit POST refresh
        # route to genuinely exercise a real scan against the seeded
        # catalog baseline, which is this test's whole point.
        resp = client.post(f"/api/ticker/{TICKER}/refresh")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        dp_gate = body["gates"]["dark_pool_anomaly"]
        assert dp_gate["passed"] is True, (
            "dark_pool_anomaly did NOT reflect the seeded catalog history via the real "
            f"HTTP route -- got gate={dp_gate!r}. This would be exactly the wiring gap "
            "item 4 asks to check for: catalog/baseline computed but not actually "
            "consumed by the live gate-evaluation path."
        )
        assert dp_gate["metrics"]["bootstrap"] is False, (
            "dark_pool_anomaly fell back to the bootstrap/no-baseline path despite 60 "
            "seeded COMPLETE historical sessions existing in the catalog."
        )
        ratio = dp_gate["metrics"]["ratio"]
        assert ratio is not None and ratio >= 3.0, f"expected a real qualifying ratio, got {ratio!r}"

        # Single real category (dark-pool only, no options-flow seeded) --
        # must cap at medium/non-CONFIRMED, exactly per the UNCHANGED
        # 2-independent-category rule (signal_engine.py was not touched
        # by this item; this assertion is a sanity check, not new logic).
        assert body["confidence"] == "medium"
        assert body["state"] != "CONFIRMED"

    def test_market_radar_http_response_is_consistent_with_ticker_analysis(self, live_mode_seeded):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        # W2 (cache-first watchlist) + S1 security fix: an ordinary GET
        # /api/radar no longer performs a live scan for a LiveUWAdapter,
        # and no longer accepts a `refresh` query parameter at all (the
        # S1 fix removed that side-effect path from GET entirely). This
        # test's whole point is checking a FRESH scan's dark-pool-
        # baseline wiring, so it must use the real POST /api/radar/
        # refresh action instead, exactly like the "Refresh Data" UI
        # button does.
        radar_resp = client.post("/api/radar/refresh")
        assert radar_resp.status_code == 200, radar_resp.text
        radar_body = radar_resp.json()

        all_entries = radar_body["highest"] + radar_body["other"]
        matching = [e for e in all_entries if e["ticker"] == TICKER]
        assert matching, f"{TICKER} not present in /api/radar response at all: {radar_body}"

        ticker_resp = client.get(f"/api/ticker/{TICKER}")
        ticker_body = ticker_resp.json()

        # Both routes read from the SAME underlying baseline wiring (the
        # Phase C _run_scan()/_catalog_dark_pool_baseline_best_effort()
        # code path), so the evidence they each independently compute
        # for this ticker must agree -- NOTE each HTTP call triggers its
        # OWN real scan (via store.record_scan), so the lifecycle STATE
        # genuinely and correctly advances between the radar call above
        # and this ticker_analysis call (NEW -> BUILDING, the normal,
        # expected state-machine progression -- not a bug, and not
        # re-verified here since signal_engine.py's transition logic was
        # not touched by this item). What must stay IDENTICAL across
        # both calls, because it's driven by the same unchanging seeded
        # catalog history every time, is confidence/signal_type and the
        # one gate this item is actually about.
        assert matching[0]["state"] in ("NEW", "BUILDING")
        assert ticker_body["state"] in ("NEW", "BUILDING")
        assert matching[0]["confidence"] == ticker_body["confidence"] == "medium"
        assert matching[0]["signal_type"] == ticker_body["signal_type"] == "single-signal-dark-pool"
        assert ticker_body["gates"]["dark_pool_anomaly"]["passed"] is True
