"""OHLC bar normalization + evidence-marker construction.

Ported BY VALUE (not imported) from the production app's
`webapp/backend.py::_build_candles` (~line 621) -- that file is a
read-only reference and is never a runtime dependency of this repo. The
UW `stock_ohlc` gotchas documented there, plus one found live during
Phase-B final verification (2026-10-15), are reproduced here:

  1. UW returns bars newest-first -- must be re-sorted chronological
     before any charting library can use them.
  2. UW occasionally returns a negative `volume` value (a real, rare
     upstream artifact). It is clamped to 0 for rendering, but the raw
     negative value is preserved on `v_raw` rather than silently
     dropped -- callers can tell this happened via `neg_vol_count`.
  3. REAL BUG, confirmed live against UW 2026-10-15 (NOT a UW history-
     depth limitation as first suspected): for `1d` candle_size, UW
     returns up to THREE rows per calendar date -- `market_time` of
     "pr" (pre-market), "r" (regular session), "po" (post-market) --
     each with genuinely different OHLC/volume, not duplicates. The
     earlier dedupe-by-date (last-write-wins, no session awareness)
     silently kept whichever of the 3 happened to be last in UW's
     response array -- incidentally "r" in every sample checked, but
     never guaranteed, and it discarded 2 of every 3 raw rows requested
     (the actual source of the "~1/3 of requested candles" finding from
     Phase B -- UW's real history depth is fine; our dedup was wrong).
     `select_regular_session_bars()` below explicitly selects
     `market_time == "r"` instead of relying on response order.
  4. Volume field semantics (live-verified 2026-10-15, not from public
     docs -- UW's OpenAPI schema does not define a session-level volume
     breakdown): the "r" row's own `volume` field is CUMULATIVE
     pre-market + regular (it closely matches the SAME date's "po" row
     `total_volume`), even though the "r" row's OHLC prices are
     regular-session-only. Showing that cumulative figure next to
     regular-session-only prices would overstate regular-session
     activity. `select_regular_session_bars()` computes a
     regular-session-only volume as a delta (r.volume - that date's pr
     row volume, when a pre-market row exists) so volume stays
     consistent with what the displayed price action represents.

A fifth gotcha (hard 422 above limit=2500) is handled by `clamp_limit`,
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


def raw_limit_for_trading_days(desired_trading_days: int) -> int:
    """How many RAW rows to request from UW to actually get
    `desired_trading_days` unique regular-session daily bars. UW's
    `limit` applies to raw session records (up to 3 per date: pr/r/po),
    not unique dates -- requesting exactly `desired_trading_days` would
    silently under-deliver by ~2/3 (see gotcha #3 above). 4x covers the
    worst case of 3 segments/day plus some headroom for an occasional
    extra/duplicate record, capped at UW's hard request ceiling."""
    try:
        desired_trading_days = int(desired_trading_days)
    except (TypeError, ValueError):
        desired_trading_days = 180
    return clamp_limit(desired_trading_days * 4)


def select_regular_session_bars(raw_data: list) -> list:
    """For daily (`1d`) rows: explicitly select `market_time == "r"`
    (regular session) instead of dedupe-by-date-last-write-wins. Also
    computes a regular-session-only `volume` (see gotcha #4 above) by
    subtracting that date's pre-market row's volume, when one exists,
    so volume stays consistent with the regular-session-only OHLC
    prices being shown. Rows with no `market_time` at all (synthetic
    fixtures; any future UW shape without session tagging; intraday
    rows, which are never passed here) are returned unchanged -- there
    is nothing to select between."""
    has_session_tags = any(d.get("market_time") for d in raw_data)
    if not has_session_tags:
        return list(raw_data)

    pr_volume_by_date: dict = {}
    for d in raw_data:
        if d.get("market_time") == "pr":
            key = d.get("date") or d.get("start_time")
            pr_volume_by_date[key] = d.get("volume")

    selected = []
    for d in raw_data:
        if d.get("market_time") != "r":
            continue  # drop pre-market/post-market rows -- not regular session
        key = d.get("date") or d.get("start_time")
        row = dict(d)
        pr_vol = pr_volume_by_date.get(key)
        if pr_vol is not None:
            try:
                regular_only = int(d.get("volume") or 0) - int(pr_vol or 0)
            except (TypeError, ValueError):
                regular_only = None
            if regular_only is not None and regular_only >= 0:
                row["volume"] = regular_only
        selected.append(row)
    return selected


def _dedupe_daily_bars(raw_data: list) -> list:
    """Safety net for genuinely duplicate rows (same date, same
    market_time) that can occur across paginated/overlapping requests --
    NOT the session-selection job above, which must run first."""
    by_date = {}
    for d in raw_data:
        key = d.get("date") or d.get("start_time")
        by_date[key] = d  # last-write-wins among true duplicates only
    return list(by_date.values())


def build_candles(raw_data: list, dedupe_daily: bool = False) -> tuple[list, int]:
    """raw_data: list of UW stock_ohlc rows (daily or intraday shape),
    ALREADY session-selected by the caller via select_regular_session_bars()
    when dedupe_daily=True (daily charts) -- this function's own dedupe
    step here is only the duplicate-row safety net, not session logic.
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
        premium = t.get("premium")
        markers.append({
            "kind": "darkpool_trade",
            "time": ts,
            "price": float(price) if price is not None else None,
            "label": "Dark pool print",
            # 2026-10-15 (compact evidence interface): real recorded trade
            # premium (price*size, as reported by UW), used for honest
            # day/category aggregate $ totals -- never estimated when
            # absent (None, not 0 or a derived guess).
            "value": float(premium) if premium is not None else None,
        })
    for a in flow_alerts:
        ts = a.get("created_at")
        if not ts:
            continue
        price = a.get("underlying_price")
        side = "call" if a.get("is_call") else ("put" if a.get("is_put") else None)
        premium = a.get("total_premium")
        markers.append({
            "kind": "flow_alert",
            "time": ts,
            "price": float(price) if price is not None else None,
            "label": f"Flow alert{f' ({side})' if side else ''}",
            "value": float(premium) if premium is not None else None,
        })
    markers.sort(key=lambda m: m["time"])
    return markers
