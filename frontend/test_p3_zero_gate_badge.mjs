// P3 regression test (ux-nav-watchlist-reliability round 10): proves the
// SEPARATE state-gloss BADGE/PILL element -- not the primary headline,
// which test_d2_visible_headline.mjs already covers -- is honest for a
// zero-gate-on-first-eval ticker.
//
// Real bug Claude's Windows Docker Desktop retest found: the primary
// headline fix (D2, two rounds ago) only ever changed
// #tickerPrimaryHeadline's text. A SECOND, adjacent element in the same
// header card -- the `.state-gloss` badge right next to the state tag
// (`<span class="tag state-NEW">NEW</span> <span class="muted
// state-gloss">(...)</span>`) -- is built from STATE_GLOSS[d.state]
// alone, which for state="NEW" is unconditionally "just started
// qualifying" regardless of WHY it's NEW. On a genuine zero-gate first
// evaluation (transition_rule="no_qualifying_evidence_yet"), that badge
// said "(just started qualifying)" on the exact same screen where the
// headline directly above it (already fixed) says "No qualifying signal
// detected yet." -- two visibly contradictory claims on one page.
//
// This test is deliberately separate from test_d2_visible_headline.mjs:
// it locates and asserts against `.state-gloss`/#tickerPrimaryHeadline's
// SIBLING pill specifically, never reusing that other test's headline
// regex, so a future regression in either element is caught
// independently of the other.
//
//   node frontend/test_p3_zero_gate_badge.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { renderTickerDetail, primaryStateGloss, STATE_GLOSS };");
const { renderTickerDetail, primaryStateGloss, STATE_GLOSS } = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

// Exact shape of a real GET /api/ticker/{t} response for a zero-gate
// first evaluation -- same fixture convention as test_d2_visible_
// headline.mjs's zeroGateFirstEval, intentionally not imported from
// that file so this test has no coupling to it at all.
const zeroGateFirstEval = {
  ticker: "MSEX", state: "NEW", previous_state: null,
  transition_rule: "no_qualifying_evidence_yet",
  confidence: "LOW", signal_type: "none",
  confidence_reason: "No gates passed this scan.",
  qualifies: false, qualifying_days_count: 0, gates: {},
  headline: "Unusual activity",
  current_read: "No qualifying evidence yet for MSEX.",
  evidence: [], stock_state: null, history: [],
  raw: { flow_alerts: [], darkpool_trades: [] },
  eval_freshness: null, last_evaluated_at: null, served_from_cache: false,
};

function stateGlossBadgeText(html) {
  // Not a simple `\(([^)]*)\)` -- some real gloss text (e.g.
  // INVALIDATED's "price moved against the signal (terminal state)")
  // has its OWN nested parens, so match up to the span's closing tag
  // and strip exactly one leading '(' and one trailing ')' instead.
  const m = html.match(/<span class="muted state-gloss">\(([\s\S]*?)\)<\/span>/);
  return m ? m[1] : null;
}

check("STATE_GLOSS['NEW'] is unconditionally 'just started qualifying' (the raw map, unchanged)", () => {
  assert.equal(STATE_GLOSS.NEW, "just started qualifying");
});

check("THE BUG, directly on the map alone: a naive STATE_GLOSS[state] lookup would say the misleading thing", () => {
  assert.equal(STATE_GLOSS[zeroGateFirstEval.state], "just started qualifying");
});

check("primaryStateGloss() branches on transition_rule (like primaryTickerHeadline), not state alone", () => {
  assert.equal(primaryStateGloss(zeroGateFirstEval), "no qualifying evidence yet");
  assert.notEqual(primaryStateGloss(zeroGateFirstEval), STATE_GLOSS.NEW);
});

check("the rendered .state-gloss BADGE element (separate from #tickerPrimaryHeadline) says the honest thing", () => {
  const html = renderTickerDetail(zeroGateFirstEval);
  const badge = stateGlossBadgeText(html);
  assert.ok(badge !== null, "expected a .state-gloss badge element in the rendered HTML");
  assert.equal(badge, "no qualifying evidence yet");
  assert.notEqual(badge, "just started qualifying", "the badge still shows the misleading NEW-state gloss");
});

check("the badge and the headline are NOT contradictory on the same rendered page", () => {
  const html = renderTickerDetail(zeroGateFirstEval);
  const badge = stateGlossBadgeText(html);
  const headlineMatch = html.match(/<div class="read primary-headline" id="tickerPrimaryHeadline">([^<]*)<\/div>/);
  assert.ok(headlineMatch, "expected #tickerPrimaryHeadline to exist alongside the badge");
  assert.equal(headlineMatch[1], "No qualifying signal detected yet.");
  // Neither visible string may claim qualification has actually started.
  assert.ok(!/just started qualifying/i.test(badge));
  assert.ok(!/qualifying signal/i.test(headlineMatch[1].replace("No qualifying signal detected yet.", "")));
});

check("a genuinely qualifying first-eval ticker (first_qualifying_gate) keeps the real NEW-state badge", () => {
  const qualifying = { ...zeroGateFirstEval, transition_rule: "first_qualifying_gate", headline: "Dark pool 3.2x recent average" };
  assert.equal(primaryStateGloss(qualifying), "just started qualifying");
  const html = renderTickerDetail(qualifying);
  assert.equal(stateGlossBadgeText(html), "just started qualifying");
});

check("every OTHER real lifecycle state's badge is completely unaffected by this fix", () => {
  for (const state of ["BUILDING", "CONFIRMED", "STABLE", "FADING", "INVALIDATED"]) {
    const d = { ...zeroGateFirstEval, state, transition_rule: "still_single_signal", previous_state: state };
    const html = renderTickerDetail(d);
    assert.equal(stateGlossBadgeText(html), STATE_GLOSS[state], `${state} badge regressed`);
  }
});

console.log(`\n${passed} check(s) passed.`);
