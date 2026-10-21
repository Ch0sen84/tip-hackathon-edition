// Market Discovery (C2b/C2c, 2026-10-15) frontend verification. Same
// convention as the rest of this repo's frontend/test_*.mjs scripts: a
// plain Node script (no JS test framework by design), loading the REAL
// frontend/app.core.js (+ app.live.js for the Evaluate-flow checks)
// against a REAL running backend (TIP_MODE=demo) over real HTTP, with a
// minimal hand-rolled DOM stub.
//
// Run with the backend already running:
//   node frontend/test_discovery.mjs http://127.0.0.1:8899

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

// --- minimal DOM stub (extends the established FakeEl pattern with
// dataset + a queryable children registry for the mode-toggle buttons
// and candidate-row Evaluate buttons, which the shared pattern's bare
// querySelectorAll() => [] does not support). ----------------------------
class FakeEl {
  constructor(id) {
    this.id = id; this._html = ""; this._value = ""; this._style = {};
    this.listeners = {}; this.dataset = {}; this._children = [];
  }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  get textContent() { return this._html.replace(/<[^>]*>/g, ""); }
  set textContent(v) { this._html = v; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  get style() { return this._style; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  dispatchEvent(ev) {
    const type = typeof ev === "string" ? ev : ev.type;
    (this.listeners[type] || []).forEach((fn) => fn(ev));
  }
  querySelectorAll(sel) {
    if (sel === "[data-radar-mode]") return this._modeButtons || [];
    if (sel === "[data-evaluate-ticker]") return this._evaluateButtons || [];
    return [];
  }
  querySelector() { return null; }
  classList = {
    _set: new Set(),
    add(c) { this._set.add(c); },
    remove(c) { this._set.delete(c); },
    toggle(c, on) { if (on) this._set.add(c); else this._set.delete(c); },
    contains(c) { return this._set.has(c); },
  };
}

const elements = new Map();
function el(id) { if (!elements.has(id)) elements.set(id, new FakeEl(id)); return elements.get(id); }

global.document = {
  getElementById: (id) => el(id),
  querySelectorAll: (sel) => {
    if (sel === "[data-evaluate-ticker]") return el("discoveryCandidateList")._evaluateButtons || [];
    return [];
  },
  querySelector: (sel) => el(sel),
  createElement: () => new FakeEl("_created"),
};
global.history = { replaceState() {}, pushState() {} };
global.location = { pathname: "/", search: "", hash: "" };
globalThis.__TIP_TEST_API_BASE__ = BASE; // SEC-1: the app no longer takes its API base from ?api=
global.window = { addEventListener() {}, TIP_NAV_HANDLERS: null };
global.ResizeObserver = class { observe() {} };
global.LightweightCharts = {
  createChart: () => ({
    addCandlestickSeries: () => ({ setData() {}, setMarkers() {} }),
    addHistogramSeries: () => ({ priceScale: () => ({ applyOptions() {} }), setData() {} }),
    timeScale: () => ({ fitContent() {} }),
    applyOptions() {},
  }),
};
global.fetch = fetch;
global.alert = (msg) => { global.__lastAlert = msg; };

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

function loadCoreOnly() {
  const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
  (0, eval)(coreSrc + `
globalThis.__exports = {
  renderDiscoveryFilterForm, readDiscoveryFilterForm, renderDiscoveryCandidateRow,
  renderDiscoveryCandidateList, wireRadarModeToggle, fmtMoney,
};`);
  return globalThis.__exports;
}

function loadCoreAndLive() {
  const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
  const liveSrcRaw = fs.readFileSync(fileURLToPath(new URL("./app.live.js", import.meta.url)), "utf8");
  // Strip the real module-level side effects this harness doesn't need
  // (nav-item wiring, the unified ticker combo bootstrap IIFE) -- same
  // technique test_navigation.mjs already uses for this exact file.
  const liveSrc = liveSrcRaw
    .replace(/document\.querySelectorAll\("\.nav-item"\)[\s\S]*?\}\);\n/, "")
    .replace(/document\.getElementById\("refreshRadar"\)[\s\S]*?loadRadar\);\n/, "")
    .replace(/initTickerCombo\([\s\S]*?\}\);\n/, "")
    .replace(/\(async function bootstrap[\s\S]*?\}\)\(\);\n?/, "");
  const combined = `${coreSrc}\n${liveSrc}`;
  (0, eval)(combined + `
globalThis.__exports = {
  runDiscoveryScan, evaluateDiscoveryCandidate, loadTickerDetail,
  renderDiscoveryFilterFormOnce, _discoveryStatesRef: () => _discoveryStates,
};`);
  return globalThis.__exports;
}

