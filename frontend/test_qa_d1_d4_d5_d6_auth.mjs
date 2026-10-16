// QA remediation D1/D4/D5/D6 frontend regression tests.
//
// Same convention as test_discovery.mjs/test_radar_filters.mjs: no JS
// test framework in this repo by design -- a plain Node script
// indirect-`eval()`ing the REAL frontend/app.core.js + app.live.js
// together (exactly like two real <script> tags). This file mocks
// global.fetch directly rather than hitting a real running backend --
// legitimate per this repo's own established pattern (test_discovery.mjs
// mocks at the HTTP layer too) and faster/more deterministic for these
// specific UI-state assertions, which don't depend on real backend
// business logic.
//
// Run with: node frontend/test_qa_d1_d4_d5_d6_auth.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

// --- minimal DOM stub (same shape as test_navigation.mjs) ---------------
class FakeEl {
  constructor(id) {
    this.id = id;
    this._html = "";
    this._value = "";
    this._type = "text";
    this._style = {};
    this.listeners = {};
  }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  set type(v) { this._type = v; }
  get type() { return this._type; }
  get style() { return this._style; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  querySelectorAll(sel) {
    // Minimal support for the real banner markup: find elements by id
    // substring match inside the rendered innerHTML, used only by the
    // reveal-toggle / pairing wiring tests below (never by app code
    // itself in a way these tests exercise).
    return [];
  }
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
global.location = { pathname: "/", search: "", hash: "" };
global.window = { listeners: {}, TIP_NAV_HANDLERS: null };
global.window.addEventListener = (ev, fn) => {
  (global.window.listeners[ev] = global.window.listeners[ev] || []).push(fn);
};
global.history = { pushState() {}, replaceState() {} };

const store = {};
global.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};

function loadReal() {
  const corePath = fileURLToPath(new URL("./app.core.js", import.meta.url));
  const livePath = fileURLToPath(new URL("./app.live.js", import.meta.url));
  const coreSrc = fs.readFileSync(corePath, "utf8");
  const liveSrc = fs
    .readFileSync(livePath, "utf8")
    .replace(/document\.querySelectorAll\("\.nav-item"\)[\s\S]*?\}\);\n/, "")
    .replace(/document\.getElementById\("refreshRadar"\)[\s\S]*?loadRadar\);\n/, "")
    .replace(/document\.getElementById\("loadTicker"\)[\s\S]*?\}\);\n/, "")
    .replace(/\(async function bootstrap[\s\S]*?\}\)\(\);\n?/, "");
  const combined = `${coreSrc}\n${liveSrc}`;
  (0, eval)(
    combined +
      "\nglobalThis.__exports = { renderAuthSetupBanner, showAuthSetupBanner, hideAuthSetupBanner, " +
      "ensureAuthToken, onAuthRestored, getAuthToken, setAuthToken, loadMarketWideRadar, TIP_STATE };"
  );
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
async function checkAsync(name, fn) {
  try {
    await fn();
    console.log(`PASS: ${name}`);
    passed++;
  } catch (e) {
    console.log(`FAIL: ${name} -- ${e.message}`);
    process.exitCode = 1;
  }
}

async function main() {
  const app = loadReal();

  // --- D5: masked by default -------------------------------------------
  check("D5: manual token input is type=password by default (not type=text)", () => {
    const html = app.renderAuthSetupBanner("missing");
    const match = html.match(/<input[^>]*id="authManualTokenInput"[^>]*>/);
    assert.ok(match, "authManualTokenInput not found in rendered banner");
    assert.ok(match[0].includes('type="password"'), `expected type="password", got: ${match[0]}`);
    assert.ok(!match[0].includes('type="text"'), "token input must not default to type=text");
  });

  check("D5: a reveal toggle control exists alongside the masked token field", () => {
    const html = app.renderAuthSetupBanner("missing");
    assert.ok(html.includes('id="authManualTokenRevealBtn"'), "no reveal toggle button found");
  });

  // --- D6: failure shows a clear, distinct, actionable banner -----------
  check("D6: bootstrap_failed variant renders distinct, actionable copy (pairing code path)", () => {
    const html = app.renderAuthSetupBanner("bootstrap_failed");
    assert.ok(html.includes("automatically set up access"), "missing the distinct failure heading");
    assert.ok(html.toLowerCase().includes("docker desktop"), "missing the actionable Docker Desktop explanation");
    assert.ok(html.includes("authPairingCodeInput"), "missing the pairing-code entry box (D1's fix)");
  });

  await checkAsync("D6: ensureAuthToken() shows the banner when auto-bootstrap fails", async () => {
    global.fetch = async () => ({ ok: false, status: 403, json: async () => ({}) });
    const bannerEl = el("authSetupBanner");
    bannerEl.style.display = "none";
    bannerEl.innerHTML = "";
    const ok = await app.ensureAuthToken();
    assert.equal(ok, false);
    assert.notEqual(bannerEl.style.display, "none", "banner must be shown (display cleared) on failure");
    assert.ok(bannerEl.innerHTML.length > 0, "banner must have real content on failure");
  });

  await checkAsync("D6: ensureAuthToken() never shows a banner when auto-bootstrap succeeds", async () => {
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ token: "real-token-abc" }) });
    const bannerEl = el("authSetupBanner");
    bannerEl.style.display = "none";
    bannerEl.innerHTML = "";
    const ok = await app.ensureAuthToken();
    assert.equal(ok, true);
    assert.equal(bannerEl.style.display, "none", "banner must stay hidden on a successful bootstrap");
    assert.equal(bannerEl.innerHTML, "", "banner must have zero content on a successful bootstrap");
  });

  // --- D4: auth recovery actually re-renders the stuck view --------------
  await checkAsync(
    "D4: after a successful auth event, a view previously showing an auth error re-fetches fresh content",
    async () => {
      // Simulate the EXACT stuck state D4 describes: Market-wide Radar's
      // list element is still showing the old 401 error message.
      const listEl = el("marketWideCandidateList");
      listEl.innerHTML = '<div class="empty">Failed to load Market-wide Radar: 401</div>';
      const statusEl = el("marketWideStatus");
      statusEl.innerHTML = "";
      el("schedulerControls").innerHTML = "";

      app.setAuthToken("freshly-restored-token");
      let call = 0;
      global.fetch = async (url) => {
        call += 1;
        if (String(url).includes("/api/scheduler/status")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              mode: "dry_run", last_run_finished_at: null, next_run_at: null,
              last_cycle_candidates_evaluated: null, freshness_window_s: 1200, pause_reason: null,
            }),
          };
        }
        if (String(url).includes("/api/radar/market-wide")) {
          return {
            ok: true,
            status: 200,
            json: async () => ({
              candidates: [{ ticker: "NVDA", freshness: "current", evaluation: { confidence: "LOW" } }],
              snapshot_at: "2026-10-15T14:00:00Z", coverage_note: "test",
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      };

      await app.onAuthRestored();

      assert.ok(call >= 2, "onAuthRestored() must actually re-fetch, not just clear the UI locally");
      assert.ok(
        !listEl.innerHTML.includes("Failed to load") && !listEl.innerHTML.includes("401"),
        `stale auth-error content must be gone after re-render, got: ${listEl.innerHTML}`
      );
      assert.ok(listEl.innerHTML.includes("NVDA"), "the freshly-fetched candidate must now be rendered");
    }
  );

  console.log(`\n${passed} check(s) passed.`);
}

main();
