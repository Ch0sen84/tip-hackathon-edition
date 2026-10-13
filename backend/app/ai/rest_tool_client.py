"""Executes one validated, allowlisted tool call via a plain REST call to
the already-working Unusual Whales REST API (same host/auth style as
backend/app/adapters/live_uw_adapter.UWRestClient) -- NOT the MCP protocol.

See README.md "Investigate with AI -> transport decision" and
allowlist.py's module docstring for why this is a REST fallback rather than
a real `mcp` client talking to https://api.unusualwhales.com/api/mcp.

Each function here takes the already-sanitized (ticker, limit) args
produced by validation.validate_tool_selection -- never raw LLM text -- and
returns a small dict: {"tool": <name>, "ok": bool, "data": <uw json or
None>, "error": <str or None>}. Never raises on a UW-side error (the
underlying UWRestClient.get() is itself crash-proof); a 401/403/network
failure just becomes ok=False with a message, so one bad tool call can't
take down the whole investigation.
"""
from __future__ import annotations

from typing import Any, Callable

from ..adapters.live_uw_adapter import ERROR, UNAUTHORIZED, UNAVAILABLE, UWRestClient
from .allowlist import ALLOWED_TOOL_NAMES

_FAILURE_STATUSES = (ERROR, UNAUTHORIZED, UNAVAILABLE)


def _flow_alerts(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.flow_alerts(ticker=ticker, limit=limit)


def _dark_pool_trades(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.darkpool_ticker(ticker, limit=limit)


def _option_trades(client: UWRestClient, ticker: str, limit: int) -> dict:
    # UWRestClient has no separate raw "option trades" endpoint wired up;
    # flow-alerts (flagged sweeps/large-premium option trades) is the
    # closest already-working equivalent. Documented, not a silent alias.
    return client.flow_alerts(ticker=ticker, limit=limit)


def _greek_exposure_by_ticker(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.greek_exposure_strike(ticker)


def _insider_activity_by_ticker(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.insider_transactions(ticker=ticker, limit=limit)


def _institutional_ownership_by_ticker(client: UWRestClient, ticker: str, limit: int) -> dict:
    # No existing wrapper method on UWRestClient for this; best-effort path
    # per UW's public REST docs, going through the same crash-proof .get().
    return client.get(f"/api/institution/{ticker}/ownership", {"limit": limit})


def _analyst_ratings(client: UWRestClient, ticker: str, limit: int) -> dict:
    # Best-effort path; not confirmed present on every UW tier. A 403/404
    # surfaces as a normal structured failure below, not a crash.
    return client.get(f"/api/stock/{ticker}/analyst-ratings", {"limit": limit})


def _short_data_by_ticker(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.get(f"/api/shorts/{ticker}/data")


def _company_info(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.stock_info(ticker)


def _max_pain(client: UWRestClient, ticker: str, limit: int) -> dict:
    return client.get(f"/api/stock/{ticker}/max-pain")


TOOL_REST_MAP: dict[str, Callable[[UWRestClient, str, int], dict]] = {
    "get_flow_alerts": _flow_alerts,
    "get_dark_pool_trades": _dark_pool_trades,
    "get_option_trades": _option_trades,
    "get_greek_exposure_by_ticker": _greek_exposure_by_ticker,
    "get_insider_activity_by_ticker": _insider_activity_by_ticker,
    "get_institutional_ownership_by_ticker": _institutional_ownership_by_ticker,
    "get_analyst_ratings": _analyst_ratings,
    "get_short_data_by_ticker": _short_data_by_ticker,
    "get_company_info": _company_info,
    "get_max_pain": _max_pain,
}

assert set(TOOL_REST_MAP.keys()) == set(ALLOWED_TOOL_NAMES), "TOOL_REST_MAP must cover exactly the allowlist"


def execute_tool(tool: str, ticker: str, args: dict, client: UWRestClient) -> dict:
    """`tool` and `args` MUST already be the sanitized output of
    validation.validate_tool_selection -- this function trusts its caller
    on that, so it is never exposed directly to raw LLM text."""
    if tool not in TOOL_REST_MAP:
        # Defensive only; validate_tool_selection should have already
        # filtered this out. Never execute an unknown tool.
        return {"tool": tool, "ok": False, "data": None, "error": "tool not on allowlist"}

    limit = int(args.get("limit", 20))
    try:
        body: Any = TOOL_REST_MAP[tool](client, ticker, limit)
    except Exception as e:  # pragma: no cover - defensive, UWRestClient.get() doesn't normally raise
        return {"tool": tool, "ok": False, "data": None, "error": f"{type(e).__name__}: {e}"}

    if isinstance(body, dict) and body.get("status") in _FAILURE_STATUSES:
        return {"tool": tool, "ok": False, "data": None, "error": body.get("message") or body.get("status")}

    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    return {"tool": tool, "ok": True, "data": data, "error": None}
