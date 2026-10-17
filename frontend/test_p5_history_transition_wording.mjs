// Independent regression for the third zero-gate display location: the
// Signal Lifecycle history rows (not the primary headline or state badge).
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const core = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
const exportsBlock = `\n;globalThis.__historyTest={renderLifecycleTimeline,historyEntryGloss};`;
(0, eval)(core + exportsBlock);
const { renderLifecycleTimeline, historyEntryGloss } = globalThis.__historyTest;
let passed = 0;
function check(name, fn) { try { fn(); console.log(`PASS: ${name}`); passed++; } catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; } }

const history = [
  {scan_date:"2026-10-14", state:"NEW", any_gate_passed:false, transition_rule:"no_qualifying_evidence_yet"},
  {scan_date:"2026-10-15", state:"BUILDING", any_gate_passed:true, transition_rule:"new_independent_signal"},
  {scan_date:"2026-10-16", state:"NEW", any_gate_passed:true, transition_rule:"first_qualifying_gate"},
];
const html = renderLifecycleTimeline(history, "NEW");

check("each history entry formats from its own transition_rule", () => {
  assert.equal(historyEntryGloss(history[0]), "no qualifying evidence yet");
  assert.equal(historyEntryGloss(history[1]), "one qualifying signal so far, not yet confirmed");
  assert.equal(historyEntryGloss(history[2]), "just started qualifying");
});
check("the zero-gate HISTORY ROW is honest and distinct from the headline/badge tests", () => {
  const row = html.match(/<div class="timeline-step">[\s\S]*?2026-10-14[\s\S]*?<\/div>\s*<\/div>\s*<\/div>/)?.[0] || "";
  assert.match(row, /no qualifying evidence yet/i);
  assert.doesNotMatch(row, /just started qualifying/i);
});
check("a separate qualifying NEW history entry keeps the qualifying wording", () => {
  const last = html.lastIndexOf("2026-10-16");
  assert.ok(last >= 0);
  assert.match(html.slice(last, last + 400), /just started qualifying/i);
});
console.log(`\n${passed} check(s) passed.`);
