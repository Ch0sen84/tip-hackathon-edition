#!/usr/bin/env python3
"""Build the PUBLIC, fully synthetic UW replay fixture (deterministic).

Same format as the private recorded capture (tools/build_recorded_uw_fixture.py)
so tools/uw_replay_server.py and the end-to-end tests can run the real Live
pipeline without redistributing any real Unusual Whales data. Every value here
is CONSTRUCTED from a fixed seed; tickers are fictional (SYNP, SYNC, SYNQ).

Scenarios (all relative to the replay instant 2026-10-16T15:02:30Z):
  SYNP  put-BUYING dominated flow while the stock is down ~3.46%  (the pattern
        that 05c47ca mislabeled "bullish" and falsely invalidated)
  SYNC  call-buying dominated flow, stock up ~1.2%, daily price bars for charts
  SYNQ  quiet: a few dark-pool prints, no flow alerts

Usage:
  python tools/build_synthetic_uw_fixture.py --out fixtures/synthetic_uw/synthetic_capture.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 16, 15, 2, 30, tzinfo=timezone.utc)
SESSIONS = (date(2026, 10, 15), date(2026, 10, 16))


def _iso(dt, micro=False):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if micro else "%Y-%m-%dT%H:%M:%SZ")


def build(seed: int = 20261016) -> dict:
    rnd = random.Random(seed)
    flow, darkpool, scans, cat_flow, cat_dp = {}, {}, [], [], []
    stock = {"SYNP": (200.50, 193.57), "SYNC": (101.20, 102.41), "SYNQ": (12.00, 12.01)}
    tid = 10_000_000_000_000
    for ticker, (prev_close, close) in stock.items():
        flow_ids, dp_keys = [], []
        for d in SESSIONS:
            start = datetime.combine(d, time(9, 30), tzinfo=NY)
            end = min(datetime.combine(d, time(16, 0), tzinfo=NY), AS_OF.astimezone(NY))
            n_prints = {"SYNP": 900, "SYNC": 700, "SYNQ": 6}[ticker]
            span = (end - start).total_seconds()
            vol = 1_000_000
            for i in range(n_prints):
                t = start + timedelta(seconds=int(span * (i + 1) / (n_prints + 1)))
                size = rnd.choice((100, 200, 300, 500, 1000, 2500))
                price = round(prev_close * (1 + rnd.uniform(-0.01, 0.01)), 2)
                vol += size + rnd.randint(0, 50_000)
                tid += rnd.randint(1, 3) if i % 7 else 0   # some prints SHARE a tracking_id, like real UW
                row = {"ticker": ticker, "tracking_id": tid, "executed_at": _iso(t), "price": f"{price:.2f}",
                       "size": size, "premium": f"{price * size:.2f}", "volume": vol, "canceled": i % 97 == 0,
                       "market_center": "L", "ext_hour_sold_codes": None, "sale_cond_codes": None,
                       "trade_settlement": "regular", "trade_code": None, "trf_executed_at": _iso(t),
                       "nbbo_ask": f"{price + 0.01:.2f}", "nbbo_bid": f"{price:.2f}"}
                key = f"syn-dp-{ticker}-{d.isoformat()}-{i}"
                darkpool[key] = row
                cat_dp.append({"key": key, "ticker": ticker, "retrieved_at": AS_OF.isoformat()})
                if d == SESSIONS[-1] and i >= n_prints - 500:
                    dp_keys.append(key)
            n_alerts = {"SYNP": 160, "SYNC": 140, "SYNQ": 0}[ticker]
            for i in range(n_alerts):
                t = start + timedelta(seconds=span * (i + 1) / (n_alerts + 1), microseconds=rnd.randint(0, 999_999))
                if ticker == "SYNP":
                    kind = "put" if i % 3 else "call"           # put buying dominates
                else:
                    kind = "call" if i % 4 else "put"           # call buying dominates
                prem = rnd.randint(30, 400) * 1000
                ask = round(prem * rnd.uniform(0.6, 0.9))
                bid = round((prem - ask) * rnd.uniform(0.3, 0.9))
                aid = f"syn-fa-{ticker}-{d.isoformat()}-{i:04d}"
                row = {"id": aid, "ticker": ticker, "created_at": _iso(t, micro=True), "type": kind,
                       "strike": str(round(prev_close)), "expiry": "2026-11-20", "total_premium": str(prem),
                       "total_ask_side_prem": str(ask), "total_bid_side_prem": str(bid), "total_size": prem // 100,
                       "has_sweep": i % 5 == 0, "has_multileg": i % 23 == 0, "has_singleleg": i % 23 != 0,
                       "has_floor": False, "alert_rule": "RepeatedHits", "all_opening_trades": False,
                       "underlying_price": str(prev_close), "issue_type": "Common Stock"}
                flow[aid] = row
                cat_flow.append({"id": aid, "ticker": ticker, "retrieved_at": AS_OF.isoformat()})
                if d == SESSIONS[-1]:
                    flow_ids.append(aid)
        scans.append({
            "ticker": ticker, "retrieved_at": (AS_OF - timedelta(seconds=3)).isoformat(),
            "flow_alert_ids": flow_ids[-100:], "darkpool_keys": dp_keys,
            "stock_state": {"close": f"{close:.2f}", "prev_close": f"{prev_close:.2f}", "open": f"{prev_close:.2f}",
                            "high": f"{max(close, prev_close) * 1.01:.2f}", "low": f"{min(close, prev_close) * 0.99:.2f}",
                            "volume": "8798877", "total_volume": "9061010", "market_time": "regular",
                            "tape_time": _iso(AS_OF - timedelta(seconds=5))},
            "captured_engine": {"state": None, "previous_state": None, "confidence": None,
                                "transition_rule": None, "gates_passed": []},
        })
    bars, px, d = [], 95.0, date(2026, 7, 15)
    while d <= date(2026, 10, 16):
        if d.weekday() < 5:
            o = px
            c = round(px * (1 + rnd.uniform(-0.02, 0.02)), 2)
            bars.append({"t": d.isoformat(), "o": o, "h": round(max(o, c) * 1.01, 2), "l": round(min(o, c) * 0.99, 2),
                         "c": c, "v": rnd.randint(5_000_000, 20_000_000)})
            px = c
        d += timedelta(days=1)
    return {
        "format": "tip-recorded-uw-v1",
        "provenance": {"source": "SYNTHETIC -- constructed by tools/build_synthetic_uw_fixture.py (seed %d)" % seed,
                       "contains_real_market_data": False,
                       "captured_between": [scans[0]["retrieved_at"], scans[-1]["retrieved_at"]],
                       "known_coverage": "synthetic scenario; mirrors the private capture's shape only"},
        "flow_alerts": flow, "darkpool": darkpool, "scans": scans,
        "catalog": {"options_flow": cat_flow, "darkpool": cat_dp}, "price_bars": {"SYNC": {"1d": bars}},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20261016)
    args = ap.parse_args(argv)
    data = build(args.seed)
    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    # deterministic bytes: fixed gzip mtime, sorted keys
    with open(args.out, "wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as fh:
        fh.write(json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    print(f"wrote {args.out}: {len(data['flow_alerts'])} alerts, {len(data['darkpool'])} prints, "
          f"{len(data['scans'])} scans (synthetic)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
