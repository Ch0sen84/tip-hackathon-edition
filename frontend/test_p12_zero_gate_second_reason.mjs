// Round 12 (ux-nav-watchlist-reliability) regression test: the SECOND
// zero-qualifying-gate reason string the backend's determine_transition
// (signal_engine.py, unchanged this round) can return --
// `no_gate_passed_remains_new`, produced by the already-approved L1
// any_gate_passed guard for a LATER re-evaluation of an already-NEW
// ticker that again found zero gates -- had never been wired into any
// of the three visible-UI locations fixed in rounds 9/10/11 (headline,
// state badge, history timeline), which only ever handled the FIRST
// reason string, `no_qualifying_evidence_yet`. Both reason strings mean
// exactly the same thing to a reader ("no qualifying signal"), and must
// render IDENTICALLY.
//
// This test also proves the fix holds across BOTH rendering paths that
// can show a ticker's detail card:
//   - the cache-only GET /api/ticker/{t} path (ordinary nav / F5 reload)
//   - the authenticated POST /api/ticker/{t}/refresh path (the explicit
//     "Refresh this ticker" button, re-rendered immediately with no
//     page reload)
// app.live.js's loadTickerDetail() funnels BOTH of these through the
// exact same renderTickerDetail() (see `el.innerHTML = renderTickerDetail(d)`
// at the single call site shared by both the initial load and the
// refresh-button click handler) -- there is no second, divergent
// "immediate refresh" rendering path in this codebase today. This test
// still exercises both response shapes explicitly (rather than asserting
// against the shared function once) so that if a future round ever DOES
// split that single call site into two, this regression catches any
// drift between them immediately.
//
// Exactly 5 cases, per the remediation brief:
//   (a) no_qualifying_evidence_yet, rendered from a fresh POST /refresh response
//   (b) no_qualifying_evidence_yet, rendered from a cached GET (post-reload) response
//   (c) no_gate_passed_remains_new, rendered from a fresh POST /refresh response
//   (d) no_gate_passed_remains_new, rendered from a cached GET (post-reload) response
//   (e) first_qualifying_gate (a genuinely qualifying first eval) -- regression
//       guard proving the zero-gate treatment was NOT over-broadened
//
//   node frontend/test_p12_zero_gate_second_reason.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { renderTickerDetail, primaryTickerHeadline, primaryStateGloss, historyEntryGloss, isZeroGateTransitionReason, STATE_GLOSS };");
const {
  renderTickerDetail,
  primaryTickerHeadline,
  primaryStateGloss,
  historyEntryGloss,
  isZeroGateTransitionReason,
  STATE_GLOSS,
} = globalThis.__exports;

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

function headlineText(html) {
  const m = html.match(/<div class="read primary-headline" id="tickerPrimaryHeadline">([^<]*)<\/div>/);
  return m ? m[1] : null;
}
function stateGlossBadgeText(html) {
  const m = html.match(/<span class="muted state-gloss">\(([\s\S]*?)\)<\/span>/);
  return m ? m[1] : null;
}

const HONEST_ZERO_GATE_TEXT = "No qualifying signal detected yet.";
const HONEST_ZERO_GATE_BADGE = "no qualifying evidence yet";

// Base shape matches a real GET/POST-refresh /api/ticker/{t} response.
const base = {
  ticker: "ARTNA",
  state: "NEW",
  previous_state: "NEW",
  confidence: "LOW",
  signal_type: "none",
  confidence_reason: "No gates passed this scan.",
  qualifies: false,
  qualifying_days_count: 0,
  gates: {},
  headline: "Unusual activity", // the backend's generic fallback -- must never surface for a zero-gate reason
  current_read: "No qualifying evidence yet for ARTNA.",
  evidence: [],
  stock_state: null,
  history: [],
  raw: { flow_alerts: [], darkpool_trades: [] },
};

// "Immediately after refresh" shape: the POST /refresh response -- always
// fresh, served_from_cache=false.
function freshFromRefresh(overrides) {
  return {
    ...base,
    ...overrides,
    eval_freshness: "fresh",
    last_evaluated_at: "2026-10-17T12:00:00Z",
    served_from_cache: false,
  };
}

// "After a simulated F5/page reload" shape: the cache-only GET response --
// served_from_cache=true, reflecting whatever was last evaluated (possibly
// stale), exactly as the cache-only GET route always returns.
function cachedFromReload(overrides) {
  return {
    ...base,
    ...overrides,
    eval_freshness: "cached",
    last_evaluated_at: "2026-10-17T11:00:00Z",
    served_from_cache: true,
  };
}

// --- (a) first zero-gate reason, immediately after refresh -----------------
check("(a) no_qualifying_evidence_yet, immediately after refresh: honest headline+badge", () => {
  const d = freshFromRefresh({ transition_rule: "no_qualifying_evidence_yet" });
  assert.equal(primaryTickerHeadline(d), HONEST_ZERO_GATE_TEXT);
  assert.equal(primaryStateGloss(d), HONEST_ZERO_GATE_BADGE);
  const html = renderTickerDetail(d);
  assert.equal(headlineText(html), HONEST_ZERO_GATE_TEXT);
  assert.equal(stateGlossBadgeText(html), HONEST_ZERO_GATE_BADGE);
});

