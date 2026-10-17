// Round 14 (ux-nav-watchlist-reliability) regression test -- Chad's
// exact mixed six-ticker reproduction: 4 qualifying tickers, 1 evaluated
// zero-gate ticker, 1 never-evaluated ticker -- then the never-evaluated
// ticker transitions to evaluated-zero-gate mid-test, asserting correct
// grouping/headlines/badges at BOTH stages, for both renderRadarLists
// (My Watchlist / Other Noteworthy) and renderTickerDetail (Ticker
// Analysis), against the ACTUAL rendered DOM/HTML (not just the input
// JSON shape).
//
//   node frontend/test_round14_mixed_six_ticker.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._text = ""; this._style = {}; this.listeners = {}; this.onchange = null; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = v; }
  get textContent() { return this._text; }
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
  renderRadarLists, renderRow, renderTickerDetail, isNotEvaluated, isZeroGateEntry,
  stateTagHtml, radarSummaryCounts, primaryTickerHeadline, primaryStateGloss,
  primaryCurrentRead, transitionLabel,
};`);
const core = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

function tbodyEl() { return el("#otherTable tbody"); }
function rowTickers(html) { return [...html.matchAll(/data-ticker="([^"]+)"/g)].map((m) => m[1]); }
function rowFor(html, ticker) {
  const m = html.match(new RegExp(`<tr data-ticker="${ticker}">([\\s\\S]*?)</tr>`));
  return m ? m[1] : null;
}
function setupDom() {
  elements.clear();
  ["highestGrid", "otherGrid", "highestAttentionSection", "radarFilterCounts", "radarEmpty", "otherNoteworthyCount"].forEach(el);
}

// --- fixture: exactly Chad's mixed six-ticker reproduction ------------------
function qualifying(ticker) {
  return {
    ticker, name: null, families: ["dark_pool"], evidence: [{ family: "dark_pool", text: "x", short: "x", key: true }],
    current_read: "Dark-pool activity well above normal.", headline: `${ticker} dark pool ~3x recent average`,
    label: "DARK_POOL", categories: ["dark_pool"], index_context: false, question: null, research: [], enriched: true,
    price: { last: 50.0, chg_pct: 1.5 },
    state: "BUILDING", confidence: "medium", signal_type: "dark_pool_anomaly", qualifying_days_count: 1,
    history_states: [], eval_freshness: "current", last_evaluated_at: "2026-10-17T12:00:00+00:00",
    not_evaluated: false, transition_rule: "first_qualifying_gate",
  };
}
const zeroGateEvaluated = {
  ticker: "ZGATE", name: null, families: [], evidence: [],
  current_read: "Unusual activity; stock little changed.", headline: "Unusual activity",
  label: "OTHER", categories: [], index_context: false, question: null, research: [], enriched: true,
  price: { last: 12.3, chg_pct: 0.1 },
  state: "NEW", confidence: "LOW", signal_type: "none", qualifying_days_count: 0,
  history_states: [], eval_freshness: "current", last_evaluated_at: "2026-10-17T12:00:00+00:00",
  not_evaluated: false, zero_gate: true, transition_rule: "no_qualifying_evidence_yet",
};
const neverEvaluated = {
  ticker: "NEVERX", name: null, families: [], evidence: [],
  current_read: "This ticker has not been scanned yet.", headline: "Not yet evaluated",
  label: null, categories: [], index_context: false, question: null, research: [], enriched: false,
  price: null, state: null, confidence: null, signal_type: null, qualifying_days_count: 0,
  history_states: [], eval_freshness: "unevaluated", last_evaluated_at: null,
  not_evaluated: true, transition_rule: null,
};
// Same ticker, after its first evaluation -- zero qualifying gates.
const neverEvaluatedAfterZeroGateEval = {
  ...neverEvaluated,
  current_read: "Unusual activity; stock little changed.", headline: "Unusual activity",
  state: "NEW", confidence: "LOW", signal_type: "none",
  eval_freshness: "current", last_evaluated_at: "2026-10-17T13:00:00+00:00",
  not_evaluated: false, zero_gate: true, transition_rule: "no_qualifying_evidence_yet",
};

const qualifyingTickers = ["QUAL1", "QUAL2", "QUAL3", "QUAL4"].map(qualifying);

function buildData(nevEntry) {
  return {
    scanned: 6, watchlist_size: 6, evaluated_count: nevEntry.not_evaluated ? 5 : 6,
    highest: [],
    other: [...qualifyingTickers, zeroGateEvaluated, nevEntry],
  };
}

console.log("=== STAGE 1: before NEVERX's first evaluation ===");
let html1, counts1;
{
  setupDom();
  const data = buildData(neverEvaluated);
  core.renderRadarLists(data, {}, async () => {});
  html1 = tbodyEl().innerHTML;
  counts1 = core.radarSummaryCounts(data);

  check("all 6 tickers render as distinct rows", () => {
    assert.deepEqual([...rowTickers(html1)].sort(),
      ["NEVERX", "QUAL1", "QUAL2", "QUAL3", "QUAL4", "ZGATE"]);
  });

  check("Item 1: qualifying rows keep their real headline text", () => {
    for (const t of ["QUAL1", "QUAL2", "QUAL3", "QUAL4"]) {
      const row = rowFor(html1, t);
      assert.ok(row.includes(`${t} dark pool`), row);
    }
  });

  check("Item 1: ZGATE (evaluated zero-gate) headline is neutral, not 'Unusual activity'", () => {
    const row = rowFor(html1, "ZGATE");
    assert.ok(row.includes("No qualifying signal detected."), row);
    assert.ok(!row.includes(">Unusual activity<"), row);
  });

  check("NEVERX shows the honest 'Not evaluated' badge, no fabricated state", () => {
    const row = rowFor(html1, "NEVERX");
    assert.ok(row.includes("Not evaluated"), row);
    assert.ok(!row.includes("state-NEW"), row);
  });

  check("Item 3: table is grouped -- 'No Qualifying Signal' and 'Not Evaluated' group headers present with counts matching rendered rows", () => {
    assert.match(html1, /No Qualifying Signal \(1\)/);
    assert.match(html1, /Not Evaluated \(1\)/);
  });

  check("Item 3: 'Other Noteworthy' heading count (4) matches the qualifying rows actually rendered", () => {
    assert.equal(el("otherNoteworthyCount").textContent, " (4)");
  });

  check("Item 3: overall summary counts (fixed 2 rounds ago) stay correct", () => {
    assert.equal(counts1.watchlistCount, 6);
    assert.equal(counts1.evaluatedCount, 5);
    assert.equal(counts1.qualifyingCount, 4);
    assert.equal(counts1.otherNoteworthyCount, 4);
  });

  check("radarEmpty is NOT shown -- real rows exist", () => {
    assert.equal(el("radarEmpty").style.display, "none");
  });
}

console.log("\n=== STAGE 1 (repeat, F5/no-reload consistency check) ===");
{
  // Re-render from the SAME data object again (simulates re-render after
  // a cached GET, immediate no-reload refresh) -- counts/groups/headings
  // must be byte-identical to the first render.
  setupDom();
  const data = buildData(neverEvaluated);
  core.renderRadarLists(data, {}, async () => {});
  const htmlRepeat = tbodyEl().innerHTML;
  check("re-rendering the same payload (simulating F5 / immediate refresh) preserves identical grouping/counts", () => {
    assert.equal(htmlRepeat, html1);
    assert.equal(el("otherNoteworthyCount").textContent, " (4)");
  });
}

console.log("\n=== STAGE 2: after NEVERX's first evaluation (zero gates) ===");
let html2, counts2;
{
  setupDom();
  const data = buildData(neverEvaluatedAfterZeroGateEval);
  core.renderRadarLists(data, {}, async () => {});
  html2 = tbodyEl().innerHTML;
  counts2 = core.radarSummaryCounts(data);

  check("all 6 tickers still render as distinct rows", () => {
    assert.deepEqual([...rowTickers(html2)].sort(),
      ["NEVERX", "QUAL1", "QUAL2", "QUAL3", "QUAL4", "ZGATE"]);
  });

  check("NEVERX now renders the ZERO-GATE presentation (real NEW state + zero-gate note), not 'Not evaluated' and not a qualifying tag", () => {
    const row = rowFor(html2, "NEVERX");
    assert.ok(row.includes("state-NEW"), row);
    assert.ok(row.includes("no qualifying signal"), row);
    assert.ok(!row.includes(">Not evaluated<"), row);
    assert.ok(row.includes("No qualifying signal detected."), row);
  });

  check("Item 3: 'No Qualifying Signal' group now has 2 rows (ZGATE + NEVERX), 'Not Evaluated' group is gone entirely", () => {
    assert.match(html2, /No Qualifying Signal \(2\)/);
    assert.doesNotMatch(html2, /Not Evaluated \(/);
  });

  check("Item 3: 'Other Noteworthy' heading count stays 4 (NEVERX's transition never counted as qualifying)", () => {
    assert.equal(el("otherNoteworthyCount").textContent, " (4)");
  });

  check("Item 3: overall summary counts updated correctly -- evaluated_count now 6, qualifying unchanged at 4", () => {
    assert.equal(counts2.watchlistCount, 6);
    assert.equal(counts2.evaluatedCount, 6);
    assert.equal(counts2.qualifyingCount, 4);
    assert.equal(counts2.otherNoteworthyCount, 4);
  });
}

console.log("\n=== STAGE 2 (repeat, F5/no-reload consistency check) ===");
{
  setupDom();
  const data = buildData(neverEvaluatedAfterZeroGateEval);
  core.renderRadarLists(data, {}, async () => {});
  const htmlRepeat = tbodyEl().innerHTML;
  check("re-rendering the post-evaluation payload again preserves identical grouping/counts", () => {
    assert.equal(htmlRepeat, html2);
    assert.equal(el("otherNoteworthyCount").textContent, " (4)");
  });
}

console.log("\n=== Item 2: Ticker Analysis badge/headline for the never-evaluated ticker, both stages ===");
{
  // Shape backend/app/main.py's _not_evaluated_ticker_analysis_response
  // now actually produces (state: null, not_evaluated: true) -- see
  // backend/tests/test_round14_item2_never_evaluated_badge_honesty.py
  // for the backend-side proof of this exact shape.
  const neverEvaluatedDetail = {
    ticker: "NEVERX", state: null, previous_state: null, transition_rule: null,
    confidence: null, signal_type: null, confidence_reason: null,
    qualifies: false, qualifying_days_count: 0, gates: {},
    headline: "Not yet evaluated",
    current_read: "This ticker has not been scanned yet in this session. Use the explicit Refresh action to run a real evaluation.",
    evidence: [], stock_state: null, history: [], raw: null,
    eval_freshness: "unevaluated", last_evaluated_at: null, served_from_cache: true,
    not_evaluated: true,
  };
  const html = core.renderTickerDetail(neverEvaluatedDetail);

  check("badge shows honest 'Not evaluated', never 'NEW'/'just started qualifying'", () => {
    assert.match(html, /state-unevaluated"[^>]*>Not evaluated</);
    assert.doesNotMatch(html, /state-NEW/);
    assert.doesNotMatch(html, /just started qualifying/);
  });

  check("no contradiction: the badge and the primary headline agree -- neither claims qualifying activity", () => {
    assert.match(html, /Not yet evaluated/);
  });

  check("transitionLabel never renders 'Initial detection: null'", () => {
    assert.doesNotMatch(html, /Initial detection: null/);
    assert.match(html, /No evaluation recorded yet\./);
  });

  // Same ticker, now genuinely evaluated with zero gates -- the
  // lifecycle badge logic must take over normally (zero-gate
  // presentation, from prior rounds' already-correct fix).
  const zeroGateDetail = {
    ...neverEvaluatedDetail,
    state: "NEW", transition_rule: "no_qualifying_evidence_yet", confidence: "LOW",
    signal_type: "none", headline: "Unusual activity",
    current_read: "Unusual activity; stock little changed.",
    eval_freshness: "current", last_evaluated_at: "2026-10-17T13:00:00+00:00",
    not_evaluated: false,
  };
  const html2Detail = core.renderTickerDetail(zeroGateDetail);
  check("after evaluation, badge switches to the REAL zero-gate presentation (NEW + honest gloss), not never-evaluated, not qualifying", () => {
    assert.match(html2Detail, /state-NEW">NEW</);
    assert.match(html2Detail, /no qualifying evidence yet/);
    assert.doesNotMatch(html2Detail, /state-unevaluated/);
    assert.match(html2Detail, /No qualifying signal detected yet\./);
  });
}

console.log(`\n${passed} check(s) passed.`);
