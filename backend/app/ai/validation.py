"""Strict validation/sanitization for everything that crosses the
LLM -> tool-execution boundary. Nothing the LLM produces is ever passed to
a UW call, or trusted in a final result, without going through here first.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .allowlist import ALLOWED_TOOL_NAMES, DEFAULT_LIMIT, MAX_LIMIT, MAX_TOOLS_PER_INVESTIGATION, MIN_LIMIT

logger = logging.getLogger(__name__)

# Plain US equity-style ticker: 1-5 letters, optional single-letter share
# class suffix (e.g. BRK.A). Rejects anything with spaces, quotes, path
# separators, SQL/shell metacharacters, etc.
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

# Heuristic guardrail only (not a semantic guarantee): phrases that look
# like a price-direction prediction or a trade recommendation. Used to keep
# an LLM-authored "observed fact" from smuggling in a forecast dressed up
# as an observation. See orchestrator.py / schemas.py for where this is
# applied to the synthesis round's output.
FORBIDDEN_FACT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bwill (rise|fall|rally|drop|surge|crash|break out)\b",
        r"\bis going to\b",
        r"\bexpect(ed)? to (rise|fall|rally|drop)\b",
        r"\b(buy|sell|short|long) (this|it|the stock|the ticker)\b",
        r"\brecommend(ed|s)? (buying|selling|shorting)\b",
        r"\bprice target of\b.*\bby\b",
        r"\bguaranteed\b",
    ]
]


class ValidationError(ValueError):
    pass


def validate_ticker(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValidationError(f"ticker must be a string, got {type(raw).__name__}")
    ticker = raw.strip().upper()
    if not TICKER_RE.match(ticker):
        raise ValidationError(f"'{raw}' is not a valid ticker symbol")
    return ticker


def clamp_limit(raw: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(MIN_LIMIT, min(MAX_LIMIT, value))


def validate_tool_selection(raw: Any, ticker: str) -> tuple[list[dict], list[dict]]:
    """Validate the LLM's round-1 tool-selection output.

    Returns (accepted_calls, rejected_calls). `accepted_calls` is capped at
    MAX_TOOLS_PER_INVESTIGATION entries, each shaped
    {"tool": str, "args": {"ticker": str, "limit": int}}. Anything that
    isn't a well-formed list of {tool, args} dicts, any tool name outside
    the allowlist, and any argument that fails sanitization is dropped into
    `rejected_calls` (with a reason) and logged -- never raised as a crash,
    never passed through unchecked.
    """
    accepted: list[dict] = []
    rejected: list[dict] = []

    if not isinstance(raw, list):
        logger.warning("tool selection round did not return a list: %r", type(raw))
        return accepted, [{"raw": raw, "reason": "not a list"}]

    for item in raw:
        if len(accepted) >= MAX_TOOLS_PER_INVESTIGATION:
            rejected.append({"raw": item, "reason": "exceeds max tool calls per investigation"})
            continue
        if not isinstance(item, dict):
            rejected.append({"raw": item, "reason": "not an object"})
            continue
        tool = item.get("tool")
        if not isinstance(tool, str) or tool not in ALLOWED_TOOL_NAMES:
            rejected.append({"raw": item, "reason": f"tool '{tool}' is not on the read-only allowlist"})
            logger.warning("rejected non-allowlisted tool selection: %r", tool)
            continue
        raw_args: dict = item.get("args") if isinstance(item.get("args"), dict) else {}  # type: ignore[assignment]
        try:
            safe_ticker = validate_ticker(raw_args.get("ticker", ticker))
        except ValidationError as e:
            rejected.append({"raw": item, "reason": str(e)})
            continue
        if safe_ticker != ticker:
            # Never let the LLM redirect a tool call at a different ticker
            # than the one the user actually selected for this investigation.
            rejected.append({"raw": item, "reason": "ticker in args does not match the investigation ticker"})
            continue
        safe_args = {"ticker": safe_ticker, "limit": clamp_limit(raw_args.get("limit"))}
        accepted.append({"tool": tool, "args": safe_args})

    return accepted, rejected


def contains_forbidden_prediction_language(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return any(p.search(text) for p in FORBIDDEN_FACT_PATTERNS)


# ---------------------------------------------------------------------------
# Evidence-integrity checks (added 2026-10-15, real NVDA investigation
# findings #1/#2): a real synthesis response described "large long-dated put
# buying" when only 1 of 3 cited put alerts was actually ask-side (the other
# 2 were bid-side) -- ask-side execution is generally associated with buying
# pressure, bid-side with selling pressure, so collapsing mixed evidence into
# one directional word is a factual misrepresentation, not just loose
# wording. A second real response claimed "heavy near-dated put prints at
# several strikes" without enough actually-cited observed_facts to support
# that specific claim. These functions give schemas.py a way to catch both
# patterns AGAINST THE ACTUAL TOOL-RESULT DATA, extending the same
# drop-and-log pattern contains_forbidden_prediction_language already uses --
# not a parallel system.
# ---------------------------------------------------------------------------

# Words that assert a single, uniform direction of options-flow pressure.
# Finding one of these in model prose is not itself a violation -- it only
# becomes one when the underlying cited tool data was genuinely mixed
# ask/bid-side AND the text never discloses that mix (see MIXED_DISCLOSURE_RE).
DIRECTIONAL_CLAIM_RE = re.compile(r"\b(buying|selling|accumulation|distribution)\b", re.IGNORECASE)
MIXED_DISCLOSURE_RE = re.compile(
    r"\bmixed\b|\bboth ask[- ]side and bid[- ]side\b|\bunclear\b|\bno clear (direction|lean)\b|\bsplit between\b",
    re.IGNORECASE,
)

# Words that imply enough corroborating observations exist to generalize --
# "several strikes", "heavy prints", "numerous sweeps". A claim using one of
# these needs real cited-record backing; see MIN_RECORDS_FOR_QUANTIFIED_CLAIM.
QUANTIFIER_CLAIM_RE = re.compile(r"\b(several|many|numerous|heavy|multiple)\b", re.IGNORECASE)
MIN_RECORDS_FOR_QUANTIFIED_CLAIM = 2


def classify_flow_alert_side(record: Any) -> str:
    """Classify one get_flow_alerts record as ask-side-dominant, bid-side-
    dominant, or unknown, from the real UW field names
    (total_ask_side_prem/total_bid_side_prem -- see live_uw_adapter.py's
    _normalize_flow_alert for the same real-field-name note) or the
    REST-normalized total_ask_premium/total_bid_premium equivalents."""
    if not isinstance(record, dict):
        return "unknown"
    try:
        ask = float(record.get("total_ask_side_prem", record.get("total_ask_premium")) or 0)
        bid = float(record.get("total_bid_side_prem", record.get("total_bid_premium")) or 0)
    except (TypeError, ValueError):
        return "unknown"
    if ask == 0 and bid == 0:
        return "unknown"
    if ask > bid:
        return "ask"
    if bid > ask:
        return "bid"
    return "unknown"


def summarize_execution_side_mix(tool_results: Any) -> dict:
    """Counts ask/bid/unknown across every get_flow_alerts record actually
    returned this investigation (the full data, not the bounded sample fed
    to the LLM -- we want to check the model's claim against reality, not
    against its own narrowed view). Tools with no ask/bid split (dark pool,
    option trades) are not counted here -- they genuinely have no such
    field, confirmed in llm_provider.py's field-mapping notes."""
    counts = {"ask": 0, "bid": 0, "unknown": 0}
    if not isinstance(tool_results, list):
        return counts
    for r in tool_results:
        if not isinstance(r, dict) or r.get("tool") != "get_flow_alerts" or not r.get("ok"):
            continue
        data = r.get("data")
        if not isinstance(data, list):
            continue
        for rec in data:
            counts[classify_flow_alert_side(rec)] += 1
    return counts


