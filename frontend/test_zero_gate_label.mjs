// Targeted regression test for the zero-gate label correction (Chad-
// approved, 2026-10-16, final item on ux-nav-watchlist-reliability).
//
// Backend's determine_transition() now returns reason
// "no_qualifying_evidence_yet" (state still "NEW") on a ticker's FIRST
// evaluation when zero gates passed, instead of the misleading
// "first_qualifying_gate" (confirmed real-world defect: ARTNA/MSEX/CZWI
// on Windows QA). This test proves the frontend's TRANSITION_RULE_GLOSS
// glossary used to render that reason string:
//   - has an honest, standalone entry for "no_qualifying_evidence_yet"
//     that does NOT imply qualification happened
//   - leaves the pre-existing "first_qualifying_gate" entry completely
//     unchanged (still used correctly for a real qualifying first gate)
// Loads the REAL frontend/app.core.js (no reimplementation).
//
//   node frontend/test_zero_gate_label.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { TRANSITION_RULE_GLOSS };");
const { TRANSITION_RULE_GLOSS } = globalThis.__exports;

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

check("no_qualifying_evidence_yet has an honest, non-qualifying entry", () => {
  const text = TRANSITION_RULE_GLOSS["no_qualifying_evidence_yet"];
  assert.ok(text, "expected a glossary entry for no_qualifying_evidence_yet");
  assert.equal(text, "No qualifying signal detected yet.");
  // Must not imply qualification happened.
  assert.ok(!/qualified/i.test(text), `must not say "qualified": ${text}`);
  assert.ok(!/just started qualifying/i.test(text), `must not say "just started qualifying": ${text}`);
});

check("first_qualifying_gate entry is unchanged by this round", () => {
  assert.equal(TRANSITION_RULE_GLOSS["first_qualifying_gate"], "First time this ticker qualified.");
});

check("the two reason strings map to genuinely distinct wording", () => {
  assert.notEqual(
    TRANSITION_RULE_GLOSS["no_qualifying_evidence_yet"],
    TRANSITION_RULE_GLOSS["first_qualifying_gate"]
  );
});

console.log(`\n${passed} check(s) passed.`);
