// Regression tests for the frontend findings of the post-v7 audit (FE-F1, F2, F4-F13):
// the 428 confirmation contract, the stale-response guards, serialized watchlist saves,
// mode-scoped discovery state, in-flight guards and honest counts/labels.
//
// Drives the REAL frontend/app.core.js + frontend/app.live.js. Content comes from a real
// demo backend where one is needed; a fetch wrapper controls timing and can rewrite or
// synthesize individual responses so each race is reproduced deterministically.
//
//   node frontend/test_cycle_fe_guards.mjs <backend_base_url>

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

class FakeEl {
  constructor(id) {
    this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {};
    this.textContent = ""; this.disabled = false; this.dataset = {}; this.isConnected = true; this._chips = [];
  }
  set innerHTML(v) {
    this._html = v;
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
  };`);
  return globalThis.__x;
}

async function main() {
  const token = await obtainAuthToken(BASE);
  const store = { tip_auth_token: token };
  global.localStorage = {
    getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; },
  };
  global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const x = load();

  console.log("\n=== FE-F1: the server's 428 confirmation contract ===");
  const RUN_ONCE_428 = { confirmation_required: true, action: "scheduler_run_once", consequence: "1 market screener request. Send confirm=true to proceed." };
  const stubRunOnce = () => {
    rules.push({ match: (m, p) => m === "POST" && p === "/api/scheduler/run-once",
      respond: (b) => (b && b.confirm ? { json: { ran: true, mode: "disabled", depth: b.depth, candidates_evaluated: 0 } }
                                      : { status: 428, json: { detail: RUN_ONCE_428 } }) });
    rules.push({ match: (m, p) => m === "GET" && p.startsWith("/api/scheduler/status"), respond: () => ({ json: { mode: "disabled" } }) });
    rules.push({ match: (m, p) => m === "GET" && p.startsWith("/api/radar/market-wide"), respond: () => ({ json: { candidates: [] } }) });
  };
  await check("a declined confirmation sends nothing further and says so", async () => {
    reset(); stubRunOnce(); confirmAnswer = false;
    await x.runOneTimeDepthAction("discovery_only");
    assert.equal(posts("/api/scheduler/run-once").length, 1, "only the unconfirmed probe may be sent");
    assert.equal(confirmPrompts.length, 1);
    assert.ok(confirmPrompts[0].includes("1 market screener request"), "the dialog shows the SERVER's consequence text");
    assert.match(el("runOnceDepthProgress").textContent, /not started -- nothing was sent/);
  });
  await check("an accepted confirmation re-sends once with confirm=true", async () => {
    reset(); stubRunOnce(); confirmAnswer = true;
    await x.runOneTimeDepthAction("discovery_plus_evaluation");
    const sent = posts("/api/scheduler/run-once");
    assert.equal(sent.length, 2);
    assert.equal(sent[0].body.confirm, undefined);
    assert.deepEqual(sent[1].body, { depth: "discovery_plus_evaluation", confirm: true });
    assert.match(el("runOnceDepthProgress").textContent, /complete/);
  });
  await check("scheduler mode confirmation carries the mode itself as the confirm value", async () => {
    reset(); confirmAnswer = true;
    rules.push({ match: (m, p) => m === "POST" && p === "/api/scheduler/mode",
      respond: (b) => (b.confirm === b.mode ? { json: { mode: b.mode } }
                                            : { status: 428, json: { detail: { confirmation_required: true, consequence: "Arms RECURRING requests" } } }) });
    const out = await x.apiPostConfirmed("/api/scheduler/mode", { mode: "enabled" }, "enabled");
    assert.equal(out.mode, "enabled");
    assert.equal(posts("/api/scheduler/mode")[1].body.confirm, "enabled");
  });
  await check("against the REAL backend: an unconfirmed one-time run is refused with 428 and nothing runs", async () => {
    reset();
    const before = await (await realFetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } })).json();
    let err = null;
    try { await x.apiPost("/api/scheduler/run-once", { depth: "discovery_only" }); } catch (e) { err = e; }
    assert.ok(err && err.confirmation, "a 428 must surface as a confirmation error, not a generic failure");
    assert.match(err.confirmation.consequence, /market screener request/);
    assert.ok(!String(err.message).includes("[object Object]"));
    const after = await (await realFetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } })).json();
    assert.equal(after.last_run_started_at ?? null, before.last_run_started_at ?? null, "no cycle may have run");
  });
  await check("against the REAL backend: an unconfirmed Dry Run is refused and the mode is unchanged", async () => {
    reset();
    let err = null;
    try { await x.apiPost("/api/scheduler/mode", { mode: "dry_run" }); } catch (e) { err = e; }
    assert.ok(err && err.confirmation);
    const st = await (await realFetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } })).json();
    assert.equal(st.mode, "disabled");
  });

  console.log("\n=== FE-F5: a late Market Radar response never overwrites a newer one ===");
  await check("the older load's answer is discarded", async () => {
    reset();
    // the older answer claims a 777-ticker watchlist; only the newer one may be rendered
    const seen = {};
    rules.push({ match: (m, p) => m === "GET" && p === "/api/radar",
      delay: (n) => (n === 1 ? 300 : 0),
      rewrite: (json, n) => (seen[n] = n === 1 ? { ...json, highest: [], other: [], scanned: 777, watchlist_size: 777, evaluated_count: 777,
                                                    tickers: Array.from({ length: 777 }, (_, i) => `T${i}`) } : json) });
    const first = x.loadRadar();
    await sleep(20);
    const second = x.loadRadar();
    await Promise.all([first, second]);
    assert.notEqual(x.renderRadarStatus(seen[1]), x.renderRadarStatus(seen[2]), "precondition: the two answers render differently");
    assert.equal(el("radarStatus").textContent, x.renderRadarStatus(seen[2]), "the stale (older) answer was rendered");
  });

  console.log("\n=== FE-F4 / F10: discovery is mode-scoped and one scan at a time ===");
  await check("two quick Scan submits make ONE screener request", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p === "/api/radar/discover", delay: 150 });
    await Promise.all([x.runDiscoveryScan(), x.runDiscoveryScan()]);
    assert.equal(posts("/api/radar/discover").length, 1);
  });
  await check("a mode switch drops results, and a scan answer landing after the switch is not shown", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p === "/api/radar/discover", delay: 200 });
    const scan = x.runDiscoveryScan();
    await sleep(20);
    x.resetDiscoveryState();
    await scan;
    assert.equal(x.lastDiscovery(), null);
    assert.ok(!el("discoveryCandidateList").innerHTML.includes("data-evaluate-ticker"), "stale candidates rendered after reset");
    const src = fs.readFileSync(fileURLToPath(new URL("./app.live.js", import.meta.url)), "utf8");
    const setModeSrc = src.slice(src.indexOf("async function setMode"), src.indexOf("async function saveUwKey"));
    assert.ok(setModeSrc.includes("resetDiscoveryState()") && setModeSrc.includes("_radarSeq++") && setModeSrc.includes("_marketWideLoaded = false"));
  });

  console.log("\n=== FE-F7: a failed Evaluate in TIP is not shown as Evaluated ===");
  await check("a 409 from the refresh route leaves the candidate un-evaluated", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p === "/api/ticker/ZZZ/refresh",
      respond: () => ({ status: 409, json: { detail: "A refresh for ZZZ is already in progress." } }) });
    await x.evaluateDiscoveryCandidate("ZZZ");
    assert.equal(x.discoveryStates().ZZZ, "candidate");
    assert.match(el("tickerDetail").innerHTML, /already in progress/);
  });
  await check("a successful refresh reports success", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p === "/api/ticker/FLUX/refresh",
      respond: () => ({ json: { ticker: "FLUX" } }) });
    const ok = await x.loadTickerDetail("FLUX", { refresh: true });
    assert.equal(ok, true);
  });

  console.log("\n=== FE-F1/F12: Live refreshes state the server's budget first ===");
  await check("in Live, a declined budget dialog sends no refresh", async () => {
    reset(); x.setMode("live"); x.setBudget({ per_ticker_scan_requests: 9, attempts_per_request: 3 }); confirmAnswer = false;
    await x.evaluateDiscoveryCandidate("FLUX");
    x.setMode("demo");
    assert.equal(posts("/api/ticker/FLUX/refresh").length, 0);
    assert.ok(confirmPrompts[0].includes("up to 9 Unusual Whales requests"), confirmPrompts[0]);
  });

  console.log("\n=== FE-F11: chart range race ===");
  await check("a slow earlier range never overwrites the later pick", async () => {
    reset();
    const ohlcFor = (range) => ({ ticker: "FLUX", range, candle_count: 2, range_fully_satisfied: false,
      candles: [{ time: "2026-10-01", open: 1, high: 2, low: 1, close: 2 }, { time: "2026-10-02", open: 2, high: 3, low: 2, close: 3 }] });
    rules.push({ match: (m, p) => m === "GET" && p.includes("/ohlc?range=1Y"), delay: 250, respond: () => ({ json: ohlcFor("1Y") }) });
    rules.push({ match: (m, p) => m === "GET" && p.includes("/ohlc?range=1M"), respond: () => ({ json: ohlcFor("1M") }) });
    const seq = x.navSeq();
    el("chartRangeNote").innerHTML = "";
    const slow = x.loadOhlcChart("FLUX", "1Y", seq);
    await sleep(20);
    const fast = x.loadOhlcChart("FLUX", "1M", seq);
    await Promise.all([slow, fast]);
    assert.ok(el("chartRangeNote").innerHTML.includes("1M requested"), `expected the 1M note: ${el("chartRangeNote").innerHTML.slice(0, 120)}`);
    assert.ok(!el("chartRangeNote").innerHTML.includes("1Y"), "1Y note shown while 1M is selected");
  });

  console.log("\n=== FE-F2: one paid investigation at a time ===");
  await check("two runs started together send ONE investigate request", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p.startsWith("/api/investigate/"), delay: 150, respond: () => ({ json: { simulated: true } }) });
    await Promise.all([x.runInvestigate("FLUX", x.navSeq()), x.runInvestigate("FLUX", x.navSeq())]);
    assert.equal(requests.filter((r) => r.method === "POST" && r.path.startsWith("/api/investigate/")).length, 1);
  });

  console.log("\n=== FE-F8: quick consecutive watchlist adds ===");
  await check("two quick adds keep both tickers", async () => {
    reset();
    let server = ["AAA"];
    rules.push({ match: (m, p) => m === "PUT" && p === "/api/settings/watchlist", delay: 80,
      respond: (b) => { server = b.watchlist; return { json: { watchlist: server } }; } });
    el("watchlistChips").innerHTML = server.map((t) => `<span data-ticker="${t}"></span>`).join("");
    globalThis.loadSettings = async () => { el("watchlistChips").innerHTML = server.map((t) => `<span data-ticker="${t}"></span>`).join(""); };
    x.initWatchlistEditor();
    el("watchlistAddInput").value = "BBB"; el("watchlistAddBtn").click();
    el("watchlistAddInput").value = "CCC"; el("watchlistAddBtn").click();
    await sleep(400);
    assert.deepEqual(server, ["AAA", "BBB", "CCC"]);
    const bodies = requests.filter((r) => r.method === "PUT").map((r) => r.body.watchlist);
    assert.deepEqual(bodies, [["AAA", "BBB"], ["AAA", "BBB", "CCC"]]);
  });

  console.log("\n=== FE-F6 / F9 / F12 / F13: honest labels and fresh views ===");
  await check("FE-F13: failed collections are counted separately, not as awaiting evaluation", async () => {
    const html = x.renderMarketWideSummary({ candidates: [{ freshness: "current" }, { freshness: "collection_failed" }, { freshness: "unevaluated" }] }, { mode: "disabled" });
    assert.match(html, /<strong>1<\/strong> awaiting evaluation/);
    assert.match(html, /<strong>1<\/strong> attempted, collection failed/);
  });
  await check("FE-F9: legacy sessions are shown as needing re-verification and not counted", async () => {
    const html = x.renderBaselineDatasetCell({ complete_prior_sessions: 3, required_sessions: 20, legacy_uncertified_prior_sessions: 17 });
    assert.match(html, /3 \/ 20/);
    assert.match(html, /\+17 legacy, needs re-verification/);
    assert.ok(html.includes("state-FADING"));
  });
  await check("FE-F12: the watchlist copy uses the server-reported budget", async () => {
    const html = x.renderWatchlistEditor({ mode: "live", watchlist: { tickers: ["AMD"], max_size: 20 }, request_budget: { per_ticker_scan_requests: 11 } });
    assert.match(html, /up to 11\s+real Unusual Whales requests/);
    assert.ok(!html.includes("~3 real UW API calls"));
  });
  await check("FE-F6: activating a view calls the refresh hook", async () => {
    const seen = [];
    global.window.TIP_NAV_HANDLERS = { onViewActivated: (v) => seen.push(v) };
    x.switchView("settings");
    x.switchView("marketwide");
    global.window.TIP_NAV_HANDLERS = null;
    assert.deepEqual(seen, ["settings", "marketwide"]);
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => { console.error("FATAL:", e); process.exit(1); });