def contains_unidirectional_claim_without_mixed_disclosure(text: Any, side_mix: dict) -> bool:
    """True when `text` asserts a uniform buying/selling direction while the
    actual get_flow_alerts data this investigation retrieved was genuinely
    mixed (at least one real ask-side AND one real bid-side record) and the
    text never discloses that mix. Returns False (nothing to flag) whenever
    the data wasn't actually mixed -- this must never fire on a genuinely
    one-sided real dataset."""
    if not isinstance(text, str) or not text.strip():
        return False
    if side_mix.get("ask", 0) < 1 or side_mix.get("bid", 0) < 1:
        return False
    if not DIRECTIONAL_CLAIM_RE.search(text):
        return False
    if MIXED_DISCLOSURE_RE.search(text):
        return False
    return True


def contains_unsupported_quantified_claim(text: Any, accepted_fact_count: int) -> bool:
    """True when `text` uses a corroboration-implying quantifier ("several",
    "heavy", "numerous", "many", "multiple") while fewer than
    MIN_RECORDS_FOR_QUANTIFIED_CLAIM observed_facts actually survived
    validation this investigation. A bounded heuristic, not a semantic
    guarantee -- same caveat as FORBIDDEN_FACT_PATTERNS above."""
    if not isinstance(text, str) or not text.strip():
        return False
    if not QUANTIFIER_CLAIM_RE.search(text):
        return False
    return accepted_fact_count < MIN_RECORDS_FOR_QUANTIFIED_CLAIM
