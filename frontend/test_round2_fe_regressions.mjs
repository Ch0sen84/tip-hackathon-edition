// Round-2 frontend review (F2-1..F2-7), converted from the reviewer's reproductions into regression
// checks asserting the corrected behaviour.
// Audit-2 frontend reproductions (339c84f). A FAIL line is a finding; PASS lines are verified-sound checks.
//   node frontend/test_zz_audit2_fe.mjs <backend_base_url>

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

const REPLACES = {
  radarWatchlistEditor: ['watchlistAddBtn', 'watchlistAddInput', 'watchlistChips', 'watchlistStatus'],
  baselineCard: ['baselineStatus', 'baselineStartWatchlistBtn', 'baselinePauseBtn', 'baselineResumeBtn', 'baselineCancelBtn'],
};
class FakeEl {
  constructor(id) {
    this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {};
    this.textContent = ""; this.disabled = false; this.dataset = {}; this.isConnected = true; this._chips = [];
  }
  set innerHTML(v) {
    this._html = v;
    (REPLACES[this.id] || []).forEach((id) => { const n = new FakeEl(id); elements.set(id, n); if (id === 'watchlistChips') n.innerHTML = v; });
    this._chips = Array.from(String(v).matchAll(/data-ticker="([^"]*)"/g)).map((m) => m[1]);
  }
  get innerHTML() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  get style() { return this._style; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  querySelectorAll(sel) {
    if (sel === "[data-ticker]") return this._chips.map((t) => ({ dataset: { ticker: t } }));
    return [];
  }
  querySelector() { return null; }
  closest() { return null; }
  classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
  click() { (this.listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); }
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
  createElement: () => new FakeEl("_created"),
};
global.history = { replaceState() {}, pushState() {} };
global.location = { pathname: "/", search: "", hash: "" };
globalThis.__TIP_TEST_API_BASE__ = BASE;
let confirmAnswer = false;
const confirmPrompts = [];
global.window = {
  addEventListener() {}, TIP_NAV_HANDLERS: null,
  confirm: (msg) => { confirmPrompts.push(msg); return confirmAnswer; },
};
global.alert = () => {};
global.ResizeObserver = class { observe() {} };
global.LightweightCharts = {
  createChart: () => ({
    addCandlestickSeries: () => ({ setData() {}, setMarkers() {} }),
    addHistogramSeries: () => ({ priceScale: () => ({ applyOptions() {} }), setData() {} }),
    timeScale: () => ({ fitContent() {} }),
    applyOptions() {},
    remove() {},
  }),
};

// ---- programmable fetch -------------------------------------------------------------------------
const realFetch = globalThis.fetch;
const requests = [];
let rules = []; // {match(method,path,body) -> bool, delay, respond?(body) -> {status, json}, rewrite?(json)}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
global.fetch = async (url, opts = {}) => {
  const u = new URL(url);
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path: u.pathname, search: u.search, body });
  const rule = rules.find((r) => r.match(method, u.pathname + u.search, body));
  const n = rule ? (rule._n = (rule._n || 0) + 1) : 0; // per-rule request ordinal, fixed at send time
  if (rule && rule.delay) await sleep(typeof rule.delay === "function" ? rule.delay(n, body) : rule.delay);
  if (rule && rule.respond) {
    const { status = 200, json = {} } = rule.respond(body, u, n);
    return new Response(JSON.stringify(json), { status, headers: { "Content-Type": "application/json" } });
  }
  const res = await realFetch(url, opts);
  if (rule && rule.rewrite) {
    const json = await res.json();
    return new Response(JSON.stringify(rule.rewrite(json, n, body)), { status: res.status, headers: { "Content-Type": "application/json" } });
  }
  return res;
};

