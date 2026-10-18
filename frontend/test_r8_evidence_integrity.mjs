// R8-1 regression guard (chart-data-integrity-fixes round 9, 2026-10-18,
// Claude Code real-data QA on port 8200): mountCandlestickChart() used to
// derive `allEvents` (fed to renderEvidencePanel's table/drill-down AND
// mountActivityTimeline) FROM the chart-plotting `groups` map, so any
// event nearestBarTime() excluded from the chart (no in-range candle --
// either before the first fetched candle or after the last) was ALSO
// silently dropped from the evidence table, the individual-observations
// drill-down, and the Activity Timeline. Real-data QA measured this
// directly: SPY -561 observations, NVDA -575, matching the October-15
// lower-bound-exclusion count exactly.
//
// Fixture: a single day (Oct 17) of intraday candles, with evidence
// markers spanning TWO real dates (Oct 16 -- entirely before the first
// candle, and Oct 17 -- in range). Asserts, separately:
//   (a) the chart's plotted/histogram event count only counts in-range
//       (Oct 17) events,
//   (b) the evidence table's event count equals the TOTAL fetched event
//       count (Oct 16 + Oct 17) regardless of range,
//   (c) the Activity Timeline shows observations from both dates,
//   (d) the disclosure text's own claim (total/plotted/excluded numbers)
//       matches what's actually retained -- no double counting between
//       plotted and excluded.
//
//   node frontend/test_r8_evidence_integrity.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

// --- minimal DOM stub (same pattern as test_manual_bugfixes.mjs) ------
class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._style = {}; this._nextSibling = null; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  get style() { return this._style; }
  addEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  classList = { contains: (c) => (this._html || "").includes(`class="${c}"`) };
  get nextElementSibling() { return this._nextSibling; }
  after(node) {
    node.remove = () => { if (this._nextSibling === node) this._nextSibling = null; };
    this._nextSibling = node;
  }
}
function fakeCreateElement() {
  const node = { className: "", _html: "", style: {} };
  Object.defineProperty(node, "innerHTML", { set(v) { this._html = v; }, get() { return this._html; } });
  node.classList = { contains: (c) => node.className.split(/\s+/).includes(c) };
  return node;
}
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new FakeEl(id));
  return elements.get(id);
}
global.document = {
  getElementById: (id) => el(id),
  querySelectorAll: () => [],
  querySelector: (sel) => el(sel),
  createElement: () => fakeCreateElement(),
};
global.window = { addEventListener: () => {} };
global.ResizeObserver = class { observe() {} };

// Minimal LightweightCharts fake: enough for mountCandlestickChart's
// candle/volume series + mountEvidenceHistogram's two histogram series +
// mountActivityTimeline's own second chart instance. Hover/click
// drill-down (mountEvidenceHistogram's subscribeCrosshairMove branch)
// safely no-ops because this fake has no root.getBoundingClientRect.
function makeFakeChart() {
  const seriesCalls = []; // every addHistogramSeries({...}) call's resulting setData, in creation order
  return {
    addCandlestickSeries: () => ({
      priceScale: () => ({ applyOptions: () => {} }),
      setData: () => {},
    }),
    addHistogramSeries: (opts) => {
      const rec = { opts, data: null };
      seriesCalls.push(rec);
      return {
        priceScale: () => ({ applyOptions: () => {} }),
        setData: (d) => { rec.data = d; },
      };
    },
    timeScale: () => ({ fitContent: () => {}, applyOptions: () => {}, height: () => 20 }),
    applyOptions: () => {},
    _seriesCalls: seriesCalls,
  };
}
let lastEvidenceChart = null; // captures the SAME chart instance mountCandlestickChart/mountEvidenceHistogram share
global.LightweightCharts = {
  createChart: (container) => {
    const chart = makeFakeChart();
    // mountActivityTimeline mounts its OWN separate chart on a different
    // container id (ends with "-chart"); only track the main candlestick
    // chart instance (container id === the id passed to
    // mountCandlestickChart) for the evidence-histogram assertions below.
    if (container && container.id === "ohlcChartContainer") lastEvidenceChart = chart;
    return chart;
  },
};

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { mountCandlestickChart, mountActivityTimeline };");
const { mountCandlestickChart } = globalThis.__exports;

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

