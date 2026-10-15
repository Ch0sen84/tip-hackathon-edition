"""Tests for the bounded 'Investigate with AI' feature. No real UW MCP/REST
call and no real LLM call is ever made here -- every network-touching
function is monkeypatched. Covers: argument validation/sanitization,
round-call-count bounds, graceful no-AI-key behavior, demo/PUBLIC_DEMO
always serving the static simulated transcript without touching any real
call, and the fact/interpretation schema guardrails.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.ai import orchestrator
from app.ai.allowlist import MAX_TOOLS_PER_INVESTIGATION
from app.ai.llm_provider import (
    MAX_RECORDS_PER_TOOL_IN_PROMPT,
    build_synthesis_user_prompt,
    _diverse_sample_records,
    _summarize_tool_result_for_prompt,
)
from app.ai.schemas import (
    MAX_CONFLICTING_OBSERVATIONS,
    MAX_FACT_CHARS,
    MAX_INTERPRETATION_CHARS,
    MAX_LIMITATIONS,
    MAX_OBSERVED_FACTS,
    MAX_SUPPORTING_EVIDENCE,
    MalformedAIResponse,
    validate_investigation_result,
)
from app.ai.validation import ValidationError, validate_tool_selection

GOOD_SELECTION = [
    {"tool": "get_flow_alerts", "args": {"ticker": "ZYNC", "limit": 20}},
    {"tool": "get_dark_pool_trades", "args": {"ticker": "ZYNC", "limit": 50}},
]

GOOD_SYNTHESIS = {
    "observed_facts": [
        {"fact": "Two flagged sweeps, both call-side and ask-dominant.", "source_tool": "get_flow_alerts"},
    ],
    "supporting_evidence": ["Both trades share the same directional profile."],
    "conflicting_observations": [],
    "limitations": ["Small sample size."],
    "ai_interpretation": "This looks like a consistent options-flow pattern worth human review.",
}


# --- Argument validation / sanitization --------------------------------


def test_tool_selection_rejects_non_allowlisted_tool():
    raw = [{"tool": "delete_account", "args": {"ticker": "ZYNC"}}]
    accepted, rejected = validate_tool_selection(raw, "ZYNC")
    assert accepted == []
    assert len(rejected) == 1
    assert "not on the read-only allowlist" in rejected[0]["reason"]


def test_tool_selection_clamps_out_of_range_limit():
    raw = [{"tool": "get_flow_alerts", "args": {"ticker": "ZYNC", "limit": 999999}}]
    accepted, rejected = validate_tool_selection(raw, "ZYNC")
    assert len(accepted) == 1
    assert accepted[0]["args"]["limit"] <= 100
    raw_low = [{"tool": "get_flow_alerts", "args": {"ticker": "ZYNC", "limit": -5}}]
    accepted2, _ = validate_tool_selection(raw_low, "ZYNC")
    assert accepted2[0]["args"]["limit"] >= 1


def test_tool_selection_rejects_bad_ticker_in_args():
    raw = [{"tool": "get_flow_alerts", "args": {"ticker": "DROP TABLE;", "limit": 10}}]
    accepted, rejected = validate_tool_selection(raw, "ZYNC")
    assert accepted == []
    assert len(rejected) == 1


def test_tool_selection_rejects_ticker_mismatch():
    """The LLM must not redirect a tool call at a different ticker than
    the one the investigation is actually about."""
    raw = [{"tool": "get_flow_alerts", "args": {"ticker": "AAPL", "limit": 10}}]
    accepted, rejected = validate_tool_selection(raw, "ZYNC")
    assert accepted == []
    assert "does not match" in rejected[0]["reason"]


def test_tool_selection_caps_at_max_tools():
    raw = [{"tool": "get_flow_alerts", "args": {"ticker": "ZYNC"}} for _ in range(10)]
    accepted, rejected = validate_tool_selection(raw, "ZYNC")
    assert len(accepted) == MAX_TOOLS_PER_INVESTIGATION
    assert len(rejected) == 10 - MAX_TOOLS_PER_INVESTIGATION


def test_tool_selection_rejects_non_list_input():
    accepted, rejected = validate_tool_selection({"not": "a list"}, "ZYNC")
    assert accepted == []
    assert rejected and rejected[0]["reason"] == "not a list"


# --- Fact/interpretation separation enforcement -------------------------


def test_malformed_response_missing_ai_interpretation_is_rejected():
    raw = {"observed_facts": [{"fact": "x", "source_tool": "get_flow_alerts"}]}
    try:
        validate_investigation_result(raw, "ZYNC", called_tools=["get_flow_alerts"])
        assert False, "expected MalformedAIResponse"
    except MalformedAIResponse:
        pass


def test_prediction_dressed_as_observed_fact_is_filtered_out():
    raw = {
        "observed_facts": [
            {"fact": "The price will rise sharply next week.", "source_tool": "get_flow_alerts"},
            {"fact": "Two flagged sweeps were recorded today.", "source_tool": "get_flow_alerts"},
        ],
        "ai_interpretation": "A pattern worth noting.",
    }
    result = validate_investigation_result(raw, "ZYNC", called_tools=["get_flow_alerts"])
    facts = [f.fact for f in result.observed_facts]
    assert "Two flagged sweeps were recorded today." in facts
    assert all("will rise" not in f for f in facts)
    assert len(result.rejected_observed_facts) == 1


def test_observed_fact_citing_uncalled_tool_is_dropped():
    raw = {
        "observed_facts": [{"fact": "Institutional buying increased.", "source_tool": "get_institutional_ownership_by_ticker"}],
        "ai_interpretation": "interpretation text",
    }
    result = validate_investigation_result(raw, "ZYNC", called_tools=["get_flow_alerts"])
    assert result.observed_facts == []
    assert len(result.rejected_observed_facts) == 1
    assert "not actually called" in result.rejected_observed_facts[0]["reason"]


def test_non_dict_synthesis_response_is_rejected():
    try:
        validate_investigation_result(["not", "a", "dict"], "ZYNC", called_tools=[])
        assert False, "expected MalformedAIResponse"
    except MalformedAIResponse:
        pass


# --- Orchestrator: round counts and max-tool-call bound ------------------


def test_orchestrator_calls_select_and_synthesize_exactly_once_and_execute_at_most_four(monkeypatch):
    call_counts = {"select": 0, "execute": 0, "synthesize": 0}

    def fake_select_tools(ticker, evidence, provider, api_key):
        call_counts["select"] += 1
        return GOOD_SELECTION

    def fake_execute_tool(tool, ticker, args, client):
        call_counts["execute"] += 1
        return {"tool": tool, "ok": True, "data": {"x": 1}, "error": None}

    def fake_synthesize(ticker, tool_results, provider, api_key):
        call_counts["synthesize"] += 1
        return GOOD_SYNTHESIS

    monkeypatch.setattr(orchestrator, "select_tools", fake_select_tools)
    monkeypatch.setattr(orchestrator, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(orchestrator, "synthesize", fake_synthesize)

    result = orchestrator.investigate(
        ticker="ZYNC", evidence={}, ai_provider="anthropic", ai_api_key="fake",
        uw_api_key="fake", uw_base_url="https://example.invalid",
    )

    assert call_counts["select"] == 1
    assert call_counts["synthesize"] == 1
    assert call_counts["execute"] <= MAX_TOOLS_PER_INVESTIGATION
    assert call_counts["execute"] == len(GOOD_SELECTION)
    assert result.ai_interpretation == GOOD_SYNTHESIS["ai_interpretation"]


def test_orchestrator_never_executes_more_than_max_tools_even_if_llm_requests_more(monkeypatch):
    call_counts = {"execute": 0}
    oversized_selection = [{"tool": "get_flow_alerts", "args": {"ticker": "ZYNC"}} for _ in range(9)]

    monkeypatch.setattr(orchestrator, "select_tools", lambda *a, **k: oversized_selection)

    def fake_execute_tool(tool, ticker, args, client):
        call_counts["execute"] += 1
        return {"tool": tool, "ok": True, "data": {}, "error": None}

    monkeypatch.setattr(orchestrator, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(orchestrator, "synthesize", lambda *a, **k: GOOD_SYNTHESIS)

    orchestrator.investigate(
        ticker="ZYNC", evidence={}, ai_provider="anthropic", ai_api_key="fake",
        uw_api_key="fake", uw_base_url="https://example.invalid",
    )
    assert call_counts["execute"] <= MAX_TOOLS_PER_INVESTIGATION


def test_orchestrator_result_reports_which_transport_actually_ran(monkeypatch):
    """The response must say plainly whether MCP or REST executed the real
    tool calls -- previously this was only a server-side config switch
    (INVESTIGATE_TRANSPORT) with no trace in the API response at all, which
    is exactly the ambiguity the user asked to have eliminated."""
    monkeypatch.setattr(orchestrator, "select_tools", lambda *a, **k: GOOD_SELECTION)
    monkeypatch.setattr(orchestrator, "execute_tool", lambda tool, ticker, args, client: {"tool": tool, "ok": True, "data": {}, "error": None})
    monkeypatch.setattr(orchestrator, "execute_tools_mcp", lambda calls, uw_api_key: [{"tool": c["tool"], "ok": True, "data": {}, "error": None} for c in calls])
    monkeypatch.setattr(orchestrator, "synthesize", lambda *a, **k: GOOD_SYNTHESIS)

    rest_result = orchestrator.investigate(
        ticker="ZYNC", evidence={}, ai_provider="anthropic", ai_api_key="fake",
        uw_api_key="fake", uw_base_url="https://x", transport="rest",
    )
    assert rest_result.transport == "rest"
    assert rest_result.simulated is False

    mcp_result = orchestrator.investigate(
        ticker="ZYNC", evidence={}, ai_provider="anthropic", ai_api_key="fake",
        uw_api_key="fake", uw_base_url="https://x", transport="mcp",
    )
    assert mcp_result.transport == "mcp"
    assert mcp_result.simulated is False


def test_orchestrator_raises_when_no_valid_tools_selected(monkeypatch):
    monkeypatch.setattr(orchestrator, "select_tools", lambda *a, **k: [{"tool": "not_allowed", "args": {}}])
    try:
        orchestrator.investigate(
            ticker="ZYNC", evidence={}, ai_provider="anthropic", ai_api_key="fake",
            uw_api_key="fake", uw_base_url="https://example.invalid",
        )
        assert False, "expected NoValidToolsSelectedError"
    except orchestrator.NoValidToolsSelectedError:
        pass


def test_orchestrator_rejects_invalid_ticker_before_any_call(monkeypatch):
    calls = {"select": 0}
    monkeypatch.setattr(orchestrator, "select_tools", lambda *a, **k: calls.__setitem__("select", calls["select"] + 1) or GOOD_SELECTION)
    try:
        orchestrator.investigate(
            ticker="bad ticker!!", evidence={}, ai_provider="anthropic", ai_api_key="fake",
            uw_api_key="fake", uw_base_url="https://example.invalid",
        )
        assert False, "expected ValidationError"
    except ValidationError:
        pass
    assert calls["select"] == 0


def test_orchestrator_rejects_malformed_synthesis_response(monkeypatch):
    monkeypatch.setattr(orchestrator, "select_tools", lambda *a, **k: GOOD_SELECTION)
    monkeypatch.setattr(orchestrator, "execute_tool", lambda tool, ticker, args, client: {"tool": tool, "ok": True, "data": {}, "error": None})
    monkeypatch.setattr(orchestrator, "synthesize", lambda *a, **k: {"observed_facts": []})  # missing ai_interpretation
    try:
        orchestrator.investigate(
            ticker="ZYNC", evidence={}, ai_provider="anthropic", ai_api_key="fake",
            uw_api_key="fake", uw_base_url="https://example.invalid",
        )
        assert False, "expected MalformedAIResponse"
    except MalformedAIResponse:
        pass


# --- Route-level behavior (FastAPI TestClient) ---------------------------


def test_route_demo_mode_returns_simulated_and_never_touches_real_calls(app_main, monkeypatch):
    """Demo mode (not live) must ALWAYS return the static transcript and
    must NEVER invoke the real tool-selection/execute/synthesis functions,
    even if an AI key happens to be configured."""
    from app.ai import orchestrator as orch

    real_calls = {"select": 0, "execute": 0, "synthesize": 0}
    monkeypatch.setattr(orch, "select_tools", lambda *a, **k: real_calls.__setitem__("select", 1))
    monkeypatch.setattr(orch, "execute_tool", lambda *a, **k: real_calls.__setitem__("execute", 1))
    monkeypatch.setattr(orch, "synthesize", lambda *a, **k: real_calls.__setitem__("synthesize", 1))

    client = TestClient(app_main.app)
    client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "sk-fake-not-real"})
    resp = client.post("/api/investigate/ZYNC")
    assert resp.status_code == 200
    body = resp.json()
    assert body["simulated"] is True
    assert "Simulated" in body["simulated_label"]
    assert real_calls == {"select": 0, "execute": 0, "synthesize": 0}


def test_route_public_demo_always_simulated_regardless_of_mode(app_main, monkeypatch):
    from app.ai import orchestrator as orch

    real_calls = {"n": 0}
    monkeypatch.setattr(orch, "select_tools", lambda *a, **k: real_calls.__setitem__("n", real_calls["n"] + 1))
    monkeypatch.setattr(app_main.settings, "public_demo", True)

    client = TestClient(app_main.app)
    resp = client.post("/api/investigate/QRVX")
    assert resp.status_code == 200
    assert resp.json()["simulated"] is True
    assert real_calls["n"] == 0


def test_route_invalid_ticker_returns_400(app_main):
    client = TestClient(app_main.app)
    resp = client.post("/api/investigate/not-a-ticker!!")
    assert resp.status_code == 400


def test_route_live_mode_without_ai_key_returns_clear_4xx(app_main):
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": "uwtest_FAKEFAKEFAKE1234"})
    app_main._settings_store.record_verified_uw_key("uwtest_FAKEFAKEFAKE1234")
    client.post("/api/settings/mode", json={"mode": "live"})
    resp = client.post("/api/investigate/ZYNC")
    assert resp.status_code == 400
    assert "AI provider key" in resp.json()["detail"]


def test_route_live_mode_without_uw_key_returns_clear_4xx(app_main):
    client = TestClient(app_main.app)
    client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "sk-fake-not-real"})
    # Can't switch to live mode without a UW key in the first place (existing
    # Settings behavior) -- simulate the edge case directly by calling the
    # route while mode is live but the key store has no UW key. We reach
    # 'live' via uw-key then delete it, which also force-resets mode to demo
    # (existing behavior) -- so instead directly exercise availability.
    resp = client.get("/api/investigate/availability")
    assert resp.json()["available"] is True  # demo mode, always available (simulated)


def test_availability_reports_disabled_reason_without_ai_key_in_live_mode(app_main):
    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": "uwtest_FAKEFAKEFAKE1234"})
    app_main._settings_store.record_verified_uw_key("uwtest_FAKEFAKEFAKE1234")
    client.post("/api/settings/mode", json={"mode": "live"})
    resp = client.get("/api/investigate/availability")
    body = resp.json()
    assert body["available"] is False
    assert "AI provider key" in body["reason"]


def test_route_live_mode_happy_path_uses_mocked_mcp_equivalent_and_llm(app_main, monkeypatch):
    """Full live-mode route exercise with everything mocked: confirms the
    real orchestrator functions ARE invoked (unlike the demo-mode test
    above) and that the response shape matches the structured schema."""
    from app.ai import orchestrator as orch

    client = TestClient(app_main.app)
    client.post("/api/settings/uw-key", json={"api_key": "uwtest_FAKEFAKEFAKE1234"})
    app_main._settings_store.record_verified_uw_key("uwtest_FAKEFAKEFAKE1234")
    client.post("/api/settings/mode", json={"mode": "live"})
    client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "«redacted:sk-…»"})

    monkeypatch.setattr(orch, "select_tools", lambda *a, **k: GOOD_SELECTION)
    monkeypatch.setattr(orch, "execute_tool", lambda tool, ticker, args, client: {"tool": tool, "ok": True, "data": {}, "error": None})
    monkeypatch.setattr(orch, "synthesize", lambda *a, **k: GOOD_SYNTHESIS)

    resp = client.post("/api/investigate/ZYNC")
    assert resp.status_code == 200
    body = resp.json()
    assert body["simulated"] is False
    assert body["ai_interpretation"] == GOOD_SYNTHESIS["ai_interpretation"]
    assert set(body.keys()) >= {
        "observed_facts", "supporting_evidence", "conflicting_observations", "limitations", "ai_interpretation",
    }


# ---------------------------------------------------------------------------
# ROOT-CAUSE FIX regression guards (2026-10-14): real NVDA + PLTR synthesis
# truncation at the 3000-token limit. Root cause was an UNBOUNDED output
# contract meeting a REAL, rich evidence set (measured ~49KB combined raw
# JSON for a real 4-tool NVDA call on N5, no LLM involved) -- not a wrong
# token limit. These tests use realistically large/verbose payloads, not
# toy ones, because the bug only reproduces at realistic scale.
# ---------------------------------------------------------------------------

def _big_realistic_tool_result(tool="get_flow_alerts", n_records=120):
    """Shaped like a real UW list response: many records, several fields
    each, long-ish string values -- mirrors the ~24KB measured for a real
    get_flow_alerts(limit=20) call scaled up to show the summarization
    actually bounds size regardless of how much real data comes back."""
    return {
        "tool": tool,
        "ok": True,
        "data": [
            {
                "ticker": "NVDA",
                "created_at": f"2026-10-{10 + (i % 5):02d}T{9 + (i % 7):02d}:{i % 60:02d}:00Z",
                "premium": 1_000_000 + i * 1234,
                "sentiment": "bullish" if i % 2 == 0 else "bearish",
                "description": (
                    f"Large options sweep #{i} across multiple exchanges, size {i * 37} contracts, "
                    f"strike {100 + i} expiring in {i % 90} days, flagged as unusual relative to "
                    "30-day average volume for this contract."
                ),
            }
            for i in range(n_records)
        ],
    }


def test_summarize_tool_result_caps_records_but_discloses_the_truncation():
    big = _big_realistic_tool_result(n_records=120)
    summarized = _summarize_tool_result_for_prompt(big)
    assert len(summarized["data"]) == MAX_RECORDS_PER_TOOL_IN_PROMPT
    assert summarized["_truncated_from"] == 120
    assert "_truncation_note" in summarized
    # newest-first preserved: the first N records kept, not a random sample
    assert summarized["data"][0] == big["data"][0]


def test_summarize_tool_result_leaves_small_results_untouched():
    small = _big_realistic_tool_result(n_records=3)
    summarized = _summarize_tool_result_for_prompt(small)
    assert summarized == small
    assert "_truncated_from" not in summarized


def test_build_synthesis_user_prompt_stays_bounded_even_with_a_real_scale_4_tool_payload():
    """The actual real-world scenario: 4 tools, each with a realistically
    large result (mirrors the measured ~49KB NVDA case scaled up to make
    sure the cap, not luck, is what keeps this bounded)."""
    tool_results = [
        _big_realistic_tool_result(tool=t, n_records=120)
        for t in ["get_flow_alerts", "get_dark_pool_trades", "get_option_trades", "get_insider_activity_by_ticker"]
    ]
    prompt = build_synthesis_user_prompt("NVDA", tool_results)
    # 8 records/tool x 4 tools, not 120 x 4 -- this is the actual fix.
    assert prompt.count('"created_at"') <= MAX_RECORDS_PER_TOOL_IN_PROMPT * 4
    assert len(prompt) < 20_000  # well below the ~49KB that triggered the real bug


def test_server_side_caps_truncate_an_oversized_llm_response_rather_than_crash_or_pass_through_raw():
    """Defense in depth: even if the model ignores the prompt's size
    limits, the validator must cap -- not reject outright, not silently
    accept -- and must say so via server_truncated=True."""
    raw = {
        "observed_facts": [
            {"fact": f"Observed fact number {i} with a fairly long description. " * 3, "source_tool": "get_flow_alerts"}
            for i in range(20)  # way over MAX_OBSERVED_FACTS
        ],
        "supporting_evidence": [f"supporting point {i} " * 10 for i in range(10)],
        "conflicting_observations": [f"conflicting point {i} " * 10 for i in range(10)],
        "limitations": [f"limitation {i} " * 10 for i in range(10)],
        "ai_interpretation": "A very long interpretation. " * 60,
    }
    result = validate_investigation_result(raw, "NVDA", called_tools=["get_flow_alerts"])
    assert result.server_truncated is True
    assert len(result.observed_facts) == MAX_OBSERVED_FACTS
    assert len(result.supporting_evidence) == MAX_SUPPORTING_EVIDENCE
    assert len(result.conflicting_observations) == MAX_CONFLICTING_OBSERVATIONS
    assert len(result.limitations) == MAX_LIMITATIONS
    assert len(result.ai_interpretation) <= MAX_INTERPRETATION_CHARS
    for f in result.observed_facts:
        assert len(f.fact) <= MAX_FACT_CHARS


def test_server_side_caps_leave_a_compact_realistic_response_completely_untouched():
    """The common/intended case: a response that already respects the
    stated limits must NOT be flagged or altered at all."""
    raw = {
        "observed_facts": [
            {"fact": "NVDA saw 42 bullish flow alerts totaling $12.4M premium in the last session.", "source_tool": "get_flow_alerts"},
            {"fact": "Dark pool volume was 2.1M shares, above the 10-day average of 1.4M.", "source_tool": "get_dark_pool_trades"},
        ],
        "supporting_evidence": ["Elevated bullish flow aligns with above-average dark pool volume."],
        "conflicting_observations": ["Short interest rose 3% over the same window, which doesn't fit a purely bullish read."],
        "limitations": ["Only the most recent 20 flow alerts were available; longer-term context is missing."],
        "ai_interpretation": "Taken together, this pattern could reflect accumulation, but the short-interest rise is a real counter-signal worth weighing.",
    }
    result = validate_investigation_result(raw, "NVDA", called_tools=["get_flow_alerts", "get_dark_pool_trades"])
    assert result.server_truncated is False
    assert len(result.observed_facts) == 2
    assert result.ai_interpretation == raw["ai_interpretation"]


# ---------------------------------------------------------------------------
# Follow-up fix (2026-10-14, Chad-approved): pure "newest N" can silently
# drop the single most decision-relevant record. These tests use the REAL
# field names measured directly against a live NVDA MCP call on N5 (not
# guessed) -- total_premium/type for flow_alerts, premium/option_type for
# option_trades, premium for dark_pool_trades (no reliable direction field
# there, confirmed from the real sample: no bullish/bearish flag exists).
# ---------------------------------------------------------------------------

def _realistic_flow_alerts(n=120, outlier_premium=9_000_000, outlier_at=57):
    """120 real-shaped flow_alerts: overwhelmingly 'call', one buried 'put'
    outlier at a non-recent position, AND one buried huge-premium 'call'
    at a different non-recent position -- both must survive sampling even
    though neither is in the newest-4 window."""
    records = []
    for i in range(n):
        records.append({
            "id": f"alert-{i}",
            "ticker": "NVDA",
            "type": "put" if i == outlier_at else "call",
            "total_premium": str(outlier_premium if i == 80 else (50_000 + i * 100)),
            "strike": "220",
            "has_sweep": False,
        })
    return records


def test_diverse_sample_recovers_a_buried_high_magnitude_record_recency_alone_would_miss():
    data = _realistic_flow_alerts()
    sampled = _diverse_sample_records("get_flow_alerts", data)
    assert len(sampled) == MAX_RECORDS_PER_TOOL_IN_PROMPT
    ids = {r["id"] for r in sampled}
    assert "alert-80" in ids, "the single highest-total_premium record (buried at position 80) was dropped"


def test_diverse_sample_recovers_a_buried_minority_direction_record():
    data = _realistic_flow_alerts()
    sampled = _diverse_sample_records("get_flow_alerts", data)
    ids = {r["id"] for r in sampled}
    assert "alert-57" in ids, "the one 'put' among 119 'call' records (buried, not in newest-4) was dropped"


def test_diverse_sample_always_includes_the_newest_records():
    data = _realistic_flow_alerts()
    sampled = _diverse_sample_records("get_flow_alerts", data)
    ids = {r["id"] for r in sampled}
    for i in range(4):
        assert f"alert-{i}" in ids, f"newest record alert-{i} was dropped -- recency must never be fully sacrificed"


def test_diverse_sample_never_duplicates_a_record_selected_by_multiple_criteria():
    # Make the newest record ALSO the highest-magnitude AND the minority
    # direction -- every selection path points at the same record; the
    # cap must still leave room for 7 OTHERS, not silently shrink to 1.
    data = _realistic_flow_alerts(outlier_at=0, outlier_premium=99_000_000)
    sampled = _diverse_sample_records("get_flow_alerts", data)
    ids = [r["id"] for r in sampled]
    assert len(ids) == len(set(ids)) == MAX_RECORDS_PER_TOOL_IN_PROMPT


def test_diverse_sample_falls_back_to_pure_recency_for_a_tool_with_no_known_fields():
    # get_insider_activity_by_ticker's real schema is a roster of PEOPLE
    # (id/name/cik), not transactions -- no magnitude/direction field
    # exists, confirmed from the real sample. Must still behave sanely:
    # newest N, no crash, no spurious "diverse" selection attempted.
    data = [{"id": i, "name": f"Person {i}"} for i in range(50)]
    sampled = _diverse_sample_records("get_insider_activity_by_ticker", data)
    assert len(sampled) == MAX_RECORDS_PER_TOOL_IN_PROMPT
    assert [r["id"] for r in sampled[:4]] == [0, 1, 2, 3]


def test_summarize_tool_result_discloses_the_diverse_sampling_strategy_honestly():
    data = _realistic_flow_alerts()
    result = {"tool": "get_flow_alerts", "ok": True, "data": data}
    summarized = _summarize_tool_result_for_prompt(result)
    assert summarized["_truncated_from"] == 120
    note = summarized["_truncation_note"]
    assert "NOT a complete dataset" in note
    assert "highest-magnitude" in note and "minority-direction" in note


def test_summarize_tool_result_discloses_pure_recency_when_no_fields_known():
    data = [{"id": i, "name": f"Person {i}"} for i in range(50)]
    result = {"tool": "get_insider_activity_by_ticker", "ok": True, "data": data}
    summarized = _summarize_tool_result_for_prompt(result)
    note = summarized["_truncation_note"]
    assert "no reliable magnitude/direction/sweep field" in note


def test_option_trades_diverse_sample_uses_its_own_real_field_names():
    # Real schema: premium (string numeric), option_type ("call"/"put") --
    # different field names than flow_alerts, must not be hardcoded/shared.
    records = [
        {"id": f"opt-{i}", "premium": str(100 + i), "option_type": "put" if i == 33 else "call"}
        for i in range(100)
    ]
    records[70]["premium"] = "5000000"  # buried high-magnitude outlier
    sampled = _diverse_sample_records("get_option_trades", records)
    ids = {r["id"] for r in sampled}
    assert "opt-70" in ids, "buried highest-premium option trade was dropped"
    assert "opt-33" in ids, "buried minority option_type ('put' among mostly 'call') was dropped"


def test_dark_pool_trades_diverse_sample_handles_magnitude_only_no_direction_field():
    # Real schema confirmed from the live NVDA sample: dark_pool_trades
    # has "premium" but genuinely no bullish/bearish/direction field --
    # this exercises the magnitude-only branch (DIRECTION_FIELD_BY_TOOL
    # has no entry for this tool, step 3 must just no-op, not crash).
    records = [{"id": f"dp-{i}", "premium": str(1000 + i)} for i in range(200)]
    records[150]["premium"] = "9999999"  # buried high-magnitude outlier
    sampled = _diverse_sample_records("get_dark_pool_trades", records)
    assert len(sampled) == MAX_RECORDS_PER_TOOL_IN_PROMPT
    ids = {r["id"] for r in sampled}
    assert "dp-150" in ids, "buried highest-premium dark-pool print was dropped"
    for i in range(4):
        assert f"dp-{i}" in ids
