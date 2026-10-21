// Lightweight manual-run verification for the 3 bug fixes in this pass.
//
// There is NO existing JS test framework/runner in this repo (no
// package.json, no jest/mocha config) -- this is a plain Node script, not
// automated CI coverage. It drives the REAL frontend/app.core.js +
// frontend/app.live.js against a REAL running backend (TIP_MODE=demo) over
// real HTTP, using a minimal hand-rolled DOM stub (just enough getElementById
// elements/attributes for these two files to run unmodified). Run it with:
//
//   node frontend/test_manual_bugfixes.mjs <backend_base_url>
//
// e.g. node frontend/test_manual_bugfixes.mjs http://127.0.0.1:8899
//
// It is a smoke/regression check for THIS fix, not a permanent suite -- see
// the PR report's "Verification" section for the full manual checklist
// (clicking through the real UI) that this does not replace.

import assert from "node:assert/strict";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

// --- minimal DOM stub -------------------------------------------------
class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.children = []; this.listeners = {}; this._nextSibling = null; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  get style() { return this._style; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  classList = { add() {}, remove() {}, toggle() {}, contains: (c) => (this._html || "").includes(`class="${c}"`) };
  // Minimal real-DOM-shaped sibling support (2026-10-14): mountCandlestickChart
  // now attaches an info panel via root.after(el) / reads root.nextElementSibling
  // to remove a stale one on re-render. A plain object stand-in (not a real
  // FakeEl) is enough -- the code under test only reads .classList.contains
  // and .remove() on it.
  get nextElementSibling() { return this._nextSibling; }
  after(node) {
    node.remove = () => { if (this._nextSibling === node) this._nextSibling = null; };
    this._nextSibling = node;
  }
}

// document.createElement stub -- mountCandlestickChart's info panel creates
// a plain div via this, sets className/innerHTML, then attaches it with
// root.after(). Needs classList.contains for the de-dup check on re-render.
function fakeCreateElement() {
  const node = { className: "", _html: "", style: {} };
  Object.defineProperty(node, "innerHTML", { set(v) { this._html = v; }, get() { return this._html; } });
  node.classList = { contains: (c) => node.className.split(/\s+/).includes(c) };
  return node;
}

const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, new FakeEl(id));
  return elements.get(id);
}

global.document = {
  getElementById: (id) => el(id),
  querySelectorAll: () => [],
  // loadRadar() does `document.querySelector("#otherTable tbody").innerHTML
  // = ...` -- reuse the same id-keyed FakeEl cache so that selector gets a
  // real, settable element instead of null (a null return here is what a
  // naive stub produces, and it crashes loadRadar with "Cannot set
  // properties of null", which is exactly the failure this harness hit
  // before this fix -- not a bug in the app code under test).
  querySelector: (sel) => el(sel),
  createElement: () => fakeCreateElement(),
};
global.history = { replaceState: () => {}, pushState: () => {} };
global.location = { pathname: "/", search: "", hash: "" };
// Minimal window stub: app.core.js's wireHistoryHandling() registers a real
// popstate listener on `window`, and app.live.js/app.static.js stash
// TIP_NAV_HANDLERS on it. Not exercising real browser history here (that's
// done separately against a real backend+browser) -- just enough for the
// module to load and for other checks in this file to run unmodified.
global.window = { addEventListener: () => {}, TIP_NAV_HANDLERS: null };
global.ResizeObserver = class { observe() {} };
// Minimal fake of the vendored LightweightCharts global so
// mountCandlestickChart() (app.core.js) runs its real logic end to end
// (data mapping, marker time-matching, unavailable-state branch) without
// needing an actual browser canvas. Captures what the real function
// would have handed to the real library, for the chart checks below.
// C1 activity timeline (2026-10-15): mountCandlestickChart now ALSO
// triggers a second, separate createChart() call (mountActivityTimeline,
// a flow-alert/dark-pool histogram pane) -- that second chart never
// calls addCandlestickSeries, so `lastChartCall` is only (re)assigned
// from inside addCandlestickSeries below, keeping it pointed at the
// actual candlestick chart regardless of how many other chart
// instances get created afterward.
let lastChartCall = null;
global.LightweightCharts = {
  createChart: () => {
    const call = { bars: null, markers: null, width: null, volume: null };
    return {
      addCandlestickSeries: () => {
        lastChartCall = call;
        return {
          setData: (d) => { call.bars = d; },
          setMarkers: (m) => { call.markers = m; },
        };
      },
      // Phase B (2026-10-14): volume histogram series fake, mirroring the
      // real lightweight-charts API shape mountCandlestickChart() now uses
      // (priceScale().applyOptions(...), setData(...)).
      addHistogramSeries: () => ({
        priceScale: () => ({ applyOptions: () => {} }),
        setData: (d) => { call.volume = d; },
      }),
      timeScale: () => ({ fitContent: () => {} }),
      applyOptions: (opts) => { if (opts && opts.width) call.width = opts.width; },
    };
  },
};
// Real fetch against the real running backend (Node's built-in global
// fetch, Node >=18) -- no response mocking whatsoever.

