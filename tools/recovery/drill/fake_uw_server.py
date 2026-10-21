# DRILL ONLY: synthetic fake provider (ticker SYNT) for disposable recovery rehearsals.
"""Local fake Unusual Whales provider for the disposable integration rehearsal.
SYNTHETIC data only (ticker SYNT). Verified pagination contract:
  /api/darkpool/{T}?date&newer_than(incl)&older_than(EXCL)&limit<=500, newest first
  /api/option-trades/flow-alerts?ticker_symbol&newer_than(incl)&older_than(INCL)&limit<=200
Deterministic faults (see FAULTS). Control endpoints: /__stats, /__oracle, /__plan.
No credential is checked or logged; the server never makes outbound calls."""
import json
import sys
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, "/srv/backend")
from app.core import trading_calendar as tc   # noqa: E402

NY = ZoneInfo("America/New_York")
T = "SYNT"
NOW = datetime.now(timezone.utc)
EVIDENCE = tc.evidence_session_date(NOW)
SESSIONS = []
d = EVIDENCE
for _ in range(24):
    d = tc.previous_trading_day(d)
    SESSIONS.append(d)


def z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def P(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


DP, FLOW = [], []
for si, sd in enumerate(SESSIONS + [EVIDENCE]):
    info = tc.session_info(sd)
    o, c = info.session_open_et, info.session_close_et
    t, i = o, 0
    while t <= c and i < 640:                       # ~640 prints: two 500-row pages; 3 share each second
        for k in range(3):
            DP.append({"ticker": T, "executed_at": z(t), "price": "50.00", "size": 10 + k, "premium": "500.00",
                       "tracking_id": f"{sd}-d{i}", "volume": i, "canceled": False})
            i += 1
        t += timedelta(seconds=37)
    DP.append({"ticker": T, "executed_at": z(c), "price": "50.00", "size": 1, "premium": "50.00",
               "tracking_id": f"{sd}-dclose", "volume": 10**6, "canceled": False})      # a print AT the close
    t, i = o + timedelta(minutes=1), 0
    while t < c and i < 260:                        # ~260 alerts: two 200-row pages
        FLOW.append({"id": f"{sd}-a{i}", "ticker": T, "created_at": t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                     "type": "call" if i % 3 else "put", "total_premium": 1000.0, "total_ask_side_prem": 800.0,
                     "total_bid_side_prem": 100.0, "has_sweep": i % 5 == 0, "has_multileg": False})
        t += timedelta(seconds=83, microseconds=137 * i)
        i += 1

S_RECOVER_DP = SESSIONS[2].isoformat()      # dark pool: one out-of-window page, recovered by the retry
S_PERSIST_DP = SESSIONS[4].isoformat()      # dark pool: every response has a non-object row -> unknown
S_RECOVER_FL = SESSIONS[1].isoformat()      # flow: one short page with an unparseable timestamp, recovered
S_TRANSPORT = SESSIONS[6].isoformat()       # dark pool: one 503 and one 429 + Retry-After (transport retries)
lock = Lock()
STATS = {"attempts": 0, "by_path": {}, "fault_hits": {}, "transport_failures": 0}
seen = {}


def hit(name):
    STATS["fault_hits"][name] = STATS["fault_hits"].get(name, 0) + 1


def page(rows, field, lo, hi, inclusive_hi, limit):
    out = [r for r in rows if P(r[field]) >= lo and (P(r[field]) <= hi if inclusive_hi else P(r[field]) < hi)]
    out.sort(key=lambda r: P(r[field]), reverse=True)
    return out[:limit]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/__stats":
            with lock:
                return self.send(200, STATS)
        if u.path == "/__plan":
            return self.send(200, {"evidence": EVIDENCE.isoformat(), "sessions": [s.isoformat() for s in SESSIONS],
                                   "recover_dp": S_RECOVER_DP, "persist_dp": S_PERSIST_DP,
                                   "recover_flow": S_RECOVER_FL, "transport": S_TRANSPORT})
        if u.path == "/__oracle":
            sd = q["date"]
            info = tc.session_info(datetime.fromisoformat(sd).date())
            o, c = info.session_open_et.astimezone(timezone.utc), info.session_close_et.astimezone(timezone.utc)
            if q["dataset"] == "darkpool":
                keys = [r["tracking_id"] for r in DP if r["executed_at"][:10] == sd and o <= P(r["executed_at"]) <= c]
            else:
                keys = [r["id"] for r in FLOW if o <= P(r["created_at"]) <= c]
            return self.send(200, {"keys": keys})
        with lock:
            STATS["attempts"] += 1
            STATS["by_path"][u.path] = STATS["by_path"].get(u.path, 0) + 1
        if u.path.startswith("/api/darkpool/") and u.path.rsplit("/", 1)[1].upper() == T:
            return self.darkpool(q)
        if u.path == "/api/option-trades/flow-alerts" and q.get("ticker_symbol", "").upper() == T:
            return self.flow(q)
        if "/stock-state" in u.path or "/info" in u.path:
            return self.send(200, {"data": {"close": "50.00", "prev_close": "49.50"}})
        return self.send(404, {"error": "no route"})

    def darkpool(self, q):
        sd, lo, hi, limit = q["date"], P(q["newer_than"]), P(q["older_than"]), min(int(q.get("limit", 500)), 500)
        rows = page([r for r in DP if P(r["executed_at"]).astimezone(NY).date().isoformat() == sd],
                    "executed_at", lo, hi, False, limit)
        key = (sd, q["older_than"])
        with lock:
            n = seen[key] = seen.get(key, 0) + 1
        if sd == S_TRANSPORT and n == 1 and STATS["fault_hits"].get("503", 0) == 0:
            hit("503"); STATS["transport_failures"] += 1
            return self.send(503, {"error": "upstream"})
        if sd == S_TRANSPORT and n == 2 and STATS["fault_hits"].get("429", 0) == 0:
            hit("429"); STATS["transport_failures"] += 1
            return self.send(429, {"error": "slow down"}, {"Retry-After": "1"})
        if sd == S_RECOVER_DP and n == 1 and not q["older_than"].endswith(":01Z") and rows:
            hit("dp_out_of_window")                 # the session's 2nd-page window answered with a row above the cursor, once
            bad = dict(rows[0], tracking_id="INJECTED", executed_at=z(hi + timedelta(seconds=45)))
            return self.send(200, {"data": [bad] + rows[:-1]})
        if sd == S_PERSIST_DP:
            hit("dp_non_object")
            return self.send(200, {"data": rows[:-1] + [["SYNT", q["older_than"], "garbage"]] if rows else [["x"]]})
        return self.send(200, {"data": rows})

    def flow(self, q):
        lo, hi, limit = P(q["newer_than"]), P(q["older_than"]), min(int(q.get("limit", 200)), 200)
        rows = page(FLOW, "created_at", lo, hi, True, limit)
        key = ("flow", q["newer_than"], q["older_than"])
        with lock:
            n = seen[key] = seen.get(key, 0) + 1
        if q["newer_than"][:10] == S_RECOVER_FL and len(rows) < limit and n == 1:
            hit("flow_unparseable")                 # the session's last (short) page carries a bad timestamp, once
            return self.send(200, {"data": rows + [dict(rows[0] if rows else {}, id="INJECTED", created_at="not-a-time")]})
        return self.send(200, {"data": rows})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
