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

const BASE = process.argv[2] || "http://127.0.0.1:8899";

// --- minimal DOM stub -------------------------------------------------
class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.children = []; this.listeners = {}; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
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
  // loadRadar() does `document.querySelector("#otherTable tbody").innerHTML
  // = ...` -- reuse the same id-keyed FakeEl cache so that selector gets a
  // real, settable element instead of null (a null return here is what a
  // naive stub produces, and it crashes loadRadar with "Cannot set
  // properties of null", which is exactly the failure this harness hit
  // before this fix -- not a bug in the app code under test).
  querySelector: (sel) => el(sel),
};
global.history = { replaceState: () => {} };
global.location = { pathname: "/", search: "", hash: "" };
// Real fetch against the real running backend (Node's built-in global
// fetch, Node >=18) -- no response mocking whatsoever.

// Load the real source files and eval them in this global context so they
// wire up exactly as they do in a browser (functions become globals).
import fs from "node:fs";
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

async function main() {
  // Override API_BASE indirectly isn't possible (const in module scope of
  // app.live.js, read at module-eval time from location.search) -- set
  // location.search before loading. Re-load with query param instead.
  global.location.search = `?api=${BASE}`;
  const liveB = loadReal(
    [
      new URL("./app.core.js", import.meta.url).pathname,
      new URL("./app.live.js", import.meta.url).pathname,
    ],
    ["setMode", "loadRadar", "loadTickerDetail", "setCurrentTicker", "TIP_STATE"],
  );

  console.log(`\n=== Bug 2: all 5 scenario tickers reachable via the <select> ===`);
  await liveB.loadRadar();
  const selHtml = el("tickerSelect").innerHTML;
  check("tickerSelect includes all 5 scenario tickers (FLUX,QRVX,ZYNC,DRFT,NADA)", () => {
    for (const t of ["FLUX", "QRVX", "ZYNC", "DRFT", "NADA"]) {
      assert.ok(selHtml.includes(`value="${t}"`), `missing ${t} in <select>: ${selHtml}`);
    }
  });
  check("tickerSelect has a placeholder option", () => {
    assert.ok(selHtml.startsWith('<option value="">'), selHtml.slice(0, 80));
  });

  console.log(`\n=== Bug 1: mode switch clears ticker/investigate state ===`);
  await liveB.loadTickerDetail("FLUX");
  check("ticker detail populated before mode switch", () => {
    assert.ok(el("tickerDetail").innerHTML.includes("FLUX"));
    assert.ok(liveB.TIP_STATE.currentTicker === null || true); // loadTickerDetail doesn't set currentTicker itself (navigateToTicker does) -- real check below
  });
  // Simulate the real navigateToTicker path (Radar "Investigate" click) so
  // TIP_STATE.currentTicker is actually populated like production code does.
  liveB.setCurrentTicker("FLUX");
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
  check("#tickerSelect value reset to placeholder after setMode", () => {
    assert.equal(el("tickerSelect").value, "");
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

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
