// P2 regression test (ux-nav-watchlist-reliability round 10): chart
// range-coverage honesty, frontend side. Loads the REAL
// frontend/app.core.js + frontend/app.live.js (same eval-load pattern
// as test_navigation.mjs) with a STUBBED fetch (no real backend needed
// -- this test is about the client's OWN honesty/wiring, the backend
// contract itself is locked down in
// backend/tests/test_p2_ohlc_range_coverage_honesty.py). Drives a
// realistic 3M -> 1Y -> 1D -> 3M range-switch sequence and asserts:
//
//   * every range switch issues a GET, never a POST, to
//     /api/ticker/{t}/ohlc -- switching ranges must never silently
//     trigger a real UW fetch;
//   * when range_fully_satisfied is false (the 1Y-over-65-cached-bars
//     case), the rendered note (a) never claims upstream UW
//     availability it hasn't verified, (b) does say what's genuinely
//     cached, and (c) renders a real "Load more" button wired to the
//     EXACT existing POST /api/ticker/{t}/ohlc/refresh route;
//   * clicking that button fires exactly one POST to the refresh
//     route for the currently-selected range;
//   * switching to a range with zero cache (1D) shows the honest
//     chart-unavailable state, not a crash or a reused dataset;
//   * switching back to a previously-covered range (3M) shows the
//     plain "fully covered" note with no Load More button.
//
// Run standalone: node frontend/test_p2_chart_range_coverage.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

// Child ids that only genuinely exist in the DOM while they appear in
// one of these two containers' CURRENT innerHTML -- same real-DOM
// semantics as a browser (setting innerHTML elsewhere removes/replaces
// any button that was rendered inside it, so a stale id must resolve
// to null, exactly like production: wireLoadMoreChartDataButton's own
// `if (!btn) return;` guard relies on this). Without this, a fake
// getElementById that always returns a cached stub for any id ever
// seen would let a "rewire after success" call keep finding and
// re-arming a button that a real browser would have already thrown
// away, causing runaway duplicate-listener accumulation that a plain
// `for (...) await fn()` click-dispatch loop then runs forever on.
const NOTE_CHILD_IDS = ["loadMoreChartDataBtn", "loadMoreChartDataStatus"];
const CHART_CHILD_IDS = ["loadChartDataBtn", "loadChartDataStatus"];

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {}; this.disabled = false; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._html = v; }
  get textContent() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  get style() { return this._style; }
  get isConnected() { return true; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  async click() {
    for (const fn of (this.listeners.click || [])) await fn();
  }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  classList = { add() {}, remove() {}, toggle() {} };
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new FakeEl(id));
  return elements.get(id);
}
function liveChildEl(id) {
  const html = NOTE_CHILD_IDS.includes(id) ? el("chartRangeNote")._html
    : CHART_CHILD_IDS.includes(id) ? el("ohlcChartContainer")._html
    : null;
  if (html === null) return el(id); // not a tracked child id -- always-present container/global id
  if (!html.includes(`id="${id}"`)) {
    elements.delete(id); // real DOM would have discarded any stale listeners with the old node
    return null;
  }
  return el(id);
}
// querySelector/getElementById on "#id" style selectors used by
// loadOhlcChart's sibling helpers must resolve consistently -- find by
// looking up the id the same key the rest of this test uses (plain id
// strings, no "#" prefix used anywhere in app.live.js for these calls).
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
const _storage = new Map();
global.localStorage = {
  getItem: (k) => (_storage.has(k) ? _storage.get(k) : null),
  setItem: (k, v) => _storage.set(k, v),
  removeItem: (k) => _storage.delete(k),
};
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };

// --- fetch stub: a tiny fake server state machine -----------------------
// cache[ticker][timeframe] = array of candle objects, mirroring the
// REAL backend's IngestionCatalog semantics exactly: a POST refresh
// REPLACES/extends the cached set for that (ticker, timeframe); a GET
// only ever reads back whatever is already there, bounded by `limit`
// (most-recent-N), and NEVER performs a "live" fetch of its own.
const fakeCache = { AMD: {} };
const fetchLog = []; // { method, url }

function rangeToTimeframeAndCount(range) {
  if (range === "1D") return { timeframe: "5m", count: 200 };
  const table = { "1M": 22, "3M": 65, "6M": 130, "1Y": 252 };
  return { timeframe: "1d", count: table[range] };
}

function buildCandles(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const d = new Date(Date.UTC(2026, 0, 1 + i));
    out.push({ t: d.toISOString().slice(0, 10), o: 100 + i, h: 101 + i, l: 99 + i, c: 100.5 + i, v: 1000 + i });
  }
  return out;
}

