"""Backfills REAL, computed multi-day scan history for the persistence-
progression demo scenario (fixtures/scenario_persistence_progression.json,
ticker PRSX). See that fixture's own "description" field for the full
rationale -- short version: this repo has no multi-week Postgres history
like production, so a live/demo scan at process start always starts with
current_state=None, which determine_transition() always maps to NEW
regardless of qualifying days. To actually show a BUILDING -> CONFIRMED
transition (not a single-scan snapshot relabeled), something has to put
two genuinely distinct scan_date rows with any_gate_passed=True into the
local SQLite store BEFORE the first live scan happens.

This module does that by running the SAME real
backend/app/core/scan_service.evaluate_ticker_scan function used by every
live scan, twice, with explicit backdated scan_date values -- never by
writing a hardcoded state string into the store. Idempotent: a no-op once
PRSX already has a current_state recorded (so it only seeds once per
store lifetime, and never overwrites anything a real subsequent live scan
computes).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

PERSISTENCE_DEMO_TICKER = "PRSX"


def seed_persistence_progression(store, fixtures_dir: str) -> Optional[str]:
    """Returns the resulting current_state after seeding (or the existing
    one if already seeded / nothing to seed), for logging/testing. Safe to
    call on every store creation -- it checks store.current_state() first
    and does nothing once a state is already recorded for this ticker."""
    from .adapters.synthetic_adapter import SyntheticDataAdapter
    from .core import scan_service

    if store.current_state(PERSISTENCE_DEMO_TICKER) is not None:
        return store.current_state(PERSISTENCE_DEMO_TICKER)

    if not Path(fixtures_dir).exists():
        return None
    adapter = SyntheticDataAdapter(fixtures_dir)
    if PERSISTENCE_DEMO_TICKER not in adapter.get_universe():
        return None  # fixture not present (e.g. a trimmed test fixtures dir) -- nothing to seed

    flow_alerts = adapter.get_flow_alerts(PERSISTENCE_DEMO_TICKER)
    darkpool_trades = adapter.get_darkpool_trades(PERSISTENCE_DEMO_TICKER)
    stock_state = adapter.get_stock_state(PERSISTENCE_DEMO_TICKER)
    price_move_pct = None
    if stock_state and not stock_state.get("unavailable"):
        price_move_pct = stock_state.get("change_percent")

    today = date.today()
    backdated_days = [today - timedelta(days=2), today - timedelta(days=1)]
    current_state = None
    for d in backdated_days:
        qualifying_days = store.qualifying_days_count(PERSISTENCE_DEMO_TICKER)
        scan = scan_service.evaluate_ticker_scan(
            ticker=PERSISTENCE_DEMO_TICKER,
            flow_alerts=flow_alerts,
            darkpool_trades=darkpool_trades,
            current_state=current_state,
            qualifying_days_count=qualifying_days,
            price_move_pct=price_move_pct,
        )
        any_gate_passed = len(scan["gates_passed"]) > 0
        store.record_scan(PERSISTENCE_DEMO_TICKER, any_gate_passed, scan["state"],
                           scan_date=d.isoformat())
        current_state = scan["state"]
    return current_state
