"""Regression tests for the 2026-10-15 real-NVDA-investigation evidence-
quality pass. ALL fixtures here are CONSTRUCTED (hand-built dicts shaped
like real UW/MCP records, same pattern as test_investigate.py's
_realistic_flow_alerts) -- no real UW MCP call and no real/paid Anthropic
call is ever made in this file. Covers:

  1. direction-accuracy: mixed ask/bid-side evidence must not be collapsed
     into a uniform "buying"/"selling" claim (app/ai/validation.py +
     schemas.py).
  2. factual-claim support: a quantified claim ("several", "heavy") must be
     backed by enough actual observed_facts records.
  3. representative sampling: sweep-vs-non-sweep representation in the
     bounded sample given to the LLM (app/ai/llm_provider.py).
  4. token budget: nested/deeply-structured and oversized tool-result
     payloads get bounded before reaching the prompt.
  5. missing fields / malformed records degrade gracefully, never crash.
  6. refusal/error behavior: a failed tool call and a malformed provider
     response are both handled honestly (tool_outcomes, MalformedAIResponse).
"""
from __future__ import annotations

from app.ai.llm_provider import (
    MAX_NESTED_DEPTH,
    MAX_RECORDS_PER_TOOL_IN_PROMPT,
    MAX_TOTAL_SYNTHESIS_PROMPT_CHARS,
    SWEEP_FIELD_BY_TOOL,
    _bound_nested_value,
    _diverse_sample_records,
    build_synthesis_user_prompt,
    _summarize_tool_result_for_prompt,
)
from app.ai.schemas import MalformedAIResponse, validate_investigation_result
from app.ai.validation import (
    classify_flow_alert_side,
    contains_unidirectional_claim_without_mixed_disclosure,
    contains_unsupported_quantified_claim,
    summarize_execution_side_mix,
)


# ---------------------------------------------------------------------------
# Item #1: execution-side (ask/bid) direction accuracy
# ---------------------------------------------------------------------------

def test_classify_flow_alert_side_ask_bid_unknown():
    assert classify_flow_alert_side({"total_ask_side_prem": "1900000", "total_bid_side_prem": "100000"}) == "ask"
    assert classify_flow_alert_side({"total_ask_side_prem": "100000", "total_bid_side_prem": "900000"}) == "bid"
    assert classify_flow_alert_side({"total_ask_side_prem": "0", "total_bid_side_prem": "0"}) == "unknown"
    assert classify_flow_alert_side({}) == "unknown"
    assert classify_flow_alert_side("not a dict") == "unknown"
    # REST-normalized field-name fallback (live_uw_adapter.py's naming)
    assert classify_flow_alert_side({"total_ask_premium": "500", "total_bid_premium": "10"}) == "ask"


def test_summarize_execution_side_mix_counts_only_ok_flow_alerts():
    tool_results = [
        {
            "tool": "get_flow_alerts",
            "ok": True,
            "data": [
                {"total_ask_side_prem": "1900000", "total_bid_side_prem": "100000"},  # ask
                {"total_ask_side_prem": "100000", "total_bid_side_prem": "900000"},  # bid
                {"total_ask_side_prem": "200000", "total_bid_side_prem": "900000"},  # bid
            ],
        },
        {"tool": "get_dark_pool_trades", "ok": True, "data": [{"premium": "500"}]},  # no ask/bid field, ignored
        {"tool": "get_flow_alerts", "ok": False, "data": None, "error": "boom"},  # failed call, ignored
    ]
    mix = summarize_execution_side_mix(tool_results)
    assert mix == {"ask": 1, "bid": 2, "unknown": 0}


def test_unidirectional_claim_flagged_when_evidence_is_genuinely_mixed():
    """The exact real NVDA bug: text says 'put buying' but only 1 of 3
    cited alerts was ask-side, the other 2 bid-side."""
    mix = {"ask": 1, "bid": 2, "unknown": 0}
    assert contains_unidirectional_claim_without_mixed_disclosure(
        "This looks like large long-dated put buying across several strikes.", mix
    )


def test_unidirectional_claim_not_flagged_when_text_discloses_the_mix():
    mix = {"ask": 1, "bid": 2, "unknown": 0}
    assert not contains_unidirectional_claim_without_mixed_disclosure(
        "Mixed ask/bid activity on puts (1 ask-side, 2 bid-side) -- no uniform directional read.", mix
    )


