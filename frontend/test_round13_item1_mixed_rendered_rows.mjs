// Round 13 item 1 regression test (ux-nav-watchlist-reliability round 13):
// the real bug Claude's Windows Docker Desktop acceptance test found was
// NOT a frontend render-skip (the P1 fix two rounds ago already covers
// that class of bug for not_evaluated placeholders) -- it was the BACKEND
// ROUTE (_radar_impl in backend/app/main.py) silently excluding evaluated-
// but-zero-qualifying-gate tickers (e.g. ARTNA/MSEX) from the /api/radar
// response entirely. No frontend fix could ever have caught this because
// the ticker never arrived in the JSON at all.
//
// This test proves the FULL round trip at the frontend-rendering layer,
// using the exact backend entry shape _radar_impl now produces (see that
// file's "Round 13 item 1" comment): a mixed fixture of exactly three
// watchlist tickers --
//   1. NEVERX  -- never evaluated at all (not_evaluated: true)
//   2. ZEROX   -- genuinely evaluated this scan, zero qualifying gates
//                 (zero_gate: true, a real state like "NEW", a real
//                 transition_rule of "no_qualifying_evidence_yet")
//   3. QUALX   -- genuinely qualifying (reached `other` via
//                 tip_evidence.present() the normal way, no special flags)
// -- and asserts all three render as DISTINCT, correctly-labeled rows
// in the actual rendered HTML (not just checking the input JSON shape).
//
//   node frontend/test_round13_item1_mixed_rendered_rows.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {}; this.onchange = null; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  get textContent() { return this._html; }
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
  renderRadarLists, qualifyingEntries, radarSummaryCounts, renderActivitySummary,
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

const neverEvaluated = {
  ticker: "NEVERX", name: null, families: [], evidence: [],
  current_read: "This ticker has not been scanned yet.", headline: "Not yet evaluated",
  label: null, categories: [], index_context: false, question: null, research: [], enriched: false,
  price: null, state: null, confidence: null, signal_type: null, qualifying_days_count: 0,
  history_states: [], eval_freshness: "unevaluated", last_evaluated_at: null,
  not_evaluated: true, transition_rule: null,
};

// Exact shape backend/app/main.py's new force-append block produces for a
// genuinely-evaluated zero-gate ticker (see that file's Round 13 item 1
// comment): a REAL state/confidence, zero_gate: true, not_evaluated: false.
const zeroGateEvaluated = {
  ticker: "ZEROX", name: null, families: [], evidence: [],
  current_read: "Unusual activity; stock little changed.", headline: "Unusual activity",
  label: "OTHER", categories: [], index_context: false, question: null, research: [], enriched: true,
  price: { last: 12.3, chg_pct: 0.1 },
  state: "NEW", confidence: "LOW", signal_type: "none", qualifying_days_count: 0,
  history_states: [], eval_freshness: "current", last_evaluated_at: "2026-10-17T12:00:00+00:00",
  not_evaluated: false, zero_gate: true, transition_rule: "no_qualifying_evidence_yet",
};

const genuinelyQualifying = {
  ticker: "QUALX", name: null, families: ["dark_pool"], evidence: [{ family: "dark_pool", text: "x", short: "x", key: true }],
  current_read: "Dark-pool activity well above normal.", headline: "Dark pool ~3.2x recent average",
  label: "DARK_POOL", categories: ["dark_pool"], index_context: false, question: null, research: [], enriched: true,
  price: { last: 55.0, chg_pct: 2.1 },
  state: "BUILDING", confidence: "medium", signal_type: "dark_pool_anomaly", qualifying_days_count: 1,
  history_states: [], eval_freshness: "current", last_evaluated_at: "2026-10-17T12:00:00+00:00",
  not_evaluated: false, transition_rule: "first_qualifying_gate",
};

function setupDom() {
  elements.clear();
  ["highestGrid", "otherGrid", "highestAttentionSection", "radarFilterCounts", "radarEmpty"].forEach(el);
}

console.log("=== Round 13 item 1: mixed fixture (never-evaluated / zero-gate-evaluated / qualifying) ===");
{
  setupDom();
  const data = {
    scanned: 3, watchlist_size: 3, evaluated_count: 2,
    highest: [],
    other: [neverEvaluated, zeroGateEvaluated, genuinelyQualifying],
  };
  core.renderRadarLists(data, {}, async () => {});
  const html = tbodyEl().innerHTML;

  check("all THREE watchlist tickers render as distinct rows -- none silently dropped", () => {
    assert.deepEqual([...rowTickers(html)].sort(), ["NEVERX", "QUALX", "ZEROX"]);
  });

  check("NEVERX renders the honest 'Not evaluated' badge", () => {
    const row = rowFor(html, "NEVERX");
    assert.ok(row.includes("Not evaluated"), row);
    assert.ok(!row.includes("state-NEW"), row);
  });

  check("ZEROX renders its REAL state tag (NEW), distinctly labeled as zero-gate -- never 'Not evaluated'", () => {
    const row = rowFor(html, "ZEROX");
    assert.ok(row.includes("state-NEW"), row);
    assert.ok(!row.includes(">Not evaluated<"), row);
    assert.ok(row.includes("no qualifying signal"), `expected a zero-gate note in: ${row}`);
  });

  check("QUALX renders its real state tag with NO zero-gate note -- a genuinely qualifying row", () => {
    const row = rowFor(html, "QUALX");
    assert.ok(row.includes("state-BUILDING"), row);
    assert.ok(!row.includes("no qualifying signal"), row);
    assert.ok(!row.includes("Not evaluated"), row);
  });

  check("ZEROX and NEVERX are each other's own distinct, correctly-labeled row (not merged/confused)", () => {
    assert.notEqual(rowFor(html, "ZEROX"), rowFor(html, "NEVERX"));
  });

  check("qualifyingEntries() excludes BOTH never-evaluated and zero-gate entries", () => {
    const q = core.qualifyingEntries(data.other);
    assert.deepEqual(q.map((e) => e.ticker), ["QUALX"]);
  });

  check("summary counts: only the genuinely-qualifying ticker counts toward qualifying/other-noteworthy", () => {
    const c = core.radarSummaryCounts(data);
    assert.equal(c.watchlistCount, 3);
    assert.equal(c.evaluatedCount, 2);
    assert.equal(c.qualifyingCount, 1);
    assert.equal(c.otherNoteworthyCount, 1);
  });

  check("the activity-summary strip reflects the same honest counts", () => {
    const strip = core.renderActivitySummary(data);
    assert.match(strip, />3<\/span><span class="activity-label">tickers in watchlist/);
    assert.match(strip, />2<\/span><span class="activity-label">evaluated/);
    assert.match(strip, />1<\/span><span class="activity-label">qualifying signals/);
    assert.match(strip, />1<\/span><span class="activity-label">other noteworthy/);
  });

  check("radarEmpty is NOT shown -- real rows exist", () => {
    assert.equal(el("radarEmpty").style.display, "none");
  });
}

console.log(`\n${passed} check(s) passed.`);
