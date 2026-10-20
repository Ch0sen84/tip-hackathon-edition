#!/usr/bin/env python3
"""Build the sanitized recorded-UW fixture from a captured TIP database.

Reads (read-only, SQLite mode=ro) a tip_hackathon.db that a real Live-mode
install captured, and writes fixtures/recorded_uw/<name>.json.gz containing:

  * flow_alerts   {id: row}           unique UW flow-alert rows
  * darkpool      {key: row}          unique UW dark-pool prints
  * scans         [...]               each captured Live evaluation: which
                                       rows it received, its stock-state
                                       response, and the state the shipped
                                       engine assigned at capture time
  * catalog       options_flow/darkpool observation keys + retrieved_at
  * price_bars    {ticker: {timeframe: [bar, ...]}}

Rows are converted back to UW's WIRE format (the adapter's normalization
added total_ask_premium/total_bid_premium/is_call/is_put and float-typed
stock-state fields; those are removed/stringified) so replaying them through
the real LiveUWAdapter exercises the real normalization code.

Sanitization: only market-data fields the app reads are kept (see KEEP_*).
No credential, account, settings, or user field exists in these tables; the
tool never opens .env / settings.json / .auth_token. The data is real UW
market data -- check the Unusual Whales terms before redistributing it.

Usage:
  python tools/build_recorded_uw_fixture.py --db <captured.db> --out fixtures/recorded_uw/uw_capture_2026-10-15_17.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys

KEEP_FLOW = ("id", "ticker", "created_at", "start_time", "end_time", "type", "strike", "expiry",
             "total_premium", "total_ask_side_prem", "total_bid_side_prem", "total_size",
             "trade_count", "volume", "open_interest", "price", "underlying_price",
             "has_sweep", "has_multileg", "has_singleleg", "has_floor", "alert_rule",
             "all_opening_trades", "issue_type")
KEEP_DARKPOOL = ("tracking_id", "ticker", "executed_at", "price", "size", "premium", "volume",
                 "canceled", "market_center", "ext_hour_sold_codes", "sale_cond_codes",
                 "trade_settlement", "trade_code", "trf_executed_at", "nbbo_ask", "nbbo_bid")
STOCK_STATE_WIRE = ("close", "prev_close", "open", "high", "low", "volume", "total_volume",
                    "market_time", "tape_time")


def flow_wire(row: dict) -> dict:
    out = {k: row.get(k) for k in KEEP_FLOW if k in row}
    # adapter-normalized duplicates -> real UW names only
    if "total_ask_side_prem" not in out and row.get("total_ask_premium") is not None:
        out["total_ask_side_prem"] = row["total_ask_premium"]
    if "total_bid_side_prem" not in out and row.get("total_bid_premium") is not None:
        out["total_bid_side_prem"] = row["total_bid_premium"]
    return out


def darkpool_wire(row: dict) -> dict:
    return {k: row.get(k) for k in KEEP_DARKPOOL if k in row}


def darkpool_key(row: dict) -> str:
    if row.get("tracking_id") is not None:
        return f"t{row['tracking_id']}"
    return "f|" + "|".join(str(row.get(k)) for k in ("ticker", "executed_at", "price", "size", "premium"))


def stock_state_wire(norm: dict) -> dict:
    """Normalized -> UW wire (string numerics, as UW actually sends them)."""
    mapping = {"close": norm.get("last_price"), "prev_close": norm.get("prev_close"),
               "open": norm.get("open"), "high": norm.get("high"), "low": norm.get("low"),
               "volume": norm.get("volume"), "total_volume": norm.get("total_volume"),
               "market_time": norm.get("market_time"), "tape_time": norm.get("tape_time")}
    out = {}
    for k, v in mapping.items():
        if v is None:
            continue
        if isinstance(v, float) and k in ("volume", "total_volume"):
            v = int(v)
        out[k] = str(v) if isinstance(v, (int, float)) else v
    return out


def build(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    flow, darkpool, scans = {}, {}, []
    catalog = {"options_flow": [], "darkpool": []}
    for ticker, retrieved_at, raw_json in conn.execute(
            "SELECT ticker, retrieved_at, raw_json FROM signal_evaluations ORDER BY retrieved_at, id"):
        r = json.loads(raw_json)
        raw = r.get("raw") or {}
        flow_ids, dp_keys = [], []
        for a in raw.get("flow_alerts") or []:
            w = flow_wire(a)
            flow[str(w["id"])] = w
            flow_ids.append(str(w["id"]))
        for t in raw.get("darkpool_trades") or []:
            w = darkpool_wire(t)
            k = darkpool_key(w)
            darkpool[k] = w
            dp_keys.append(k)
        ss = r.get("stock_state") or {}
        scans.append({
            "ticker": ticker, "retrieved_at": retrieved_at,
            "flow_alert_ids": flow_ids, "darkpool_keys": dp_keys,
            "stock_state": stock_state_wire(ss) if ss and not ss.get("unavailable") else None,
            "captured_engine": {"state": r.get("state"), "previous_state": r.get("previous_state"),
                                "confidence": r.get("confidence"), "transition_rule": r.get("transition_rule"),
                                "gates_passed": r.get("gates_passed")},
        })
    for ticker, event_time, retrieved_at, raw_json in conn.execute(
            "SELECT ticker, event_time, retrieved_at, raw_json FROM options_flow_observations ORDER BY event_time"):
        w = flow_wire(json.loads(raw_json))
        flow.setdefault(str(w["id"]), w)
        catalog["options_flow"].append({"id": str(w["id"]), "ticker": ticker, "retrieved_at": retrieved_at})
    for ticker, event_time, retrieved_at, raw_json in conn.execute(
            "SELECT ticker, event_time, retrieved_at, raw_json FROM darkpool_observations ORDER BY event_time"):
        w = darkpool_wire(json.loads(raw_json))
        k = darkpool_key(w)
        darkpool.setdefault(k, w)
        catalog["darkpool"].append({"key": k, "ticker": ticker, "retrieved_at": retrieved_at})
    bars: dict = {}
    # newest revision per (ticker, timeframe, event_time); works with or
    # without the later price_bar_current pointer table
    for ticker, timeframe, event_time, o, h, l, c, v in conn.execute(
            "SELECT pb.ticker, pb.timeframe, pb.event_time, pb.open, pb.high, pb.low, pb.close, pb.volume "
            "FROM price_bars pb WHERE pb.id = (SELECT p2.id FROM price_bars p2 WHERE p2.ticker = pb.ticker "
            "AND p2.timeframe = pb.timeframe AND p2.event_time = pb.event_time "
            "ORDER BY p2.retrieved_at DESC, p2.id DESC LIMIT 1) ORDER BY pb.event_time"):
        bars.setdefault(ticker, {}).setdefault(timeframe, []).append(
            {"t": event_time, "o": o, "h": h, "l": l, "c": c, "v": v})
    conn.close()
    return {
        "format": "tip-recorded-uw-v1",
        "provenance": {
            "source": "Unusual Whales API responses captured by a TIP Hackathon Edition Live-mode install",
            "captured_between": [scans[0]["retrieved_at"], scans[-1]["retrieved_at"]] if scans else None,
            "builder": "tools/build_recorded_uw_fixture.py",
            "sanitization": "market-data fields only (KEEP_FLOW/KEEP_DARKPOOL); wire-format field names; "
                            "no credentials or user/account data",
            "known_coverage": "no trading session in this capture is fully paginated: every scan saw at most "
                              "the newest 100 flow alerts and 500 dark-pool prints",
        },
        "flow_alerts": flow, "darkpool": darkpool, "scans": scans, "catalog": catalog, "price_bars": bars,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    data = build(args.db)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with gzip.open(args.out, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"), sort_keys=True)
    print(f"wrote {args.out}: {len(data['scans'])} scans, {len(data['flow_alerts'])} flow alerts, "
          f"{len(data['darkpool'])} dark-pool prints, {sum(len(v) for t in data['price_bars'].values() for v in t.values())} bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
