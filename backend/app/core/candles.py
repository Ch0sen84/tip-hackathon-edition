"""OHLC bar normalization + evidence-marker construction.

Ported BY VALUE (not imported) from the production app's
`webapp/backend.py::_build_candles` (~line 621) -- that file is a
read-only reference and is never a runtime dependency of this repo. The
three real UW `stock_ohlc` gotchas documented there and re-verified in
Phase-A live checks are reproduced here:

  1. UW returns bars newest-first -- must be re-sorted chronological
     before any charting library can use them.
  2. UW occasionally returns a negative `volume` value (a real, rare
     upstream artifact). It is clamped to 0 for rendering, but the raw
     negative value is preserved on `v_raw` rather than silently
     dropped -- callers can tell this happened via `neg_vol_count`.
  3. `1d` bars need de-duplication -- UW sometimes returns the same date
     more than once (last-write-wins per date is good enough here; there
     is no reason to believe one duplicate row is more authoritative than
     another).

A fourth gotcha (hard 422 above limit=2500) is handled by `clamp_limit`,
called by the adapter before it ever makes the request.
"""
from __future__ import annotations

MAX_UW_OHLC_LIMIT = 2500


def clamp_limit(limit: int) -> int:
    """UW's own OHLC endpoint 422s at limit>=2500 (verified live). Clamp
    defensively so a caller-supplied limit can never trigger that."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 180
    if limit <= 0:
        return 180
    return min(limit, MAX_UW_OHLC_LIMIT - 1)


def _dedupe_daily_bars(raw_data: list) -> list:
    by_date = {}
    for d in raw_data:
        key = d.get("date") or d.get("start_time")
        by_date[key] = d  # last-write-wins; UW gives no ordering guarantee among duplicates
    return list(by_date.values())


def build_candles(raw_data: list, dedupe_daily: bool = False) -> tuple[list, int]:
    """raw_data: list of UW stock_ohlc rows (daily or intraday shape).
    Returns (candles, neg_vol_count). Each candle:
        {"t": <ISO timestamp or date str>, "o","h","l","c": float, "v": int}
        plus "v_raw": int only when the real volume was negative.
    Never fabricates a bar: a row missing a usable timestamp is skipped,
    not synthesized.
    """
    if dedupe_daily:
        raw_data = _dedupe_daily_bars(raw_data)
    candles = []
    neg_vol_count = 0
    for d in raw_data:
        ts = d.get("start_time") or d.get("date")
        if ts is None:
            continue
        try:
            o, h, l, c = float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"])
        except (KeyError, TypeError, ValueError):
            continue
        v = int(d.get("volume") or 0)
        bar = {"t": ts, "o": o, "h": h, "l": l, "c": c, "v": v}
        if v < 0:
            neg_vol_count += 1
            bar["v"] = 0
            bar["v_raw"] = v
        candles.append(bar)
    candles.sort(key=lambda b: b["t"])  # UW returns newest-first; render chronological
    return candles, neg_vol_count


def build_markers(flow_alerts: list, darkpool_trades: list) -> list:
    """Evidence markers from the SAME per-event data the detection engine
    already consumes -- never a new data source, never an invented price.

    - darkpool_trades: real `executed_at` + real `price` -> a priced
      point marker.
    - flow_alerts: real `created_at`; price coordinate uses the alert's
      own `underlying_price` field when present (the real recorded
      underlying price AT the alert, not a derived/estimated value) --
      when absent, the marker is time-only (price omitted, never
      invented) so the frontend must render it as a vertical/time
      marker rather than placed at a fabricated height.
    """
    markers = []
    for t in darkpool_trades:
        ts = t.get("executed_at")
        price = t.get("price")
        if not ts:
            continue
        markers.append({
            "kind": "darkpool_trade",
            "time": ts,
            "price": float(price) if price is not None else None,
            "label": "Dark pool print",
        })
    for a in flow_alerts:
        ts = a.get("created_at")
        if not ts:
            continue
        price = a.get("underlying_price")
        side = "call" if a.get("is_call") else ("put" if a.get("is_put") else None)
        markers.append({
            "kind": "flow_alert",
            "time": ts,
            "price": float(price) if price is not None else None,
            "label": f"Flow alert{f' ({side})' if side else ''}",
        })
    markers.sort(key=lambda m: m["time"])
    return markers
