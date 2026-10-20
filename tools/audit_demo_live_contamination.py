#!/usr/bin/env python3
"""READ-ONLY audit: find Demo-mode lifecycle rows inside a Live database.

Before the Demo/Live storage split, Demo Mode wrote its lifecycle state and
scan history (ticker_scan_log / ticker_current_state) into the SAME SQLite
file Live Mode used, with no mode column. This tool reports which of those
legacy rows are attributable to Demo fixtures so an operator can decide what
to do with them. It never modifies the database (it opens it with
SQLite's `mode=ro` URI flag).

Attribution rule (conservative, explained per row):
  * ticker is a Demo fixture ticker (fixtures/scenario_*.json "ticker"), AND
  * no Live catalog evidence exists for that ticker on that scan date
    (no signal_evaluations row -- the catalog is only ever written by a real
    LiveUWAdapter scan).
Rows for fixture tickers that DO have Live catalog evidence are reported as
"ambiguous" (a real Live scan of a symbol that is also a fixture name).

Usage:
    python tools/audit_demo_live_contamination.py --db ./data/tip_hackathon.db [--fixtures ./fixtures] [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys


def fixture_tickers(fixtures_dir: str) -> set:
    out = set()
    for path in glob.glob(os.path.join(fixtures_dir, "scenario_*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                ticker = json.load(fh).get("ticker")
        except (OSError, ValueError):
            continue
        if ticker:
            out.add(str(ticker).upper())
    return out


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def audit(db_path: str, fixtures_dir: str) -> dict:
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    try:
        fixtures = fixture_tickers(fixtures_dir)
        report = {"db": os.path.abspath(db_path), "fixture_tickers": sorted(fixtures),
                  "scan_log": [], "current_state": [], "summary": {}}
        if not _table_exists(conn, "ticker_scan_log"):
            report["summary"] = {"note": "no legacy ticker_scan_log table"}
            return report
        live_dates = set()
        if _table_exists(conn, "signal_evaluations"):
            live_dates = {(t, (ra or "")[:10]) for t, ra in
                          conn.execute("SELECT ticker, retrieved_at FROM signal_evaluations")}
        live_tickers = {t for t, _ in live_dates}
        counts = {"demo_attributed": 0, "ambiguous": 0}
        for rid, ticker, scan_date, any_gate, state, created_at in conn.execute(
                "SELECT id, ticker, scan_date, any_gate_passed, state, created_at FROM ticker_scan_log ORDER BY id"):
            if ticker not in fixtures:
                continue
            ambiguous = (ticker, scan_date) in live_dates or (ticker, (created_at or "")[:10]) in live_dates
            verdict = "ambiguous" if ambiguous else "demo_attributed"
            counts[verdict] += 1
            report["scan_log"].append({"id": rid, "ticker": ticker, "scan_date": scan_date,
                                       "any_gate_passed": bool(any_gate), "state": state,
                                       "created_at": created_at, "verdict": verdict})
        for ticker, state, updated_at in conn.execute(
                "SELECT ticker, state, updated_at FROM ticker_current_state ORDER BY ticker"):
            if ticker in fixtures:
                report["current_state"].append({
                    "ticker": ticker, "state": state, "updated_at": updated_at,
                    "verdict": "ambiguous" if ticker in live_tickers else "demo_attributed"})
        report["summary"] = {
            "scan_log_rows_demo_attributed": counts["demo_attributed"],
            "scan_log_rows_ambiguous": counts["ambiguous"],
            "current_state_rows_for_fixture_tickers": len(report["current_state"]),
            "recommendation": (
                "No action is required for correctness: Demo activity is written to a separate "
                "database (TIP_DEMO_DB_PATH) and lifecycle decisions no longer read these legacy tables "
                "(episode tables, migration 7). The rows are left in place as audit history; deleting "
                "them is optional and must be an explicit operator decision -- back up the DB first."),
        }
        return report
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--fixtures", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = audit(args.db, args.fixtures)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"database: {report['db']}")
    for k, v in report["summary"].items():
        print(f"  {k}: {v}")
    for row in report["current_state"]:
        print(f"  current_state  {row['ticker']:6s} {row['state']:11s} {row['updated_at']}  [{row['verdict']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
