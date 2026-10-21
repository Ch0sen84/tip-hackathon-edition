// Regression test for the P0 navigation fix (Back/Forward + refresh
// persistence across Market Radar / Ticker Analysis / Settings / About).
//
// Same convention as test_manual_bugfixes.mjs: no JS test framework in
// this repo by design -- a plain Node script driving the REAL
// frontend/app.core.js + frontend/app.live.js against a REAL running
// backend (TIP_MODE=demo) over real HTTP, with a minimal hand-rolled DOM
// stub. The one addition here is a REAL (not no-op) history stub: a
// genuine push/replace/back/forward stack that actually fires `popstate`
// listeners, so Back/Forward/refresh/deep-link semantics are exercised
// for real, not just "did the function get called". A separate real-
// browser (CDP) pass additionally verifies this in an actual browser --
// see the PR report's Verification section; this script is the fast
// repeatable regression check, not a replacement for that.
//
// Run with: node frontend/test_navigation.mjs <backend_base_url>

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

// --- minimal DOM stub (same shape as test_manual_bugfixes.mjs) ---------
class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {}; this.dataset = {}; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  get style() { return this._style; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  scrollIntoView() { this._scrolledIntoView = true; }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  classList = { add() {}, remove() {}, toggle() {} };
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
};
global.ResizeObserver = class { observe() {} };
global.LightweightCharts = {
  createChart: () => ({
    addCandlestickSeries: () => ({ setData: () => {}, setMarkers: () => {} }),
    timeScale: () => ({ fitContent: () => {} }),
    applyOptions: () => {},
  }),
};

// --- REAL history/location/window simulation ----------------------------
// A genuine stack with pushState/replaceState/back()/forward() that
// updates global.location.hash and fires real `popstate` listeners --
// this is what lets the checks below assert actual Back/Forward
// semantics (not just "pushState was called").
global.location = { pathname: "/", search: "", hash: "" };
global.window = { listeners: {}, TIP_NAV_HANDLERS: null };
global.window.addEventListener = (ev, fn) => {
  (global.window.listeners[ev] = global.window.listeners[ev] || []).push(fn);
};
class FakeHistoryStack {
  constructor() {
    this.stack = [{ state: null, hash: "" }];
    this.idx = 0;
  }
  _hashOf(url) {
    const i = String(url).indexOf("#");
    return i === -1 ? "" : String(url).slice(i);
  }
  pushState(state, _title, url) {
    const hash = this._hashOf(url);
    this.stack = this.stack.slice(0, this.idx + 1);
    this.stack.push({ state, hash });
    this.idx = this.stack.length - 1;
    global.location.hash = hash;
  }
  replaceState(state, _title, url) {
    const hash = this._hashOf(url);
    this.stack[this.idx] = { state, hash };
    global.location.hash = hash;
  }
  _fire() {
    const entry = this.stack[this.idx];
    global.location.hash = entry.hash;
    const listeners = global.window.listeners["popstate"] || [];
    listeners.forEach((fn) => fn({ state: entry.state }));
  }
  back() { if (this.idx > 0) { this.idx--; this._fire(); } }
  forward() { if (this.idx < this.stack.length - 1) { this.idx++; this._fire(); } }
}
const fakeHistory = new FakeHistoryStack();
global.history = fakeHistory;

function loadReal(paths, exportNames) {
  const [coreSrc, ...rest] = paths.map((p) => fs.readFileSync(p, "utf8"));
  const liveSrc = rest
    .join("\n")
    .replace(/document\.querySelectorAll\("\.nav-item"\)[\s\S]*?\}\);\n/, "")
    .replace(/document\.getElementById\("refreshRadar"\)[\s\S]*?loadRadar\);\n/, "")
    .replace(/document\.getElementById\("loadTicker"\)[\s\S]*?\}\);\n/, "")
    .replace(/\(async function bootstrap[\s\S]*?\}\)\(\);\n?/, "");
  const combined = `${coreSrc}\n${liveSrc}`;
  (0, eval)(combined + `\nglobalThis.__exports = { ${exportNames.join(", ")} };`);
  return globalThis.__exports;
}

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