def test_unidirectional_claim_not_flagged_when_data_is_genuinely_one_sided():
    mix = {"ask": 3, "bid": 0, "unknown": 0}
    assert not contains_unidirectional_claim_without_mixed_disclosure(
        "This reflects clear call-side buying pressure.", mix
    )


def test_unidirectional_claim_not_flagged_with_no_directional_word_at_all():
    mix = {"ask": 1, "bid": 1, "unknown": 0}
    assert not contains_unidirectional_claim_without_mixed_disclosure(
        "Flow alerts were observed across two strikes.", mix
    )


def test_validate_investigation_result_surfaces_direction_warning_end_to_end():
    """Full round-trip through validate_investigation_result with the
    actual tool_results that produced the real bug shape."""
    tool_results = [
        {
            "tool": "get_flow_alerts",
            "ok": True,
            "data": [
                {"id": "a1", "type": "put", "total_ask_side_prem": "1800000", "total_bid_side_prem": "100000"},
                {"id": "a2", "type": "put", "total_ask_side_prem": "50000", "total_bid_side_prem": "1200000"},
                {"id": "a3", "type": "put", "total_ask_side_prem": "40000", "total_bid_side_prem": "900000"},
            ],
        },
    ]
    raw = {
        "observed_facts": [
            {"fact": "Three large put alerts cited across strikes.", "source_tool": "get_flow_alerts"},
        ],
        "ai_interpretation": "This looks like large long-dated put buying.",
    }
    result = validate_investigation_result(raw, "NVDA", called_tools=["get_flow_alerts"], tool_results=tool_results)
    assert any("Unsupported directional claim" in w for w in result.evidence_integrity_warnings)


def test_validate_investigation_result_no_warning_when_claim_is_accurate():
    tool_results = [
        {
            "tool": "get_flow_alerts",
            "ok": True,
            "data": [
                {"id": "a1", "type": "call", "total_ask_side_prem": "1800000", "total_bid_side_prem": "10000"},
                {"id": "a2", "type": "call", "total_ask_side_prem": "900000", "total_bid_side_prem": "20000"},
            ],
        },
    ]
    raw = {
        "observed_facts": [
            {"fact": "Two large ask-side call alerts.", "source_tool": "get_flow_alerts"},
        ],
        "ai_interpretation": "This reflects clear call buying pressure, both ask-side.",
    }
    result = validate_investigation_result(raw, "NVDA", called_tools=["get_flow_alerts"], tool_results=tool_results)
    assert result.evidence_integrity_warnings == []


# ---------------------------------------------------------------------------
# Item #2: factual-claim (quantified) support
# ---------------------------------------------------------------------------

def test_unsupported_quantified_claim_flagged_with_too_few_records():
    assert contains_unsupported_quantified_claim("Heavy near-dated put prints at several strikes.", accepted_fact_count=1)
    assert contains_unsupported_quantified_claim("Numerous sweeps were observed.", accepted_fact_count=0)


def test_quantified_claim_not_flagged_with_enough_records():
    assert not contains_unsupported_quantified_claim("Heavy near-dated put prints at several strikes.", accepted_fact_count=2)


def test_quantified_claim_not_flagged_without_a_quantifier_word():
    assert not contains_unsupported_quantified_claim("A single large put print was observed.", accepted_fact_count=1)


def test_validate_investigation_result_flags_overclaiming_interpretation():
    raw = {
        "observed_facts": [{"fact": "One large put alert.", "source_tool": "get_flow_alerts"}],
        "ai_interpretation": "Heavy near-dated put prints at several strikes suggest broad put positioning.",
    }
    result = validate_investigation_result(raw, "NVDA", called_tools=["get_flow_alerts"], tool_results=[])
    assert any("Unsupported quantified claim" in w for w in result.evidence_integrity_warnings)


# ---------------------------------------------------------------------------
# Item #3: representative sampling -- sweep vs non-sweep representation
# ---------------------------------------------------------------------------

def _flow_alerts_all_nonsweep_with_one_buried_sweep(n=60, sweep_at=41):
    records = []
    for i in range(n):
        records.append({
            "id": f"alert-{i}",
            "type": "call",
            "total_premium": str(50_000 + i * 10),  # no magnitude outlier -- isolates the sweep axis
            "has_sweep": (i == sweep_at),
        })
    return records