let passed = 0;
async function check(name, fn) {
  try { await fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}
function reset() { requests.length = 0; rules = []; confirmPrompts.length = 0; confirmAnswer = false; }
const posts = (path) => requests.filter((r) => r.method === "POST" && r.path === path);

function load() {
  const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
  const liveSrcRaw = fs.readFileSync(fileURLToPath(new URL("./app.live.js", import.meta.url)), "utf8");
  const liveSrc = liveSrcRaw
    .replace(/document\.querySelectorAll\("\.nav-item"\)[\s\S]*?\}\);\n/, "")
    .replace(/document\.getElementById\("refreshRadar"\)[\s\S]*?loadRadar\);\n/, "")
    .replace(/initTickerCombo\([\s\S]*?\}\);\n/, "")
    .replace(/\(async function bootstrap[\s\S]*?\}\)\(\);\n?/, "");
  (0, eval)(`${coreSrc}\n${liveSrc}\nglobalThis.__x = {
    apiPost: typeof apiPost === "function" ? apiPost : undefined, apiPostConfirmed: typeof apiPostConfirmed === "function" ? apiPostConfirmed : undefined, runOneTimeDepthAction: typeof runOneTimeDepthAction === "function" ? runOneTimeDepthAction : undefined, loadRadar: typeof loadRadar === "function" ? loadRadar : undefined, runDiscoveryScan: typeof runDiscoveryScan === "function" ? runDiscoveryScan : undefined, resetDiscoveryState: typeof resetDiscoveryState === "function" ? resetDiscoveryState : undefined, evaluateDiscoveryCandidate: typeof evaluateDiscoveryCandidate === "function" ? evaluateDiscoveryCandidate : undefined, loadTickerDetail: typeof loadTickerDetail === "function" ? loadTickerDetail : undefined, loadOhlcChart: typeof loadOhlcChart === "function" ? loadOhlcChart : undefined, runInvestigate: typeof runInvestigate === "function" ? runInvestigate : undefined, initWatchlistEditor: typeof initWatchlistEditor === "function" ? initWatchlistEditor : undefined, renderMarketWideSummary: typeof renderMarketWideSummary === "function" ? renderMarketWideSummary : undefined, renderBaselineDatasetCell: typeof renderBaselineDatasetCell === "function" ? renderBaselineDatasetCell : undefined, renderWatchlistEditor: typeof renderWatchlistEditor === "function" ? renderWatchlistEditor : undefined, renderRadarStatus: typeof renderRadarStatus === "function" ? renderRadarStatus : undefined, switchView: typeof switchView === "function" ? switchView : undefined,
    discoveryStates: () => _discoveryStates, navSeq: () => _navSeq, lastDiscovery: () => _lastDiscoveryData,
    setMode: (m) => { _currentMode = m; }, setBudget: (b) => { _requestBudget = b; },
    realSetMode: setMode, wireLoadChartDataButton, navSeqBump: () => ++_navSeq, loadSettings, refreshWatchlistData, loadBaselinePanel, currentMode: () => _currentMode,
    handlers: () => window.TIP_NAV_HANDLERS,
  };`);
  return globalThis.__x;
}

