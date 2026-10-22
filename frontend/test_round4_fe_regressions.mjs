// Round-4 frontend review (R4F-1..R4F-3), converted from the reviewer's reproductions into regression
// checks asserting the corrected behaviour.
// Audit-4 frontend reproductions (2c7cc85, scope 05d7c9d). A FAIL line is a finding; PASS lines are verified-sound checks.
//   node frontend/test_zz_audit4_fe.mjs <demo_backend_base_url>

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
  // history writes update location.hash, as in a browser
  const hist = [];
  global.history = {
    pushState(s, t, url) { hist.push(["push", url]); location.hash = url; },
    replaceState(s, t, url) { hist.push(["replace", url]); location.hash = url; },
  };
  const x = load();
  const H = () => x.handlers();
  const radarReads = () => requests.filter((r) => r.method === "GET" && r.path === "/api/radar").length;
  const R401 = { status: 401, json: { detail: "Missing or invalid X-TIP-Auth header." } };

  const r4f1 = async () => {   // run last: it loads Radar, which would mask the Network-Mode state below
  console.log("\n=== R4F-1: Live Market Radar re-read on return (F2-5) never fires after the R3F-4 change ===");
  await check("Live: Radar -> Settings -> Radar (nav clicks) re-reads /api/radar", async () => {
    reset();
    x.setMode("live");
    await x.loadRadar();                                   // page load: _lastRadarData set
    globalThis.applyNavState(globalThis.parseNavHash(""), H(), { push: false });
    const n0 = radarReads();
    globalThis.navigateToView("settings");
    globalThis.navigateToView("radar");
    await sleep(400);
    const viaNav = radarReads() - n0;
    // Back/Forward restore goes through applyNavState -> replaceNav -> switchView: same ordering
    globalThis.navigateToView("settings");
    globalThis.applyNavState({ view: "radar", ticker: null, mode: null }, H(), { push: false });
    await sleep(400);
    const viaPop = radarReads() - n0 - viaNav;
    console.log(`   GET /api/radar on return via nav click: ${viaNav} | via popstate restore: ${viaPop} | TIP_STATE.currentView is set by pushNav/replaceNav BEFORE switchView reads 'previous'`);
    x.setMode("demo");
    assert.ok(viaNav >= 1 && viaPop >= 1, "previous === view on every real navigation, so the Live re-read on return is dead code");
  });
  };

  console.log("\n=== R4F-2 / R3F-11: Network Mode deep link #view=ticker&ticker=BRNX before pairing ===");
  delete store.tip_auth_token;
  x.setAuthMode("network");
  reset();
  rules.push({ match: (m, p) => m === "GET" && (p === "/api/settings" || p === "/api/radar" || p === "/api/scenarios"), respond: () => R401 });
  location.hash = "#view=ticker&ticker=BRNX";
  hist.length = 0;
  await x.loadSettings().catch(() => {});
  await x.loadRadar();
  globalThis.applyNavState(globalThis.parseNavHash(location.hash), H(), { push: false });   // bootstrap()
  const hashBeforePair = location.hash;
  await check("the first (unauthenticated) load keeps the deep-linked ticker in the URL", async () => {
    console.log(`   URL after first load: ${hashBeforePair} | history writes: ${JSON.stringify(hist)} | #tickerDetail: ${JSON.stringify(el("tickerDetail").innerHTML.replace(/<[^>]+>/g, "").slice(0, 70))}`);
    assert.match(hashBeforePair, /ticker=BRNX/, "known tickers = [] after the 401 is treated as an allowlist; replaceNav drops the ticker before pairing");
  });
  store.tip_auth_token = token;                            // pairing succeeded
  rules = [];
  const n1 = radarReads();
  await x.onAuthRestored();
  await sleep(600);
  await check("after pairing from a non-Radar view Market Radar is read (known tickers / combo populated)", async () => {
    const reads = radarReads() - n1;
    console.log(`   GET /api/radar during onAuthRestored: ${reads} | known tickers: ${H().getKnownTickers().length} | view: ${x.tipState().currentView} | radarStatus: ${JSON.stringify(el("radarStatus").textContent.slice(0, 60))}`);
    assert.ok(reads >= 1 && H().getKnownTickers().length > 0, "Radar stays on the 401 error and the ticker list stays empty");
  });
  await check("after pairing the deep-linked ticker is shown, not 'not available in demo mode'", async () => {
    const t = el("tickerDetail").innerHTML.replace(/<[^>]+>/g, "").slice(0, 80);
    console.log(`   URL: ${location.hash} | #tickerDetail: ${JSON.stringify(t)}`);
    assert.ok(!/not available/.test(t) && /ticker=BRNX/.test(location.hash), "the stale message stays and the ticker was dropped");
  });
  await check("after pairing, opening Market Radar shows real data", async () => {
    // fixed by R4F-2: onAuthRestored reads Market Radar from any view, so opening it needs no extra read
    globalThis.navigateToView("radar");
    await sleep(300);
    const status = el("radarStatus").textContent;
    console.log(`   radarStatus after opening Radar: ${JSON.stringify(status.slice(0, 60))}`);
    assert.ok(status && !/Error loading|Authentication required/.test(status), "Radar stays broken until a reload");
  });

  console.log("\n=== sound: R3F-11 banner variant ===");
  await check("first run keeps bootstrap_failed; an expired token gets 'error'", async () => {
    reset();
    delete store.tip_auth_token;
    rules.push({ match: (m, p) => m === "GET" && p === "/api/settings", respond: () => R401 });
    globalThis.showAuthSetupBanner("bootstrap_failed");
    await x.loadSettings();
    const first = el("authSetupBanner").innerHTML.match(/<strong>([^<]*)<\/strong>/)[1];
    store.tip_auth_token = "expired";
    await x.loadSettings();
    const expired = el("authSetupBanner").innerHTML.match(/<strong>([^<]*)<\/strong>/)[1];
    console.log(`   first run: ${JSON.stringify(first)} | expired token: ${JSON.stringify(expired)}`);
    assert.match(first, /automatically set up access/); assert.equal(expired, "Authentication required");
    store.tip_auth_token = token; rules = []; x.setAuthMode("local");
  });

  await r4f1();
  console.log(`\n${passed} check(s) passed.`);
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