def test_diverse_sample_preserves_a_buried_sweep_record_the_real_bug():
    """THE real bug: 8 sampled flow alerts were ALL non-sweep even though
    the larger response had sweep activity. The sweep record here is
    buried (not newest, not highest-magnitude, not minority-direction --
    everything is 'call') so only the sweep-representation step can find it."""
    data = _flow_alerts_all_nonsweep_with_one_buried_sweep()
    sampled = _diverse_sample_records("get_flow_alerts", data)
    assert len(sampled) == MAX_RECORDS_PER_TOOL_IN_PROMPT
    ids = {r["id"] for r in sampled}
    assert "alert-41" in ids, "the one real has_sweep=true record was dropped from the bounded sample"


def test_diverse_sample_never_fabricates_sweep_when_none_exists():
    data = [{"id": f"alert-{i}", "type": "call", "total_premium": str(i), "has_sweep": False} for i in range(30)]
    sampled = _diverse_sample_records("get_flow_alerts", data)
    assert all(not r.get("has_sweep") for r in sampled)


def test_diverse_sample_still_includes_newest_records_alongside_sweep_preservation():
    data = _flow_alerts_all_nonsweep_with_one_buried_sweep()
    sampled = _diverse_sample_records("get_flow_alerts", data)
    ids = {r["id"] for r in sampled}
    for i in range(3):
        assert f"alert-{i}" in ids


def test_summarize_tool_result_discloses_sweep_representation_in_strategy_text():
    data = _flow_alerts_all_nonsweep_with_one_buried_sweep()
    result = {"tool": "get_flow_alerts", "ok": True, "data": data}
    summarized = _summarize_tool_result_for_prompt(result)
    assert "sweep-flagged" in summarized["_truncation_note"]


def test_sweep_field_is_only_defined_for_tools_with_a_real_sweep_flag():
    assert SWEEP_FIELD_BY_TOOL == {"get_flow_alerts": "has_sweep"}


# ---------------------------------------------------------------------------
# Item #4: token budget -- nested/deeply-structured and oversized payloads
# ---------------------------------------------------------------------------

def test_bound_nested_value_truncates_long_strings():
    long_str = "x" * 1000
    bounded = _bound_nested_value(long_str)
    assert len(bounded) < len(long_str)
    assert bounded.endswith("...[truncated]")


def test_bound_nested_value_caps_deep_nesting():
    # Build a dict nested far deeper than MAX_NESTED_DEPTH.
    deep = {"v": 1}
    for _ in range(MAX_NESTED_DEPTH + 5):
        deep = {"nested": deep}
    bounded = _bound_nested_value(deep)
    # Must not raise, and must not recurse all the way down -- the
    # deepest preserved level is a placeholder string, not another dict.
    cur = bounded
    depths = 0
    while isinstance(cur, dict) and "nested" in cur:
        cur = cur["nested"]
        depths += 1
    assert depths <= MAX_NESTED_DEPTH
    assert isinstance(cur, str) and "omitted beyond nesting depth" in cur


def test_bound_nested_value_caps_long_lists():
    bounded = _bound_nested_value(list(range(500)))
    assert len(bounded) < 500
    assert any("more items omitted" in str(x) for x in bounded)


def test_summarize_tool_result_bounds_a_small_but_deeply_nested_record():
    """ROOT CAUSE of item #4: record-count capping alone doesn't bound an
    individual record's SIZE -- a response with only 2 records (well under
    MAX_RECORDS_PER_TOOL_IN_PROMPT) but one deeply-nested sub-object must
    still get bounded."""
    nested_blob = {"level1": {"level2": {"level3": {"level4": {"level5": "deeply buried value"}}}}}
    small = {
        "tool": "get_company_info",
        "ok": True,
        "data": [
            {"id": 1, "detail": nested_blob},
            {"id": 2, "detail": {"x": 1}},
        ],
    }
    summarized = _summarize_tool_result_for_prompt(small)
    assert summarized.get("_nested_content_bounded") is True
    # the deeply nested record no longer contains the raw 5-level structure
    assert "deeply buried value" not in str(summarized["data"])