// Phase A: seed a real X-TIP-Auth token (fetched over genuine loopback
// HTTP from this same machine) before any mutating call -- see
// test_navigation.mjs's seedAuthToken() for the full rationale; this
// harness's loadReal() strips the real bootstrap() IIFE the same way.
// Windows Docker Desktop QA round 2 (finding T1): falls back to the
// pairing code (TIP_TEST_PAIRING_CODE) when bootstrap fails under real
// Docker Desktop's port-publishing NAT -- see test_lib_auth.mjs.
async function seedAuthToken() {
  const token = await obtainAuthToken(BASE);
  const store = { tip_auth_token: token };
  global.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
}

async function main() {
  await seedAuthToken();
  const core = loadCoreOnly();

  console.log("=== renderDiscoveryFilterForm / readDiscoveryFilterForm ===");
  check("quota-cost disclosure is present in the form markup BEFORE any scan runs", () => {
    const html = core.renderDiscoveryFilterForm(20);
    assert.ok(html.includes("1 screener request"), "quota disclosure text missing from form");
    assert.ok(html.includes('id="discoveryScanBtn"'));
  });
  check("readDiscoveryFilterForm reads real input values into the filter body", () => {
    core.renderDiscoveryFilterForm(20);
    el("discoveryFilter_min_premium").value = "5000000";
    el("discoveryFilter_min_call_volume").value = "";
    el("discoveryFilter_limit").value = "15";
    const body = core.readDiscoveryFilterForm();
    assert.equal(body.min_premium, 5000000);
    assert.ok(!("min_call_volume" in body), "empty field must not be sent as a filter");
    assert.equal(body.limit, 15);
  });
  check("readDiscoveryFilterForm clamps an out-of-range limit to 50 client-side too", () => {
    el("discoveryFilter_limit").value = "500";
    const body = core.readDiscoveryFilterForm();
    assert.equal(body.limit, 50);
  });

  console.log("\n=== renderDiscoveryCandidateRow / renderDiscoveryCandidateList ===");
  const sampleCandidate = {
    ticker: "FLUX", net_premium: 24500000, repeated_hits: 18, call_volume: 38700,
    put_volume: 9100, iv_rank: 81.2, change: 3.4, discovery_state: "candidate",
  };
  check("a raw candidate renders the 'Not yet evaluated' state, never a signal_engine state", () => {
    const html = core.renderDiscoveryCandidateRow(sampleCandidate);
    assert.ok(html.includes("Not yet evaluated"));
    assert.ok(html.includes("state-candidate"));
    assert.ok(!/state-CONFIRMED|state-BUILDING|state-NEW/.test(html), "a raw candidate must never show a signal_engine state");
    assert.ok(html.includes("Evaluate FLUX in TIP"));
  });
  check("an 'evaluating' candidate shows the in-progress label", () => {
    const html = core.renderDiscoveryCandidateRow(sampleCandidate, "evaluating");
    assert.ok(html.includes("Evaluating..."));
    assert.ok(html.includes("state-evaluating"));
  });
  check("an 'evaluated' candidate shows the completed label and a re-evaluate action", () => {
    const html = core.renderDiscoveryCandidateRow(sampleCandidate, "evaluated");
    assert.ok(html.includes("Evaluated"));
    assert.ok(html.includes("state-evaluated"));
    assert.ok(html.includes("Re-evaluate FLUX"));
  });
  check("the candidate list shows real screener metrics and the snapshot/quota disclosure", () => {
    const html = core.renderDiscoveryCandidateList([sampleCandidate], {
      count: 1, screener_requests_used: 1, snapshot_at: "2026-10-15T13:30:00Z",
    });
    assert.ok(html.includes("24.5M")); // fmtMoney of 24,500,000
    assert.ok(html.includes("used 1 screener request"));
    assert.ok(html.includes("2026-10-15T13:30:00Z"));
  });
  check("an empty candidate list renders an honest empty state, never fabricated rows", () => {
    const html = core.renderDiscoveryCandidateList([], { count: 0 });
    assert.ok(html.includes("No candidates matched"));
  });

  console.log("\n=== wireRadarModeToggle: My Watchlist vs Market Discovery ===");
  check("switching to Discovery mode hides Watchlist, shows Discovery, and fires the lazy-load callback exactly once", () => {
    const toggleEl = el("radarModeToggle");
    const watchlistBtn = new FakeEl("watchlistBtn"); watchlistBtn.dataset.radarMode = "watchlist"; watchlistBtn.classList.add("active");
    const discoveryBtn = new FakeEl("discoveryBtn"); discoveryBtn.dataset.radarMode = "discovery";
    toggleEl._modeButtons = [watchlistBtn, discoveryBtn];
    let loadCalls = 0;
    core.wireRadarModeToggle(() => { loadCalls++; });
    discoveryBtn.listeners.click[0]();
    assert.equal(el("watchlistRadarSection").style.display, "none");
    assert.equal(el("discoveryRadarSection").style.display, "");
    assert.equal(loadCalls, 1, "onDiscoverySelected should fire on first switch to Discovery");
    discoveryBtn.listeners.click[0]();
    assert.equal(loadCalls, 1, "onDiscoverySelected must not re-fire on a second click (lazy-load once)");
    watchlistBtn.listeners.click[0]();
    assert.equal(el("watchlistRadarSection").style.display, "");
    assert.equal(el("discoveryRadarSection").style.display, "none");
  });

  console.log("\n=== End-to-end over the REAL /api/radar/discover endpoint ===");
  // Phase A: this is a direct harness-level fetch (not routed through
  // app.live.js's apiPost), so the X-TIP-Auth header from seedAuthToken()
  // must be attached explicitly here too.
  const res = await fetch(`${BASE}/api/radar/discover`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-TIP-Auth": global.localStorage.getItem("tip_auth_token") },
    body: JSON.stringify({}),
  });
  check("POST /api/radar/discover returns 200 with the real disclosure fields", () => {
    assert.equal(res.status, 200);
  });
  const data = await res.json();
  check("every returned candidate is explicitly discovery_state=candidate, never pre-evaluated", () => {
    assert.ok(data.candidates.length > 0);
    for (const c of data.candidates) assert.equal(c.discovery_state, "candidate");
  });
  check("candidates are ranked descending by |net_premium|", () => {
    const premiums = data.candidates.map((c) => Math.abs(c.net_premium));
    assert.deepEqual(premiums, [...premiums].sort((a, b) => b - a));
  });
  check("exactly one screener request is disclosed per call", () => {
    assert.equal(data.screener_requests_used, 1);
  });
  check("a real snapshot timestamp is present", () => {
    assert.ok(data.snapshot_at);
  });
  check("the server-side limit cap (50) is disclosed and respected", () => {
    assert.equal(data.limit_cap, 50);
    assert.ok(data.count <= 50);
  });

  console.log("\n=== Evaluate in TIP: reuses the real per-ticker pipeline end-to-end ===");
  const live = loadCoreAndLive();
  const fluxCandidate = data.candidates.find((c) => c.ticker === "FLUX");
  check("fixture includes FLUX as a discovery candidate (precondition for this check)", () => {
    assert.ok(fluxCandidate, "FLUX must be a discovery candidate for this end-to-end check");
  });
  await check_async("clicking Evaluate transitions candidate -> evaluating -> evaluated via the real GET /api/ticker/{t} call", async () => {
    const promise = live.evaluateDiscoveryCandidate("FLUX");
    // Allow a microtask tick so the synchronous "evaluating" state assignment
    // (set before the await inside evaluateDiscoveryCandidate) has happened.
    await Promise.resolve();
    assert.equal(live._discoveryStatesRef().FLUX, "evaluating");
    await promise;
    assert.equal(live._discoveryStatesRef().FLUX, "evaluated");
    // loadTickerDetail (the SAME function My Watchlist's Investigate/Analyze
    // flow calls) must have actually rendered real evaluated content --
    // not a stub -- proving this reused the real pipeline, not a parallel one.
    const detailHtml = el("tickerDetail").innerHTML;
    assert.ok(detailHtml.includes("FLUX"), "ticker detail did not render for FLUX");
  });

  console.log(`\n${passed} check(s) passed.`);
}

async function check_async(name, fn) {
  try { await fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
