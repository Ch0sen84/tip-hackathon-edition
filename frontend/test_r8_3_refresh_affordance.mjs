// R8-3 regression test (chart-data-integrity-fixes round 9, 2026-10-18):
// the "Refresh last bar" affordance must appear exactly when a range is
// otherwise fully cached/satisfied AND the backend reports the trailing
// daily bar is not yet final (ohlc.latest_bar_is_final === false), and
// must NOT appear in any other case (final bar, intraday 1D, or a
// genuinely partial range -- which already gets its own "Load more"
// affordance from the P2 fix). Also verifies the click fires a SCOPED
// refresh request (a small `limit`, no `range`), never the full range.
//
//   node frontend/test_r8_3_refresh_affordance.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._style = {}; this.listeners = {}; this.disabled = false; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._html = v; }
  get textContent() { return this._html; }
  get style() { return this._style; }
  get isConnected() { return true; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  async click() { for (const fn of (this.listeners.click || [])) await fn(); }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  classList = { add() {}, remove() {}, toggle() {} };
}

// Same "child id only resolves while actually present in the owning
// container's current innerHTML" real-DOM semantics as test_p2_chart_range_coverage.mjs.
const NOTE_CHILD_IDS = ["loadMoreChartDataBtn", "loadMoreChartDataStatus", "refreshLastBarBtn", "refreshLastBarStatus"];
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new FakeEl(id));
  return elements.get(id);
}
function liveChildEl(id) {
  if (!NOTE_CHILD_IDS.includes(id)) return el(id);
  const html = el("chartRangeNote")._html;
  if (!html.includes(`id="${id}"`)) { elements.delete(id); return null; }
  return el(id);
}
global.document = {
  getElementById: (id) => liveChildEl(id),
  querySelectorAll: () => [],
  querySelector: (sel) => liveChildEl(sel),
};
global.location = { pathname: "/", search: "", hash: "" };
global.window = { addEventListener() {}, TIP_NAV_HANDLERS: null };
global.ResizeObserver = class { observe() {} };
global.LightweightCharts = {
  createChart: () => ({
    addCandlestickSeries: () => ({ setData() {}, setMarkers() {} }),
    addHistogramSeries: () => ({ setData() {}, priceScale: () => ({ applyOptions() {} }) }),
    timeScale: () => ({ fitContent() {} }),
    applyOptions() {},
    priceScale: () => ({ applyOptions() {} }),
  }),
};
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };

function buildCandles(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const d = new Date(Date.UTC(2026, 0, 1 + i));
    out.push({ t: d.toISOString().slice(0, 10), o: 100 + i, h: 101 + i, l: 99 + i, c: 100.5 + i, v: 1000 + i });
  }
  return out;
}
const candles65 = buildCandles(65); // a genuinely fully-satisfied 3M range (65 trading days)

// Mutable fixture knob -- flips between the two real backend states
// this test exercises; `fetchLog` records every call so the scoping
// assertion can inspect the exact query string used.
let latestBarIsFinal = false;
const fetchLog = [];

global.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  fetchLog.push({ method, url: String(url) });
  const u = new URL(String(url), "http://fake.local");
  const isRefresh = /\/ohlc\/refresh$/.test(u.pathname);
  if (isRefresh) {
    // A real scoped refresh (R8-3) must mark the bar final going
    // forward -- mirrors the real backend's R8-2 grace-period-then-
    // final behavior closely enough for this frontend-only test.
    latestBarIsFinal = true;
  }
  const body = {
    ticker: "AMD", timeframe: "1d", range: "3M", candles: candles65,
    source: "cache", negative_volume_bars_clamped: 0, unavailable: false, reason: null,
    markers: [], candle_count: candles65.length,
    earliest_candle: candles65[0].t, latest_candle: candles65[candles65.length - 1].t,
    range_fully_satisfied: true, available_span_days: 90,
    raw_records_requested: null, raw_records_returned: null, regular_session_records_retained: null,
    served_from_cache: !isRefresh, cached_at: "2026-10-18T12:00:00+00:00",
    latest_bar_is_final: latestBarIsFinal,
  };
  return { ok: true, status: 200, json: async () => body };
};

const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
const liveSrc = fs.readFileSync(fileURLToPath(new URL("./app.live.js", import.meta.url)), "utf8");
(0, eval)(coreSrc + "\n" + liveSrc + `\nglobalThis.__exports = { loadOhlcChart };`);
const liveMod = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}
async function checkAsync(name, fn) {
  try { await fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

["ohlcChartContainer", "chartRangeNote", "chartVolumeLabel"].forEach(el);

async function main() {
  console.log("=== Case: range fully satisfied but latest bar NOT final -> affordance appears ===");
  latestBarIsFinal = false;
  await liveMod.loadOhlcChart("AMD", "3M", 0);
  check("the 'Refresh last bar' button IS rendered", () => {
    const note = el("chartRangeNote").innerHTML;
    assert.ok(note.includes("refreshLastBarBtn"), note);
    assert.ok(note.includes("may not be final"), note);
  });
  check("the button has a real click handler wired", () => {
    assert.ok((liveChildEl("refreshLastBarBtn").listeners.click || []).length > 0);
  });
  check("the generic 'Load more' button must NOT also appear (different, already-handled case)", () => {
    assert.ok(!el("chartRangeNote").innerHTML.includes("loadMoreChartDataBtn"));
  });

  console.log("\n=== Case: clicking it issues a SCOPED refresh (small limit, no range) ===");
  fetchLog.length = 0;
  await liveChildEl("refreshLastBarBtn").click();
  await checkAsync("exactly one POST to ohlc/refresh, with a small limit and NO range param", async () => {
    const posts = fetchLog.filter((e) => e.method === "POST");
    assert.equal(posts.length, 1, JSON.stringify(fetchLog));
    const postUrl = new URL(posts[0].url, "http://fake.local");
    assert.ok(postUrl.pathname.endsWith("/ohlc/refresh"));
    assert.ok(!postUrl.searchParams.has("range"), `scoped refresh must never forward a range param: ${posts[0].url}`);
    const limit = Number(postUrl.searchParams.get("limit"));
    assert.ok(limit > 0 && limit <= 10, `expected a small scoped limit, got ${limit}`);
  });
  await checkAsync("after a successful scoped refresh, loadOhlcChart re-fetches the FULL range (GET), not just 5 bars", async () => {
    // Some background polling calls this test harness's app.live.js
    // module makes on its own (e.g. /api/settings, /api/radar) are
    // unrelated to this assertion -- only the OHLC GET matters here.
    const ohlcGets = fetchLog.filter((e) => e.method === "GET" && e.url.includes("/ohlc?"));
    assert.ok(ohlcGets.length >= 1, "expected a follow-up GET for the full range after the scoped POST");
    assert.ok(ohlcGets[ohlcGets.length - 1].url.includes("range=3M"), JSON.stringify(ohlcGets));
  });

  console.log("\n=== Case: latest bar now final -> affordance disappears on the next render ===");
  await liveMod.loadOhlcChart("AMD", "3M", 0); // latestBarIsFinal flipped true by the refresh above
  check("the 'Refresh last bar' button is NOT rendered once the bar is final", () => {
    const note = el("chartRangeNote").innerHTML;
    assert.ok(!note.includes("refreshLastBarBtn"), note);
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
