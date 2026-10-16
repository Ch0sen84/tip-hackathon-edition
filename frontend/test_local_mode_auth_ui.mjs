// Local Mode / Network Mode (Fetch Metadata auth redesign) frontend
// regression tests -- refinement #5: Local Mode must show ZERO
// authentication UI, Network Mode's existing banner/token flow must
// stay fully intact. Same established pattern as
// test_qa_d1_d4_d5_d6_auth.mjs: a plain Node script indirect-`eval()`ing
// the REAL app.core.js + app.live.js together, mocking global.fetch
// rather than hitting a real backend.
//
// Run with: node frontend/test_local_mode_auth_ui.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

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
      "\nglobalThis.__exports = { fetchAuthMode, showAuthSetupBanner, ensureAuthToken, " +
      "getTipAuthMode: () => TIP_AUTH_MODE, setTipAuthMode: (v) => { TIP_AUTH_MODE = v; } };"
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

  await checkAsync("fetchAuthMode() returns 'local' when the backend reports local mode", async () => {
    global.fetch = async (url) => {
      assert.ok(String(url).includes("/api/auth/mode"));
      return { ok: true, status: 200, json: async () => ({ mode: "local", local_mode_active: true }) };
    };
    const mode = await app.fetchAuthMode();
    assert.equal(mode, "local");
  });

  await checkAsync("fetchAuthMode() returns 'network' when the backend reports network mode", async () => {
    global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ mode: "network", local_mode_active: false }) });
    const mode = await app.fetchAuthMode();
    assert.equal(mode, "network");
  });

  await checkAsync("fetchAuthMode() fails CLOSED to 'network' if the mode-check call itself fails", async () => {
    global.fetch = async () => { throw new Error("network down"); };
    const mode = await app.fetchAuthMode();
    assert.equal(mode, "network");
  });

  await checkAsync("fetchAuthMode() fails CLOSED to 'network' on a non-200 response", async () => {
    global.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });
    const mode = await app.fetchAuthMode();
    assert.equal(mode, "network");
  });

  check("showAuthSetupBanner() is a complete no-op in Local Mode (refinement #5: zero auth UI)", () => {
    app.setTipAuthMode("local");
    const bannerEl = el("authSetupBanner");
    bannerEl.style.display = "none";
    bannerEl.innerHTML = "";
    app.showAuthSetupBanner("bootstrap_failed");
    assert.equal(bannerEl.style.display, "none", "banner must stay hidden in Local Mode even if asked to show");
    assert.equal(bannerEl.innerHTML, "", "banner must stay empty in Local Mode even if asked to show");
  });

  check("showAuthSetupBanner() still renders normally in Network Mode (unmodified behavior)", () => {
    app.setTipAuthMode("network");
    const bannerEl = el("authSetupBanner");
    bannerEl.style.display = "none";
    bannerEl.innerHTML = "";
    app.showAuthSetupBanner("bootstrap_failed");
    assert.notEqual(bannerEl.style.display, "none", "Network Mode's banner must still appear, unchanged");
    assert.ok(bannerEl.innerHTML.length > 0, "Network Mode's banner must still have real content, unchanged");
  });

  console.log(`\n${passed} check(s) passed.`);
}

main();
