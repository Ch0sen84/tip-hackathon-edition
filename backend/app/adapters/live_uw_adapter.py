"""LiveUWAdapter: REST client for https://api.unusualwhales.com, ported from
the production app's `uw_client.py`.

PORTING NOTES:
  * Same ~15 endpoint functions as production (stock info/state/options-
    volume/ohlc, darkpool recent/ticker/price-levels, flow-alerts,
    net-prem-ticks, greek-exposure & spot-exposure by strike, market-tide,
    screener stocks/option-contracts, insider transactions, congress
    trades, news headlines).
  * Production reads the API key from a fixed path on one machine
    (~/.hermes/secrets/unusualwhales.env) and never allows a failed call to
    crash the caller's audit logging. This standalone repo instead reads
    UW_API_KEY from the environment (12-factor, works in Docker/CI) and
    drops the Postgres audit-log call entirely (no DB here) -- everything
    else about the request/response handling is the same.
  * NEW in this port: 401 (bad key) and 403 (entitlement not on this UW
    tier -- e.g. CME futures is not included in Basic) are caught and
    turned into a clear structured result instead of a raised exception,
    so a misconfigured key or an out-of-tier endpoint never crashes the
    adapter or the FastAPI route calling it. Any other HTTP error or
    network failure is still surfaced as an "error" entry in the same
    shape, also without raising -- callers (scan_service / routes) only
    ever see dicts, never exceptions, from this adapter.
"""
from __future__ import annotations

from typing import Optional

import requests

from .base import UWDataAdapter
from ..core.candles import (
    build_candles, clamp_limit, raw_limit_for_trading_days, select_regular_session_bars,
)

DEFAULT_BASE_URL = "https://api.unusualwhales.com"
UNAVAILABLE = "unavailable_on_tier"
UNAUTHORIZED = "unauthorized"
ERROR = "error"

# Market Discovery (C2b/C2c, 2026-10-15): hard server-side cap on how many
# screener rows a single /api/radar/discover call may request, regardless
# of what the caller asks for. Never raised implicitly, never bypassed by
# pagination -- this endpoint makes exactly ONE request to UW per call.
DISCOVERY_LIMIT_CAP = 50


def _unavailable(path: str, status_code: int, body: str, kind: str) -> dict:
    return {
        "ok": False,
        "status": kind,
        "status_code": status_code,
        "path": path,
        "message": {
            UNAUTHORIZED: "UW API rejected the API key (401). Check UW_API_KEY.",
            UNAVAILABLE: "This endpoint is not available on the configured UW tier (403). "
                         "Example: CME ES/NQ futures data requires the Advanced tier / futures add-on.",
            ERROR: "UW API request failed.",
        }[kind],
        "detail": body[:500],
    }


