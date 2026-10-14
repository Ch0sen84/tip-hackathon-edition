// REAL BUG FIX regression guard (2026-10-14, reported live on NVDA):
// nearestBarTime() used to silently clamp any marker dated AFTER every
// fetched candle onto the LAST candle -- every out-of-range marker (e.g.
// today's real-time flow alerts while today's daily bar hasn't closed/
// finalized yet) collapsing onto one bar at the chart's right edge. Fixed
// to return null (marker excluded, not clamped) in that case; the
// "marker predates the series" fallback to the FIRST candle is untouched
// (that one's intentional and documented).
//
//   node frontend/test_chart_markers.mjs

import assert from "node:assert/strict";
import fs from "node:fs";

const src = fs.readFileSync(new URL("./app.core.js", import.meta.url).pathname, "utf8");
(0, eval)(src + "\nglobalThis.__exports = { nearestBarTime, dateOnly };");
const { nearestBarTime, dateOnly } = globalThis.__exports;

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

const candleDates = ["2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]; // last candle = 2026-10-13

check("marker exactly on the last candle's date -> matches, not excluded", () => {
  assert.equal(nearestBarTime("2026-10-13", candleDates), "2026-10-13");
});

check("marker between two candle dates -> maps to the latest candle <= it (non-trading-day case)", () => {
  assert.equal(nearestBarTime("2026-10-11T15:30:00Z".slice(0, 10), candleDates), "2026-10-11");
});

check("marker BEFORE the whole series -> falls back to the FIRST candle (unchanged, intentional)", () => {
  assert.equal(nearestBarTime("2026-10-01", candleDates), "2026-10-10");
});

check("THE ACTUAL BUG: marker AFTER the last candle -> excluded (null), never clamped to the last bar", () => {
  assert.equal(nearestBarTime("2026-10-14", candleDates), null);
  assert.equal(nearestBarTime("2026-10-20", candleDates), null);
});

check("empty candle series -> never crashes, returns null", () => {
  assert.equal(nearestBarTime("2026-10-13", []), null);
});

check("dateOnly truncates an ISO datetime to just the date portion", () => {
  assert.equal(dateOnly("2026-10-13T14:22:01Z"), "2026-10-13");
  assert.equal(dateOnly("2026-10-13"), "2026-10-13");
});

console.log(`\n${passed} check(s) passed.`);
