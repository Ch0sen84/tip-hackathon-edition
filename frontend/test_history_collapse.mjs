// Focused unit tests for collapseHistory() and the bounded-height
// expandable-details behavior added in the UX-correction pass (P0).
//
// Pure-function tests: no backend required, no network calls. Same
// plain-Node-script pattern as the rest of this repo's harnesses (no test
// framework by design) -- constructs single/multi-transition/long-
// repeated-state history arrays directly and asserts against
// collapseHistory()'s real output, plus the rendered HTML that consumes it
// (renderHistoryChip for Market Radar, renderLifecycleTimeline for Ticker
// Analysis) to confirm both actually use the shared collapsed
// representation rather than two separate re-implementations.
//
//   node frontend/test_history_collapse.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const coreSrc = fs.readFileSync(path.join(__dirname, "app.core.js"), "utf8");

// app.core.js has zero top-level DOM access (only function/const
// declarations run at load time -- see its own module docstring), so it
// can be evaluated with no document/window stubs at all for testing its
// pure helpers directly.
(0, eval)(
  coreSrc +
    "\nglobalThis.__exports = { collapseHistory, renderSegmentProgression, renderHistoryChip, renderLifecycleTimeline, formatSegmentLabel };"
);
const lib = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`PASS: ${name}`);
    passed++;
  } catch (e) {
    console.log(`FAIL: ${name} -- ${e.message}`);
  }
}

function mkEntry(state, scan_date, any_gate_passed = true) {
  return { state, scan_date, any_gate_passed, created_at: `${scan_date}T00:00:00Z` };
}

console.log("=== collapseHistory: empty / single-entry history ===");
check("empty history collapses to zero segments", () => {
  assert.deepEqual(lib.collapseHistory([]), []);
  assert.deepEqual(lib.collapseHistory(undefined), []);
});
check("single-entry history collapses to exactly one segment, scanCount 1", () => {
  const segs = lib.collapseHistory([mkEntry("NEW", "2026-10-10")]);
  assert.equal(segs.length, 1);
  assert.equal(segs[0].state, "NEW");
  assert.equal(segs[0].scanCount, 1);
  assert.equal(segs[0].entries.length, 1);
});

console.log("\n=== collapseHistory: multi-transition history (several distinct state changes) ===");
check("a real multi-transition history produces one segment PER transition, never merged across one", () => {
  const history = [
    mkEntry("NEW", "2026-10-08"),
    mkEntry("BUILDING", "2026-10-09"),
    mkEntry("BUILDING", "2026-10-10"),
    mkEntry("CONFIRMED", "2026-10-11"),
    mkEntry("CONFIRMED", "2026-10-12"),
    mkEntry("CONFIRMED", "2026-10-13"),
  ];
  const segs = lib.collapseHistory(history);
  assert.deepEqual(segs.map((s) => s.state), ["NEW", "BUILDING", "CONFIRMED"]);
  assert.deepEqual(segs.map((s) => s.scanCount), [1, 2, 3]);
  assert.equal(segs[1].firstDate, "2026-10-09");
  assert.equal(segs[1].lastDate, "2026-10-10");
});
check("a state that recurs AFTER a real transition away from it starts a NEW segment (never merged across the gap)", () => {
  // A, A, B, A -- the second "A" run must NOT be merged with the first.
  const history = [
    mkEntry("BUILDING", "2026-10-08"),
    mkEntry("BUILDING", "2026-10-09"),
    mkEntry("FADING", "2026-10-10"),
    mkEntry("BUILDING", "2026-10-11"),
  ];
  const segs = lib.collapseHistory(history);
  assert.deepEqual(segs.map((s) => s.state), ["BUILDING", "FADING", "BUILDING"]);
  assert.deepEqual(segs.map((s) => s.scanCount), [2, 1, 1]);
});

console.log("\n=== collapseHistory: long repeated-state history (many consecutive same-state scans) ===");
check("50 consecutive identical-state scans collapse to exactly ONE segment with scanCount 50", () => {
  const history = Array.from({ length: 50 }, (_, i) =>
    mkEntry("CONFIRMED", `2026-09-${String((i % 28) + 1).padStart(2, "0")}`));
  const segs = lib.collapseHistory(history);
  assert.equal(segs.length, 1);
  assert.equal(segs[0].scanCount, 50);
  assert.equal(segs[0].entries.length, 50);
});
check("collapsing is lossless -- summing scanCount across all segments equals the real entry count, for every shape tested above", () => {
  const history = [
    ...Array.from({ length: 12 }, (_, i) => mkEntry("BUILDING", `2026-10-0${(i % 9) + 1}`)),
    mkEntry("CONFIRMED", "2026-10-11"),
    ...Array.from({ length: 30 }, (_, i) => mkEntry("CONFIRMED", `2026-10-${12 + (i % 15)}`)),
  ];
  const segs = lib.collapseHistory(history);
  const total = segs.reduce((n, s) => n + s.scanCount, 0);
  assert.equal(total, history.length);
  // Re-expanding each segment must show the exact real entries, never a
  // derived approximation.
  const reExpanded = segs.flatMap((s) => s.entries);
  assert.deepEqual(reExpanded, history);
});