async function main() {
  const token = await obtainAuthToken(BASE);
  const store = { tip_auth_token: token };
  global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
  global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const x = load();
  const H = { "X-TIP-Auth": token, "Content-Type": "application/json" };
  const putWl = (wl) => realFetch(`${BASE}/api/settings/watchlist`, { method: "PUT", headers: H, body: JSON.stringify({ watchlist: wl }) });
  const getWl = async () => (await (await realFetch(`${BASE}/api/settings`, { headers: H })).json()).watchlist.tickers;

  console.log("\n=== F2-1: watchlist saves are serialized only within ONE editor instance (re-render boundary) ===");
  await check("three quick adds across the post-save re-render all persist", async () => {
    reset();
    await putWl(["AMD"]);
    await x.loadSettings();                                   // REAL loadSettings -> real initWatchlistEditor
    rules.push({ match: (m, p) => m === "PUT" && p === "/api/settings/watchlist", delay: 80 });
    rules.push({ match: (m, p) => m === "GET" && p === "/api/settings", delay: 150 });
    el("watchlistAddInput").value = "MSFT"; el("watchlistAddBtn").click();      // save 1
    await sleep(110);                                         // PUT 1 done; loadSettings' GET in flight
    el("watchlistAddInput").value = "NVDA"; el("watchlistAddBtn").click();      // old editor: queued save 2
    const oldBtn = el("watchlistAddBtn");
    for (let i = 0; i < 200 && el("watchlistAddBtn") === oldBtn; i++) await sleep(5);   // wait for the re-render
    assert.notEqual(el("watchlistAddBtn"), oldBtn, "precondition: the editor was re-rendered");
    el("watchlistAddInput").value = "TSLA"; el("watchlistAddBtn").click();      // new editor: independent chain
    await sleep(1200);
    const bodies = requests.filter((r) => r.method === "PUT").map((r) => r.body.watchlist.join(","));
    const final = await getWl();
    console.log("   PUT bodies:", JSON.stringify(bodies), "| server now:", JSON.stringify(final),
      "| status:", JSON.stringify(el("watchlistStatus").textContent));
    await putWl(["AMD"]);
    for (const t of ["MSFT", "NVDA", "TSLA"]) assert.ok(final.includes(t), `${t} was silently lost`);
  });

  console.log("\n=== F2-2: baseline panel: declined / failed action messages are wiped by the immediate re-render ===");
  const OV = { mode: "live", evidence_session: "2026-10-16", required_sessions: 20, job: null, job_thread_alive: false,
    tickers: [{ ticker: "AMD", datasets: { darkpool: { complete_prior_sessions: 3, legacy_uncertified_prior_sessions: 17, required_sessions: 20, insufficient_reason: "x" },
                                           options_flow: { complete_prior_sessions: 0, required_sessions: 20, insufficient_reason: "y" } } }] };
  const C428 = { status: 428, json: { detail: { confirmation_required: true, consequence: "Collects ... 400 pages" } } };
  const cases = [
    ["declined confirmation", false, () => C428],
    ["409 conflict after OK", true, (b) => (b && b.confirm ? { status: 409, json: { detail: "baseline job 7 is still active; pause/cancel it first" } } : C428)],
  ];
  for (const [label, answer, respond] of cases) {
    await check(`the ${label} message stays visible`, async () => {
      reset(); confirmAnswer = answer;
      rules.push({ match: (m, p) => m === "GET" && p === "/api/baseline/overview", respond: () => ({ json: OV }) });
      rules.push({ match: (m, p) => m === "POST" && p === "/api/baseline/jobs", respond });
      await x.loadBaselinePanel();
      el("baselineStartWatchlistBtn").click();
      await sleep(150);
      const shown = el("baselineStatus").textContent;
      console.log(`   POSTs: ${posts("/api/baseline/jobs").length}, prompts: ${confirmPrompts.length}, #baselineStatus now: ${JSON.stringify(shown)}`);
      assert.ok(shown, "the user is told nothing (status re-rendered empty)");
    });
  }
  await check("legacy sessions are labelled and not counted (FE-F9)", async () => {
    const html = x.renderBaselineDatasetCell(OV.tickers[0].datasets.darkpool);
    assert.match(html, /3 \/ 20/); assert.match(html, /\+17 legacy, needs re-verification/); assert.match(html, /state-FADING/);
  });

  console.log("\n=== F2-3: the Live spend confirmation depends on a cached mode / cached ticker count ===");
  await check("after a mode switch whose settings re-read fails, Live refreshes go out unconfirmed", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p === "/api/settings/mode", respond: (b) => ({ json: { mode: b.mode } }) });
    rules.push({ match: (m, p) => m === "GET" && p === "/api/settings", respond: () => ({ status: 503, json: { detail: "busy" } }) });
    // F2-3 fix: the SERVER (now Live) answers an unconfirmed refresh with 428, whatever the tab thinks the mode is
    rules.push({ match: (m, p) => m === "POST" && p === "/api/radar/refresh",
      respond: (b) => (b && b.confirm ? { json: {} }
        : { status: 428, json: { detail: { confirmation_required: true, consequence: "Refreshes the watchlist: up to 108 requests" } } }) });
    await x.realSetMode("live");                               // server now Live; the re-read failed
    await x.refreshWatchlistData();
    const confirmedSends = posts("/api/radar/refresh").filter((r) => r.body && r.body.confirm === true).length;
    console.log(`   _currentMode=${x.currentMode()} | confirm prompts: ${confirmPrompts.length} | confirmed sends: ${confirmedSends}`);
    assert.ok(confirmPrompts.length > 0 && confirmedSends === 0, "a Live watchlist refresh was sent with no budget confirmation");
  });
  // F2-3b: the budget is now computed by the server from the CURRENT watchlist -- see
  // backend/tests/test_cycle_hardening_regressions.py::test_live_refresh_budget_counts_the_current_watchlist

  console.log("\n=== F2-4: in-flight guards survive a mode switch ===");
  await check("a Scan in the new mode is not silently ignored while the old mode's scan is in flight", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p === "/api/radar/discover", delay: 400 });
    const first = x.runDiscoveryScan();
    await sleep(30);
    x.resetDiscoveryState();                                   // what setMode() does
    await x.runDiscoveryScan();                                // user clicks Scan in the new mode
    const n = posts("/api/radar/discover").length;
    const status = el("discoveryStatus").textContent;
    await first;
    console.log(`   discover POSTs: ${n} | status after the ignored click: ${JSON.stringify(status)}`);
    assert.ok(n === 2 || status, "the click did nothing and said nothing");
  });

  console.log("\n=== F2-5: view re-reads (FE-F6) ===");
  await check("returning to Market Radar re-reads its freshness/last-scanned state", async () => {
    // F2-5 as refined by R3F-4: Live only (a Live radar read is cache-only; a Demo read is a synthetic scan
    // that writes history), and only when returning from another view.
    reset();
    await x.loadRadar();
    x.setMode("live");
    const before = requests.filter((r) => r.method === "GET" && r.path === "/api/radar").length;
    x.handlers().onViewActivated("radar", "settings");
    await sleep(50);
    const n = requests.filter((r) => r.method === "GET" && r.path === "/api/radar").length - before;
    x.handlers().onViewActivated("radar", "radar");                  // already on Radar: no extra read
    await sleep(50);
    const again = requests.filter((r) => r.method === "GET" && r.path === "/api/radar").length - before;
    x.setMode("demo");
    x.handlers().onViewActivated("radar", "settings");               // Demo: never a writing re-scan
    await sleep(50);
    const demo = requests.filter((r) => r.method === "GET" && r.path === "/api/radar").length - before;
    console.log(`   GET /api/radar on Live return: ${n} | staying: ${again - n} | Demo return: ${demo - again}`);
    assert.ok(n >= 1, "Market Radar keeps its first-load 'last scanned ...' / freshness text for the whole session");
    assert.equal(again - n, 0);
    assert.equal(demo - again, 0);
  });

  console.log("\n=== F2-6: chart 'Load chart data' (ohlc/refresh) bypasses the FE-F11 range guard ===");
  await check("a late provider-refresh answer for 1Y never overwrites a newer 1M pick", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p.startsWith("/api/ticker/BRNX/ohlc/refresh"), delay: 300,
      respond: () => ({ json: { ticker: "BRNX", candle_count: 252, candles: [], range_fully_satisfied: true } }) });
    const seq = x.navSeqBump();
    x.wireLoadChartDataButton("BRNX", "1Y", seq);
    el("loadChartDataBtn").click();                            // provider fetch for 1Y in flight
    await sleep(20);
    await x.loadOhlcChart("BRNX", "1M", seq);                   // user picks 1M (cache-only GET)
    const after1M = el("chartRangeNote").textContent;
    await sleep(400);
    const final = el("chartRangeNote").textContent;
    console.log(`   note after 1M: ${JSON.stringify(after1M)} | after the late 1Y answer: ${JSON.stringify(final)}`);
    assert.ok(!/^1Y/.test(final), "1M is selected but the chart/note now show the 1Y refresh");
  });

  console.log("\n=== F2-7: one-time run result ignores ran/pause_reason ===");
  for (const [label, result] of [
    ["screener failed (S3 pause)", { ran: true, mode: "disabled", depth: "discovery_only", candidates_evaluated: 0,
      pause_reason: "discovery unavailable: {'status': 'error', 'status_code': 429}" }],
    ["lease not held (nothing ran)", { ran: false, reason: "lease_not_held", triggered_by: "run_once", depth: "discovery_only" }],
  ]) {
    await check(`Discover Once does not report success when ${label}`, async () => {
      reset(); confirmAnswer = true;
      rules.push({ match: (m, p) => m === "POST" && p === "/api/scheduler/run-once",
        respond: (b) => (b && b.confirm ? { json: result } : { status: 428, json: { detail: { confirmation_required: true, consequence: "1 market screener request." } } }) });
      rules.push({ match: (m, p) => m === "GET" && p.startsWith("/api/scheduler/status"), respond: () => ({ json: { mode: "disabled" } }) });
      rules.push({ match: (m, p) => m === "GET" && p.startsWith("/api/radar/market-wide"), respond: () => ({ json: { candidates: [] } }) });
      await x.runOneTimeDepthAction("discovery_only");
      const msg = el("runOnceDepthProgress").textContent;
      console.log(`   shown: ${JSON.stringify(msg)}`);
      assert.ok(!/complete -- candidates discovered and cataloged/.test(msg), "reported as a successful, cataloged discovery");
    });
  }

  console.log(`\n${passed} check(s) passed.`);
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