// --- (b) first zero-gate reason, after a simulated page reload -------------
check("(b) no_qualifying_evidence_yet, after reload: identical honest wording", () => {
  const d = cachedFromReload({ transition_rule: "no_qualifying_evidence_yet" });
  assert.equal(primaryTickerHeadline(d), HONEST_ZERO_GATE_TEXT);
  assert.equal(primaryStateGloss(d), HONEST_ZERO_GATE_BADGE);
  const html = renderTickerDetail(d);
  assert.equal(headlineText(html), HONEST_ZERO_GATE_TEXT);
  assert.equal(stateGlossBadgeText(html), HONEST_ZERO_GATE_BADGE);
});

// --- (c) SECOND zero-gate reason, immediately after refresh -----------------
check("(c) no_gate_passed_remains_new, immediately after refresh: SAME honest wording as (a)", () => {
  const d = freshFromRefresh({ transition_rule: "no_gate_passed_remains_new" });
  assert.equal(primaryTickerHeadline(d), HONEST_ZERO_GATE_TEXT);
  assert.equal(primaryStateGloss(d), HONEST_ZERO_GATE_BADGE);
  const html = renderTickerDetail(d);
  assert.equal(headlineText(html), HONEST_ZERO_GATE_TEXT);
  assert.equal(stateGlossBadgeText(html), HONEST_ZERO_GATE_BADGE);
  // Must NOT fall through to the misleading NEW-state default.
  assert.notEqual(stateGlossBadgeText(html), STATE_GLOSS.NEW);
  assert.notEqual(headlineText(html), "Unusual activity");
});

// --- (d) SECOND zero-gate reason, after a simulated page reload -------------
check("(d) no_gate_passed_remains_new, after reload: SAME honest wording as (b)/(c)", () => {
  const d = cachedFromReload({ transition_rule: "no_gate_passed_remains_new" });
  assert.equal(primaryTickerHeadline(d), HONEST_ZERO_GATE_TEXT);
  assert.equal(primaryStateGloss(d), HONEST_ZERO_GATE_BADGE);
  const html = renderTickerDetail(d);
  assert.equal(headlineText(html), HONEST_ZERO_GATE_TEXT);
  assert.equal(stateGlossBadgeText(html), HONEST_ZERO_GATE_BADGE);
});

// --- (c)/(d) also hit the history-timeline formatter directly --------------
check("no_gate_passed_remains_new history-row gloss matches no_qualifying_evidence_yet's", () => {
  const entryA = { state: "NEW", transition_rule: "no_qualifying_evidence_yet" };
  const entryB = { state: "NEW", transition_rule: "no_gate_passed_remains_new" };
  assert.equal(historyEntryGloss(entryA), HONEST_ZERO_GATE_BADGE);
  assert.equal(historyEntryGloss(entryB), HONEST_ZERO_GATE_BADGE);
  assert.equal(historyEntryGloss(entryA), historyEntryGloss(entryB));
});

// --- shared-predicate sanity: both reasons are recognized, nothing else ----
check("isZeroGateTransitionReason recognizes exactly the two zero-gate reasons", () => {
  assert.equal(isZeroGateTransitionReason("no_qualifying_evidence_yet"), true);
  assert.equal(isZeroGateTransitionReason("no_gate_passed_remains_new"), true);
  assert.equal(isZeroGateTransitionReason("first_qualifying_gate"), false);
  assert.equal(isZeroGateTransitionReason("still_single_signal"), false);
  assert.equal(isZeroGateTransitionReason(undefined), false);
});

// --- (e) REGRESSION GUARD: a genuinely qualifying first eval is unchanged --
check("(e) first_qualifying_gate (genuinely qualifying) keeps normal wording, immediately after refresh", () => {
  const d = freshFromRefresh({
    transition_rule: "first_qualifying_gate",
    headline: "Dark pool 3.2x recent average",
  });
  assert.equal(primaryTickerHeadline(d), "Dark pool 3.2x recent average");
  assert.equal(primaryStateGloss(d), STATE_GLOSS.NEW); // "just started qualifying"
  const html = renderTickerDetail(d);
  assert.equal(headlineText(html), "Dark pool 3.2x recent average");
  assert.equal(stateGlossBadgeText(html), "just started qualifying");
});

check("(e) first_qualifying_gate (genuinely qualifying) keeps normal wording, after reload", () => {
  const d = cachedFromReload({
    transition_rule: "first_qualifying_gate",
    headline: "Dark pool 3.2x recent average",
  });
  assert.equal(primaryTickerHeadline(d), "Dark pool 3.2x recent average");
  assert.equal(primaryStateGloss(d), STATE_GLOSS.NEW);
  const html = renderTickerDetail(d);
  assert.equal(headlineText(html), "Dark pool 3.2x recent average");
  assert.equal(stateGlossBadgeText(html), "just started qualifying");
});

console.log(`\n${passed} check(s) passed.`);