console.log("\n=== renderHistoryChip (Market Radar): collapsed, capped progression ===");
check("Radar chip collapses dozens of repeated-state badges into one compact segment chip", () => {
  const states = Array.from({ length: 24 }, () => "CONFIRMED");
  const html = lib.renderHistoryChip(states);
  const stepCount = (html.match(/class="hc-step/g) || []).length;
  assert.equal(stepCount, 1, `expected exactly 1 collapsed step, got ${stepCount} in: ${html}`);
  assert.ok(html.includes("24 scans") || html.includes("(24"), html);
});
check("Radar chip with <2 raw entries still renders nothing (unchanged, never a fabricated 1-step history)", () => {
  assert.equal(lib.renderHistoryChip([]), "");
  assert.equal(lib.renderHistoryChip(["NEW"]), "");
});
check("Radar chip caps visible segments and shows a '+N earlier' affordance beyond 5 real transitions", () => {
  const states = ["NEW", "BUILDING", "CONFIRMED", "FADING", "BUILDING", "CONFIRMED", "STABLE"]; // 7 real transitions
  const html = lib.renderHistoryChip(states);
  const stepCount = (html.match(/class="hc-step/g) || []).length;
  assert.equal(stepCount, 5, `expected 5 visible segments (capped), got ${stepCount}`);
  assert.ok(html.includes("hp-more") && html.includes("+2 earlier"), html);
});

console.log("\n=== renderLifecycleTimeline (Ticker Analysis): collapsed summary + bounded expandable full history ===");
check("empty history preserves the exact P7 minimal-state message", () => {
  const html = lib.renderLifecycleTimeline([], "NEW");
  assert.ok(html.includes("No scan history recorded yet"));
  assert.ok(!html.includes("<details"));
});
check("single-entry history renders the minimal single-step state, no progression arrows, no <details>", () => {
  const html = lib.renderLifecycleTimeline([mkEntry("NEW", "2026-10-13")], "NEW");
  assert.ok(html.includes("timeline-single"));
  assert.ok(!html.includes("hc-arrow"));
  assert.ok(!html.includes("<details"));
});
check("a long repeated-state history (many consecutive same-state scans) renders ONE compact segment, not one row per scan", () => {
  const history = [
    mkEntry("NEW", "2026-10-01"),
    ...Array.from({ length: 40 }, (_, i) => mkEntry("CONFIRMED", `2026-10-${String(2 + (i % 27)).padStart(2, "0")}`)),
  ];
  const html = lib.renderLifecycleTimeline(history, "CONFIRMED");
  const stepCount = (html.match(/class="hc-step/g) || []).length;
  assert.equal(stepCount, 2, `expected 2 collapsed segments (NEW, CONFIRMED), got ${stepCount}`);
  assert.ok(html.includes("current"), "current segment should be labeled current");
});
check("the full real scan-by-scan history is reachable via a bounded-height expandable <details> panel, with every real entry present", () => {
  const history = [
    mkEntry("NEW", "2026-10-01"),
    ...Array.from({ length: 40 }, (_, i) => mkEntry("CONFIRMED", `2026-10-${String(2 + (i % 27)).padStart(2, "0")}`)),
  ];
  const html = lib.renderLifecycleTimeline(history, "CONFIRMED");
  assert.ok(html.includes("<details class=\"how history-details\">"), "no expandable details panel found");
  assert.ok(html.includes("history-details-body"), "details body missing the bounded-height/scroll class");
  assert.ok(html.includes(`Full real scan-by-scan history (${history.length} entries)`));
  const rawStepCount = (html.match(/class="timeline-step"/g) || []).length;
  assert.equal(rawStepCount, history.length, "expandable panel must contain every real raw entry, losslessly");
});
check("the main collapsed summary never contains the full 40-row raw list outside the <details> panel", () => {
  const history = Array.from({ length: 40 }, (_, i) => mkEntry("CONFIRMED", `2026-10-${String(1 + (i % 28)).padStart(2, "0")}`));
  const html = lib.renderLifecycleTimeline(history, "CONFIRMED");
  const beforeDetails = html.split("<details")[0];
  assert.ok(!beforeDetails.includes("timeline-step-gloss"), "a raw per-scan row leaked outside the collapsed summary / details panel");
});

console.log(`\n${passed} check(s) passed.`);
const total = 14;
if (passed !== total) {
  console.log(`EXPECTED ${total}, GOT ${passed}`);
  process.exit(1);
}