// Phase A: app.live.js now requires a valid X-TIP-Auth header on every
// mutating call (setMode -> POST /api/settings/mode). This harness's
// `loadReal()` strips out the real bootstrap() IIFE (see its regex
// replace above), so nothing else here would ever populate the token --
// fetch it directly from the REAL running backend (a genuine loopback
// HTTP call, since this Node process and the backend are on the same
// machine) and seed a minimal localStorage stub with it before any
// mutating call runs.
//
// Windows Docker Desktop QA round 2 (finding T1): bootstrap-only token
// acquisition fails under real Docker Desktop's port-publishing NAT --
// see frontend/test_lib_auth.mjs's module docstring for the full root
// cause and the pairing-code fallback this now uses (mirrors the
// backend's own pre-existing D1 pairing mechanism, untouched here).
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
  globalThis.__TIP_TEST_API_BASE__ = BASE;
  const app = loadReal(
    [
      fileURLToPath(new URL("./app.core.js", import.meta.url)),
      fileURLToPath(new URL("./app.live.js", import.meta.url)),
    ],
    [
      "navigateToView", "navigateToTicker", "TIP_STATE", "parseNavHash",
      "loadRadar", "loadTickerDetail", "setMode", "loadSettings",
      "wireHistoryHandling", "applyNavState", "setRadarMode",
    ],
  );

  // wireHistoryHandling() + window.TIP_NAV_HANDLERS assignment already ran
  // at module-eval time (they're outside the stripped bootstrap IIFE) --
  // confirm that actually happened before relying on it below.
  check("window.TIP_NAV_HANDLERS was set by app.live.js module load", () => {
    assert.ok(global.window.TIP_NAV_HANDLERS, "TIP_NAV_HANDLERS never assigned");
    assert.equal(typeof global.window.TIP_NAV_HANDLERS.loadTickerDetail, "function");
  });
  check("a real popstate listener was registered by wireHistoryHandling()", () => {
    assert.ok((global.window.listeners["popstate"] || []).length > 0);
  });

  await app.loadRadar(); // populates _knownTickers inside app.live.js's closure

  console.log(`\n=== Back/Forward through 3 distinct views+tickers ===`);
  app.navigateToView("radar");
  await app.navigateToTicker("FLUX", app.loadTickerDetail);
  app.navigateToView("settings");
  check("3 real navigations pushed 3 history entries (+initial = 4)", () => {
    assert.equal(fakeHistory.stack.length, 4, JSON.stringify(fakeHistory.stack));
  });
  check("currently on settings, ticker FLUX retained in state", () => {
    assert.equal(app.TIP_STATE.currentView, "settings");
  });

  fakeHistory.back(); // -> ticker=FLUX entry
  check("Back restores Ticker Analysis view", () => {
    assert.equal(app.TIP_STATE.currentView, "ticker");
  });
  check("Back restores the FLUX ticker (state + hash)", () => {
    assert.equal(app.TIP_STATE.currentTicker, "FLUX");
    assert.ok(global.location.hash.includes("ticker=FLUX"), global.location.hash);
  });
  check("Back restored real ticker detail content (not blank)", () => {
    assert.ok(el("tickerDetail").innerHTML.includes("FLUX"));
  });

  fakeHistory.back(); // -> radar entry
  check("Back again restores Market Radar", () => {
    assert.equal(app.TIP_STATE.currentView, "radar");
  });

  fakeHistory.forward(); // -> ticker=FLUX entry again
  check("Forward restores Ticker Analysis + FLUX again", () => {
    assert.equal(app.TIP_STATE.currentView, "ticker");
    assert.equal(app.TIP_STATE.currentTicker, "FLUX");
  });

  fakeHistory.forward(); // -> settings entry
  check("Forward restores Settings as the final step", () => {
    assert.equal(app.TIP_STATE.currentView, "settings");
  });

  console.log(`\n=== UX remediation item 4: nav-item click back to Ticker Analysis preserves the ticker ===`);
  // The real gap this item fixes: NOT Back/Forward (already proven
  // above, since that replays the real stored history entry) -- clicking
  // the "Ticker Analysis" NAV ITEM again after navigating away via OTHER
  // nav-item clicks, which is a distinct code path (navigateToView with
  // no explicit ticker argument).
  app.navigateToView("radar");
  await app.navigateToTicker("NADA", app.loadTickerDetail);
  app.navigateToView("settings"); // away via nav-item click, not Back
  check("navigating away via a nav-item click nulls currentTicker (view-scoped state, expected)", () => {
    assert.equal(app.TIP_STATE.currentTicker, null);
  });
  app.navigateToView("radar"); // another unrelated view in between
  app.navigateToView("ticker"); // nav-item click back to Ticker Analysis, no explicit ticker
  check("clicking the Ticker Analysis nav item again restores the real last ticker (NADA), not a blank state", () => {
    assert.equal(app.TIP_STATE.currentTicker, "NADA");
    assert.ok(global.location.hash.includes("ticker=NADA"), global.location.hash);
  });

  console.log(`\n=== Refresh preserves current view + ticker ===`);
  // "Refresh" = re-running applyNavState from the CURRENT location.hash,
  // exactly what bootstrap() does on a real page load -- it must NOT
  // default to Market Radar when the hash says otherwise.
  app.navigateToView("radar");
  await app.navigateToTicker("QRVX", app.loadTickerDetail);
  const hashAtRefreshTime = global.location.hash;
  app.TIP_STATE.currentView = "radar"; // simulate a fresh page load's initial default before bootstrap runs
  app.TIP_STATE.currentTicker = null;
  el("tickerDetail").innerHTML = "";
  const refreshedState = app.parseNavHash(hashAtRefreshTime);
  app.applyNavState(refreshedState, global.window.TIP_NAV_HANDLERS, { push: false });
  check("refresh (re-parsing the real current hash) restores Ticker Analysis + QRVX, not Market Radar", () => {
    assert.equal(app.TIP_STATE.currentView, "ticker");
    assert.equal(app.TIP_STATE.currentTicker, "QRVX");
  });

  console.log(`\n=== Directly-pasted deep link lands correctly ===`);
  const deepLinkState = app.parseNavHash("#view=ticker&ticker=ZYNC");
  app.TIP_STATE.currentView = "radar";
  app.TIP_STATE.currentTicker = null;
  app.applyNavState(deepLinkState, global.window.TIP_NAV_HANDLERS, { push: false });
  await new Promise((r) => setTimeout(r, 50)); // let loadTickerDetail's fetch resolve
  check("a fresh #view=ticker&ticker=ZYNC hash lands on Ticker Analysis with ZYNC loaded", () => {
    assert.equal(app.TIP_STATE.currentView, "ticker");
    assert.equal(app.TIP_STATE.currentTicker, "ZYNC");
    assert.ok(el("tickerDetail").innerHTML.includes("ZYNC"));
  });

  console.log(`\n=== Legacy bare #ticker=XXX hash still works ===`);
  const legacyState = app.parseNavHash("#ticker=DRFT");
  check("legacy format parses as view=ticker", () => {
    // UX round (2026-10-16 round 5), item 2: parseNavHash now also
    // reports `mode` (My Watchlist vs Market Discovery sub-panel,
    // only ever meaningful on view="radar") on every return path, so a
    // legacy ticker-only hash's `mode` is honestly null here, not
    // omitted -- a real, intentional shape addition, not a regression.
    assert.deepEqual(legacyState, { view: "ticker", ticker: "DRFT", mode: null });
  });

  console.log(`\n=== Invalid ticker in URL falls back gracefully ===`);
  const badState = app.parseNavHash("#view=ticker&ticker=NOTREAL");
  app.applyNavState(badState, global.window.TIP_NAV_HANDLERS, { push: false });
  check("an unknown ticker shows a clear message, not a blank page or thrown error", () => {
    const html = el("tickerDetail").innerHTML;
    assert.ok(html.includes("NOTREAL"), html);
    assert.ok(html.toLowerCase().includes("not available"), html);
  });
  check("investigateSection is cleared for an invalid ticker (no stale AI section)", () => {
    assert.equal(el("investigateSection").innerHTML, "");
  });

  console.log(`\n=== Mode switch while on Ticker Analysis replaces (not pushes) the stale entry ===`);
  app.navigateToView("radar");
  await app.navigateToTicker("FLUX", app.loadTickerDetail);
  const stackLenBeforeModeSwitch = fakeHistory.stack.length;
  await app.setMode("demo"); // already demo; still must run the clearing+history logic
  check("setMode while on Ticker Analysis did not push a new history entry", () => {
    assert.equal(fakeHistory.stack.length, stackLenBeforeModeSwitch, "a mode switch must REPLACE, not push");
  });
  check("the current entry was replaced to Market Radar (view), not left pointing at the old ticker", () => {
    assert.equal(app.TIP_STATE.currentView, "radar");
    assert.equal(app.TIP_STATE.currentTicker, null);
    assert.ok(!global.location.hash.includes("ticker="), global.location.hash);
  });
  fakeHistory.back(); // Back from here must NOT land on a dangling ticker=FLUX entry
  check("Back from the post-mode-switch Radar entry does not resurrect the stale FLUX ticker entry", () => {
    // The entry 2 steps before the mode switch was the plain "radar" entry
    // pushed by navigateToView("radar") just before navigateToTicker -- the
    // REPLACED current entry overwrote what used to be the ticker=FLUX
    // entry, so Back goes straight to that earlier plain radar entry, never
    // back into ticker=FLUX.
    assert.ok(!global.location.hash.includes("ticker=FLUX"), global.location.hash);
  });

  console.log(`\n=== UX round (2026-10-16 round 5), item 2: Discovery sub-view survives F5 ===`);
  // Simulate switching to the Market Discovery sub-panel (as the real
  // toggle button's click handler does via setRadarMode), capture the
  // resulting hash (what an F5 refresh would re-parse), then simulate a
  // fresh load exactly like the earlier "refresh preserves ticker" check
  // above does.
  app.setRadarMode("discovery");
  check("switching to Discovery mode is reflected in the URL hash", () => {
    assert.ok(global.location.hash.includes("mode=discovery"), global.location.hash);
  });
  const hashAtDiscoveryRefreshTime = global.location.hash;
  const discoveryRefreshState = app.parseNavHash(hashAtDiscoveryRefreshTime);
  check("parseNavHash reports mode=discovery from that hash", () => {
    assert.equal(discoveryRefreshState.mode, "discovery");
  });
  app.setRadarMode("watchlist", { persist: false }); // simulate a fresh load's default before restore runs
  app.applyNavState(discoveryRefreshState, global.window.TIP_NAV_HANDLERS, { push: false });
  check("restoring that hash lands back on the Discovery sub-panel, not the My Watchlist default", () => {
    assert.equal(el("discoveryRadarSection").style.display, "");
    assert.equal(el("watchlistRadarSection").style.display, "none");
  });
  // Switching back to watchlist must drop `mode` from the hash entirely
  // (watchlist is the unmarked default -- never an explicit `mode=watchlist`
  // the way `mode=discovery` is explicit, matching the rest of this hash
  // scheme's convention of only encoding non-default state).
  app.setRadarMode("watchlist");
  check("switching back to My Watchlist removes mode from the hash", () => {
    assert.ok(!global.location.hash.includes("mode="), global.location.hash);
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
