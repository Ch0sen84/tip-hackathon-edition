// Round 13 item 3 regression test (ux-nav-watchlist-reliability round 13):
// a FOURTH-round-running, previously-unfound display location for the
// same zero-gate-wording bug the headline (D2) and state-gloss badge
// (P3) fixes already covered two/three rounds ago. The phrase "Unusual
// activity; stock little changed" is tip_evidence.current_read()'s raw
// backend narrative, rendered directly as `d.current_read` immediately
// below the (already honest) primary headline in the Ticker Analysis
// header card -- a results/activity-summary line, distinct from both
// the headline and the badge. This test proves the fix reuses the SAME
// shared isZeroGateTransitionReason predicate (never a new/fourth check)
// via the new primaryCurrentRead() helper.
//
//   node frontend/test_round13_item3_current_read_zero_gate.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { renderTickerDetail, primaryCurrentRead, isZeroGateTransitionReason };");
const { renderTickerDetail, primaryCurrentRead, isZeroGateTransitionReason } = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

// Exact shape of a real GET /api/ticker/{t} response for ARTNA/MSEX's
// first zero-gate evaluation, per the task brief's reported symptom.
const zeroGateFirstEval = {
  ticker: "ARTNA", state: "NEW", previous_state: null,
  transition_rule: "no_qualifying_evidence_yet",
  confidence: "LOW", signal_type: "none",
  confidence_reason: "No gates passed this scan.",
  qualifies: false, qualifying_days_count: 0, gates: {},
  headline: "Unusual activity",
  // The exact misleading phrase reported in the task brief.
  current_read: "Unusual activity; stock little changed.",
  evidence: [], stock_state: null, history: [],
  raw: { flow_alerts: [], darkpool_trades: [] },
  eval_freshness: "current", last_evaluated_at: "2026-10-17T12:00:00+00:00", served_from_cache: true,
};

// The L1 any_gate_passed guard's second zero-gate reason -- must behave
// identically, same shared predicate.
const zeroGateSecondEval = { ...zeroGateFirstEval, transition_rule: "no_gate_passed_remains_new" };

const genuinelyQualifying = {
  ...zeroGateFirstEval,
  state: "BUILDING", transition_rule: "first_qualifying_gate",
  headline: "Dark pool ~3.2x recent average",
  current_read: "Dark-pool activity well above normal; stock little changed.",
};

function currentReadLine(html) {
  // The second ".read" div in the header card (right after the
  // primary-headline div) -- extract it positionally, same fragile-but-
  // deliberate convention this repo's other tests already use.
  const m = html.match(/id="tickerPrimaryHeadline">[^<]*<\/div>\s*<div class="read">([^<]*)<\/div>/);
  return m ? m[1] : null;
}

check("isZeroGateTransitionReason recognizes both zero-gate reasons (shared helper, unchanged)", () => {
  assert.equal(isZeroGateTransitionReason("no_qualifying_evidence_yet"), true);
  assert.equal(isZeroGateTransitionReason("no_gate_passed_remains_new"), true);
  assert.equal(isZeroGateTransitionReason("first_qualifying_gate"), false);
});

check("primaryCurrentRead() substitutes an honest sentence for a zero-gate first evaluation", () => {
  const read = primaryCurrentRead(zeroGateFirstEval);
  assert.notEqual(read, "Unusual activity; stock little changed.");
  assert.notEqual(read, zeroGateFirstEval.current_read);
});

check("primaryCurrentRead() behaves IDENTICALLY for the second zero-gate reason (no_gate_passed_remains_new)", () => {
  assert.equal(primaryCurrentRead(zeroGateFirstEval), primaryCurrentRead(zeroGateSecondEval));
});

check("primaryCurrentRead() leaves a genuinely qualifying ticker's current_read UNTOUCHED", () => {
  assert.equal(primaryCurrentRead(genuinelyQualifying), genuinelyQualifying.current_read);
});

check("the ACTUAL RENDERED activity-summary line (below the headline) no longer shows the misleading phrase", () => {
  const html = renderTickerDetail(zeroGateFirstEval);
  const line = currentReadLine(html);
  assert.ok(line, "expected to find the .read line after #tickerPrimaryHeadline");
  assert.notEqual(line, "Unusual activity; stock little changed.");
  assert.ok(!line.toLowerCase().includes("little changed"), line);
});

check("the second zero-gate reason renders the SAME honest line in the real DOM", () => {
  const html = renderTickerDetail(zeroGateSecondEval);
  const line = currentReadLine(html);
  assert.equal(line, currentReadLine(renderTickerDetail(zeroGateFirstEval)));
});

check("a genuinely qualifying ticker's rendered activity-summary line is UNCHANGED (no over-broadening)", () => {
  const html = renderTickerDetail(genuinelyQualifying);
  const line = currentReadLine(html);
  assert.equal(line, "Dark-pool activity well above normal; stock little changed.");
});

console.log(`\n${passed} check(s) passed.`);
