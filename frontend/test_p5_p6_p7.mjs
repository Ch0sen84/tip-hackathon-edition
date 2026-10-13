// Targeted verification for this pass's P5 (evidence-first Ticker
// Analysis + CONFLICTING explanation), P6 (AI Investigation promotion +
// ticker-switch race guard), and P7 (signal lifecycle timeline).
//
// Same pattern as test_manual_bugfixes.mjs: plain Node script (no test
// framework in this repo by design), drives the REAL frontend/app.core.js
// + frontend/app.live.js against a REAL running backend over real HTTP.
//
//   node frontend/test_p5_p6_p7.mjs <backend_base_url>

import assert from "node:assert/strict";
import fs from "node:fs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

class FakeEl {
  constructor(id) { this.id = id; this._html = ""; this._value = ""; this._style = {}; this.listeners = {}; }
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
  querySelector: (sel) => el(sel),
};
global.history = { replaceState: () => {}, pushState: () => {} };
global.location = { pathname: "/", search: `?api=${BASE}`, hash: "" };
global.window = { addEventListener: () => {}, TIP_NAV_HANDLERS: null };
global.ResizeObserver = class { observe() {} };
global.LightweightCharts = {
  createChart: () => ({
    addCandlestickSeries: () => ({ setData: () => {}, setMarkers: () => {} }),
    timeScale: () => ({ fitContent: () => {} }),
    applyOptions: () => {},
  }),
};

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
  const lib = loadReal(
    [
      new URL("./app.core.js", import.meta.url).pathname,
      new URL("./app.live.js", import.meta.url).pathname,
    ],
    [
      "loadTickerDetail", "navigateToTicker", "TIP_STATE", "apiGet",
      "renderLifecycleTimeline", "gateWhyItMatters", "renderTickerDetail",
      "renderInvestigateResult", "loadInvestigateSection", "runInvestigate",
    ],
  );

  console.log(`\n=== P5: CONFL conflicting-evidence explanation is computed from real gate metrics ===`);
  const confl = await lib.apiGet("/api/ticker/CONFL");
  check("CONFL's price_confirmation gate metrics match the documented real values", () => {
    const m = confl.gates.price_confirmation.metrics;
    assert.equal(m.confirmed, false);
    assert.equal(m.direction_heuristic, "bullish-lean");
    assert.equal(m.price_move_pct, -1.8);
  });
  const whyText = lib.gateWhyItMatters("price_confirmation", confl.gates.price_confirmation);
  check("explanation text names the real direction + real magnitude, not a canned string", () => {
    assert.ok(whyText.includes("bullish"), whyText);
    assert.ok(whyText.includes("1.8%"), whyText);
    assert.ok(whyText.toLowerCase().includes("fell"), whyText);
    assert.ok(whyText.toLowerCase().includes("opposite"), whyText);
  });
  check("rendered Ticker Analysis HTML actually includes that explanation under the CONFLICTING group", () => {
    const html = lib.renderTickerDetail(confl);
    assert.ok(html.includes("gc-conflicting"), "no conflicting group rendered");
    const conflictingBlockStart = html.indexOf("gc-conflicting");
    const nextGroupStart = html.indexOf("gate-group gc-", conflictingBlockStart + 20);
    const conflictingBlock = html.slice(conflictingBlockStart, nextGroupStart > -1 ? nextGroupStart : conflictingBlockStart + 2000);
    assert.ok(conflictingBlock.includes("gate-why"), "no gate-why explanation div inside the conflicting group");
    assert.ok(conflictingBlock.includes("opposite directions"), conflictingBlock);
  });

  console.log(`\n=== P5: evidence-first layout -- chart + evidence lead, raw JSON stays inside <details> ===`);
  const fluxDetail = await lib.apiGet("/api/ticker/FLUX");
  const fluxHtml = lib.renderTickerDetail(fluxDetail);
  check("chart card appears before the evidence classification card", () => {
    const chartIdx = fluxHtml.indexOf("chart-card");
    const evidenceIdx = fluxHtml.indexOf("evidence-primary-card");
    assert.ok(chartIdx > -1 && evidenceIdx > -1 && chartIdx < evidenceIdx, `chartIdx=${chartIdx} evidenceIdx=${evidenceIdx}`);
  });
  check("evidence classification card appears before the plain state-summary detail-grid card", () => {
    const evidenceIdx = fluxHtml.indexOf("evidence-primary-card");
    const detailGridIdx = fluxHtml.indexOf("detail-grid");
    assert.ok(evidenceIdx > -1 && detailGridIdx > -1 && evidenceIdx < detailGridIdx, `evidenceIdx=${evidenceIdx} detailGridIdx=${detailGridIdx}`);
  });
  check("raw JSON stays inside a collapsed <details> after the evidence/timeline sections", () => {
    const detailsIdx = fluxHtml.indexOf("<details");
    const evidenceIdx = fluxHtml.indexOf("evidence-primary-card");
    assert.ok(detailsIdx > evidenceIdx, "raw <details> appears before the evidence card");
    assert.ok(fluxHtml.slice(detailsIdx).includes("Raw adapter data"));
  });
  check("every gate item in the rendered output carries a why-this-matters explanation", () => {
    for (const [name] of Object.entries(fluxDetail.gates)) {
      assert.ok(lib.gateWhyItMatters(name, fluxDetail.gates[name]).length > 0, `no explanation for gate ${name}`);
    }
  });
  check("no success-rate/prediction framing appears anywhere in the rendered evidence copy", () => {
    const lower = fluxHtml.toLowerCase();
    for (const bad of ["success rate", "win rate", "probability of", "likely to go up", "predicted outcome"]) {
      assert.ok(!lower.includes(bad), `found forbidden framing: "${bad}"`);
    }
  });

  console.log(`\n=== P7: signal lifecycle timeline -- sparse vs multi-entry, never padded ===`);
  check("empty history renders a clean minimal 'no scan history' state, not an error", () => {
    const html = lib.renderLifecycleTimeline([], "NEW");
    assert.ok(html.includes("No scan history recorded yet"));
    assert.ok(!html.toLowerCase().includes("error"));
  });
  check("single-entry history renders a minimal single-step state, not a padded timeline", () => {
    const html = lib.renderLifecycleTimeline([{ scan_date: "2026-10-13", any_gate_passed: true, state: "NEW", created_at: "x" }], "NEW");
    assert.ok(html.includes("too early for a track record"));
    assert.ok(!html.includes("timeline-step"), "single-entry case must not render the multi-step timeline markup");
  });
  const prsx = await lib.apiGet("/api/ticker/PRSX");
  check("PRSX has genuinely multi-entry real history (the seeded persistence-progression scenario)", () => {
    assert.ok(Array.isArray(prsx.history) && prsx.history.length >= 3, `PRSX history too short: ${JSON.stringify(prsx.history)}`);
  });
  check("PRSX's multi-entry history renders the full connected timeline with real dates/states", () => {
    const html = lib.renderLifecycleTimeline(prsx.history, prsx.state);
    assert.ok(html.includes("timeline-step"), "expected the multi-step timeline markup for real multi-entry history");
    for (const e of prsx.history) {
      assert.ok(html.includes(e.scan_date), `missing real scan_date ${e.scan_date} in rendered timeline`);
    }
  });
  const volx = await lib.apiGet("/api/ticker/VOLX");
  check("VOLX also has genuine multi-entry history and renders the full timeline", () => {
    assert.ok(volx.history.length >= 3, JSON.stringify(volx.history));
    const html = lib.renderLifecycleTimeline(volx.history, volx.state);
    assert.ok(html.includes("timeline-step"));
  });

  console.log(`\n=== P6: Investigate workspace always shows ticker + active mode ===`);
  const fakeInvestigate = {
    ticker: "FLUX", tools_used: ["get_company_info"], observed_facts: [], supporting_evidence: [],
    conflicting_observations: [], limitations: [], ai_interpretation: "test interpretation",
    simulated: true, simulated_label: "Simulated transcript (demo mode)",
  };
  check("renderInvestigateResult always renders the ticker + mode context banner", () => {
    const html = lib.renderInvestigateResult(fakeInvestigate, "FLUX", "DEMO mode \u2014 simulated AI transcript");
    assert.ok(html.includes("investigate-context"));
    assert.ok(html.includes(">FLUX<") || html.includes("FLUX"));
    assert.ok(html.includes("DEMO mode"));
    assert.ok(html.includes("simulated AI transcript"));
  });
  check("the SIMULATED banner is still at least as prominent as before (strong banner class preserved)", () => {
    const html = lib.renderInvestigateResult(fakeInvestigate, "FLUX", "DEMO mode");
    assert.ok(html.includes("ai-simulated-banner-strong"));
    assert.ok(html.includes("SIMULATED"));
  });

  console.log(`\n=== P6 CRITICAL: Investigate race guard -- rapid ticker switch mid-investigation ===`);
  // Load FLUX's ticker detail (which wires up its own Investigate button
  // and loadInvestigateSection with FLUX's own navSeq), start an
  // investigation for FLUX, then IMMEDIATELY switch to QRVX before the
  // FLUX investigation's POST resolves. The stale FLUX investigation
  // result must never be rendered into #investigateBody after the user
  // has moved on to QRVX.
  await lib.loadTickerDetail("FLUX");
  const fluxInvestigatePromise = lib.runInvestigate("FLUX", undefined); // uses current _navSeq at call time
  const switchPromise = lib.loadTickerDetail("QRVX"); // increments _navSeq immediately, invalidating the FLUX investigation in flight
  await Promise.all([fluxInvestigatePromise, switchPromise]);
  check("investigateBody does not show the superseded FLUX investigation result", () => {
    const html = el("investigateBody").innerHTML;
    assert.ok(!html.includes("investigate-context-ticker\">FLUX<"), `stale FLUX investigation leaked into DOM: ${html.slice(0, 300)}`);
  });
  check("tickerDetail reflects the newer QRVX navigation, not stale FLUX", () => {
    assert.ok(el("tickerDetail").innerHTML.includes("QRVX"));
  });

  console.log(`\n${passed} check(s) passed.`);
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
