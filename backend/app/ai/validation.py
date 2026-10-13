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
