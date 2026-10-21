// Audit-3 frontend reproductions (7206583). A FAIL line is a finding; PASS lines are verified-sound checks.
//   node frontend/test_zz_audit3_fe.mjs <demo_backend_base_url>

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

const REPLACES = {
  radarWatchlistEditor: ['watchlistAddBtn', 'watchlistAddInput', 'watchlistChips', 'watchlistStatus'],
  investigateSection: ['runInvestigateBtn', 'investigateBody'],
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
    handlers: () => window.TIP_NAV_HANDLERS, onAuthRestored, loadInvestigateSection, setAuthMode: (m) => { TIP_AUTH_MODE = m; }, tipState: () => TIP_STATE,
  };`);
  return globalThis.__x;
}

async function main() {
  const token = await obtainAuthToken(BASE);
  const store = { tip_auth_token: token };
  global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; } };
  global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const x = load();
  const C428 = (msg) => ({ status: 428, json: { detail: { confirmation_required: true, consequence: msg } } });

  console.log("\n=== R3F-2: Investigate always sends confirm:true, so the server's Live 428 can never be shown ===");
  await check("a panel loaded in Demo (simulated) lets the server ask before a Live, billed run", async () => {
    reset(); confirmAnswer = false;
    await x.loadInvestigateSection("PRSX", x.navSeq());       // REAL demo availability: simulated
    const note = el("investigateSection").innerHTML.match(/Demo mode:[^<]*/);
    // the server has since been switched to Live by another tab: it answers 428 unless confirm is sent
    rules.push({ match: (m, p) => m === "POST" && p === "/api/investigate/PRSX",
      respond: (b) => (b && b.confirm ? { json: { simulated: false, transport: "rest", ticker: "PRSX", tools_used: [] } }
        : C428("Runs an AI investigation of PRSX: two billed calls to your anthropic account ...")) });
    el("runInvestigateBtn").click(); el("runInvestigateBtn").click();   // the two-click arm
    await sleep(100);
    const sent = posts("/api/investigate/PRSX").map((r) => JSON.stringify(r.body));
    console.log(`   panel said: ${JSON.stringify(note && note[0])} | POST bodies: ${sent} | cost dialogs shown: ${confirmPrompts.length}`);
    assert.ok(confirmPrompts.length > 0, "a billed Live investigation ran from a panel that promised a simulated transcript; the server's cost statement was never shown");
  });

  console.log("\n=== R3F-3: the F2-4 'previous investigation is still running' message is unreachable from the button ===");
  await check("clicking Investigate on another ticker while one is in flight says something", async () => {
    reset();
    rules.push({ match: (m, p) => m === "POST" && p.startsWith("/api/investigate/"), delay: 400 });
    await x.loadInvestigateSection("PRSX", x.navSeq());
    el("runInvestigateBtn").click(); el("runInvestigateBtn").click();   // PRSX run in flight
    await sleep(30);
    const seq = x.navSeqBump();                                          // user opens FLUX
    await x.loadInvestigateSection("FLUX", seq);
    const btn = el("runInvestigateBtn");
    const label0 = btn.textContent;
    btn.click();
    await sleep(20);
    const body = el("investigateBody").innerHTML;
    console.log(`   FLUX button label before/after click: ${JSON.stringify(label0)} / ${JSON.stringify(btn.textContent)} | body: ${JSON.stringify(body)}`);
    await sleep(500);
    assert.ok(/still running/.test(body) || btn.textContent !== label0, "the click did nothing and said nothing");
  });

  console.log("\n=== R3F-1: Network Mode -- after pairing, Settings / mode are never re-read ===");
  await check("onAuthRestored re-reads /api/settings after a 401 on first load", async () => {
    reset();
    x.setAuthMode("network");
    rules.push({ match: (m, p) => m === "GET" && p === "/api/settings", respond: () => ({ status: 401, json: { detail: "Missing or invalid X-TIP-Auth header." } }) });
    await x.loadSettings();
    const pill = el("modePill").textContent;
    store.tip_auth_token = token;                               // pairing succeeded
    rules = [];
    await x.onAuthRestored();
    const n = requests.filter((r) => r.method === "GET" && r.path === "/api/settings").length;
    console.log(`   pill after the 401: ${JSON.stringify(pill)} | GET /api/settings issued in total: ${n} (1 = never re-read)`);
    x.setAuthMode("local");
    assert.ok(n >= 2, "Settings, the mode pill and the watchlist editor stay broken until a manual page reload");
  });

  console.log("\n=== R3F-4: Market Radar re-read on activation also fires on the initial navigation ===");
  await check("page load reads /api/radar once", async () => {
    reset();
    await x.loadRadar();                                        // bootstrap()
    x.switchView("radar");                                      // applyNavState(initial) -> switchView
    await sleep(300);
    const n = requests.filter((r) => r.method === "GET" && r.path === "/api/radar").length;
    console.log(`   GET /api/radar during bootstrap: ${n} (each one is a full synthetic scan + write in Demo)`);
    assert.equal(n, 1);
  });

  console.log("\n=== R3F-5: F2-2 baseline message survives the reload but not the 3 s job poll ===");
  await check("a failed Pause message (job active, so the 3 s poll runs) is still visible 3.5 s later", async () => {
    reset(); confirmAnswer = true;
    el("view-settings").classList = { add() {}, remove() {}, toggle() {}, contains: (c) => c === "active" };
    const OV = { mode: "live", evidence_session: "2026-10-16", required_sessions: 20, job_thread_alive: true,
      job: { id: 7, status: "running", tickers: ["AMD"], progress: {}, max_pages_per_ticker: 400 },
      tickers: [{ ticker: "AMD", datasets: { darkpool: { complete_prior_sessions: 3, required_sessions: 20 }, options_flow: { complete_prior_sessions: 0, required_sessions: 20 } } }] };
    rules.push({ match: (m, p) => m === "GET" && p === "/api/baseline/overview", respond: () => ({ json: OV }) });
    rules.push({ match: (m, p) => m === "POST" && p === "/api/baseline/jobs/7/pause",
      respond: () => ({ status: 503, json: { detail: "settings file busy; nothing was changed, try again" } }) });
    await x.loadBaselinePanel();
    el("baselinePauseBtn").click();
    await sleep(200);
    const at200 = el("baselineStatus").textContent;
    await sleep(3400);
    const at3600 = el("baselineStatus").textContent;
    el("view-settings").classList = { add() {}, remove() {}, toggle() {}, contains: () => false };   // stop the poll
    await sleep(3200);
    console.log(`   #baselineStatus at 0.2 s: ${JSON.stringify(at200)} | at 3.6 s: ${JSON.stringify(at3600)}`);
    assert.ok(at3600, "the poll's re-render wiped the outcome message");
  });

  console.log("\n=== sound: Evaluate in TIP, Live, declined (server 428) ===");
  await check("one dialog, nothing confirmed sent, candidate kept, honest message", async () => {
    reset(); confirmAnswer = false;
    rules.push({ match: (m, p) => m === "POST" && p === "/api/radar/discover",
      respond: () => ({ json: { candidates: [{ ticker: "ZZZQ", evaluate_supported: true }], snapshot_at: "2026-10-21T15:00:00Z", screener_requests_used: 1 } }) });
    rules.push({ match: (m, p) => m === "POST" && p === "/api/ticker/ZZZQ/refresh",
      respond: (b) => (b && b.confirm ? { json: {} } : C428("Refreshes ZZZQ from Unusual Whales now: up to 9 requests")) });
    rules.push({ match: (m, p) => m === "GET" && p.startsWith("/api/ticker/ZZZQ"), respond: () => ({ status: 404, json: { detail: "not cached" } }) });
    await x.runDiscoveryScan();
    await x.evaluateDiscoveryCandidate("ZZZQ");
    const confirmed = posts("/api/ticker/ZZZQ/refresh").filter((r) => r.body && r.body.confirm).length;
    console.log(`   dialogs: ${confirmPrompts.length} | refresh POSTs: ${posts("/api/ticker/ZZZQ/refresh").length} (confirmed ${confirmed}) | state: ${x.discoveryStates().ZZZQ} | status: ${JSON.stringify(el("discoveryStatus").textContent)}`);
    assert.equal(confirmPrompts.length, 1); assert.equal(confirmed, 0);
    assert.equal(x.discoveryStates().ZZZQ, "candidate");
    assert.match(el("discoveryStatus").textContent, /nothing was sent/);
  });

  console.log(`\n${passed} check(s) passed.`);
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
