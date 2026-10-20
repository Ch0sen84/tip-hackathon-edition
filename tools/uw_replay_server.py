#!/usr/bin/env python3
"""Local HTTP replay of RECORDED Unusual Whales responses (test tooling).

Serves fixtures/recorded_uw/*.json.gz over a real socket so the unmodified
LiveUWAdapter (real `requests`, real retries, real normalization), session
collector, gates, lifecycle, API and browser UI can be exercised end to end
without any live UW request. Point the backend at it with
UW_BASE_URL=http://127.0.0.1:<port> and pin its clock with TIP_REPLAY_CLOCK
(honored only for a loopback UW_BASE_URL).

Semantics follow the LIVE-VERIFIED UW contract (docs/UW_PAGINATION_VERIFICATION.md):
  GET /api/darkpool/{T}?date=D&newer_than=A&older_than=B&limit=N
      prints of NY date D, A <= executed_at < B (older_than exclusive)
  GET /api/option-trades/flow-alerts?ticker_symbol=T&newer_than=A&older_than=B&limit=N
      A <= created_at <= B (older_than inclusive); 7-day default window
  GET /api/stock/{T}/stock-state      the latest recorded snapshot at/before --as-of
  GET /api/stock/{T}/ohlc/{size}      recorded bars (wire rows, market_time "r")
  GET /api/stock/{T}/info             {"data": {"ticker": T}}  (connection test)
  GET /api/screener/stocks            {"data": []}
Rows newer than --as-of are never served (no look-ahead). Before the first
recorded row of a ticker the replay answers 403 historic_data_access_missing
(it has no history there). LIMITATION: inside the recorded dates the capture
is a sparse sample (each Live scan saw only the newest page), so coverage
"completeness" computed against this replay reflects the recording, not the
real provider -- real coverage semantics are tested with exact paginated
fakes (backend/tests/uw_paginated.py) and were verified live
(docs/UW_PAGINATION_VERIFICATION.md).

Failure injection / introspection (loopback only, for tests):
  POST /__control/fail?path=<substring>&status=<code>   e.g. path=flow-alerts&status=503
  POST /__control/malformed?path=<substring>            serve a non-JSON 200 body
  POST /__control/reset
  GET  /__control/calls                                  request log

Usage:
  python tools/uw_replay_server.py --port 18900 --as-of 2026-10-16T15:02:30Z
"""
from __future__ import annotations

import argparse
import gzip
import json
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "recorded_uw" / "uw_capture_2026-10-15_17.json.gz"


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


