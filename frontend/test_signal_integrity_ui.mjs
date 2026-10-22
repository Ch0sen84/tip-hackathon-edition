// Signal-integrity remediation: presentation honesty regressions.
// Loads the REAL frontend/app.core.js (no reimplementation).
//
//   node frontend/test_signal_integrity_ui.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + `
globalThis.__exports = { gateWhyItMatters, classifyGate, renderGateItem, isNotEvaluated, stateTagHtml,
  renderCard, renderRow, renderFeaturedSignal, freshnessBadgeHtml, renderRawDataSummary,
  markerProvenanceText, TRANSITION_RULE_GLOSS, GATE_LIMITATION_NOTES, renderTickerDetail, renderLifecycleTimeline, renderHistoryChip };`);
const X = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

check("synthetic percentile text never claims a trailing window or a real percentile", () => {
  const g = { passed: false, status: "synthetic", metrics: { illustrative_percentile: 0.98, bucket_passed: true } };
  const t = X.gateWhyItMatters("historical_percentile", g);
  assert.ok(!/trailing/i.test(t), t);
  assert.ok(/illustrative/i.test(t) && /not a historical percentile/i.test(t), t);
  const note = X.GATE_LIMITATION_NOTES.historical_percentile;
  assert.ok(!/(of|in) the trailing|trailing \d|-day window/i.test(note), note);   // no affirmative window claim
  assert.ok(/no trailing window/i.test(note) && /not a historical percentile/i.test(note), note);
  assert.equal(X.classifyGate("historical_percentile", g).key, "DATA_LIMITATION");
});

check("dark-pool pass text does not call it a 'statistical anomaly' and names the baseline", () => {
  const g = { passed: true, status: "pass", source: "uw_darkpool_session_baseline",
              metrics: { baseline: { ratio: 10, percentile: 1, eligible_sessions: 25, elapsed_minutes: 150 } } };
  const t = X.gateWhyItMatters("dark_pool_anomaly", g);
  assert.ok(!/statistical anomaly/i.test(t), t);
  assert.ok(/25 earlier fully collected sessions/.test(t) && /10\.0x/.test(t), t);
});

check("insufficient history and unavailable data are data limitations, not unmet criteria", () => {
  for (const status of ["insufficient", "unavailable", "unsupported", "not_evaluated"]) {
    const g = { passed: false, status, metrics: { baseline: { reason: "only 3 complete prior sessions" } } };
    assert.equal(X.classifyGate("options_flow_anomaly", g).key, "DATA_LIMITATION", status);
  }
  const u = X.gateWhyItMatters("options_flow_anomaly", { passed: false, status: "unavailable", metrics: {} });
  assert.ok(/not a negative observation/i.test(u), u);
  const item = X.renderGateItem("dark_pool_anomaly", { passed: false, status: "insufficient", metrics: {} });
  assert.ok(item.includes("insufficient history") && !item.includes(">no<"), item);
});

check("data_quality never claims the data is 'trustworthy' and names failed sources", () => {
  const okTxt = X.gateWhyItMatters("data_quality", { passed: true, status: "pass", metrics: { failed_sources: [] } });
  assert.ok(!/trustworthy/i.test(okTxt), okTxt);
  const bad = X.gateWhyItMatters("data_quality", { passed: false, status: "fail",
    metrics: { failed_sources: ["options_flow"], collection_status: { options_flow: "rate_limited" } } });
  assert.ok(/options flow \(rate_limited\)/.test(bad) && /not negative evidence/.test(bad), bad);
});

check("price confirmation wording: inferred lean, coincident not proof, no dark-pool claim", () => {
  const t = X.gateWhyItMatters("price_confirmation", { passed: true, status: "pass",
    metrics: { confirmed: true, direction_heuristic: "bearish-lean", price_move_pct: -3.46 } });
  assert.ok(/inferred/.test(t) && /not proof/.test(t) && !/dark-pool/i.test(t), t);
  const c = X.gateWhyItMatters("price_confirmation", { passed: false, status: "fail",
    metrics: { confirmed: false, direction_heuristic: "bullish-lean", price_move_pct: -2 } });
  assert.ok(!/Options\/dark-pool/.test(c), c);
});

check("an evaluated ticker without an episode is not shown as 'Not evaluated'", () => {
  const entry = { ticker: "SPY", state: null, confidence: "insufficient", signal_type: "insufficient_evidence",
                  not_evaluated: false };
  assert.equal(X.isNotEvaluated(entry), false);
  const html = X.stateTagHtml(entry);
  assert.ok(html.includes("No active qualification") && html.includes("insufficient evidence"), html);
  assert.equal(X.isNotEvaluated({ ticker: "AAA", state: null, not_evaluated: true }), true);
});

check("stale / failed evaluations carry a freshness badge next to confidence on cards, rows and hero", () => {
  const base = { ticker: "NVDA", state: "BUILDING", confidence: "medium", label: "Options", headline: "x",
                 current_read: "y", categories: [], price: {}, history_states: [] };
  for (const f of ["stale", "collection_failed"]) {
    const e = { ...base, eval_freshness: f };
    for (const html of [X.renderCard(e), X.renderRow(e), X.renderFeaturedSignal(e)]) {
      assert.ok(html.includes(`freshness-${f}`), `${f} badge missing`);
    }
  }
  assert.equal(X.freshnessBadgeHtml({ eval_freshness: "current" }), "");
  assert.ok(X.renderFeaturedSignal(base).includes("not a probability"));
});

check("evidence detail distinguishes a failed collection from 'no activity' and never shows null as $0", () => {
  const d = { ticker: "SPY", raw: { flow_alerts: [], darkpool_trades: [{ premium: null, size: 5, executed_at: "t" }] },
              collection: { options_flow: { ok: false, status: "rate_limited" }, darkpool: { ok: true } },
              gates: {}, history: [] };
  const html = X.renderRawDataSummary(d);
  assert.ok(html.includes("could not be collected this scan (rate_limited)"), "failed collection not disclosed");
  assert.ok(!html.includes("No options flow alerts returned this scan."));
  assert.ok(/Dark-pool print<\/td><td>n\/a<\/td><td>n\/a/.test(html), "null premium rendered as a number");
});

check("chart caption no longer claims markers are the gates' exact inputs", () => {
  const html = X.renderTickerDetail({ ticker: "SPY", state: null, confidence: "low", gates: {}, history: [],
                                      raw: {}, transition_rule: "no_qualifying_evidence_yet" });
  assert.ok(!html.includes("from the same flow_alerts/darkpool_trades the gates use"));
  assert.ok(html.includes("not necessarily the exact snapshot"));
  const cat = X.markerProvenanceText({ marker_sources: { origin: "catalog", options_flow: { row_count: 3 }, darkpool: { row_count: 9 } } });
  assert.ok(cat.includes("3 cataloged flow alerts") && cat.includes("9 cataloged dark-pool prints") && !cat.includes("not drawn"), cat);
  // Live P0: a certified history is far too large to draw; the caption must say only the newest are drawn
  const big = X.markerProvenanceText({ marker_sources: { origin: "catalog",
    options_flow: { row_count: 1000, total: 226418, truncated: true }, darkpool: { row_count: 1000, total: 269953, truncated: true } } });
  assert.ok(big.includes("the newest 1000 of 226418 cataloged flow alerts") && big.includes("the newest 1000 of 269953")
    && big.includes("not drawn"), big);
  const live = X.markerProvenanceText({ marker_sources: { origin: "live_fetch",
    options_flow: { ok: false, status: "error", row_count: 0 }, darkpool: { ok: true, row_count: 5 } } });
  assert.ok(/unavailable data, not absent activity/.test(live), live);
});

check("every lifecycle rule the backend can emit has a gloss", () => {
  const rules = ["held_incomplete_collection", "held_insufficient_evidence",
    "held_identical_evidence_from_an_earlier_session", "same_session_no_advance",
    "qualifying_evidence_on_a_later_session", "opened_confirmed_two_independent_categories",
    "multi_signal_confirmation_two_independent_categories", "demoted_lost_multi_signal_confirmation",
    "confirmed_on_3_distinct_sessions_and_price_corroborates", "new_episode_after_invalidation",
    "new_episode_after_expiry", "episode_expired_no_recent_evidence", "invalidated_this_session_no_reopen",
    "no_qualifying_evidence", "adverse_price_move_since_episode_open<= -3.0%",
    "adverse_price_move_daily_change<= -3.0%", "first_qualifying_gate", "no_qualifying_evidence_yet",
    "still_single_signal", "still_confirmed", "still_stable", "reheated_single_signal", "reheated_multi_signal",
    "no_gate_passed_while_building", "no_gate_passed_while_confirmed", "no_gate_passed_while_stable",
    "still_fading", "no_gate_passed_remains_new"];
  for (const r of rules) assert.ok(X.TRANSITION_RULE_GLOSS[r], `missing gloss for ${r}`);
});

check("history with no open episode renders NONE, never 'null', and the header says evaluated", () => {
  const hist = [{ scan_date: "2026-10-16", state: null, any_gate_passed: false, transition_rule: "held_insufficient_evidence" },
                { scan_date: "2026-10-16", state: null, any_gate_passed: false, transition_rule: "held_insufficient_evidence" }];
  const tl = X.renderLifecycleTimeline(hist, null);
  assert.ok(!/null/.test(tl) && tl.includes("NONE") && tl.includes("current state: NONE"), tl);
  assert.ok(!/null/.test(X.renderHistoryChip([null, "NEW"])));
  const html = X.renderTickerDetail({ ticker: "SPY", state: null, previous_state: null, confidence: "insufficient",
    signal_type: "insufficient_evidence", gates: {}, history: hist, raw: {}, transition_rule: "held_insufficient_evidence" });
  assert.ok(!html.includes("No evaluation recorded yet") && html.includes("Evaluated: no active qualification"), "header");
});

console.log(`\n${passed} check(s) passed.`);
