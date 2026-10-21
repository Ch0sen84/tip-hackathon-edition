// Regression tests for the 2026-10-15 "Investigate with AI" safeguards
// pass (items #1/#2/#4/#5 from the real NVDA investigation review):
// - a real two-click confirmation before the paid action fires, every time
// - the configured transport shown BEFORE execution (no credentials)
// - per-tool-call outcomes, truncation disclosure, and evidence-integrity
//   warnings all rendered in the result view
//
// Drives the REAL frontend/app.core.js + frontend/app.live.js against a
// REAL running backend (TIP_MODE=demo, isolated DB) over real HTTP, same
// pattern as test_manual_bugfixes.mjs. Run it with:
//
//   node frontend/test_investigate_safeguards.mjs <backend_base_url>

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {}; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._html = v; }
  get textContent() { return this._html; }
  set value(v) { this._value = v; }
  get value() { return this._value; }
  get style() { return this._style; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({})); }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
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
  createElement: () => new FakeEl("tmp"),
};
global.history = { replaceState: () => {}, pushState: () => {} };
global.location = { pathname: "/", search: "", hash: "" };
globalThis.__TIP_TEST_API_BASE__ = BASE;
global.window = { addEventListener: () => {}, TIP_NAV_HANDLERS: null };
global.ResizeObserver = class { observe() {} };

function loadReal(paths, exportNames) {
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
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

const lib = loadReal(
  [
    fileURLToPath(new URL("./app.core.js", import.meta.url)),
    fileURLToPath(new URL("./app.live.js", import.meta.url)),
  ],
  [
    "renderToolsUsed", "renderEvidenceIntegrityWarnings", "renderTruncationDisclosure",
    "transportLabel", "renderInvestigateResult", "loadInvestigateSection", "esc",
  ],
);

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
  console.log("\n=== Pre-run transport disclosure (item 5) ===");
  // navSeq must match app.live.js's module-internal _navSeq (starts at 0)
  // -- passing a mismatched value makes loadInvestigateSection's own
  // superseded-navigation guard return early with nothing rendered, which
  // is a correct REAL guard, not a bug to work around with a mismatch.
  await lib.loadInvestigateSection("ZYNC", 0);
  check("investigate card rendered with no crash", () => {
    assert.ok(el("investigateSection").innerHTML.includes("Investigate with AI"));
  });
  // Demo mode: transport is null (no real transport used) -- must not
  // render a confusing transport line implying a real backend ran.
  check("demo mode does not render a misleading transport line", () => {
    assert.ok(!el("investigateSection").innerHTML.includes("Configured transport:"));
  });

  console.log("\n=== Confirmation step: every click sequence re-arms (item 5) ===");
  const btn = el("runInvestigateBtn");
  const bodyBefore = el("investigateBody").innerHTML;
  check("button starts in its normal, non-armed label", () => {
    assert.ok(!btn.textContent.includes("Confirm:"));
  });
  btn.click(); // first click: arm only, must NOT fire the real call yet
  check("first click arms the button but does not fire the investigation", () => {
    assert.ok(btn.textContent.includes("Confirm:"));
    assert.equal(el("investigateBody").innerHTML, bodyBefore);
  });
  btn.click(); // second click: now it actually fires
  await new Promise((r) => setTimeout(r, 50));
  check("second click actually fires the investigation (body populated)", () => {
    assert.ok(el("investigateBody").innerHTML.length > bodyBefore.length);
  });
  check("button resets to its original label after firing (re-arms next time, not session-sticky)", () => {
    assert.ok(!btn.textContent.includes("Confirm:"));
  });

  console.log("\n=== Result-view disclosures (items #1/#2/#4/#5) ===");
  check("tool outcomes render OK/FAILED per actual tool call", () => {
    const html = lib.renderToolsUsed(
      ["get_flow_alerts", "get_dark_pool_trades"],
      [
        { tool: "get_flow_alerts", ok: true, error: null },
        { tool: "get_dark_pool_trades", ok: false, error: "timeout" },
      ],
    );
    assert.ok(html.includes("tool-step-ok"));
    assert.ok(html.includes("tool-step-fail"));
    assert.ok(html.includes("timeout"));
    assert.ok(!html.includes("real gap"));
  });
  check("tool outcomes falls back to the honest gap note when outcomes are absent (older/cached response)", () => {
    const html = lib.renderToolsUsed(["get_flow_alerts"], []);
    assert.ok(html.includes("not available for this response"));
  });
  check("truncation disclosure renders only when server_truncated is true", () => {
    assert.equal(lib.renderTruncationDisclosure({ server_truncated: false }), "");
    const html = lib.renderTruncationDisclosure({ server_truncated: true });
    assert.ok(html.includes("capped server-side"));
  });
  check("evidence-integrity warnings render as their own distinct block", () => {
    const html = lib.renderEvidenceIntegrityWarnings([
      "Unsupported directional claim: \"put buying\" asserts a uniform direction, but the actual data was mixed.",
    ]);
    assert.ok(html.includes("Evidence Integrity Warnings"));
    assert.ok(html.includes("ai-integrity-warning"));
    assert.ok(html.includes("put buying"));
  });
  check("evidence-integrity warnings render nothing when the list is empty", () => {
    assert.equal(lib.renderEvidenceIntegrityWarnings([]), "");
  });
  check("full result view wires truncation + integrity warnings together with existing sections", () => {
    const d = {
      ticker: "NVDA", simulated: false, transport: "mcp",
      tools_used: ["get_flow_alerts"],
      tool_outcomes: [{ tool: "get_flow_alerts", ok: true, error: null }],
      observed_facts: [{ fact: "1 ask-side, 2 bid-side put alerts.", source_tool: "get_flow_alerts" }],
      supporting_evidence: [], conflicting_observations: [], limitations: [],
      ai_interpretation: "Mixed ask/bid activity on puts -- no uniform directional read.",
      server_truncated: true,
      evidence_integrity_warnings: ["Unsupported directional claim: test."],
    };
    const html = lib.renderInvestigateResult(d, "NVDA", "LIVE mode");
    assert.ok(html.includes("capped server-side"));
    assert.ok(html.includes("Evidence Integrity Warnings"));
    assert.ok(html.includes("Real MCP-protocol calls"));
    assert.ok(html.includes("tool-step-ok"));
  });

  console.log(`\n${passed} check(s) passed.`);
}

main();
