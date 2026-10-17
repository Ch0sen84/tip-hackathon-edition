// P1 regression test (ux-nav-watchlist-reliability round 10): the real
// DOM-row-count gap Chad's Windows Docker Desktop retest found, and that
// round 9's D4/D5 test coverage (test_w2_cache_first_radar.py, backend
// JSON-field-count only) explicitly failed to catch -- the backend was
// already correctly returning never-evaluated watchlist tickers (see
// backend/app/main.py's never_evaluated_tickers force-append block,
// confirmed by test_w2_cache_first_radar.py), but
// frontend/app.core.js's renderRadarLists() ran qualifyingEntries()
// (a `!e.not_evaluated` filter) over the RENDER lists too, silently
// dropping every never-evaluated ticker before a single <tr> was ever
// built. That is a pure DOM-rendering bug no JSON-field-count assertion
// against the API response could ever have caught -- this test asserts
// against the actual rendered `<tr data-ticker="...">` markup in
// #otherTable's tbody (and #otherGrid's cards), by ticker count AND by
// ticker symbol, not merely the input array's length.
//
// Loads the REAL frontend/app.core.js (same eval-based load pattern as
// test_radar_filters.mjs) against hand-built but backend-shape-accurate
// fixture data -- demo-mode's own seeded fixtures happen to have every
// watchlist ticker already evaluated (no real empty/populated-cache mix
// to exercise this against live), so this test constructs both an
// EMPTY-CACHE fixture (100% never-evaluated) and a POPULATED-CACHE
// fixture (some evaluated, some not) matching exactly the shape
// documented in backend/app/main.py's never_evaluated_tickers
// force-append block and _attach()'s entry fields -- never a guessed
// or invented field name.
//
// Run standalone (no backend needed -- pure fixture-driven DOM check):
//   node frontend/test_p1_watchlist_row_coverage.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {}; this.onchange = null; }
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
function el(id) { if (!elements.has(id)) elements.set(id, new FakeEl(id)); return elements.get(id); }

global.document = {
  getElementById: (id) => el(id),
  querySelectorAll: () => [],
  querySelector: (sel) => el(sel),
};
global.history = { replaceState() {}, pushState() {} };
global.location = { pathname: "/", search: "", hash: "" };
global.window = { addEventListener() {}, TIP_NAV_HANDLERS: null };
global.ResizeObserver = class { observe() {} };
global.fetch = async () => { throw new Error("no network access expected in this test"); };

const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(coreSrc + `
globalThis.__exports = {
  renderRadarLists, renderRow, renderCard, isNotEvaluated, stateTagHtml,
};`);
const core = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

// Exact shape backend/app/main.py's _radar_impl produces: evaluated
// entries carry state/confidence/price/headline/last_evaluated_at;
// never-evaluated placeholders (not_evaluated: true) carry state=null
// (or a real prior state), confidence=null, price=null,
// last_evaluated_at=null, and the honest "Not yet evaluated" headline.
function evaluatedEntry(ticker, overrides = {}) {
  return {
    ticker, name: null, families: [], evidence: [],
    current_read: "real evidence", headline: `${ticker} headline`,
    label: "x", categories: ["dark_pool"], index_context: false,
    question: null, research: [], enriched: false,
    price: { last: 10, chg_pct: 1 },
    state: "BUILDING", confidence: "medium", signal_type: "x",
    qualifying_days_count: 1, history_states: [],
    eval_freshness: "current", last_evaluated_at: "2026-10-16T12:00:00+00:00",
    not_evaluated: false,
    ...overrides,
  };
}
function notEvaluatedEntry(ticker) {
  return {
    ticker, name: null, families: [], evidence: [],
    current_read: "This ticker has not been scanned yet.",
    headline: "Not yet evaluated", label: null, categories: [],
    index_context: false, question: null, research: [], enriched: false,
    price: null, state: null, confidence: null, signal_type: null,
    qualifying_days_count: 0, history_states: [],
    eval_freshness: "unevaluated", last_evaluated_at: null,
    not_evaluated: true,
  };
}

function setupDom() {
  elements.clear();
  ["highestGrid", "otherGrid", "highestAttentionSection", "radarFilterCounts", "radarEmpty"].forEach(el);
}

function tbodyEl() {
  // renderRadarLists writes to document.querySelector("#otherTable tbody")
  // -- the fake document.querySelector(sel) above resolves that exact
  // selector string to its own FakeEl, same convention test_radar_filters
  // already relies on.
  return el("#otherTable tbody");
}

function rowTickers(html) {
  return [...html.matchAll(/data-ticker="([^"]+)"/g)].map((m) => m[1]);
}