// Fixture: one real trading day (Oct 17) of 5-minute intraday candles.
const candles = [
  { t: "2026-10-17T13:30:00Z", o: 100, h: 101, l: 99, c: 100.5, v: 1000 },
  { t: "2026-10-17T13:35:00Z", o: 100.5, h: 102, l: 100, c: 101.5, v: 1200 },
  { t: "2026-10-17T13:40:00Z", o: 101.5, h: 103, l: 101, c: 102, v: 900 },
];

// Markers across TWO real dates: 3 on Oct 16 (entirely before the first
// fetched candle -- the exact R8-1/lower-bound scenario), 3 on Oct 17
// (in range, genuinely plottable).
const markers = [
  { time: "2026-10-16T15:00:00Z", kind: "flow_alert", value: 500000, price: 99.1, label: "a" },
  { time: "2026-10-16T15:05:00Z", kind: "flow_alert", value: 600000, price: 99.2, label: "b" },
  { time: "2026-10-16T16:00:00Z", kind: "darkpool_trade", value: 700000, price: 99.3, label: "c" },
  { time: "2026-10-17T13:31:00Z", kind: "flow_alert", value: 800000, price: 100.6, label: "d" },
  { time: "2026-10-17T13:36:00Z", kind: "flow_alert", value: 900000, price: 101.6, label: "e" },
  { time: "2026-10-17T13:42:00Z", kind: "darkpool_trade", value: 1000000, price: 101.9, label: "f" },
];

mountCandlestickChart("ohlcChartContainer", { candles, markers });

const infoHtml = el("ohlcChartContainer").nextElementSibling ? el("ohlcChartContainer").nextElementSibling.innerHTML : "";
const timelineHtml = el("activityTimelineContainer").innerHTML;

check("(d) disclosure text states the real total/plotted/excluded split, no silent lie", () => {
  assert.ok(infoHtml.includes("6 total fetched observations"), `expected '6 total fetched observations' in: ${infoHtml.slice(0, 400)}`);
  assert.ok(infoHtml.includes("3 plotted on this chart"), `expected '3 plotted on this chart' in: ${infoHtml.slice(0, 400)}`);
  assert.ok(infoHtml.includes("3 fall outside that range"), `expected '3 fall outside that range' in: ${infoHtml.slice(0, 400)}`);
  assert.ok(infoHtml.includes("remain listed"), "disclosure must claim they remain listed -- and it must be true (checked below)");
});

check("(b) evidence table / drill-down total equals ALL fetched observations (6), not just the 3 plotted ones", () => {
  assert.ok(infoHtml.includes("Evidence by day (6 real events total)"), `expected 6-event summary header in: ${infoHtml.slice(0, 400)}`);
  assert.ok(infoHtml.includes("Individual observations (6)"), `expected 6 individual observations in: ${infoHtml.slice(0, 400)}`);
});

check("(c) Activity Timeline shows observations from BOTH real dates (Oct 16 and Oct 17), not just the plotted day", () => {
  assert.ok(timelineHtml.includes("2 real distinct date"), `expected 2-distinct-date coverage note in: ${timelineHtml}`);
});

check("(a) the chart's own plotted/histogram event count only counts in-range (Oct 17) events", () => {
  assert.ok(lastEvidenceChart, "expected mountCandlestickChart's own chart instance to have been captured");
  const flowCall = lastEvidenceChart._seriesCalls.find((s) => s.opts && s.opts.title === "Flow alerts");
  const dpCall = lastEvidenceChart._seriesCalls.find((s) => s.opts && s.opts.title === "Dark-pool");
  assert.ok(flowCall && dpCall, "expected both evidence histogram series to have been created");
  const flowTotal = (flowCall.data || []).reduce((sum, b) => sum + b.value, 0);
  const dpTotal = (dpCall.data || []).reduce((sum, b) => sum + b.value, 0);
  // Of the 6 real markers, only the 2 Oct-17 flow_alert and 1 Oct-17
  // darkpool_trade are in-range/plottable -- the 3 Oct-16 markers must
  // NEVER appear in the chart's own histogram bars.
  assert.equal(flowTotal, 2, `expected 2 plotted flow_alert events, got ${flowTotal}`);
  assert.equal(dpTotal, 1, `expected 1 plotted darkpool_trade event, got ${dpTotal}`);
  assert.equal(flowTotal + dpTotal, 3, "plotted total must equal allEvents.length - excludedCount (3), never double-counted against the excluded set");
});

console.log(`\n${passed} check(s) passed.`);
