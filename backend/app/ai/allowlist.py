"""Fixed, read-only tool allowlist for the bounded AI investigation feature.

These are the 10 tool names given to us as a verified starting point for
Unusual Whales's official hosted MCP server. We did NOT end up connecting to
that live MCP server at all (see README.md "Investigate with AI" section
for why -- short version: the installed `mcp` Python SDK's dependency
pins conflicted with this project's FastAPI/Starlette version in a way that
either broke the whole app's test suite or required downgrading to an `mcp`
release with published HIGH-severity security advisories; neither was an
acceptable bounded-effort tradeoff). Execution instead goes through
rest_tool_client.py, which maps each of these exact tool names onto a
plain REST call against the already-working UW REST API (same host
LiveUWAdapter already talks to). The tool *names* below are preserved
exactly as given; only the transport underneath changed from MCP to REST.

This allowlist is intentionally the ONLY set of tool names the orchestrator
will ever execute. Never add a write/order/account tool here.
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