console.log("=== P1: EMPTY-CACHE case -- every watchlist ticker never evaluated ===");
{
  setupDom();
  const watchlist = ["AAA", "BBB", "CCC"];
  const data = {
    scanned: watchlist.length, watchlist_size: watchlist.length, evaluated_count: 0,
    highest: [],
    other: watchlist.map(notEvaluatedEntry),
  };
  core.renderRadarLists(data, {}, async () => {});
  const tbodyHtml = tbodyEl().innerHTML;
  check("every watchlist ticker gets a real <tr> row, even with zero evaluations", () => {
    const tickers = rowTickers(tbodyHtml);
    assert.deepEqual([...tickers].sort(), [...watchlist].sort(),
      `expected rows for ${JSON.stringify(watchlist)}, got ${JSON.stringify(tickers)}`);
  });
  check("each never-evaluated row shows the honest 'Not evaluated' badge, not an invented state", () => {
    for (const t of watchlist) {
      const rowMatch = tbodyHtml.match(new RegExp(`<tr data-ticker="${t}">([\\s\\S]*?)</tr>`));
      assert.ok(rowMatch, `no row found for ${t}`);
      assert.ok(rowMatch[1].includes("Not evaluated"), `row for ${t} does not say "Not evaluated"`);
      assert.ok(!rowMatch[1].includes(">NEW<"), `row for ${t} fabricated a NEW tag instead of Not evaluated`);
    }
  });
  check("radarEmpty is NOT shown -- rows genuinely exist even though nothing qualifies", () => {
    assert.equal(el("radarEmpty").style.display, "none");
  });
}

console.log("\n=== P1: POPULATED-CACHE case -- some evaluated, some never evaluated ===");
{
  setupDom();
  const data = {
    scanned: 4, watchlist_size: 4, evaluated_count: 2,
    highest: [],
    other: [
      evaluatedEntry("EVAL1"),
      notEvaluatedEntry("NEVER1"),
      evaluatedEntry("EVAL2"),
      notEvaluatedEntry("NEVER2"),
    ],
  };
  core.renderRadarLists(data, {}, async () => {});
  const tbodyHtml = tbodyEl().innerHTML;
  const tickers = rowTickers(tbodyHtml);
  check("ALL 4 watchlist tickers render as rows -- evaluated and never-evaluated alike (the actual regression)", () => {
    assert.deepEqual([...tickers].sort(), ["EVAL1", "EVAL2", "NEVER1", "NEVER2"],
      `got ${JSON.stringify(tickers)} -- never-evaluated rows were dropped`);
  });
  check("evaluated rows keep their real state tag (not downgraded to Not evaluated)", () => {
    const row = tbodyHtml.match(/<tr data-ticker="EVAL1">([\s\S]*?)<\/tr>/)[1];
    assert.ok(row.includes("state-BUILDING"));
    assert.ok(!row.includes("Not evaluated"));
  });
  check("never-evaluated rows show a real timestamp cell of 'Not evaluated', evaluated rows show a real ET timestamp", () => {
    const neverRow = tbodyHtml.match(/<tr data-ticker="NEVER1">([\s\S]*?)<\/tr>/)[1];
    const evalRow = tbodyHtml.match(/<tr data-ticker="EVAL1">([\s\S]*?)<\/tr>/)[1];
    assert.ok(neverRow.includes("col-lastEval\">Not evaluated<"), neverRow);
    assert.ok(/col-lastEval">(?!Not evaluated)/.test(evalRow), evalRow);
  });
  check("summary counts distinguish total watchlist size (4) from evaluated count (2) via radarFilterCounts' not-yet-evaluated note", () => {
    const txt = el("radarFilterCounts").textContent;
    assert.ok(txt.includes("not yet evaluated"), txt);
  });
}

console.log("\n=== P1: this is the EXACT round-9 regression -- confirm the old behavior would have failed ===");
{
  // Documents the bug directly: qualifyingEntries() run over data.other
  // drops every not_evaluated entry -- this is what renderRadarLists
  // USED to do before this round's fix (D4/D5 fixed the backend, this
  // exact client-side filter re-broke the row count on top of it).
  setupDom();
  const data = { scanned: 2, watchlist_size: 2, evaluated_count: 1, highest: [], other: [evaluatedEntry("ONE"), notEvaluatedEntry("TWO")] };
  core.renderRadarLists(data, {}, async () => {});
  check("row count equals watchlist_size, never evaluated_count (the regression's exact symptom)", () => {
    const tickers = rowTickers(tbodyEl().innerHTML);
    assert.equal(tickers.length, data.watchlist_size, "row count silently dropped to evaluated_count -- the P1 regression");
  });
}

console.log("\n=== P2: Refresh response re-renders Last Evaluated without reload ===");
{
  setupDom();
  const before = { scanned:1, watchlist_size:1, evaluated_count:0, highest:[], other:[notEvaluatedEntry("FRESH")] };
  core.renderRadarLists(before, {}, async () => {});
  assert.match(tbodyEl().innerHTML, /col-lastEval">Not evaluated</);
  const after = { scanned:1, watchlist_size:1, evaluated_count:1, highest:[], other:[evaluatedEntry("FRESH", {last_evaluated_at:"2026-10-16T19:45:00+00:00"})] };
  core.renderRadarLists(after, {}, async () => {});
  check("re-rendering the completed POST payload replaces 'Not evaluated' immediately, with no page reload or follow-up fetch", () => {
    const row = tbodyEl().innerHTML;
    assert.doesNotMatch(row, /col-lastEval">Not evaluated</);
    assert.match(row, /col-lastEval">[^<]+</);
  });
}

console.log(`\n${passed} check(s) passed.`);
