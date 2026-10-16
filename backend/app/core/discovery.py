"""Shared Market Discovery screener logic, used by BOTH the existing
`POST /api/radar/discover` route (app/main.py) and the Phase D
background scheduler (app/scheduler.py). Moved here, verbatim, from
main.py this phase specifically so the scheduler calls through the
EXACT SAME normalize/rank code the interactive route already uses --
never a second/duplicate discovery pipeline (Chad's explicit item 6).

Nothing here makes a network call -- `run_discovery` takes an already-
constructed adapter (from app.main.get_adapter(), same as every other
caller) and an already-decided filters dict; it is pure orchestration +
normalization over whatever `adapter.discover_candidates()` returns.
"""
from __future__ import annotations

from typing import Optional


def _coerce_number(v):
    """UW's live screener sometimes returns numeric fields as strings
    (confirmed from Chad's real Live Mode test) -- never trust a raw
    value's Python type. Returns a float, or None when the value is
    missing/unparseable (never a guessed 0)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def normalize_discovery_candidate(raw: dict) -> dict:
    """Normalizes ONE raw /api/screener/stocks row into the shape this
    app's ranking/frontend rely on. See the original (pre-Phase-D)
    main.py docstring history for the two real UW field-name bugs this
    fixes (one_day_perc vs change; string-typed numeric fields)."""
    out = dict(raw)
    out["ticker"] = raw.get("ticker") or raw.get("symbol")
    out["net_premium"] = _coerce_number(raw.get("net_premium"))
    out["call_volume"] = _coerce_number(raw.get("call_volume"))
    out["put_volume"] = _coerce_number(raw.get("put_volume"))
    out["iv_rank"] = _coerce_number(raw.get("iv_rank"))
    out["repeated_hits"] = _coerce_number(raw.get("repeated_hits"))
    change_pct = raw.get("one_day_perc")
    if change_pct is None:
        change_pct = raw.get("change")  # defensive fallback only -- one_day_perc is the real field
    out["change_percent"] = _coerce_number(change_pct)
    out["is_index"] = bool(raw.get("is_index")) if raw.get("is_index") is not None else False
    out["source_date"] = raw.get("date")
    return out


def rank_discovery_candidates(candidates: list) -> list:
    """Rank by |net_premium| magnitude primarily, repeated_hits as a
    tiebreaker. Never alters the raw candidate dicts; only reorders."""
    def _key(c: dict):
        return (abs(float(c.get("net_premium") or 0)), float(c.get("repeated_hits") or 0))
    return sorted(candidates, key=_key, reverse=True)


def run_discovery(adapter, filters: Optional[dict] = None) -> dict:
    """The EXACT discovery pipeline both the interactive route and the
    scheduler call: one bounded adapter.discover_candidates() call,
    normalize, rank, annotate discovery_state/evaluate_supported. Returns
    the same shape app.main.radar_discover's response body is built
    from."""
    filters = filters or {}
    result = adapter.discover_candidates(filters)
    normalized = [normalize_discovery_candidate(c) for c in (result.get("candidates") or [])]
    candidates = rank_discovery_candidates(normalized)
    for c in candidates:
        c["discovery_state"] = "candidate"
        c["evaluate_supported"] = not c.get("is_index")
    return {
        "candidates": candidates,
        "count": len(candidates),
        "snapshot_at": result.get("snapshot_at"),
        "screener_requests_used": result.get("screener_requests_used", 1),
        "source": result.get("source"),
        "unavailable": result.get("unavailable", False),
        "reason": result.get("reason"),
        "scan_scope": "market_wide_screener",
        "limit_applied": filters.get("limit"),
        "limit_cap": 50,
    }
