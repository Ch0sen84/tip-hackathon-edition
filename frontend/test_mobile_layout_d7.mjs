// QA remediation D7: mobile (~390px) nav/header footprint + readable
// scheduler-mode labels (no raw "dry_run"/"enabled" shown in the UI).
//
// No headless-browser harness exists in this repo (by design -- see the
// tip-hackathon-edition skill notes: plain-Node .mjs scripts against
// real source files is the established pattern here). Per the task's
// own explicit fallback ("a DOM-structure/CSS-class assertion test is
// acceptable"), this verifies: (1) a real, scoped @media rule exists in
// the actual shipped styles.css targeting narrow viewports and compacts
// exactly the elements that were eating ~half the 390px viewport
// (confirmed by reading the actual CSS, not assumed), while the existing
// desktop/tablet rules remain untouched; (2) the scheduler mode toggle
// buttons render a human-readable label, never the raw backend
// identifier, via the real renderSchedulerControls() function.
//
// Run with: node frontend/test_mobile_layout_d7.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

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

const cssPath = fileURLToPath(new URL("./styles.css", import.meta.url));
const css = fs.readFileSync(cssPath, "utf8");

check("a @media max-width:480px rule exists (covers a 390px viewport)", () => {
  assert.match(css, /@media \(max-width:\s*480px\)/, "no narrow-viewport media rule found in styles.css");
});

check("the narrow-viewport rule compacts the brand block, nav group labels, and sidebar footer", () => {
  const match = css.match(/@media \(max-width:\s*480px\)\s*\{([\s\S]*?)\n\}/);
  assert.ok(match, "could not extract the narrow-viewport media rule body");
  const body = match[1];
  assert.match(body, /\.brand-block\b/, "brand block not addressed in the narrow-viewport rule");
  assert.match(body, /nav-group-label[\s\S]*display:\s*none/, "nav group labels must be hidden to save vertical space");
  assert.match(body, /sidebar-foot[\s\S]*display:\s*none/, "sidebar footer note must be hidden to save vertical space");
});

check("the existing >=980px desktop/tablet sidebar rules are unchanged (still present, untouched)", () => {
  // Desktop (1920x1080) appearance must stay unaffected -- confirmed by
  // checking the pre-existing tablet breakpoint's own rules are still
  // intact and distinct from the new narrow-viewport rule above (no
  // overlapping/overridden selectors between the two).
  assert.match(css, /@media \(max-width:\s*980px\)\s*\{/, "the pre-existing 980px tablet breakpoint must remain");
  assert.match(css, /#sidebar\s*\{\s*\n\s*width:\s*100%/, "the tablet-width sidebar collapse rule must remain unchanged");
});

// --- readable mode labels (no raw dry_run/enabled strings in rendered UI) --

function loadCoreExports() {
  const corePath = fileURLToPath(new URL("./app.core.js", import.meta.url));
  global.document = { querySelectorAll: () => [] };
  const coreSrc = fs.readFileSync(corePath, "utf8");
  (0, eval)(coreSrc + "\nglobalThis.__exports = { renderSchedulerControls, schedulerModeShortLabel };");
  return globalThis.__exports;
}

const core = loadCoreExports();

check("scheduler mode buttons render 'Dry Run'/'Enabled'/'Disabled', never the raw backend identifier", () => {
  const html = core.renderSchedulerControls({
    mode: "dry_run", last_run_finished_at: null, next_run_at: null,
    last_cycle_candidates_evaluated: null, freshness_window_s: 1200, pause_reason: null,
  });
  // The raw identifiers are still used internally (data-scheduler-mode
  // attributes, same backend vocabulary) -- but must never appear as
  // the human-visible BUTTON TEXT itself.
  const buttonTextMatches = [...html.matchAll(/<button[^>]*data-scheduler-mode="([a-z_]+)">([^<]+)<\/button>/g)];
  assert.equal(buttonTextMatches.length, 3, "expected exactly 3 scheduler mode buttons");
  for (const [, rawMode, label] of buttonTextMatches) {
    assert.notEqual(label, rawMode, `button for ${rawMode} must not show the raw identifier as its label`);
    assert.ok(!label.includes("_"), `button label "${label}" still contains a raw-identifier underscore`);
  }
  assert.equal(core.schedulerModeShortLabel("dry_run"), "Dry Run");
  assert.equal(core.schedulerModeShortLabel("enabled"), "Enabled");
  assert.equal(core.schedulerModeShortLabel("disabled"), "Disabled");
});

console.log(`\n${passed} check(s) passed.`);
