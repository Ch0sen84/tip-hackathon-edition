#!/usr/bin/env python3
"""Generates a coherent synthetic daily-OHLC series for each demo-mode
fixture in fixtures/scenario_*.json and writes it into that fixture under
the "ohlc" key.

This is NOT live UW data and never claims to be (every fixture already
carries "synthetic": true). It IS internally consistent with that
scenario's own flow_alerts/darkpool_trades/stock_state: the series ends
on the date of the fixture's own events, the day-of-event close sits
within the event's known price(s), and the final close equals
stock_state.last_price -- so a reader comparing the chart to the
evidence text never sees a contradiction.

Deterministic (seeded per ticker) so re-running this script on unchanged
input fixtures reproduces byte-identical output -- not a new random
"capture" every run.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
DAYS = 14  # 2 trading weeks of daily bars ending on the event date


def _event_date_and_anchor_prices(scenario: dict) -> tuple[date, list[float]]:
    anchors = []
    event_date = None
    for a in scenario.get("flow_alerts", []):
        if a.get("underlying_price") is not None:
            anchors.append(float(a["underlying_price"]))
        if a.get("created_at"):
            event_date = date.fromisoformat(a["created_at"][:10])
    for t in scenario.get("darkpool_trades", []):
        if t.get("price") is not None:
            anchors.append(float(t["price"]))
        if t.get("executed_at"):
            event_date = date.fromisoformat(t["executed_at"][:10])
    last_price = scenario.get("stock_state", {}).get("last_price")
    if last_price is not None:
        anchors.append(float(last_price))
    if event_date is None:
        event_date = date(2026, 10, 12)
    if not anchors:
        anchors = [100.0]
    return event_date, anchors


def generate_series(scenario: dict) -> list:
    ticker = scenario["ticker"]
    rng = random.Random(f"ohlc-{ticker}")
    event_date, anchors = _event_date_and_anchor_prices(scenario)
    final_close = anchors[-1]
    event_day_price = sum(anchors) / len(anchors)

    # Walk backward DAYS trading days from event_date, starting from a
    # price ~3-6% away from the event-day level and drifting toward it,
    # so the chart visually agrees with "something happened" on the
    # event date rather than being flat noise.
    start_price = event_day_price * (1 - rng.uniform(0.03, 0.06) * rng.choice([1, -1]))
    dates = []
    d = event_date
    while len(dates) < DAYS:
        if d.weekday() < 5:  # skip weekends -- no fabricated weekend trading
            dates.append(d)
        d -= timedelta(days=1)
    dates.reverse()

    bars = []
    n = len(dates)
    for i, day in enumerate(dates):
        # linear drift from start_price toward event_day_price, reaching
        # it on the event day, then settling at final_close on the last bar.
        if day < event_date:
            frac = i / max(1, n - 2)
            base = start_price + (event_day_price - start_price) * frac
        elif day == event_date:
            base = event_day_price
        else:
            base = final_close
        noise = base * rng.uniform(-0.006, 0.006)
        o = round(base + noise, 2)
        is_event_day = day == event_date
        swing = base * (0.018 if is_event_day else 0.008)
        h = round(max(o, base) + abs(rng.uniform(0.2, 1.0)) * swing, 2)
        l = round(min(o, base) - abs(rng.uniform(0.2, 1.0)) * swing, 2)
        c = round(event_day_price if is_event_day and day == dates[len(dates) // 2 + 1] else base + base * rng.uniform(-0.004, 0.004), 2)
        if day == dates[-1]:
            c = round(final_close, 2)
        c = min(max(c, l), h)
        o = min(max(o, l), h)
        vol = int(rng.uniform(800_000, 2_500_000))
        bars.append({
            "date": day.isoformat(),
            "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "market_time": "r", "total_volume": vol,
        })
    return bars


def main():
    changed = []
    for path in sorted(FIXTURES_DIR.glob("scenario_*.json")):
        scenario = json.loads(path.read_text())
        scenario["ohlc"] = generate_series(scenario)
        path.write_text(json.dumps(scenario, indent=2) + "\n")
        changed.append(path.name)
    print(f"Wrote OHLC series into: {', '.join(changed)}")


if __name__ == "__main__":
    sys.exit(main())