def test_summarize_tool_result_bounds_a_dict_shaped_result_too():
    """get_company_info/get_max_pain etc. return a dict, not a list --
    nested bounding must apply there too, not just to list-shaped tools."""
    nested = {"company": {"sector": {"detail": {"notes": "x" * 1000}}}}
    result = {"tool": "get_company_info", "ok": True, "data": nested}
    summarized = _summarize_tool_result_for_prompt(result)
    assert summarized.get("_nested_content_bounded") is True


def test_build_synthesis_user_prompt_has_a_real_total_character_backstop():
    """Even after per-record bounding, a pathological combination (many
    tools, each near its own bound) must never silently exceed the total
    prompt character budget -- construct a case engineered to still be
    large after bounding (many records, each with content right at the
    per-record string cap) and confirm the final backstop still holds."""
    big_tool_results = []
    for t in ["get_flow_alerts", "get_dark_pool_trades", "get_option_trades", "get_insider_activity_by_ticker"]:
        data = [{"id": f"{t}-{i}", "note": "y" * 290} for i in range(MAX_RECORDS_PER_TOOL_IN_PROMPT)]
        big_tool_results.append({"tool": t, "ok": True, "data": data})
    prompt = build_synthesis_user_prompt("NVDA", big_tool_results)
    assert len(prompt) <= MAX_TOTAL_SYNTHESIS_PROMPT_CHARS + 500  # + small fixed overhead for ticker/tools-list text


# ---------------------------------------------------------------------------
# Item #6: missing fields / malformed records degrade gracefully
# ---------------------------------------------------------------------------

def test_classify_flow_alert_side_handles_non_numeric_fields_without_crashing():
    assert classify_flow_alert_side({"total_ask_side_prem": "not-a-number", "total_bid_side_prem": "also-bad"}) == "unknown"
    assert classify_flow_alert_side({"total_ask_side_prem": None, "total_bid_side_prem": None}) == "unknown"


def test_diverse_sample_handles_records_missing_the_sweep_field_entirely():
    data = [{"id": f"alert-{i}", "type": "call", "total_premium": str(i)} for i in range(20)]  # no has_sweep key at all
    sampled = _diverse_sample_records("get_flow_alerts", data)
    assert len(sampled) == MAX_RECORDS_PER_TOOL_IN_PROMPT  # no crash, falls through sweep step as a no-op


def test_summarize_execution_side_mix_handles_malformed_tool_results_list():
    assert summarize_execution_side_mix(None) == {"ask": 0, "bid": 0, "unknown": 0}
    assert summarize_execution_side_mix("not a list") == {"ask": 0, "bid": 0, "unknown": 0}
    assert summarize_execution_side_mix([{"tool": "get_flow_alerts", "ok": True, "data": "not a list"}]) == {
        "ask": 0, "bid": 0, "unknown": 0,
    }


# ---------------------------------------------------------------------------
# Item #6: refusal/error behavior -- a failed tool call, a malformed response
# ---------------------------------------------------------------------------

def test_tool_outcomes_disclose_per_tool_success_and_failure():
    tool_results = [
        {"tool": "get_flow_alerts", "ok": True, "data": [], "error": None},
        {"tool": "get_dark_pool_trades", "ok": False, "data": None, "error": "UW API 503"},
    ]
    raw = {"ai_interpretation": "Limited data this run."}
    result = validate_investigation_result(raw, "NVDA", called_tools=["get_flow_alerts", "get_dark_pool_trades"], tool_results=tool_results)
    assert result.tool_outcomes == [
        {"tool": "get_flow_alerts", "ok": True, "error": None},
        {"tool": "get_dark_pool_trades", "ok": False, "error": "UW API 503"},
    ]


def test_tool_outcomes_empty_when_no_tool_results_provided_never_crashes():
    raw = {"ai_interpretation": "interpretation"}
    result = validate_investigation_result(raw, "NVDA", called_tools=[])
    assert result.tool_outcomes == []


def test_malformed_provider_response_without_ai_interpretation_still_raises_cleanly():
    """A malformed provider response (e.g. missing the required field
    entirely) must still raise MalformedAIResponse even with real
    tool_results passed through -- the new tool_results param must not
    change this existing guardrail's behavior."""
    tool_results = [{"tool": "get_flow_alerts", "ok": True, "data": []}]
    try:
        validate_investigation_result({"observed_facts": []}, "NVDA", called_tools=["get_flow_alerts"], tool_results=tool_results)
        assert False, "expected MalformedAIResponse"
    except MalformedAIResponse:
        pass
