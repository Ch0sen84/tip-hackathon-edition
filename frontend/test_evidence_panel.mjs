// Regression tests for the compact paginated evidence interface
// (2026-10-15, Chad-approved) -- replaces the old flat all-rows-at-once
// table that produced a 5-page PDF on expand.
//
//   node frontend/test_evidence_panel.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this.disabled = false; this.listeners = {}; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  dispatchEvent(ev) { (this.listeners[ev] || []).forEach((fn) => fn({})); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = v; }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get textContent() { return this._html; }
  set textContent(v) { this._html = v; }
}

const registry = new Map();
global.document = {
  getElementById: (id) => {
    if (!registry.has(id)) return null;
    return registry.get(id);
  },
  createElement: () => new FakeEl("tmp"),
};
function register(id) { const el = new FakeEl(id); registry.set(id, el); return el; }

function loadReal(paths, names) {
  const src = paths.map((p) => fs.readFileSync(p, "utf8")).join("\n;\n");
  const body = `${src}\nreturn {${names.join(",")}};`;
  // eslint-disable-next-line no-new-func
  return new Function(body)();
}

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); failed++; }
}

const lib = loadReal(
  [fileURLToPath(new URL("./app.core.js", import.meta.url))],
  ["buildEvidenceDaySummary", "renderEvidencePanel", "wireEvidencePanel", "fmtMoney", "dateOnly",
   "buildActivityBuckets", "distinctEventDates"],
);

function makeEvent(kind, time, price, value) {
  return { kind, time, price, value, label: kind === "flow_alert" ? "Flow alert" : "Dark pool print" };
}

console.log("\n=== buildEvidenceDaySummary: real grouping by date + category ===");
check("groups events into the correct real dates, never fabricating a day", () => {
  const events = [
    makeEvent("flow_alert", "2026-10-14T19:00:00Z", 100, 1_800_000),
    makeEvent("flow_alert", "2026-10-14T19:05:00Z", 101, 500_000),
    makeEvent("darkpool_trade", "2026-10-13T20:00:00Z", 99, 8_000_000),
  ];
  const byDay = lib.buildEvidenceDaySummary(events);
  assert.equal(byDay.length, 2);
  const oct14 = byDay.find((d) => d.date === "2026-10-14");
  assert.equal(oct14.flowCount, 2);
  assert.equal(oct14.flowValue, 2_300_000);
  assert.equal(oct14.dpCount, 0);
  const oct13 = byDay.find((d) => d.date === "2026-10-13");
  assert.equal(oct13.dpCount, 1);
  assert.equal(oct13.dpValue, 8_000_000);
});

check("sorted newest-day-first", () => {
  const events = [
    makeEvent("flow_alert", "2026-10-10T19:00:00Z", 1, null),
    makeEvent("flow_alert", "2026-10-14T19:00:00Z", 1, null),
    makeEvent("flow_alert", "2026-10-12T19:00:00Z", 1, null),
  ];
  const byDay = lib.buildEvidenceDaySummary(events);
  assert.deepEqual(byDay.map((d) => d.date), ["2026-10-14", "2026-10-12", "2026-10-10"]);
});

check("fmtMoney compact formatting", () => {
  assert.equal(lib.fmtMoney(1_800_000), "$1.8M");
  assert.equal(lib.fmtMoney(8_000), "$8K");
  assert.equal(lib.fmtMoney(500), "$500");
  assert.equal(lib.fmtMoney(null), null);
});

console.log("\n=== renderEvidencePanel: concise summary always visible, detail collapsed ===");
check("summary table is NOT inside the collapsed <details> (always visible)", () => {
  const events = [makeEvent("flow_alert", "2026-10-14T19:00:00Z", 100, 1_800_000)];
  const html = lib.renderEvidencePanel(events, 0);
  const detailsIdx = html.indexOf("<details");
  const summaryTableIdx = html.indexOf("evidence-summary-table");
  assert.ok(summaryTableIdx > -1 && summaryTableIdx < detailsIdx, "summary table must render before (outside) <details>");
});

check("individual observations are collapsed by default (no 'open' attribute)", () => {
  const events = [makeEvent("flow_alert", "2026-10-14T19:00:00Z", 100, 1_800_000)];
  const html = lib.renderEvidencePanel(events, 0);
  assert.ok(!/<details[^>]*\bopen\b/.test(html), "details must not have the open attribute");
});

check("empty table body initially (no rows pre-rendered in the markup itself)", () => {
  const events = [makeEvent("flow_alert", "2026-10-14T19:00:00Z", 100, 1_800_000)];
  const html = lib.renderEvidencePanel(events, 0);
  assert.ok(html.includes('id="evTableBody"></tbody>'), "evTableBody must start empty -- wireEvidencePanel fills it");
});

check("excluded-marker disclosure note appears when excludedCount > 0", () => {
  const html = lib.renderEvidencePanel([], 3);
  assert.ok(html.includes("3 evidence marker"));
});

console.log("\n=== wireEvidencePanel: pagination never renders hundreds of rows at once ===");
function setupPanelDom(eventCount) {
  ["evFilterType", "evFilterDate", "evFilterMinValue", "evSort", "evTableBody",
   "evPageInfo", "evPrevPage", "evNextPage"].forEach(register);
  document.getElementById("evSort").value = "time_desc";
  const events = [];
  for (let i = 0; i < eventCount; i++) {
    events.push(makeEvent(i % 2 === 0 ? "flow_alert" : "darkpool_trade",
      `2026-10-${String((i % 28) + 1).padStart(2, "0")}T19:${String(i % 60).padStart(2, "0")}:00Z`,
      100 + i, i * 1000));
  }
  return events;
}

