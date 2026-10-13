"""Output schema for the synthesis (round 2) LLM call, and the validator
that enforces fact/interpretation separation. A malformed or boundary-
violating LLM response is never passed through to the user untouched --
see `validate_investigation_result` below.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from .validation import contains_forbidden_prediction_language


class MalformedAIResponse(ValueError):
    """Raised when the LLM's round-2 output cannot be trusted as-is (e.g.
    missing the required ai_interpretation field, or not valid JSON at
    all). Callers must surface this as an error, never silently patch it
    into a fake success."""


class ObservedFact(BaseModel):
    fact: str
    source_tool: str  # must be one of the tools actually called this investigation


class InvestigationResult(BaseModel):
    ticker: str
    tools_used: list[str] = Field(default_factory=list)
    observed_facts: list[ObservedFact] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    conflicting_observations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    ai_interpretation: str
    rejected_observed_facts: list[dict] = Field(default_factory=list)
    rejected_tool_selections: list[dict] = Field(default_factory=list)
    simulated: bool = False
    simulated_label: Optional[str] = None


def validate_investigation_result(raw: dict, ticker: str, called_tools: list[str]) -> InvestigationResult:
    """Validate the LLM's round-2 synthesis output against the required
    shape, then apply the fact/interpretation separation guardrails:

    1. `ai_interpretation` must be present and a non-empty string -- a
       response missing it is rejected outright (raises MalformedAIResponse),
       never silently defaulted to an empty interpretation.
    2. Every observed_facts[i].source_tool must name a tool that was
       actually called this investigation; facts citing a tool that was
       never called are dropped (not trusted) and logged into
       rejected_observed_facts.
    3. Any observed_facts entry whose text matches a forbidden
       prediction/recommendation pattern is dropped from observed_facts and
       logged into rejected_observed_facts -- a "fact" is not allowed to
       double as a forecast.
    """
    if not isinstance(raw, dict):
        raise MalformedAIResponse(f"synthesis round did not return a JSON object (got {type(raw).__name__})")

    ai_interpretation = raw.get("ai_interpretation")
    if not isinstance(ai_interpretation, str) or not ai_interpretation.strip():
        raise MalformedAIResponse("synthesis response is missing a non-empty 'ai_interpretation' field")

    raw_facts = raw.get("observed_facts")
    if raw_facts is None:
        raw_facts = []
    if not isinstance(raw_facts, list):
        raise MalformedAIResponse("'observed_facts' must be a list")

    accepted_facts: list[ObservedFact] = []
    rejected_facts: list[dict] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            rejected_facts.append({"raw": item, "reason": "not an object"})
            continue
        fact_text = item.get("fact")
        source_tool = item.get("source_tool")
        if not isinstance(fact_text, str) or not fact_text.strip():
            rejected_facts.append({"raw": item, "reason": "missing fact text"})
            continue
        if source_tool not in called_tools:
            rejected_facts.append({"raw": item, "reason": f"source_tool '{source_tool}' was not actually called"})
            continue
        if contains_forbidden_prediction_language(fact_text):
            rejected_facts.append({"raw": item, "reason": "reads as a prediction/recommendation, not an observed fact"})
            continue
        accepted_facts.append(ObservedFact(fact=fact_text, source_tool=source_tool))

    def _clean_str_list(key: str) -> list[str]:
        val = raw.get(key) or []
        if not isinstance(val, list):
            return []
        return [v for v in val if isinstance(v, str) and v.strip()]

    try:
        return InvestigationResult(
            ticker=ticker,
            tools_used=list(called_tools),
            observed_facts=accepted_facts,
            supporting_evidence=_clean_str_list("supporting_evidence"),
            conflicting_observations=_clean_str_list("conflicting_observations"),
            limitations=_clean_str_list("limitations"),
            ai_interpretation=ai_interpretation.strip(),
            rejected_observed_facts=rejected_facts,
        )
    except PydanticValidationError as e:
        raise MalformedAIResponse(f"synthesis response failed schema validation: {e}") from e