// Load the real source files and eval them in this global context so they
// wire up exactly as they do in a browser (functions become globals).
import fs from "node:fs";
import { fileURLToPath } from "node:url";
// NOTE: app.core.js and app.live.js must be eval'd TOGETHER in one indirect
// eval() call, not two separate calls -- a real browser loads them as two
// <script> tags sharing ONE global scope, but Node's indirect eval() gives
// each separate call its own top-level declarative scope for `const`
// /`let`/`function`, so a second call can't see the first's declarations
// (confirmed experimentally: a 2-call split raised "esc is not defined").
// Concatenating them reproduces the real browser's single shared scope.
function loadReal(paths, exportNames) {
  // Strip the bootstrap IIFE + nav/button wiring at the bottom of
  // app.live.js BEFORE concatenating -- app.core.js also contains an
  // (unrelated, earlier) ".nav-item" querySelectorAll inside switchView(),
  // so running this same regex against the already-joined text matched
  // from THAT occurrence through to a much later "});" and silently ate
  // real functions (e.g. esc()) in between. Stripping each file's own
  // trailing wiring independently, then joining, avoids that.
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
  // Override API_BASE indirectly isn't possible (const in module scope of
  // app.live.js, read at module-eval time from location.search) -- set
  // location.search before loading. Re-load with query param instead.
  globalThis.__TIP_TEST_API_BASE__ = BASE;
  const liveB = loadReal(
    [
      fileURLToPath(new URL("./app.core.js", import.meta.url)),
      fileURLToPath(new URL("./app.live.js", import.meta.url)),
    ],
    [
      "setMode", "loadRadar", "loadTickerDetail", "navigateToTicker", "TIP_STATE",
      "loadSettings", "testUwKey", "saveUwKey", "disconnectUwKey", "apiPost", "apiGet",
      "mountCandlestickChart", "nearestBarTime", "dateOnly",
      // Unified ticker combobox (2026-10-14) -- _comboFilter is a plain
      // `function` declaration, so (unlike the `let`-declared pool/state
      // variables it closes over) it IS exposed as a global by indirect
      // eval, same mechanism every other name in this list relies on.
      "_comboFilter", "getRecentTickers", "setTickerComboValue",
    ],
  );

  console.log(`\n=== Bug 2: all 5 scenario tickers reachable via the ticker combobox ===`);
  await liveB.loadRadar();
  // Unified combobox (2026-10-14): _comboFilter("") returns the full
  // suggestion pool (empty query -> everything, capped at
  // MAX_SUGGESTIONS_SHOWN=8) -- 5 tickers fits under that cap.
  const pool = liveB._comboFilter("").map((e) => e.ticker);
  check("ticker combobox suggestions include all 5 scenario tickers (FLUX,QRVX,ZYNC,DRFT,NADA)", () => {
    for (const t of ["FLUX", "QRVX", "ZYNC", "DRFT", "NADA"]) {
      assert.ok(pool.includes(t), `missing ${t} in combobox pool: ${JSON.stringify(pool)}`);
    }
  });
  check("ticker combobox has no stale/placeholder entries in the suggestion pool", () => {
    assert.ok(!pool.includes(""), JSON.stringify(pool));
  });

  console.log(`\n=== Bug 1: mode switch clears ticker/investigate state ===`);
  await liveB.loadTickerDetail("FLUX");
  check("ticker detail populated before mode switch", () => {
    assert.ok(el("tickerDetail").innerHTML.includes("FLUX"));
    assert.ok(liveB.TIP_STATE.currentTicker === null || true); // loadTickerDetail doesn't set currentTicker itself (navigateToTicker does) -- real check below
  });
  // Simulate the real navigateToTicker path (Radar "Investigate" click) so
  // TIP_STATE.currentTicker is actually populated like production code does.
  await liveB.navigateToTicker("FLUX", liveB.loadTickerDetail);
  check("TIP_STATE.currentTicker set before switch", () => {
    assert.equal(liveB.TIP_STATE.currentTicker, "FLUX");
  });
  await liveB.setMode("demo"); // already demo; still must run the clearing logic
  check("TIP_STATE.currentTicker cleared after setMode", () => {
    assert.equal(liveB.TIP_STATE.currentTicker, null);
  });
  check("#tickerDetail reset to prompt state after setMode", () => {
    assert.ok(el("tickerDetail").innerHTML.includes("Select a ticker to analyze"));
    assert.ok(!el("tickerDetail").innerHTML.includes("FLUX"));
  });
  check("#investigateSection cleared after setMode", () => {
    assert.equal(el("investigateSection").innerHTML, "");
  });
  check("#tickerInput value reset to empty after setMode", () => {
    assert.equal(el("tickerInput").value, "");
  });

  console.log(`\n=== Bug 3: DRFT/NADA load without crashing Investigate ===`);
  for (const t of ["DRFT", "NADA"]) {
    await liveB.loadTickerDetail(t);
    check(`${t}: ticker detail loaded (no error)`, () => {
      assert.ok(el("tickerDetail").innerHTML.includes(t));
      assert.ok(!el("tickerDetail").innerHTML.toLowerCase().includes("error loading"));
    });
    check(`${t}: investigate section rendered (no crash)`, () => {
      assert.ok(el("investigateSection").innerHTML.includes("Investigate with AI"));
    });
  }

  console.log(`\n=== Priority 1/2: candlestick chart + evidence markers (real backend data) ===`);
  lastChartCall = null;
  await liveB.loadTickerDetail("FLUX");
  check("FLUX: chart mounted with real OHLC bars from the backend", () => {
    assert.ok(lastChartCall, "LightweightCharts.createChart was never called");
    assert.ok(Array.isArray(lastChartCall.bars) && lastChartCall.bars.length > 0, "no bars passed to setData");
    for (const b of lastChartCall.bars) {
      assert.ok(typeof b.time === "string" && typeof b.open === "number" && typeof b.close === "number");
    }
  });
  // 2026-10-17 (chart-evidence-markers, round 2): the chart no longer
  // calls LightweightCharts' native series.setMarkers() at all -- the
  // overlapping always-on text labels near the chart's right edge (the
  // real SPY demo-video defect), and the dot-overlay replacement that
  // was itself rejected after Chad reviewed real SPY/NVDA screenshots,
  // are both replaced by mountEvidenceHistogram()'s native histogram
  // pane on the same chart instance -- which this minimal FakeEl DOM
  // stub (no real addHistogramSeries/subscribeCrosshairMove) deliberately
  // no-ops for -- that real-DOM behavior is covered by the dedicated
  // real-Chromium/CDP test (frontend/test_chart_evidence_markers_cdp.py),
  // which asserts actual rendered histogram bar data and tooltip content.
  // This check instead verifies, against the REAL running backend, that
  // the exact same alignment logic mountCandlestickChart uses
  // (nearestBarTime) still correctly maps real priced markers onto real
  // bar times -- i.e. the data this feature depends on is genuinely
  // present and genuinely alignable, even though this harness can't see
  // the rendered histogram bars themselves.
  const fluxOhlc = await (await fetch(`${BASE}/api/ticker/FLUX/ohlc`)).json();
  check("FLUX: real backend OHLC response includes at least one priced marker", () => {
    const priced = (fluxOhlc.markers || []).filter((m) => m.price != null);
    assert.ok(priced.length > 0, `no priced marker found: ${JSON.stringify(fluxOhlc.markers)}`);
  });
  check("FLUX: every real marker's nearestBarTime maps onto an actually-rendered bar", () => {
    const barTimes = lastChartCall.bars.map((b) => b.time).sort();
    for (const m of fluxOhlc.markers || []) {
      const mapped = liveB.nearestBarTime(liveB.dateOnly(m.time), barTimes);
      assert.ok(mapped === null || barTimes.includes(mapped), `marker ${m.time} mapped to non-existent bar ${mapped}`);
    }
  });

  console.log(`\n=== Priority 1/2: chart degrades to an explicit message, never blank/silent, on unavailable data ===`);
  check("mountCandlestickChart renders an explicit unavailable message, not a blank container", () => {
    el("ohlcChartContainer").innerHTML = "<div>stale</div>";
    liveB.mountCandlestickChart("ohlcChartContainer", { unavailable: true, reason: { message: "test-forced unavailable" } });
    const html = el("ohlcChartContainer").innerHTML;
    assert.ok(html.toLowerCase().includes("unavailable"), html);
    assert.ok(html.includes("test-forced unavailable"), html);
  });

  console.log(`\n=== Priority 4: every scenario ticker's Ticker Analysis loads without crashing ===`);
  for (const t of ["FLUX", "QRVX", "ZYNC", "DRFT", "NADA"]) {
    await liveB.loadTickerDetail(t);
    check(`${t}: selectable and loadable via Ticker Analysis`, () => {
      assert.ok(el("tickerDetail").innerHTML.includes(t));
    });
  }

  console.log(`\n=== Security: Activate Live Mode stays disabled until Test Connection passes ===`);
  // Button markup is rendered as a string INTO #view-settings's innerHTML
  // (there is no separate live DOM node this stub can address by id), so
  // inspect that container's HTML for the button's own disabled attribute
  // rather than a same-named-but-unrelated FakeEl.
  function liveButtonDisabled() {
    const html = el("view-settings").innerHTML;
    const m = /<button[^>]*id="btnModeLive"[^>]*>/.exec(html);
    assert.ok(m, `btnModeLive not found in rendered Settings HTML: ${html.slice(0, 300)}`);
    return m[0].includes("disabled");
  }

  // Clean slate: remove any key left over from a previous run of this
  // backend instance, then drive the real Settings flow end to end.
  await liveB.disconnectUwKey().catch(() => {});
  await liveB.loadSettings();
  check("Activate Live Mode disabled with no key configured", () => {
    assert.equal(liveButtonDisabled(), true);
  });
  el("uwKeyInput").value = "uwtest_sk_live_MANUALTESTKEY000111";
  await liveB.saveUwKey();
  await liveB.loadSettings();
  check("Activate Live Mode disabled right after Save (not yet tested)", () => {
    assert.equal(liveButtonDisabled(), true);
  });
  const modeResp = await liveB.apiPost("/api/settings/mode", { mode: "live" }).catch((e) => e);
  check("server rejects live activation (400) for an unverified key even if the UI button were bypassed", () => {
    assert.ok(modeResp instanceof Error, "expected the real backend to reject this, not accept it");
  });
  // This is a fake, non-functional key (there is no real UW credential in
  // this sandbox), so the REAL Test Connection call below is expected to
  // fail (network_error or unauthorized against the real UW API) -- the
  // point of this check is that it STAYS disabled/rejected after a real,
  // non-stubbed test attempt, not that a fake key magically passes.
  // NETWORK SAFETY (signal-integrity remediation): this step makes the
  // backend perform a REAL Test Connection request to whatever UW base URL
  // it was started with. Refuse to run it against the public UW host
  // unless the operator explicitly opted in -- start the backend with
  // UW_BASE_URL=http://127.0.0.1:9 (a dead local port) for normal runs.
  const netSettings = await (await fetch(`${BASE}/api/settings`)).json();
  if (netSettings.uw && netSettings.uw.base_url_is_default !== false && process.env.TIP_ALLOW_UW_NETWORK !== "1") {
    throw new Error(
      "Refusing to run the real Test Connection step: the backend under test still points at the public " +
      "Unusual Whales API. Restart it with UW_BASE_URL=http://127.0.0.1:9, or set TIP_ALLOW_UW_NETWORK=1 to opt in."
    );
  }
  await liveB.testUwKey();
  check("Activate Live Mode still disabled after a real (failing) Test Connection attempt on a fake key", () => {
    assert.equal(liveButtonDisabled(), true);
  });
  const stillRejected = await liveB.apiPost("/api/settings/mode", { mode: "live" }).catch((e) => e);
  check("server still rejects live activation after a failed Test Connection", () => {
    assert.ok(stillRejected instanceof Error);
  });

  console.log(`\n=== Security: Settings discloses a remaining .env key after Remove Saved Key ===`);
  await liveB.disconnectUwKey();
  await liveB.loadSettings();
  check("no env-fallback disclosure when no .env key is configured on this backend", () => {
    assert.ok(!el("view-settings").innerHTML.includes("env-configured key is still present"));
  });

  console.log(`\n=== Robustness: rapid ticker switch does not show stale data (race guard) ===`);
  // Fire two loadTickerDetail calls back-to-back without awaiting the
  // first -- the real backend responses can arrive in either order, but
  // the sequence-number guard in app.live.js must ensure only the LAST
  // navigation's response is ever rendered, never the first ticker's
  // stale data clobbering the screen after the user already moved on.
  const firstCall = liveB.loadTickerDetail("FLUX");
  const secondCall = liveB.loadTickerDetail("QRVX");
  await Promise.all([firstCall, secondCall]);
  check("tickerDetail shows the LAST-navigated ticker (QRVX), not the superseded FLUX", () => {
    assert.ok(el("tickerDetail").innerHTML.includes("QRVX"), el("tickerDetail").innerHTML.slice(0, 200));
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