check("600 real events: table body only ever holds <=20 rows (EVIDENCE_PAGE_SIZE)", () => {
  const events = setupPanelDom(600);
  lib.wireEvidencePanel(events);
  const tbody = document.getElementById("evTableBody");
  const rowCount = (tbody.innerHTML.match(/<tr>/g) || []).length;
  assert.ok(rowCount <= 20, `expected <=20 rows, got ${rowCount}`);
  assert.ok(rowCount > 0, "must render at least one row when events exist");
});

check("page info reflects the real total matching count, not just the page size", () => {
  const events = setupPanelDom(600);
  lib.wireEvidencePanel(events);
  const pageInfo = document.getElementById("evPageInfo").textContent;
  assert.ok(pageInfo.includes("600 matching"), pageInfo);
});

check("Next advances the page and changes the rendered rows", () => {
  const events = setupPanelDom(45);
  lib.wireEvidencePanel(events);
  const firstPageHtml = document.getElementById("evTableBody").innerHTML;
  document.getElementById("evNextPage").dispatchEvent("click");
  const secondPageHtml = document.getElementById("evTableBody").innerHTML;
  assert.notEqual(firstPageHtml, secondPageHtml);
});

check("Prev is disabled on page 1, enabled after Next", () => {
  const events = setupPanelDom(45);
  lib.wireEvidencePanel(events);
  assert.equal(document.getElementById("evPrevPage").disabled, true);
  document.getElementById("evNextPage").dispatchEvent("click");
  assert.equal(document.getElementById("evPrevPage").disabled, false);
});

check("Next is disabled on the last page", () => {
  const events = setupPanelDom(5); // fits on one page (<=20)
  lib.wireEvidencePanel(events);
  assert.equal(document.getElementById("evNextPage").disabled, true);
});

check("type filter narrows the matching count correctly", () => {
  const events = setupPanelDom(20); // 10 flow_alert, 10 darkpool_trade
  lib.wireEvidencePanel(events);
  document.getElementById("evFilterType").value = "flow_alert";
  document.getElementById("evFilterType").dispatchEvent("change");
  const pageInfo = document.getElementById("evPageInfo").textContent;
  assert.ok(pageInfo.includes("10 matching"), pageInfo);
});

check("min-value filter excludes events below the threshold", () => {
  const events = setupPanelDom(20); // values 0,1000,2000...19000
  lib.wireEvidencePanel(events);
  document.getElementById("evFilterMinValue").value = "15000";
  document.getElementById("evFilterMinValue").dispatchEvent("change");
  const pageInfo = document.getElementById("evPageInfo").textContent;
  // values >= 15000: 15000,16000,17000,18000,19000 -> 5 events
  assert.ok(pageInfo.includes("5 matching"), pageInfo);
});

check("sort by largest value puts the highest-value event on row 1", () => {
  const events = setupPanelDom(20);
  lib.wireEvidencePanel(events);
  document.getElementById("evSort").value = "value_desc";
  document.getElementById("evSort").dispatchEvent("change");
  const html = document.getElementById("evTableBody").innerHTML;
  const firstRowValue = html.match(/\$(\d+)K/);
  assert.ok(firstRowValue, html);
  assert.equal(firstRowValue[1], "19"); // $19K is the largest (value 19000)
});

check("every real event is reachable across all pages (no silent data loss)", () => {
  const events = setupPanelDom(73);
  lib.wireEvidencePanel(events);
  const totalPages = Math.ceil(73 / 20);
  let seen = 0;
  for (let p = 0; p < totalPages; p++) {
    const rows = (document.getElementById("evTableBody").innerHTML.match(/<tr>/g) || []).length;
    seen += rows;
    document.getElementById("evNextPage").dispatchEvent("click");
  }
  assert.equal(seen, 73);
});

console.log("\n=== C1 activity timeline: buildActivityBuckets / distinctEventDates ===");
check("bucket counts sum to the exact real event total (no loss, no fabrication)", () => {
  const events = [];
  for (let i = 0; i < 237; i++) {
    events.push(makeEvent(i % 2 === 0 ? "flow_alert" : "darkpool_trade",
      `2026-10-14T${String(i % 24).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}:00Z`, 100, 1000));
  }
  const buckets = lib.buildActivityBuckets(events, 30);
  const totalFlow = buckets.reduce((s, b) => s + b.flowCount, 0);
  const totalDp = buckets.reduce((s, b) => s + b.dpCount, 0);
  assert.equal(totalFlow + totalDp, 237);
});

check("single real date -> distinctEventDates reports exactly one date (honest single-session)", () => {
  const events = [
    makeEvent("flow_alert", "2026-10-14T19:00:00Z", 1, null),
    makeEvent("darkpool_trade", "2026-10-14T20:00:00Z", 1, null),
  ];
  assert.deepEqual(lib.distinctEventDates(events), ["2026-10-14"]);
});

check("genuinely multi-day events -> distinctEventDates reports all real dates, not collapsed", () => {
  const events = [
    makeEvent("flow_alert", "2026-10-12T19:00:00Z", 1, null),
    makeEvent("flow_alert", "2026-10-14T19:00:00Z", 1, null),
  ];
  assert.deepEqual(lib.distinctEventDates(events), ["2026-10-12", "2026-10-14"]);
});

check("activity buckets never invent a bucket with no real events", () => {
  const events = [makeEvent("flow_alert", "2026-10-14T19:00:00Z", 1, 500)];
  const buckets = lib.buildActivityBuckets(events, 30);
  assert.equal(buckets.length, 1); // exactly one real bucket, not padded to a full day
});

console.log(`\n${passed} check(s) passed.${failed ? ` ${failed} FAILED.` : ""}`);
process.exit(failed ? 1 : 0);
