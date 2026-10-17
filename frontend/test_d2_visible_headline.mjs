// D2 regression test (ux-nav-watchlist-reliability round 9): proves the
// PRIMARY VISIBLE Ticker Analysis headline -- not the glossary/collapsed
// diagnostics -- is honest for a zero-gate-on-first-eval ticker.
//
// Prior test_zero_gate_label.mjs only checked TRANSITION_RULE_GLOSS's map
// entry, which (before this round's fix) was computed in renderTickerDetail
// but never actually rendered anywhere except inside the bottom
// "Developer / Debug" <details> panel (renderRawDataSummary). This test
// instead calls the REAL renderTickerDetail() (no reimplementation) with a
// `d` shaped exactly like the real backend's GET /api/ticker/{t} response
// for a ticker on its first evaluation with zero qualifying gates
// (state="NEW", transition_rule="no_qualifying_evidence_yet" -- the exact
// shape backend/tests/test_signal_engine.py's
// test_new_ticker_zero_gates_first_eval_is_honest asserts determine_transition
// returns) and asserts the VISIBLE #tickerPrimaryHeadline element -- not
// TRANSITION_RULE_GLOSS, not the <details> panel -- says so plainly.
//
//   node frontend/test_d2_visible_headline.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { renderTickerDetail, primaryTickerHeadline };");
const { renderTickerDetail, primaryTickerHeadline } = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`PASS: ${name}`);
    passed++;
  } catch (e) {
    console.log(`FAIL: ${name} -- ${e.message}`);
    process.exitCode = 1;
  }
}

// Shaped exactly like a real GET /api/ticker/{t} response for a ticker's
// first-ever evaluation with zero qualifying gates -- same fields the
// real route (backend/app/main.py ticker_analysis / _ticker_analysis_impl)
// always returns, just with the specific state/transition_rule/headline
// values that backend/tests/test_signal_engine.py pins for this exact case.
const zeroGateFirstEval = {
  ticker: "ARTNA",
  state: "NEW",
  previous_state: null,
  transition_rule: "no_qualifying_evidence_yet",
  confidence: "LOW",
  signal_type: "none",
  confidence_reason: "No gates passed this scan.",
  qualifies: false,
  qualifying_days_count: 0,
  gates: {},
  // The backend's generic tip_evidence.headline() fallback when zero
  // gates passed and gates_passed is empty -- this is the misleading
  // text the primary headline must NOT show for this transition_rule.
  headline: "Unusual activity",
  current_read: "No qualifying evidence yet for ARTNA.",
  evidence: [],
  stock_state: null,
  history: [],
  raw: { flow_alerts: [], darkpool_trades: [] },
  eval_freshness: null,
  last_evaluated_at: null,
  served_from_cache: false,
};

check("primaryTickerHeadline() branches on transition_rule, not state or backend headline", () => {
  assert.equal(primaryTickerHeadline(zeroGateFirstEval), "No qualifying signal detected yet.");
});

check("the rendered HTML's VISIBLE #tickerPrimaryHeadline element says so plainly", () => {
  const html = renderTickerDetail(zeroGateFirstEval);
  const m = html.match(/<div class="read primary-headline" id="tickerPrimaryHeadline">([^<]*)<\/div>/);
  assert.ok(m, "expected a #tickerPrimaryHeadline element in the rendered HTML");
  assert.equal(m[1], "No qualifying signal detected yet.");
});

check("the misleading backend fallback headline ('Unusual activity') is NOT what's shown as primary", () => {
  const html = renderTickerDetail(zeroGateFirstEval);
  const m = html.match(/<div class="read primary-headline" id="tickerPrimaryHeadline">([^<]*)<\/div>/);
  assert.notEqual(m[1], "Unusual activity");
});

check("this is a VISIBLE top-level card line, not inside the collapsed Developer/Debug <details>", () => {
  const html = renderTickerDetail(zeroGateFirstEval);
  const headlineIdx = html.indexOf('id="tickerPrimaryHeadline"');
  const detailsIdx = html.indexOf("Developer / Debug");
  assert.ok(headlineIdx > -1, "headline element missing");
  // renderTickerDetail's own output ends before renderRawDataSummary's
  // <details> is appended by the caller in the real app -- but even
  // within this same string (ticker_detail.js concatenates both), the
  // headline must appear BEFORE any Developer/Debug disclosure, never
  // only reachable through it.
  if (detailsIdx > -1) assert.ok(headlineIdx < detailsIdx, "headline must not be inside/after the collapsed debug panel");
});

check("a genuinely qualifying first-eval ticker (first_qualifying_gate) is completely unaffected", () => {
  const qualifying = { ...zeroGateFirstEval, transition_rule: "first_qualifying_gate", headline: "Dark pool 3.2x recent average" };
  assert.equal(primaryTickerHeadline(qualifying), "Dark pool 3.2x recent average");
  const html = renderTickerDetail(qualifying);
  assert.ok(html.includes("Dark pool 3.2x recent average"));
});

console.log(`\n${passed} check(s) passed.`);
