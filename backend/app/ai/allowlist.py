"""Fixed, read-only tool allowlist for the bounded AI investigation feature.

These are the 10 tool names given to us as a verified starting point for
Unusual Whales's official hosted MCP server.

STALE-DOCSTRING FIX (2026-10-14): this file used to claim genuine MCP was
never wired up at all because of a dependency conflict, with execution
always going through rest_tool_client.py's REST mapping instead. That
was true early on, but is NOT current reality and the comment was never
updated when it changed -- this is exactly the kind of stale internal
doc that actively misleads whoever reads it next. As of INVESTIGATE_TRANSPORT
support (mcp_tool_client.py + the isolated mcp_isolated/ venv, see
INSTALL_AGENT.md / README.md), genuine MCP-protocol calls against UW's
hosted server DO run for real when INVESTIGATE_TRANSPORT=mcp -- verified
multiple times against N5. rest_tool_client.py's REST mapping remains in
place ONLY as the default/fallback transport (INVESTIGATE_TRANSPORT=rest
or unset), not as the sole execution path. The tool *names* below are
identical across both transports; only which one executes them differs.

This allowlist is intentionally the ONLY set of tool names the orchestrator
will ever execute, regardless of transport. Never add a write/order/account
tool here.
"""
from __future__ import annotations

# name -> human description shown in prompts / docs. No tool here performs
# a write, order, or account action -- keep it that way.
ALLOWLIST: dict[str, str] = {
    "get_flow_alerts": "Recent flagged options flow (sweeps, large premium) for one ticker.",
    "get_dark_pool_trades": "Recent dark-pool prints for one ticker.",
    "get_option_trades": "Recent raw option trades for one ticker.",
    "get_greek_exposure_by_ticker": "Aggregate option-dealer greek exposure for one ticker.",
    "get_insider_activity_by_ticker": "Recent insider (Form 4) transactions for one ticker.",
    "get_institutional_ownership_by_ticker": "Recent institutional 13F ownership for one ticker.",
    "get_analyst_ratings": "Recent analyst ratings/price-target changes for one ticker.",
    "get_short_data_by_ticker": "Short interest / short volume data for one ticker.",
    "get_company_info": "Basic company/sector/market-cap info for one ticker.",
    "get_max_pain": "Current options max-pain strike for one ticker.",
}

ALLOWED_TOOL_NAMES = frozenset(ALLOWLIST.keys())

MAX_TOOLS_PER_INVESTIGATION = 4
MIN_LIMIT = 1
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
