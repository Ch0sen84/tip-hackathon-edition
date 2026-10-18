// REAL BUG FIX regression guard (2026-10-14, reported live on NVDA):
// nearestBarTime() used to silently clamp any marker dated AFTER every
// fetched candle onto the LAST candle -- every out-of-range marker (e.g.
// today's real-time flow alerts while today's daily bar hasn't closed/
// finalized yet) collapsing onto one bar at the chart's right edge. Fixed
// to return null (marker excluded, not clamped) in that case.
//
// REAL BUG FIX regression guard (chart-data-integrity-fixes, 2026-10-18,
// reported live on CHAD-PC's final hackathon video capture): the
// MIRROR-IMAGE bug on the other edge. A marker dated BEFORE every
// fetched candle (e.g. a real October-15 evidence observation, when the
// 1D chart's visible range starts at the FIRST October-16 candle) used
// to fall back to the FIRST candle -- an "intentional" behavior at the
// time -- producing a false spike that misattributed October-15
// evidence to October-16. Re-examined and fixed the same way as the
// postdate case: return null (excluded, never clamped).
//
//   node frontend/test_chart_markers.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

// WINDOWS PATH BUG FIX (2026-10-15): `new URL(..., import.meta.url).pathname`
// is NOT a valid native path on Windows (a file:// URL's .pathname keeps a
// leading "/" before the drive letter, e.g. "/C:/Users/..."; concatenating
// that into further path logic is exactly how a "C:\C:\..." double-drive
// path gets built). fileURLToPath() is the documented, cross-platform-safe
// conversion (already used correctly in test_history_collapse.mjs) -- use
// it everywhere a module-relative file is read.
const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
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

check("THE OCT-15-INTO-OCT-16 BUG: marker BEFORE the whole series -> excluded (null), never clamped to the first bar", () => {
  assert.equal(nearestBarTime("2026-10-01", candleDates), null);
  assert.equal(nearestBarTime("2026-10-09", candleDates), null);
});

check("marker exactly on the first candle's date -> matches, not excluded", () => {
  assert.equal(nearestBarTime("2026-10-10", candleDates), "2026-10-10");
});

check("THE ACTUAL BUG: marker AFTER the last candle -> excluded (null), never clamped to the last bar", () => {
  assert.equal(nearestBarTime("2026-10-14", candleDates), null);
  assert.equal(nearestBarTime("2026-10-20", candleDates), null);
});

// Market-session-boundary edge case (the exact category underlying the
// Oct-15-into-Oct-16 bug): intraday (numeric UTCTimestamp) candles for a
// SINGLE day only, an event timestamped just before vs. just at/after
// that day's first bar.
const intradayCandleDates = [1000, 1300, 1600, 1900]; // one day's 5-min-ish bars, seconds epoch
check("intraday: event exactly AT the first bar's start -> matches, not excluded", () => {
  assert.equal(nearestBarTime(1000, intradayCandleDates), 1000);
});
check("intraday: event 1 second BEFORE the first bar's start (prior session) -> excluded (null), never clamped", () => {
  assert.equal(nearestBarTime(999, intradayCandleDates), null);
});
check("intraday: event just AFTER the last bar's own window -> excluded (null), never clamped", () => {
  assert.equal(nearestBarTime(1900 + 300, intradayCandleDates), null); // bar width inferred as 300s; this is the start of a NEXT (unfetched) bar
});
check("intraday: event inside the last bar's own window -> matches the last bar", () => {
  assert.equal(nearestBarTime(1900 + 250, intradayCandleDates), 1900);
});

check("empty candle series -> never crashes, returns null", () => {
  assert.equal(nearestBarTime("2026-10-13", []), null);
});

check("dateOnly truncates an ISO datetime to just the date portion", () => {
  assert.equal(dateOnly("2026-10-13T14:22:01Z"), "2026-10-13");
  assert.equal(dateOnly("2026-10-13"), "2026-10-13");
});

console.log(`\n${passed} check(s) passed.`);
