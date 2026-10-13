// Targeted regression test for the "Transition: X -> X" wording fix.
//
// Pure presentation fix requested by the user: transitionLabel() in
// app.core.js must say
//   - "Initial detection: NEW"        when there is no previous_state at all
//   - "Status: Still CONFIRMED"       when previous_state === state (no real change)
//   - "Transition: OLD -> NEW"        when the state genuinely changed
// This does not touch signal_engine.py, detection thresholds, or stored
// history -- it only changes how an already-correct previous_state/state
// pair is WORDED. Loads the REAL frontend/app.core.js (no reimplementation).
//
//   node frontend/test_transition_label.mjs

import assert from "node:assert/strict";
import fs from "node:fs";

const src = fs.readFileSync(new URL("./app.core.js", import.meta.url).pathname, "utf8");
(0, eval)(src + "\nglobalThis.__exports = { transitionLabel };");
const { transitionLabel } = globalThis.__exports;

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

check("no previous_state at all -> Initial detection wording", () => {
  assert.equal(transitionLabel(null, "NEW"), "Initial detection: NEW");
  assert.equal(transitionLabel(undefined, "NEW"), "Initial detection: NEW");
  assert.equal(transitionLabel("", "NEW"), "Initial detection: NEW");
});

check("previous_state === state -> Status: Still X wording (the actual reported bug)", () => {
  assert.equal(transitionLabel("CONFIRMED", "CONFIRMED"), "Status: Still CONFIRMED");
  assert.equal(transitionLabel("BUILDING", "BUILDING"), "Status: Still BUILDING");
  assert.equal(transitionLabel("FADING", "FADING"), "Status: Still FADING");
});

check("previous_state !== state -> real Transition wording is preserved exactly", () => {
  assert.equal(transitionLabel("NEW", "BUILDING"), "Transition: NEW \u2192 BUILDING");
  assert.equal(transitionLabel("BUILDING", "CONFIRMED"), "Transition: BUILDING \u2192 CONFIRMED");
  assert.equal(transitionLabel("CONFIRMED", "FADING"), "Transition: CONFIRMED \u2192 FADING");
});

check("case-sensitive equality -- does not falsely collapse differently-cased states", () => {
  assert.notEqual(transitionLabel("confirmed", "CONFIRMED"), "Status: Still CONFIRMED");
});

console.log(`\n${passed} check(s) passed.`);