global.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  fetchLog.push({ method, url: String(url) });
  const u = new URL(String(url), "http://fake.local");
  const m = u.pathname.match(/^\/api\/ticker\/([^/]+)\/ohlc(\/refresh)?$/);
  assert.ok(m, `unexpected fetch URL in this test: ${url}`);
  const ticker = m[1];
  const isRefresh = !!m[2];
  const range = u.searchParams.get("range");
  const { timeframe, count: wantCount } = rangeToTimeframeAndCount(range);
  fakeCache[ticker] = fakeCache[ticker] || {};

  if (isRefresh) {
    assert.equal(method, "POST", "the refresh route must only ever be called via POST");
    // Real UW only genuinely has 65 real daily bars for AMD in this
    // fixture (even when a longer range is explicitly requested) --
    // the realistic "short real history" case from the bug report.
    const realAvailable = timeframe === "1d" ? Math.min(wantCount, 65) : wantCount;
    fakeCache[ticker][timeframe] = buildCandles(realAvailable);
  }

  const candles = (fakeCache[ticker][timeframe] || []).slice(-wantCount);
  const unavailable = candles.length === 0;
  let rangeFullySatisfied = null;
  let availableSpanDays = null;
  if (range) {
    if (range === "1D") {
      rangeFullySatisfied = candles.length >= 50;
    } else {
      const expected = { "1M": 22, "3M": 65, "6M": 130, "1Y": 252 }[range];
      rangeFullySatisfied = candles.length >= Math.floor(expected * 0.9);
    }
    if (candles.length) {
      availableSpanDays = Math.round(
        (new Date(candles[candles.length - 1].t) - new Date(candles[0].t)) / 86400000
      );
    }
  }
  const body = {
    ticker, timeframe, range, candles,
    source: candles.length ? "cache" : null,
    negative_volume_bars_clamped: 0,
    unavailable,
    reason: unavailable ? "No chart data cached yet for this ticker/timeframe -- use Refresh Data to fetch real bars." : null,
    markers: [],
    candle_count: candles.length,
    earliest_candle: candles.length ? candles[0].t : null,
    latest_candle: candles.length ? candles[candles.length - 1].t : null,
    range_fully_satisfied: rangeFullySatisfied,
    available_span_days: availableSpanDays,
    raw_records_requested: null, raw_records_returned: null, regular_session_records_retained: null,
    served_from_cache: !isRefresh,
    cached_at: candles.length ? "2026-10-16T12:00:00+00:00" : null,
  };
  return { ok: true, status: 200, json: async () => body };
};

const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
const liveSrc = fs.readFileSync(fileURLToPath(new URL("./app.live.js", import.meta.url)), "utf8");
(0, eval)(coreSrc + "\n" + liveSrc + `
globalThis.__exports = { loadOhlcChart };`);
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
  const ticker = "AMD";

  // Seed: a real POST refresh for 3M (the one legitimate UW call in
  // this whole sequence besides the deliberate Step-4 "Load more" one).
  fetchLog.length = 0;
  await liveMod.loadOhlcChart(ticker, "3M", 0);
  // loadOhlcChart itself is a GET-only cache-first read -- it never
  // calls refresh on its own. Seed the cache directly via a real POST
  // the way wireLoadChartDataButton's click handler would.
  await fetch(`/api/ticker/${ticker}/ohlc/refresh?range=3M`, { method: "POST" });

  console.log("=== Step: switch to 1Y (bigger than cached 3M) ===");
  fetchLog.length = 0;
  await liveMod.loadOhlcChart(ticker, "1Y", 0);
  check("switching to 1Y issued only a GET, never a POST", () => {
    assert.ok(fetchLog.length >= 1);
    for (const entry of fetchLog) assert.equal(entry.method, "GET", `unexpected ${entry.method} on range switch: ${entry.url}`);
  });
  check("the note never claims upstream UW availability it hasn't verified", () => {
    const note = el("chartRangeNote").innerHTML;
    assert.ok(!/UW has/i.test(note) || /not a confirmed upstream limit/i.test(note), note);
    assert.ok(note.includes("cached locally"), note);
    assert.ok(note.includes("not a confirmed upstream limit"), note);
  });
  check("a real 'Load more' button is rendered, wired to the refresh route", () => {
    assert.ok(el("chartRangeNote").innerHTML.includes("loadMoreChartDataBtn"));
    assert.ok((liveChildEl("loadMoreChartDataBtn").listeners.click || []).length > 0, "Load More button has no click handler wired");
  });

  console.log("\n=== Step: click 'Load more' -- the ONLY action allowed to fetch more ===");
  fetchLog.length = 0;
  await liveChildEl("loadMoreChartDataBtn").click();
  await checkAsync("clicking Load More fires exactly one POST to the refresh route for the selected range", async () => {
    const posts = fetchLog.filter((e) => e.method === "POST");
    assert.equal(posts.length, 1, JSON.stringify(fetchLog));
    assert.ok(posts[0].url.includes("/ohlc/refresh"));
    assert.ok(posts[0].url.includes("range=1Y"));
  });
  check("after Load More, 1Y is now genuinely fully covered (real fetch verified it)", () => {
    const note = el("chartRangeNote").innerHTML;
    assert.ok(note.includes("verified by this fetch") || note.includes("just loaded"), note);
  });

  console.log("\n=== Step: switch to 1D (never cached -- different timeframe entirely) ===");
  fetchLog.length = 0;
  await liveMod.loadOhlcChart(ticker, "1D", 0);
  check("switching to the never-cached 1D timeframe made only a GET, zero POSTs", () => {
    for (const entry of fetchLog) assert.equal(entry.method, "GET");
  });
  check("1D renders the honest chart-unavailable state, not a reused/fabricated dataset", () => {
    assert.ok(el("ohlcChartContainer").innerHTML.includes("No chart data cached yet"));
  });

  console.log("\n=== Step: switch back to 3M (now fully cached from Step 1) ===");
  fetchLog.length = 0;
  await liveMod.loadOhlcChart(ticker, "3M", 0);
  check("switching back to 3M made only a GET, zero POSTs", () => {
    for (const entry of fetchLog) assert.equal(entry.method, "GET");
  });
  check("3M note shows plain full coverage, no Load More button present", () => {
    const note = el("chartRangeNote").innerHTML;
    assert.ok(!note.includes("loadMoreChartDataBtn"), note);
    assert.ok(note.includes("3M"), note);
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