class ReplayData:
    def __init__(self, fixture: Path, as_of: datetime):
        with gzip.open(fixture, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        self.as_of = as_of
        self.darkpool, self.flow = {}, {}
        for r in data["darkpool"].values():
            if r.get("executed_at") and _ts(r["executed_at"]) <= as_of:
                self.darkpool.setdefault(str(r["ticker"]).upper(), []).append(r)
        for r in data["flow_alerts"].values():
            if r.get("created_at") and _ts(r["created_at"]) <= as_of:
                self.flow.setdefault(str(r["ticker"]).upper(), []).append(r)
        self.stock_state = {}
        for s in data["scans"]:
            if s["stock_state"] and _ts(s["retrieved_at"]) <= as_of:
                self.stock_state[s["ticker"].upper()] = s["stock_state"]  # scans are chronological
        self.bars = data["price_bars"]
        # The replay's own history cutoff: before the first recorded row for a
        # ticker it has NO data, so it answers like UW's documented
        # 403 historic_data_access_missing instead of pretending the session
        # was empty. (Inside the recorded dates the capture is still a SPARSE
        # sample -- newest pages only -- which the replay cannot reveal; see
        # the module docstring.)
        self.first_dp_date = {t: min(_ts(r["executed_at"]).astimezone(NY).date() for r in rows)
                              for t, rows in self.darkpool.items()}
        self.first_flow_ts = {t: min(_ts(r["created_at"]) for r in rows) for t, rows in self.flow.items()}

    @staticmethod
    def page(rows, field, q, upper_inclusive=False):
        if q.get("newer_than"):
            lo = _ts(q["newer_than"])
            rows = [r for r in rows if _ts(r[field]) >= lo]
        if q.get("older_than"):
            cut = _ts(q["older_than"])
            rows = [r for r in rows if (_ts(r[field]) <= cut if upper_inclusive else _ts(r[field]) < cut)]
        rows = sorted(rows, key=lambda r: _ts(r[field]), reverse=True)
        return rows[: int(q.get("limit") or 100)]


def make_handler(data: ReplayData, state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code, body, raw=None):
            payload = raw.encode() if raw is not None else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/__control/fail":
                state["fail"][q["path"]] = int(q.get("status", 503))
            elif u.path == "/__control/malformed":
                state["malformed"].add(q["path"])
            elif u.path == "/__control/reset":
                state["fail"].clear()
                state["malformed"].clear()
                state["calls"].clear()
            else:
                return self._send(404, {"error": "unknown control"})
            return self._send(200, {"ok": True})

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/__control/calls":
                return self._send(200, {"calls": state["calls"]})
            state["calls"].append(self.path)
            for frag, code in state["fail"].items():
                if frag in self.path:
                    return self._send(code, {"error": f"injected {code}"})
            for frag in state["malformed"]:
                if frag in self.path:
                    return self._send(200, None, raw="<html>upstream gateway page</html>")
            p = u.path
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(401, {"error": "missing bearer token"})
            if p.startswith("/api/darkpool/"):
                t = unquote(p.rsplit("/", 1)[1]).upper()
                rows = data.darkpool.get(t, [])
                if q.get("date"):
                    d = date.fromisoformat(q["date"])
                    if t not in data.first_dp_date or d < data.first_dp_date[t]:
                        return self._send(403, {"code": "historic_data_access_missing"})
                    rows = [r for r in rows if _ts(r["executed_at"]).astimezone(NY).date() == d]
                return self._send(200, {"data": data.page(rows, "executed_at", q, upper_inclusive=False)})
            if p == "/api/option-trades/flow-alerts":
                t = str(q.get("ticker_symbol", "")).upper()
                rows = data.flow.get(t, [])
                if q.get("newer_than") and (t not in data.first_flow_ts or _ts(q["newer_than"]).astimezone(NY).date()
                                            < data.first_flow_ts[t].astimezone(NY).date()):
                    return self._send(403, {"code": "historic_data_access_missing"})
                if not q.get("newer_than"):
                    from datetime import timedelta
                    hi = _ts(q["older_than"]) if q.get("older_than") else data.as_of
                    q = {**q, "newer_than": (hi - timedelta(days=7)).isoformat()}
                return self._send(200, {"data": data.page(rows, "created_at", q, upper_inclusive=True)})
            parts = p.strip("/").split("/")
            if len(parts) >= 4 and parts[:2] == ["api", "stock"]:
                t = parts[2].upper()
                if parts[3] == "info":
                    return self._send(200, {"data": {"ticker": t}})
                if parts[3] == "stock-state":
                    ss = data.stock_state.get(t)
                    return self._send(200, {"data": ss or {}})
                if parts[3] == "ohlc" and len(parts) == 5:
                    tf = parts[4]
                    bars = (data.bars.get(t) or {}).get(tf) or []
                    rows = [{"start_time": b["t"], "end_time": b["t"], "date": str(b["t"])[:10],
                             "open": str(b["o"]), "high": str(b["h"]), "low": str(b["l"]),
                             "close": str(b["c"]), "volume": int(b["v"] or 0), "market_time": "r"}
                            for b in bars]
                    return self._send(200, {"data": list(reversed(rows))[: int(q.get("limit") or 500)]})
            if p == "/api/screener/stocks":
                return self._send(200, {"data": []})
            return self._send(404, {"error": f"no replay route for {p}"})

    return Handler


def start(port: int = 0, as_of: datetime = None, fixture: Path = DEFAULT_FIXTURE):
    """Start in a background thread; returns (server, base_url, state)."""
    as_of = as_of or datetime.now(timezone.utc)
    state = {"fail": {}, "malformed": set(), "calls": []}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(ReplayData(fixture, as_of), state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18900)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    args = ap.parse_args()
    server, url, _ = start(args.port, _ts(args.as_of), Path(args.fixture))
    print(f"UW replay serving {args.fixture} as of {args.as_of} at {url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