class UWRestClient:
    """Thin, crash-proof wrapper. Every method returns a plain dict: either
    the parsed UW JSON body, or one of the `_unavailable(...)` shapes above.
    Never raises on a UW-side HTTP error; DOES raise on a programmer error
    (e.g. missing ticker arg) since that's a bug, not an API/tier issue."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL,
                 client_api_id: str = "", timeout: int = 20, session: Optional[requests.Session] = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client_api_id = client_api_id
        self.timeout = timeout
        self.session = session or requests.Session()

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.client_api_id:
            headers["UW-CLIENT-API-ID"] = self.client_api_id
        return headers

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.api_key:
            return _unavailable(path, 401, "No UW_API_KEY configured", UNAUTHORIZED)
        url = f"{self.base_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self.session.get(url, headers=self._headers(), params=clean_params, timeout=self.timeout)
        except requests.RequestException as e:
            return _unavailable(path, 0, f"{type(e).__name__}: {e}", ERROR)

        if resp.status_code == 401:
            return _unavailable(path, 401, resp.text, UNAUTHORIZED)
        if resp.status_code == 403:
            return _unavailable(path, 403, resp.text, UNAVAILABLE)
        if not resp.ok:
            return _unavailable(path, resp.status_code, resp.text, ERROR)
        try:
            return resp.json()
        except ValueError:
            return _unavailable(path, resp.status_code, resp.text, ERROR)

    # --- Endpoint wrappers (same set as production uw_client.py) -----------

    def stock_info(self, ticker: str) -> dict:
        return self.get(f"/api/stock/{ticker}/info")

    def stock_state(self, ticker: str) -> dict:
        return self.get(f"/api/stock/{ticker}/stock-state")

    def options_volume(self, ticker: str, limit: int = 1) -> dict:
        return self.get(f"/api/stock/{ticker}/options-volume", {"limit": limit})

    def stock_ohlc(self, ticker: str, timeframe: str = "1d", limit: int = 30,
                    window: Optional[str] = None) -> dict:
        # Path-segment form (/api/stock/{ticker}/ohlc/{size}) -- confirmed live
        # (Phase-A verification) for both daily ("1d") and intraday ("5m" etc.)
        # sizes. The size string is UW's own vocabulary; pass timeframe through.
        #
        # BUG FOUND 2026-10-14 (Phase B final verification, Chad's question
        # #1): UW's real OHLC endpoint ALSO has its own query parameter
        # ALSO named "timeframe" -- confusingly distinct from the path's
        # candle_size above -- that selects the actual DATE WINDOW to
        # search within (format "1Y"/"2M"/"YTD" etc., default "1Y" per
        # UW's own OpenAPI docs: https://api.unusualwhales.com/docs/
        # operations/PublicApi.TickerController.ohlc). `limit` only caps
        # how many rows come back WITHIN whatever window is in effect --
        # it never changes the window. This adapter never sent that query
        # parameter, so every request silently defaulted to UW's 1-year
        # window regardless of `limit`, which is exactly why "1Y" and "1M"
        # requests returned near-identical history depth. Fixed by adding
        # `window` here, sent as UW's own "timeframe" query key only when
        # explicitly given (None preserves old behavior for any other
        # caller that never passes it).
        params: dict = {"limit": limit}
        if window is not None:
            params["timeframe"] = window
        return self.get(f"/api/stock/{ticker}/ohlc/{timeframe}", params)

    def darkpool_recent(self, limit: int = 100, min_premium: Optional[int] = None) -> dict:
        return self.get("/api/darkpool/recent", {"limit": limit, "min_premium": min_premium})

    def darkpool_ticker(self, ticker: str, date: Optional[str] = None, limit: int = 500,
                         min_premium: Optional[int] = None, min_size: Optional[int] = None,
                         older_than: Optional[str] = None, newer_than: Optional[str] = None,
                         order: Optional[str] = None) -> dict:
        return self.get(f"/api/darkpool/{ticker}", {
            "date": date, "limit": limit, "min_premium": min_premium, "min_size": min_size,
            "older_than": older_than, "newer_than": newer_than, "order": order,
        })

    def darkpool_price_levels(self, ticker: str) -> dict:
        return self.get(f"/api/darkpool/{ticker}/price-levels")

    def flow_alerts(self, ticker: Optional[str] = None, limit: int = 100,
                     min_premium: Optional[int] = None, is_call: Optional[bool] = None,
                     is_put: Optional[bool] = None, is_sweep: Optional[bool] = None) -> dict:
        return self.get("/api/option-trades/flow-alerts", {
            "ticker_symbol": ticker, "limit": limit, "min_premium": min_premium,
            "is_call": is_call, "is_put": is_put, "is_sweep": is_sweep,
        })

    def net_prem_ticks(self, ticker: str, limit: int = 500) -> dict:
        return self.get(f"/api/stock/{ticker}/net-prem-ticks", {"limit": limit})

    def greek_exposure_strike(self, ticker: str) -> dict:
        return self.get(f"/api/stock/{ticker}/greek-exposure/strike")

    def spot_exposure_strike(self, ticker: str) -> dict:
        return self.get(f"/api/stock/{ticker}/spot-exposures/strike")

    def market_tide(self) -> dict:
        return self.get("/api/market/market-tide")

    def stock_screener(self, min_volume_oi_ratio: Optional[float] = None, limit: int = 50,
                       min_premium: Optional[int] = None, min_call_volume: Optional[int] = None,
                       min_put_volume: Optional[int] = None, min_iv_rank: Optional[float] = None,
                       min_change: Optional[float] = None, max_change: Optional[float] = None) -> dict:
        return self.get("/api/screener/stocks", {
            "min_volume_oi_ratio": min_volume_oi_ratio, "limit": limit,
            "min_premium": min_premium, "min_call_volume": min_call_volume,
            "min_put_volume": min_put_volume, "min_iv_rank": min_iv_rank,
            "min_change": min_change, "max_change": max_change,
        })

    def option_contract_screener(self, min_premium: Optional[int] = None, limit: int = 50) -> dict:
        return self.get("/api/screener/option-contracts", {"min_premium": min_premium, "limit": limit})

    def insider_transactions(self, ticker: Optional[str] = None, limit: int = 50) -> dict:
        return self.get("/api/insider/transactions", {"ticker": ticker, "limit": limit})

    def congress_recent_trades(self, ticker: Optional[str] = None, limit: int = 50) -> dict:
        return self.get("/api/congress/recent-trades", {"ticker": ticker, "limit": limit})

    def news_headlines(self, ticker: Optional[str] = None, limit: int = 20) -> dict:
        return self.get("/api/news/headlines", {"ticker": ticker, "limit": limit})


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_stock_state(raw: dict) -> dict:
    """Normalizes a REAL /api/stock/{ticker}/stock-state response into the
    common shape scan_service/tip_evidence expect (last_price/change_percent
    -- the same shape SyntheticDataAdapter's fixtures already use directly).

    REAL BUG FIX (2026-10-15, Chad's Live-Mode Radar price finding): this
    adapter previously returned UW's RAW response unmodified and the rest of
    the app looked for keys (`last_price`, `change_percent`/`chg_pct`/
    `percent_change`) that do not exist anywhere in UW's real schema --
    confirmed live (direct curl against /api/stock/SPY/stock-state):
        {"close": "767.01", "high": "768.25", "low": "764.1", "open":
         "767.39", "volume":.., "total_volume":.., "market_time":
         "premarket", "tape_time": "2026-10-15T13:10:30Z", "prev_close":
         "769.6"}
    Every numeric field is a STRING, and there is NO native change-percent
    field at all -- UW gives close + prev_close only, so change_percent must
    be derived here, never guessed from a nonexistent field. This is why
    every real Live Radar card rendered price:{last:null, chg_pct:null}
    (pure plumbing/field-name bug, not missing data from UW).
    """
    close = _to_float(raw.get("close"))
    prev_close = _to_float(raw.get("prev_close"))
    change_percent = None
    if close is not None and prev_close:
        change_percent = (close - prev_close) / prev_close * 100.0
    return {
        "ticker": raw.get("ticker"),
        "last_price": close,
        "change_percent": change_percent,
        "prev_close": prev_close,
        "open": _to_float(raw.get("open")),
        "high": _to_float(raw.get("high")),
        "low": _to_float(raw.get("low")),
        "volume": _to_float(raw.get("volume")),
        "total_volume": _to_float(raw.get("total_volume")),
        "market_time": raw.get("market_time"),
        "tape_time": raw.get("tape_time"),
        "source": "unusual_whales_stock_state",
    }


def _normalize_flow_alert(raw: dict) -> dict:
    """Normalizes a REAL /api/option-trades/flow-alerts row.

    REAL BUG FIX (2026-10-15): scan_service._options_flow_metrics (and this
    app's own base.py docstring) assumed field names `total_ask_premium`/
    `total_bid_premium`. The REAL UW field names, confirmed live via a direct
    curl against /api/option-trades/flow-alerts, are `total_ask_side_prem`/
    `total_bid_side_prem` (total_premium itself IS correctly named). Because
    the assumed names never matched any real response key, ask_premium/
    bid_premium were silently always 0 in Live Mode -> ask_share always
    None -> one-sided options_flow_hit and direction_heuristic could only
    ever fire via has_sweep, never via the real ask/bid-side premium split.
    Normalizing here (adapter boundary) keeps scan_service/tests/fixtures,
    which already use the total_ask_premium/total_bid_premium names, exactly
    as they are -- only the LIVE adapter needed to learn the real UW names.

    REAL BUG FIX (#36, 2026-10-15 round 2): the Evidence Detail table (and
    fixtures/demo data) read `is_call`/`is_put` booleans. Real UW
    flow-alert rows carry neither of those -- the real field is `type`,
    with string values `"call"`/`"put"` (confirmed live). Because the
    assumed boolean fields never existed on a real response, every Live
    Mode call/put aggregation silently read `False`/`False` for every
    alert, producing the "zero call and zero put activity" bug even when
    the ticker genuinely had both. Derive `is_call`/`is_put` here from the
    real `type` field (case-insensitively, tolerating whitespace and any
    other/missing value) whenever the row doesn't already carry explicit
    is_call/is_put booleans -- this keeps demo fixtures (which already set
    is_call/is_put directly) byte-identical while fixing Live Mode at the
    one place real UW field names are translated into this app's internal
    shape. An unexpected/missing `type` value (empty string, numeric,
    `"CALL "` with whitespace, a typo'd value) safely yields
    is_call=False/is_put=False rather than crashing or guessing.
    """
    out = dict(raw)
    out["total_ask_premium"] = raw.get("total_ask_premium", raw.get("total_ask_side_prem"))
    out["total_bid_premium"] = raw.get("total_bid_premium", raw.get("total_bid_side_prem"))
    if "is_call" not in raw and "is_put" not in raw:
        side = str(raw.get("type") or "").strip().lower()
        out["is_call"] = side == "call"
        out["is_put"] = side == "put"
    return out


class LiveUWAdapter(UWDataAdapter):
    mode = "live"

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, client_api_id: str = "",
                 watchlist: Optional[list] = None, client: Optional[UWRestClient] = None):
        self.client = client or UWRestClient(api_key, base_url=base_url, client_api_id=client_api_id)
        self.watchlist = [t.upper() for t in (watchlist or [])]

    def get_universe(self) -> list:
        return list(self.watchlist)

    def get_flow_alerts(self, ticker: str) -> list:
        body = self.client.flow_alerts(ticker=ticker)
        if body.get("status") in (UNAVAILABLE, UNAUTHORIZED, ERROR):
            return []
        return [_normalize_flow_alert(r) for r in (body.get("data") or [])]

    def get_darkpool_trades(self, ticker: str) -> list:
        body = self.client.darkpool_ticker(ticker)
        if body.get("status") in (UNAVAILABLE, UNAUTHORIZED, ERROR):
            return []
        return list(body.get("data") or [])

    def get_stock_state(self, ticker: str) -> Optional[dict]:
        body = self.client.stock_state(ticker)
        if body.get("status") in (UNAVAILABLE, UNAUTHORIZED, ERROR):
            return {"unavailable": True, "reason": body}
        data = body.get("data") or body
        if not data:
            return None
        return _normalize_stock_state(data)

    def get_ohlc(self, ticker: str, timeframe: str = "1d", limit: int = 180,
                 window: Optional[str] = None) -> dict:
        """`limit` is always the DESIRED number of final bars the caller
        wants -- for daily (`1d`) charts this method requests MORE raw
        rows internally (see raw_limit_for_trading_days -- UW returns up
        to 3 session records per date) and trims back down to `limit`
        after selecting the regular session. Intraday candle sizes need
        no over-fetch: each bucket already has a distinct timestamp, so
        `limit` maps 1:1 to raw rows requested. Response carries
        raw_records_requested/raw_records_returned/
        regular_session_records_retained so callers can verify actual
        coverage rather than assume it (2026-10-15 fix -- see
        candles.py's module docstring gotcha #3 for the full bug
        writeup)."""
        desired_limit = clamp_limit(limit)
        is_daily = (timeframe == "1d")
        raw_request_limit = raw_limit_for_trading_days(desired_limit) if is_daily else desired_limit
        body = self.client.stock_ohlc(ticker, timeframe=timeframe, limit=raw_request_limit, window=window)
        if not isinstance(body, dict) or body.get("status") in (UNAVAILABLE, UNAUTHORIZED, ERROR):
            return {
                "candles": [], "source": "unusual_whales_stock_ohlc",
                "negative_volume_bars_clamped": 0, "unavailable": True,
                "reason": body if isinstance(body, dict) else None,
                "raw_records_requested": raw_request_limit, "raw_records_returned": 0,
                "regular_session_records_retained": 0,
            }
        raw = body.get("data") or []
        raw_records_returned = len(raw)
        session_selected = select_regular_session_bars(raw) if is_daily else raw
        regular_session_records_retained = len(session_selected)
        candles, neg_vol_count = build_candles(session_selected, dedupe_daily=is_daily)
        # We deliberately over-requested raw rows above to guarantee enough
        # UNIQUE trading dates after session selection -- trim any excess
        # back down to what was actually asked for. Never pads; only trims.
        if len(candles) > desired_limit:
            candles = candles[-desired_limit:]
        return {
            "candles": candles, "source": "unusual_whales_stock_ohlc",
            "negative_volume_bars_clamped": neg_vol_count, "unavailable": False, "reason": None,
            "raw_records_requested": raw_request_limit, "raw_records_returned": raw_records_returned,
            "regular_session_records_retained": regular_session_records_retained,
        }

    def discover_candidates(self, filters: Optional[dict] = None) -> dict:
        """ONE bounded REST call to /api/screener/stocks. `filters` may
        contain min_premium/min_call_volume/min_put_volume/min_iv_rank/
        min_change/max_change/limit; limit is ALWAYS clamped to
        DISCOVERY_LIMIT_CAP here before the request is made -- never
        trusts a caller-supplied value, never auto-paginates past it."""
        from datetime import datetime, timezone

        filters = filters or {}
        requested_limit = filters.get("limit", DISCOVERY_LIMIT_CAP)
        try:
            requested_limit = int(requested_limit)
        except (TypeError, ValueError):
            requested_limit = DISCOVERY_LIMIT_CAP
        limit = max(1, min(requested_limit, DISCOVERY_LIMIT_CAP))

        body = self.client.stock_screener(
            limit=limit,
            min_premium=filters.get("min_premium"),
            min_call_volume=filters.get("min_call_volume"),
            min_put_volume=filters.get("min_put_volume"),
            min_iv_rank=filters.get("min_iv_rank"),
            min_change=filters.get("min_change"),
            max_change=filters.get("max_change"),
        )
        snapshot_at = datetime.now(timezone.utc).isoformat()
        if not isinstance(body, dict) or body.get("status") in (UNAVAILABLE, UNAUTHORIZED, ERROR):
            return {
                "candidates": [], "snapshot_at": snapshot_at, "screener_requests_used": 1,
                "source": "unusual_whales_screener_stocks", "unavailable": True,
                "reason": body if isinstance(body, dict) else None,
            }
        candidates = list(body.get("data") or [])[:limit]
        return {
            "candidates": candidates, "snapshot_at": snapshot_at, "screener_requests_used": 1,
            "source": "unusual_whales_screener_stocks", "unavailable": False, "reason": None,
        }

    def health(self) -> dict:
        """REAL BUG FIX (2026-10-15, Priority 4 -- /api/health must make
        ZERO external UW calls): this used to call self.client.stock_state
        ("SPY") -- a REAL outbound UW API request -- on every single
        /api/health hit, which is exactly the opposite of a cheap liveness
        probe (and burns UW quota just from a monitoring/load-balancer
        health check). Structural/local-only now: reports whether a key is
        configured and which base URL would be used, with NO network call.
        Real connectivity is verified separately and explicitly via
        POST /api/settings/uw-key/test (user-triggered, one bounded call,
        already existed for exactly this purpose) -- never implicitly from
        a health check."""
        return {
            "mode": self.mode,
            "base_url": self.client.base_url,
            "api_key_configured": bool(self.client.api_key),
            "probe_ok": None,
            "probe_detail": "Connectivity is not probed by /api/health (zero external calls). "
                             "Use Settings -> Test Connection for a real, explicit UW connectivity check.",
        }
