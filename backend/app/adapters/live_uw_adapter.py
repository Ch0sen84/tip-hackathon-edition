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

import os
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import requests

from .base import UWDataAdapter
from ..core.candles import (
    build_candles, clamp_limit, raw_limit_for_trading_days, select_regular_session_bars,
)
from ..core import uw_concurrency

DEFAULT_BASE_URL = "https://api.unusualwhales.com"
UNAVAILABLE = "unavailable_on_tier"
UNAUTHORIZED = "unauthorized"
ERROR = "error"
CIRCUIT_OPEN = "circuit_open"

# --- Phase B: UW request-handling design constants ------------------------
#
# CONFIRMED THIS SESSION (2026-10-15, live validation + UW's own
# changelog/dashboard): UW removed its daily and per-minute rate limits
# on the Basic plan, but a HARD CEILING OF 3 CONCURRENT REQUESTS is still
# enforced server-side and returns HTTP 429 above it.
#
# Phase D (2026-10-15, Chad's review): this app's OWN default guard is
# now 2, not 3 -- deliberately one below UW's real hard ceiling. Two
# reasons: (1) the Phase D background scheduler now shares this exact
# budget with interactive requests (see uw_concurrency.py), so the
# previous "use the whole ceiling" default left zero headroom for a
# legitimate momentary overlap (e.g. the scheduler's own retry backoff
# landing at the same instant as a user-initiated scan); (2) the gate
# this value sizes is CONFIRMED process-local (threading.Semaphore-
# based), not cross-process-safe -- see uw_concurrency.py and
# core/single_process_guard.py for how that gap is closed structurally
# (refuse to start under a multi-worker launcher) rather than papered
# over with a bigger number. Configurable via env var only for a
# verified different ceiling/plan.
DEFAULT_MAX_CONCURRENT_REQUESTS = 2
DEFAULT_TIMEOUT_S = 20
DEFAULT_MAX_RETRIES = 2  # retries AFTER the first attempt -- 3 attempts total
DEFAULT_RETRY_BASE_DELAY_S = 0.2
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_S = 30.0

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class RequestOutcome:
    path: str
    status_code: Optional[int]
    outcome: str  # "success" | "retried_success" | "rate_limited" | "unauthorized" |
                   # "unavailable_on_tier" | "server_error" | "network_error" | "circuit_open"
    attempts: int
    latency_s: float


