"""A network-free fake of UW's paginated endpoints, for exercising the real
LiveUWAdapter + session collector + baseline + lifecycle + API end to end.

Rows are served exactly as UW's documented cursor semantics describe:
  /api/darkpool/{T}?date=D&limit=N&older_than=TS  -> prints of NY date D with
      executed_at < TS, newest first, at most N
  /api/option-trades/flow-alerts?ticker_symbol=T&limit=N&older_than=TS
      -> alerts with created_at < TS, newest first, at most N
Anything else is served from `static` routes (substring match), else 404.
`fail` maps a path substring to a FakeResponse to inject provider failures.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient
from tests.uw_fakes import FakeResponse

NY = ZoneInfo("America/New_York")


def _ts(v) -> datetime:
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PaginatedUW:
    def __init__(self, darkpool=None, flow=None, static=None, fail=None):
        self.darkpool = {}   # ticker -> [rows]
        self.flow = {}
        for r in darkpool or []:
            self.darkpool.setdefault(r["ticker"].upper(), []).append(r)
        for r in flow or []:
            self.flow.setdefault(r["ticker"].upper(), []).append(r)
        self.static = dict(static or {})
        self.fail = dict(fail or {})
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        for key, resp in self.fail.items():
            if key in url:
                return resp
        if "/api/darkpool/" in url and "price-levels" not in url:
            ticker = unquote(url.rsplit("/api/darkpool/", 1)[1]).upper()
            rows = self.darkpool.get(ticker, [])
            if params.get("date"):
                d = date.fromisoformat(params["date"])
                rows = [r for r in rows if _ts(r["executed_at"]).astimezone(NY).date() == d]
            return FakeResponse(200, {"data": self._page(rows, "executed_at", params)})
        if "flow-alerts" in url:
            rows = self.flow.get(str(params.get("ticker_symbol", "")).upper(), [])
            return FakeResponse(200, {"data": self._page(rows, "created_at", params)})
        for key, resp in self.static.items():
            if key in url:
                return resp
        return FakeResponse(404, {"error": f"no route for {url}"})

    @staticmethod
    def _page(rows, field, params):
        if params.get("older_than"):
            cut = _ts(params["older_than"])
            rows = [r for r in rows if _ts(r[field]) < cut]
        rows = sorted(rows, key=lambda r: r[field], reverse=True)
        return rows[: int(params.get("limit") or 100)]

    def adapter(self, watchlist=None) -> LiveUWAdapter:
        client = UWRestClient("fake-test-key", base_url="http://127.0.0.1:9", session=self,
                              max_retries=0, sleep_fn=lambda _s: None, circuit_breaker_threshold=1000)
        return LiveUWAdapter("fake-test-key", client=client, watchlist=watchlist or [])


def session_prints(ticker: str, d: date, premium_per_print: float, every_minutes: int = 5,
                   start: time = time(9, 30), end: time = time(16, 0), tag: str = "") -> list:
    """Regular-session dark-pool prints at a fixed cadence (constructed data)."""
    out, t = [], datetime.combine(d, start, tzinfo=NY)
    stop = datetime.combine(d, end, tzinfo=NY)
    i = 0
    while t <= stop:
        out.append({"ticker": ticker, "executed_at": iso(t), "price": "100.00", "size": 100,
                    "premium": f"{premium_per_print:.2f}", "tracking_id": f"{ticker}{tag}{d.isoformat()}-{i}",
                    "canceled": False})
        t += timedelta(minutes=every_minutes)
        i += 1
    return out


def session_alerts(ticker: str, d: date, premium: float, every_minutes: int = 15, kind: str = "call",
                   start: time = time(9, 35), end: time = time(15, 55), tag: str = "") -> list:
    """Regular-session flow alerts at a fixed cadence (constructed data)."""
    out, t = [], datetime.combine(d, start, tzinfo=NY)
    stop = datetime.combine(d, end, tzinfo=NY)
    i = 0
    while t <= stop:
        out.append({"id": f"{ticker}{tag}{d.isoformat()}-a{i}", "ticker": ticker, "created_at": iso(t),
                    "type": kind, "total_premium": premium, "total_ask_side_prem": premium * 0.8,
                    "total_bid_side_prem": premium * 0.1, "has_sweep": True, "has_multileg": False})
        t += timedelta(minutes=every_minutes)
        i += 1
    return out


def prior_sessions(d: date, n: int) -> list:
    from app.core import trading_calendar as tc
    out = []
    for _ in range(n):
        d = tc.previous_trading_day(d)
        out.append(d)
    return out
