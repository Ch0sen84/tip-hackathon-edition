// Stage 2 (P3) Market Radar filter verification. Loads the REAL
// frontend/app.core.js against the REAL running backend (TIP_MODE=demo)
// over real HTTP -- same minimal-DOM-stub pattern as
// test_manual_bugfixes.mjs / test_navigation.mjs. Run with the backend
// already running:
//   node frontend/test_radar_filters.mjs http://127.0.0.1:8899

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

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
  dispatchEvent(ev) {
    if (ev === "change" || (ev && ev.type === "change")) { if (this.onchange) this.onchange(); }
  }
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
global.fetch = fetch;

const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(coreSrc + `
globalThis.__exports = {
  entryFamilyTags, filterRadarEntries, renderRadarFilterOptions,
  renderHistoryChip, renderRadarLists, setupRadarFiltersAndRender,
};`);
const core = globalThis.__exports;

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

async function main() {
  const res = await fetch(`${BASE}/api/radar`);
  if (!res.ok) throw new Error(`GET /api/radar -> HTTP ${res.status}`);
  const data = await res.json();
  const allEntries = [...data.highest, ...data.other];

  console.log(`=== entryFamilyTags / filterRadarEntries over REAL /api/radar data ===`);
  check("at least one real multi-signal entry exists (FLUX and/or VOLX)", () => {
    const multi = allEntries.filter((e) => core.entryFamilyTags(e).includes("multi-signal"));
    assert.ok(multi.length >= 1, "no multi-signal entries found in real radar data");
  });
  check("family filter 'dark_pool' returns only entries with that real category", () => {
    const filtered = core.filterRadarEntries(allEntries, "dark_pool", "");
    assert.ok(filtered.length > 0, "expected at least one dark_pool entry");
    for (const e of filtered) assert.ok((e.categories || []).includes("dark_pool"));
  });
  check("family filter 'multi-signal' excludes single-category entries", () => {
    const filtered = core.filterRadarEntries(allEntries, "multi-signal", "");
    for (const e of filtered) assert.ok((e.categories || []).length >= 2, `${e.ticker} has <2 categories`);
  });
  check("state filter returns only entries with that exact real state", () => {
    const states = new Set(allEntries.map((e) => e.state));
    const someState = [...states][0];
    const filtered = core.filterRadarEntries(allEntries, "", someState);
    assert.ok(filtered.length > 0);
    for (const e of filtered) assert.equal(e.state, someState);
  });
  check("every real radar entry has a non-empty state field (regression: used to be undefined)", () => {
    for (const e of allEntries) {
      assert.ok(e.state, `${e.ticker} has no state`);
      assert.ok(["NEW", "BUILDING", "CONFIRMED", "STABLE", "FADING", "INVALIDATED"].includes(e.state));
    }
  });

  console.log(`\n=== renderHistoryChip: real vs empty history ===`);
  check("2+ entry history renders a chip with that many steps", () => {
    const html = core.renderHistoryChip(["NEW", "BUILDING", "CONFIRMED"]);
    assert.ok(html.includes("history-chip"));
    assert.ok((html.match(/hc-step/g) || []).length === 3);
  });
  check("single-entry or empty history renders nothing (never fabricated)", () => {
    assert.equal(core.renderHistoryChip([]), "");
    assert.equal(core.renderHistoryChip(["NEW"]), "");
    assert.equal(core.renderHistoryChip(undefined), "");
  });
  const prsx = allEntries.find((e) => e.ticker === "PRSX");
  check("PRSX (the real seeded persistence scenario) has genuine multi-entry history_states", () => {
    assert.ok(prsx, "PRSX not found in radar response");
    assert.ok(Array.isArray(prsx.history_states) && prsx.history_states.length >= 3);
    assert.deepEqual(prsx.history_states.slice(0, 3), ["NEW", "BUILDING", "CONFIRMED"]);
  });

  console.log(`\n=== setupRadarFiltersAndRender end-to-end over the fake DOM ===`);
  // Minimal DOM targets this function writes to.
  ["highestGrid", "otherGrid", "radarFilterCounts", "radarEmpty", "filterFamily", "filterState"].forEach(el);
  core.setupRadarFiltersAndRender(data, async () => {});
  check("filter <select> options were populated from real categories/states", () => {
    assert.ok(el("filterFamily").innerHTML.includes("dark_pool") || el("filterFamily").innerHTML.includes("options"));
    assert.ok(el("filterState").innerHTML.length > 0);
  });
  check("counts line distinguishes qualifying-shown from total-scanned", () => {
    const txt = el("radarFilterCounts").textContent;
    assert.ok(txt.includes("qualifying signal"));
    assert.ok(txt.includes(String(data.scanned)));
  });
  check("applying a family filter changes the rendered card set (otherGrid, mixed families)", () => {
    const beforeOther = el("otherGrid")._html;
    const beforeCount = el("radarFilterCounts").textContent;
    el("filterFamily")._value = "dark_pool";
    el("filterFamily").onchange();
    const afterOther = el("otherGrid")._html;
    const afterCount = el("radarFilterCounts").textContent;
    assert.notEqual(beforeOther, afterOther, "filtering did not change otherGrid output");
    assert.ok(afterCount.includes("after filters"), afterCount);
    assert.notEqual(beforeCount, afterCount);
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
