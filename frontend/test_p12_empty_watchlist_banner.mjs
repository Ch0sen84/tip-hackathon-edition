// Round 12 (ux-nav-watchlist-reliability) regression test: the
// featuredSignalCompactNotice banner (renderFeaturedArea) used to say
// "...see Other Noteworthy below for the tickers tracked this scan"
// whenever data.other had ANY entries at all -- including when every one
// of those entries was a not_evaluated placeholder (a watchlist ticker
// that has NEVER actually been scanned). That phrasing falsely implies a
// real scan ran and found noteworthy tickers, when the Live evaluation
// cache is genuinely empty. This test proves the banner now derives its
// wording honestly from watchlist_size/evaluated_count vs the actual
// (non-placeholder) contents of Other Noteworthy, covering all three
// genuinely distinct cases.
//
// Same DOM-stub pattern as test_p1_watchlist_row_coverage.mjs (no jsdom
// dependency) -- loads the REAL frontend/app.core.js, no reimplementation.
//
//   node frontend/test_p12_empty_watchlist_banner.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._style = {}; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  get style() { return this._style; }
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
(0, eval)(coreSrc + `\nglobalThis.__exports = { renderFeaturedArea };`);
const { renderFeaturedArea } = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

function notEvaluatedEntry(ticker) {
  return {
    ticker, not_evaluated: true, state: null, confidence: null,
    headline: "Not yet evaluated", qualifies: false,
  };
}
function qualifyingEntry(ticker) {
  return {
    ticker, not_evaluated: false, state: "BUILDING", confidence: "medium",
    headline: `${ticker} headline`, qualifies: true,
  };
}

function setupDom() {
  elements.clear();
  ["featuredSignalHeading", "featuredSignalGrid", "featuredSignalCompactNotice", "featuredSignalSlot", "breakdownSlot"].forEach(el);
}

function noticeText() {
  return el("featuredSignalCompactNotice").innerHTML;
}

console.log("=== Case 1: genuinely empty cache -- watchlist has tickers, NONE evaluated ===");
{
  setupDom();
  const data = {
    watchlist_size: 3, evaluated_count: 0, scanned: 3,
    highest: [],
    other: [notEvaluatedEntry("AAA"), notEvaluatedEntry("BBB"), notEvaluatedEntry("CCC")],
  };
  renderFeaturedArea(data);
  const text = noticeText();
  check("does NOT claim tickers were 'tracked this scan'", () => {
    assert.doesNotMatch(text, /tracked this scan/i);
  });
  check("does NOT point to Other Noteworthy (nothing real is there)", () => {
    assert.doesNotMatch(text, /Other Noteworthy/i);
  });
  check("honestly says nothing has been evaluated yet", () => {
    assert.match(text, /have been evaluated yet/i);
  });
}

console.log("\n=== Case 2: a real scan ran and Other Noteworthy has genuine entries ===");
{
  setupDom();
  const data = {
    watchlist_size: 3, evaluated_count: 3, scanned: 3,
    highest: [],
    other: [qualifyingEntry("AAA"), qualifyingEntry("BBB")],
  };
  renderFeaturedArea(data);
  const text = noticeText();
  check("points to Other Noteworthy for genuinely evaluated tickers", () => {
    assert.match(text, /Other Noteworthy/i);
  });
  check("does not use the stale 'tracked this scan' phrasing", () => {
    assert.doesNotMatch(text, /tracked this scan/i);
  });
}

console.log("\n=== Case 3: a real scan ran, evaluated everything, genuinely zero qualify anywhere ===");
{
  setupDom();
  const data = {
    watchlist_size: 2, evaluated_count: 2, scanned: 2,
    highest: [],
    other: [],
  };
  renderFeaturedArea(data);
  const text = noticeText();
  check("says plainly that nothing currently qualifies this scan", () => {
    assert.match(text, /No tickers currently qualify for Market Radar attention this scan/i);
  });
  check("does not falsely claim nothing has been evaluated", () => {
    assert.doesNotMatch(text, /not been evaluated yet/i);
  });
}

console.log("\n=== Case 4: mixed -- one REAL evaluated entry + some never-evaluated placeholders ===");
{
  setupDom();
  const data = {
    watchlist_size: 3, evaluated_count: 1, scanned: 3,
    highest: [],
    // One genuinely evaluated (not_evaluated:false) entry, plus two
    // never-evaluated placeholders -- qualifyingEntries() filters ONLY
    // on not_evaluated (matching the pre-existing backend/frontend
    // convention: "Other Noteworthy" already legitimately includes any
    // real evaluated-but-not-featured ticker, not only ones that
    // formally "qualify"), so this correctly counts as genuine content
    // and honestly pointing to it is accurate here.
    other: [{ ticker: "DDD", not_evaluated: false, headline: "evaluated, did not qualify" }, notEvaluatedEntry("EEE"), notEvaluatedEntry("FFF")],
  };
  renderFeaturedArea(data);
  const text = noticeText();
  check("points to Other Noteworthy since a genuinely evaluated entry (DDD) IS there", () => {
    assert.match(text, /Other Noteworthy/i);
  });
  check("does not use the stale 'tracked this scan' phrasing even in the mixed case", () => {
    assert.doesNotMatch(text, /tracked this scan/i);
  });
  check("evaluated_count > 0 here, so this is NOT the 'nothing evaluated yet' case", () => {
    assert.doesNotMatch(text, /have been evaluated yet/i);
  });
}

console.log("\n=== Case 5: watchlist has only never-evaluated placeholders in Other -- no genuine entry at all ===");
{
  setupDom();
  const data = {
    watchlist_size: 2, evaluated_count: 0, scanned: 2,
    highest: [],
    other: [notEvaluatedEntry("GGG"), notEvaluatedEntry("HHH")],
  };
  renderFeaturedArea(data);
  const text = noticeText();
  check("never points to Other Noteworthy when it contains only placeholders", () => {
    assert.doesNotMatch(text, /Other Noteworthy/i);
  });
  check("correctly identified as the genuinely-empty-cache case", () => {
    assert.match(text, /have been evaluated yet/i);
  });
}

console.log(`\n${passed} check(s) passed.`);
