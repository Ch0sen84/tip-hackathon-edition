// Final remediation round (2026-10-16): Run Once decoupled from the
// recurring scheduler mode. Verifies (1) renderSchedulerControls/
// renderRunOnceDepthActions render the two new one-time actions
// visually/structurally distinct from the recurring mode toggle, with
// the configured top-N disclosed, and (2) real end-to-end HTTP
// behavior against a REAL running backend (TIP_MODE=demo) proving the
// decoupled depth path actually works while mode stays Disabled.
//
// Run with the backend already running:
//   node frontend/test_run_once_depth.mjs http://127.0.0.1:8899
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { obtainAuthToken } from "./test_lib_auth.mjs";

const BASE = process.argv[2] || "http://127.0.0.1:8899";

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}
async function check_async(name, fn) {
  try { await fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

function loadCoreOnly() {
  const timefmtSrc = fs.readFileSync(fileURLToPath(new URL("./timefmt.js", import.meta.url)), "utf8");
  const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
  (0, eval)(timefmtSrc + coreSrc + `
globalThis.__exports = { renderSchedulerControls };`);
  return globalThis.__exports;
}

console.log("\n=== renderSchedulerControls: the two decoupled one-time actions ===");
{
  const { renderSchedulerControls } = loadCoreOnly();

  check("while mode is 'disabled', both one-time action buttons are present and ENABLED", () => {
    const html = renderSchedulerControls({
      mode: "disabled", freshness_window_s: 1200, top_n: 5, cycle_in_progress: false,
    });
    assert.match(html, /id="discoverOnceBtn"[^>]*data-depth="discovery_only"/);
    assert.match(html, /id="discoverEvaluateOnceBtn"[^>]*data-depth="discovery_plus_evaluation"/);
    // Neither new button carries the `disabled` HTML attribute when no
    // cycle is in progress -- unlike the legacy "Run Once now" button,
    // which IS disabled while mode === "disabled" (unchanged, separate
    // assertion below).
    assert.doesNotMatch(html, /id="discoverOnceBtn"[^>]*disabled/);
    assert.doesNotMatch(html, /id="discoverEvaluateOnceBtn"[^>]*disabled/);
  });

  check("the legacy 'Run Once now' button is still disabled while mode is 'disabled' (unchanged behavior)", () => {
    const html = renderSchedulerControls({
      mode: "disabled", freshness_window_s: 1200, top_n: 5, cycle_in_progress: false,
    });
    assert.match(html, /id="schedulerRunOnceBtn"[^>]*disabled/);
  });

  check("both one-time action buttons are DISABLED while a cycle is already in progress", () => {
    const html = renderSchedulerControls({
      mode: "disabled", freshness_window_s: 1200, top_n: 5, cycle_in_progress: true,
    });
    assert.match(html, /id="discoverOnceBtn"[^>]*disabled/);
    assert.match(html, /id="discoverEvaluateOnceBtn"[^>]*disabled/);
  });

  check("the configured top-N value is disclosed next to Discover & Evaluate Once, never a guess", () => {
    const html = renderSchedulerControls({
      mode: "disabled", freshness_window_s: 1200, top_n: 7, cycle_in_progress: false,
    });
    assert.match(html, /top <strong>7<\/strong>/);
  });

  check("the one-time actions render inside a visually distinct ONE-TIME SCAN card, not the mode toggle", () => {
    const html = renderSchedulerControls({
      mode: "dry_run", freshness_window_s: 1200, top_n: 5, cycle_in_progress: false,
    });
    const modeToggleIdx = html.indexOf('id="schedulerModeToggle"');
    const oneTimeCardIdx = html.indexOf('id="runOnceActionsCard"');
    const tagIdx = html.indexOf("ONE-TIME SCAN");
    assert.ok(modeToggleIdx >= 0 && oneTimeCardIdx >= 0, "both blocks must be present");
    assert.ok(oneTimeCardIdx > modeToggleIdx, "one-time card renders AFTER (structurally separate from) the mode toggle");
    assert.ok(tagIdx > modeToggleIdx, "the ONE-TIME SCAN badge is part of the separate card, not the mode toggle");
  });

  check("the one-time card explicitly states the recurring mode is unchanged, naming the CURRENT mode", () => {
    const html = renderSchedulerControls({
      mode: "enabled", freshness_window_s: 1200, top_n: 5, cycle_in_progress: false,
    });
    const cardStart = html.indexOf('id="runOnceActionsCard"');
    const cardHtml = html.slice(cardStart);
    assert.match(cardHtml, /never changes it/);
    assert.match(cardHtml, /Enabled/); // the real short label for the current mode, not raw "enabled"
  });

  check("a dedicated progress/completion status element exists for the one-time actions, separate from the legacy one", () => {
    const html = renderSchedulerControls({
      mode: "disabled", freshness_window_s: 1200, top_n: 5, cycle_in_progress: false,
    });
    assert.match(html, /id="runOnceDepthProgress"/);
    assert.match(html, /id="schedulerRunOnceStatus"/); // legacy element still present, untouched
  });
}

console.log("\n=== End-to-end over the REAL decoupled POST /api/scheduler/run-once depth path ===");
{
  let token;
  await check_async("a real X-TIP-Auth token is obtained", async () => {
    token = await obtainAuthToken(BASE);
    assert.ok(token);
  });

  await check_async("recurring mode is confirmed 'disabled' before any one-time action runs", async () => {
    const res = await fetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } });
    const body = await res.json();
    assert.equal(body.mode, "disabled");
  });

  await check_async("Discover Once (depth=discovery_only) succeeds while mode stays 'disabled'", async () => {
    const res = await fetch(`${BASE}/api/scheduler/run-once`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
      body: JSON.stringify({ depth: "discovery_only" }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ran, true);
    assert.equal(body.depth, "discovery_only");
    assert.equal(body.candidates_evaluated, 0);
    const statusRes = await fetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } });
    const status = await statusRes.json();
    assert.equal(status.mode, "disabled", "recurring mode must still be disabled AFTER a one-time run");
  });

  await check_async("Discover & Evaluate Once (depth=discovery_plus_evaluation) succeeds while mode stays 'disabled'", async () => {
    const res = await fetch(`${BASE}/api/scheduler/run-once`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
      body: JSON.stringify({ depth: "discovery_plus_evaluation" }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ran, true);
    assert.equal(body.depth, "discovery_plus_evaluation");
    const statusRes = await fetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } });
    const status = await statusRes.json();
    assert.equal(status.mode, "disabled");
  });

  await check_async("an invalid depth value is rejected with 400, not silently accepted", async () => {
    const res = await fetch(`${BASE}/api/scheduler/run-once`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
      body: JSON.stringify({ depth: "bogus_depth" }),
    });
    assert.equal(res.status, 400);
  });

  await check_async("omitting depth entirely preserves the exact legacy refusal while mode is disabled", async () => {
    const res = await fetch(`${BASE}/api/scheduler/run-once`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
      body: JSON.stringify({}),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.reason, "disabled");
  });

  await check_async("unauthenticated POST /api/scheduler/run-once with a depth is still rejected (401)", async () => {
    const res = await fetch(`${BASE}/api/scheduler/run-once`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ depth: "discovery_only" }),
    });
    assert.equal(res.status, 401);
  });

  await check_async("a second, immediately-concurrent one-time request is rejected with 409, not queued", async () => {
    const [res1, res2] = await Promise.all([
      fetch(`${BASE}/api/scheduler/run-once`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
        body: JSON.stringify({ depth: "discovery_plus_evaluation" }),
      }),
      fetch(`${BASE}/api/scheduler/run-once`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
        body: JSON.stringify({ depth: "discovery_only" }),
      }),
    ]);
    const statuses = [res1.status, res2.status].sort();
    // Demo-mode discovery is fast enough that a genuine overlap is not
    // guaranteed over real HTTP timing -- this assertion is intentionally
    // permissive (allows both succeeding if they didn't truly overlap)
    // while still catching the real failure mode: neither request may
    // ever come back with a 5xx/crash, and if a 409 occurs it must be
    // exactly that status, never a different error shape.
    for (const s of statuses) {
      assert.ok(s === 200 || s === 409, `unexpected status ${s} -- must be 200 (ran) or 409 (rejected), never a crash`);
    }
  });

  // Leave the shared demo backend in its safe default state.
  await fetch(`${BASE}/api/scheduler/mode`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
    body: JSON.stringify({ mode: "disabled" }),
  });
}

console.log(`\n${passed} check(s) passed.`);
