"""Backfills REAL, computed multi-day scan history for the demo scenarios
that need to demonstrate a genuine multi-day lifecycle (not a single-scan
snapshot relabeled). See fixtures/scenario_persistence_progression.json's
own "description" field for the full rationale -- short version: this repo
has no multi-week Postgres history like production, so a live/demo scan at
process start always starts with current_state=None, which
determine_transition() always maps to NEW regardless of qualifying days.
To actually show a BUILDING -> CONFIRMED transition, something has to put
two genuinely distinct scan_date rows with any_gate_passed=True into the
local SQLite store BEFORE the first live scan happens.

This module does that by running the SAME real
backend/app/core/scan_service.evaluate_ticker_scan function used by every
live scan, twice per ticker, with explicit backdated scan_date values
(relative to date.today() each time it runs -- never a hardcoded absolute
date, so this stays correct no matter how much real time passes) -- never
by writing a hardcoded state string into the store. Idempotent per ticker:
a no-op once that ticker already has a current_state recorded (so it only
seeds once per store lifetime, and never overwrites anything a real
subsequent live scan computes).

SEED_TICKERS (2026-10-15, Phase B final verification, Chad's item #2):
originally PRSX only. VOLX was ADDED after discovering
test_p5_p6_p7.mjs's "VOLX also has genuine multi-entry history" check had
never actually been backed by any seeding mechanism -- it only happened
to pass when run against a long-lived backend that had accumulated real
repeated scans against VOLX over multiple real calendar days by accident.
That's exactly the environment-dependent flakiness Chad asked to be
removed: a genuinely fresh backend never had VOLX history at all. VOLX's
own fixture (scenario_multi_signal_alt.json) was authored for a different
purpose (gate-combination distinctness, not history), but its real gate
outputs happen to pass gates on every backdated scan, so it works fine as
a second, independently-seeded history scenario -- no fixture data was
invented or altered to make this true."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

PERSISTENCE_DEMO_TICKER = "PRSX"
SEED_TICKERS = ("PRSX", "VOLX")


def _seed_one(store, adapter, ticker: str) -> Optional[str]:
    if store.current_state(ticker) is not None:
        return store.current_state(ticker)
    if ticker not in adapter.get_universe():
        return None  # fixture not present (e.g. a trimmed test fixtures dir) -- nothing to seed

    flow_alerts = adapter.get_flow_alerts(ticker)
    darkpool_trades = adapter.get_darkpool_trades(ticker)
    stock_state = adapter.get_stock_state(ticker)
    price_move_pct = None
    if stock_state and not stock_state.get("unavailable"):
        price_move_pct = stock_state.get("change_percent")

    from .core import scan_service
    today = date.today()
    backdated_days = [today - timedelta(days=2), today - timedelta(days=1)]
    current_state = None
    for d in backdated_days:
        qualifying_days = store.qualifying_days_count(ticker)
        scan = scan_service.evaluate_ticker_scan(
            ticker=ticker,
            flow_alerts=flow_alerts,
            darkpool_trades=darkpool_trades,
            current_state=current_state,
            qualifying_days_count=qualifying_days,
            price_move_pct=price_move_pct,
        )
        any_gate_passed = len(scan["independent_gates_passed"]) > 0
        store.record_scan(ticker, any_gate_passed, scan["state"], scan_date=d.isoformat())
        current_state = scan["state"]
    return current_state


def seed_persistence_progression(store, fixtures_dir: str) -> Optional[str]:
    """Seeds every ticker in SEED_TICKERS (PRSX, VOLX). Returns PRSX's
    resulting current_state specifically (kept for backward-compat with
    existing callers/return-value checks) -- call _seed_one() directly if
    you need another ticker's result."""
    from .adapters.synthetic_adapter import SyntheticDataAdapter

    if not Path(fixtures_dir).exists():
        return None
    adapter = SyntheticDataAdapter(fixtures_dir)
    results = {t: _seed_one(store, adapter, t) for t in SEED_TICKERS}
    return results.get(PERSISTENCE_DEMO_TICKER)