class RequestTelemetry:
    """Simple in-process counters/latencies per endpoint path -- exposed
    read-only via a small diagnostics structure (see main.py's
    /api/diagnostics/uw-requests route, which sits behind require_auth
    like every other sensitive route). Never persisted, never exposes a
    credential; purely request counts/latencies/outcomes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_path: dict = defaultdict(lambda: {
            "count": 0, "success": 0, "retried": 0, "failed": 0,
            "by_outcome": defaultdict(int), "total_latency_s": 0.0,
        })

    def record(self, outcome: RequestOutcome) -> None:
        with self._lock:
            entry = self._by_path[outcome.path]
            entry["count"] += 1
            entry["total_latency_s"] += outcome.latency_s
            entry["by_outcome"][outcome.outcome] += 1
            if outcome.attempts > 1:
                entry["retried"] += 1
            if outcome.outcome in ("success", "retried_success"):
                entry["success"] += 1
            else:
                entry["failed"] += 1

    def summary(self) -> dict:
        with self._lock:
            out = {}
            for path, entry in self._by_path.items():
                count = entry["count"]
                out[path] = {
                    "count": count,
                    "success": entry["success"],
                    "retried": entry["retried"],
                    "failed": entry["failed"],
                    "avg_latency_s": round(entry["total_latency_s"] / count, 4) if count else 0.0,
                    "by_outcome": dict(entry["by_outcome"]),
                }
            return out


class CircuitOpenError(Exception):
    def __init__(self, path: str, retry_after_s: float):
        super().__init__(f"circuit open for {path!r}; retry after {retry_after_s:.1f}s")
        self.path = path
        self.retry_after_s = retry_after_s


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    open_until: float = 0.0


class CircuitBreaker:
    """Per-endpoint-path circuit breaker: after `threshold` consecutive
    failures (network error or 5xx/429 exhausting retries) to the SAME
    path, pauses further calls to that path for `cooldown_s` before
    trying again. A success anywhere resets that path's counter. This
    never affects OTHER paths -- one endpoint misbehaving doesn't pause
    the whole client."""

    def __init__(self, threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
                 cooldown_s: float = DEFAULT_CIRCUIT_BREAKER_COOLDOWN_S):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._state: dict = defaultdict(_CircuitState)

    def before_call(self, path: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state[path]
            if state.open_until and now < state.open_until:
                raise CircuitOpenError(path, state.open_until - now)

    def record_success(self, path: str) -> None:
        with self._lock:
            state = self._state[path]
            state.consecutive_failures = 0
            state.open_until = 0.0

    def record_failure(self, path: str, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._state[path]
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.threshold:
                state.open_until = now + self.cooldown_s

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
                 client_api_id: str = "", timeout: Optional[int] = None,
                 session: Optional[requests.Session] = None,
                 max_concurrent_requests: Optional[int] = None,
                 max_retries: Optional[int] = None,
                 retry_base_delay_s: Optional[float] = None,
                 circuit_breaker_threshold: Optional[int] = None,
                 circuit_breaker_cooldown_s: Optional[float] = None,
                 telemetry: Optional[RequestTelemetry] = None,
                 circuit_breaker: Optional[CircuitBreaker] = None,
                 sleep_fn=None, jitter_fn=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client_api_id = client_api_id
        self.timeout = timeout if timeout is not None else _env_int("TIP_UW_TIMEOUT_S", DEFAULT_TIMEOUT_S)
        self.session = session or requests.Session()
        # Phase B: hard ceiling of 3 CONCURRENT requests, confirmed live
        # this session as still server-side enforced (429 above it) even
        # though UW removed daily/per-minute caps on Basic -- default
        # must stay 3; only override via env var for a verified
        # different plan/ceiling.
        self.max_concurrent_requests = (
            max_concurrent_requests if max_concurrent_requests is not None
            else _env_int("TIP_UW_MAX_CONCURRENT_REQUESTS", DEFAULT_MAX_CONCURRENT_REQUESTS)
        )
        # Phase D: a SHARED, process-wide gate (see core/uw_concurrency.py)
        # rather than an instance-private threading.Semaphore. A plain
        # instance semaphore only ever worked by accident: app.main's
        # get_adapter() caches one adapter (and therefore one client) per
        # (mode, key, watchlist) signature, but the moment that signature
        # changes -- a mode toggle, a saved-key change, a watchlist edit --
        # a BRAND NEW UWRestClient is constructed with a BRAND NEW
        # semaphore, silently resetting the in-flight count for whatever
        # the OLD instance still had outstanding. Pulling the gate from a
        # process-wide registry closes that gap for every call path, not
        # just the common case where the adapter signature never changes.
        self._gate = uw_concurrency.get_shared_gate(self.max_concurrent_requests)
        self.max_retries = (
            max_retries if max_retries is not None else _env_int("TIP_UW_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        )
        self.retry_base_delay_s = (
            retry_base_delay_s if retry_base_delay_s is not None
            else _env_float("TIP_UW_RETRY_BASE_DELAY_S", DEFAULT_RETRY_BASE_DELAY_S)
        )
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            threshold=(circuit_breaker_threshold if circuit_breaker_threshold is not None
                       else _env_int("TIP_UW_CIRCUIT_BREAKER_THRESHOLD", DEFAULT_CIRCUIT_BREAKER_THRESHOLD)),
            cooldown_s=(circuit_breaker_cooldown_s if circuit_breaker_cooldown_s is not None
                        else _env_float("TIP_UW_CIRCUIT_BREAKER_COOLDOWN_S", DEFAULT_CIRCUIT_BREAKER_COOLDOWN_S)),
        )
        self.telemetry = telemetry or RequestTelemetry()
        self._sleep = sleep_fn or time.sleep
        self._jitter = jitter_fn or (lambda: random.uniform(0, 0.1))

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.client_api_id:
            headers["UW-CLIENT-API-ID"] = self.client_api_id
        return headers

    def _backoff_delay(self, attempt: int, retry_after: Optional[float]) -> float:
        """Exponential backoff + jitter, honoring a real `Retry-After`
        header when the server sent one (429 case) -- otherwise
        `retry_base_delay_s * 2**attempt` plus a small random jitter so
        many clients retrying at once don't all collide on the same
        instant."""
        if retry_after is not None and retry_after > 0:
            return retry_after + self._jitter()
        return self.retry_base_delay_s * (2 ** attempt) + self._jitter()

    @staticmethod
    def _parse_retry_after(resp) -> Optional[float]:
        header = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
        if not header:
            return None
        try:
            return float(header)
        except (TypeError, ValueError):
            return None

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        if not self.api_key:
            return _unavailable(path, 401, "No UW_API_KEY configured", UNAUTHORIZED)

        try:
            self.circuit_breaker.before_call(path)
        except CircuitOpenError as e:
            self.telemetry.record(RequestOutcome(path, None, CIRCUIT_OPEN, 0, 0.0))
            return _unavailable(
                path, 0,
                f"Circuit breaker open for {path} after repeated failures; "
                f"retry in {e.retry_after_s:.1f}s",
                ERROR,
            )

        url = f"{self.base_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        attempts = 0
        max_attempts = self.max_retries + 1
        last_result: Optional[dict] = None
        start = time.monotonic()
        while attempts < max_attempts:
            attempts += 1
            with self._gate.acquire(priority=uw_concurrency.current_priority()):
                try:
                    resp = self.session.get(url, headers=self._headers(), params=clean_params, timeout=self.timeout)
                except requests.RequestException as e:
                    last_result = _unavailable(path, 0, f"{type(e).__name__}: {e}", ERROR)
                    if attempts < max_attempts:
                        self._sleep(self._backoff_delay(attempts - 1, None))
                        continue
                    self.circuit_breaker.record_failure(path)
                    self.telemetry.record(RequestOutcome(path, None, "network_error", attempts,
                                                           time.monotonic() - start))
                    return last_result

            # 401/403 are DEFINITIVE, non-transient problems (bad
            # credentials / endpoint not on this tier) -- surfaced
            # immediately, NEVER retried (retrying a bad key just wastes
            # the concurrency budget on a call that will never succeed).
            if resp.status_code == 401:
                self.circuit_breaker.record_success(path)  # not an endpoint-health failure
                self.telemetry.record(RequestOutcome(path, 401, "unauthorized", attempts,
                                                       time.monotonic() - start))
                return _unavailable(path, 401, resp.text, UNAUTHORIZED)
            if resp.status_code == 403:
                self.circuit_breaker.record_success(path)
                self.telemetry.record(RequestOutcome(path, 403, "unavailable_on_tier", attempts,
                                                       time.monotonic() - start))
                return _unavailable(path, 403, resp.text, UNAVAILABLE)

            if resp.status_code == 429:
                last_result = _unavailable(path, 429, resp.text, ERROR)
                if attempts < max_attempts:
                    retry_after = self._parse_retry_after(resp)
                    self._sleep(self._backoff_delay(attempts - 1, retry_after))
                    continue
                self.circuit_breaker.record_failure(path)
                self.telemetry.record(RequestOutcome(path, 429, "rate_limited", attempts,
                                                       time.monotonic() - start))
                return last_result

            if resp.status_code >= 500:
                last_result = _unavailable(path, resp.status_code, resp.text, ERROR)
                if attempts < max_attempts:
                    self._sleep(self._backoff_delay(attempts - 1, None))
                    continue
                self.circuit_breaker.record_failure(path)
                self.telemetry.record(RequestOutcome(path, resp.status_code, "server_error", attempts,
                                                       time.monotonic() - start))
                return last_result

            if not resp.ok:
                # Any other non-success (4xx other than 401/403/429) is
                # not retried -- it's a client-side request problem, not
                # a transient server/rate-limit condition.
                self.circuit_breaker.record_success(path)
                self.telemetry.record(RequestOutcome(path, resp.status_code, ERROR, attempts,
                                                       time.monotonic() - start))
                return _unavailable(path, resp.status_code, resp.text, ERROR)

            self.circuit_breaker.record_success(path)
            outcome = "success" if attempts == 1 else "retried_success"
            self.telemetry.record(RequestOutcome(path, resp.status_code, outcome, attempts,
                                                   time.monotonic() - start))
            try:
                return resp.json()
            except ValueError:
                return _unavailable(path, resp.status_code, resp.text, ERROR)

        return last_result if last_result is not None else _unavailable(path, 0, "exhausted retries", ERROR)

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
