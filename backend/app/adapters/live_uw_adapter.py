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

DEFAULT_BASE_URL = "https://api.unusualwhales.com"
UNAVAILABLE = "unavailable_on_tier"
UNAUTHORIZED = "unauthorized"
ERROR = "error"


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

    def stock_ohlc(self, ticker: str, timeframe: str = "1d", limit: int = 30) -> dict:
        return self.get(f"/api/stock/{ticker}/ohlc", {"timeframe": timeframe, "limit": limit})

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

    def stock_screener(self, min_volume_oi_ratio: Optional[float] = None, limit: int = 50) -> dict:
        return self.get("/api/screener/stocks", {"min_volume_oi_ratio": min_volume_oi_ratio, "limit": limit})

    def option_contract_screener(self, min_premium: Optional[int] = None, limit: int = 50) -> dict:
        return self.get("/api/screener/option-contracts", {"min_premium": min_premium, "limit": limit})

    def insider_transactions(self, ticker: Optional[str] = None, limit: int = 50) -> dict:
        return self.get("/api/insider/transactions", {"ticker": ticker, "limit": limit})

    def congress_recent_trades(self, ticker: Optional[str] = None, limit: int = 50) -> dict:
        return self.get("/api/congress/recent-trades", {"ticker": ticker, "limit": limit})

    def news_headlines(self, ticker: Optional[str] = None, limit: int = 20) -> dict:
        return self.get("/api/news/headlines", {"ticker": ticker, "limit": limit})


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
        return list(body.get("data") or [])

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
        return data if data else None

    def health(self) -> dict:
        probe = self.client.stock_state("SPY")
        tier_ok = not (isinstance(probe, dict) and probe.get("status") in (UNAVAILABLE, UNAUTHORIZED, ERROR))
        return {
            "mode": self.mode,
            "base_url": self.client.base_url,
            "api_key_configured": bool(self.client.api_key),
            "probe_ok": tier_ok,
            "probe_detail": None if tier_ok else probe,
        }
