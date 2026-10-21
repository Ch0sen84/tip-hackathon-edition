// Phase D frontend verification: Market-wide Radar scheduler controls +
// freshness-tagged candidate list. Same convention as the rest of this
// repo's frontend/test_*.mjs scripts: a plain Node script, loading the
// REAL frontend/app.core.js render functions directly, plus an
// end-to-end check against a REAL running backend (TIP_MODE=demo) over
// real HTTP for the actual routes.
//
// Run with the backend already running:
//   node frontend/test_market_wide_radar.mjs http://127.0.0.1:8899

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
  // formatTimestampFull/formatTimestampET/formatRelative live in the
  // separate, shared frontend/timefmt.js (loaded before app.core.js in
  // every real page, see index.html) -- loaded here too so the render
  // functions under test see the exact same globals they do in the
  // browser, not a second/duplicate formatter.
  const timefmtSrc = fs.readFileSync(fileURLToPath(new URL("./timefmt.js", import.meta.url)), "utf8");
  const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
  (0, eval)(timefmtSrc + coreSrc + `
globalThis.__exports = {
  renderSchedulerControls, renderMarketWideCandidateRow, renderMarketWideRadar, renderWhySurfacedBlock,
  formatTimestampFull,
};`);
  return globalThis.__exports;
}

// UX round (2026-10-16 round 5), item 1: the new "View Details" action +
// "Why TIP surfaced this" evidence block on each Market Intelligence row.
console.log("\n=== renderMarketWideCandidateRow: View Details + Why Surfaced (item 1) ===");
{
  const { renderMarketWideCandidateRow, renderWhySurfacedBlock } = loadCoreOnly();
  check("row renders a View Details action wired to the SAME data-investigate-ticker attribute Market Radar uses", () => {
    const html = renderMarketWideCandidateRow({ ticker: "NVDA", freshness: "current", evaluation: null, age_s: null, raw: null });
    assert.match(html, /data-investigate-ticker="NVDA"/);
    assert.match(html, /View Details/);
  });
  check("Why Surfaced: a real net_premium/call_volume/etc. value renders as itself, not a placeholder", () => {
    const html = renderWhySurfacedBlock({ raw: { net_premium: 5_000_000, call_volume: 1200, put_volume: 300, iv_rank: 62.4, change_percent: 3.1, source_date: "2026-10-16" } });
    assert.match(html, /Net premium/);
    // D3 fix (ux-nav-watchlist-reliability round 9): Net premium now
    // uses the SAME fmtMoney() helper as the rest of the app (abbreviated
    // magnitude + proper Unicode minus for negatives), not a raw
    // unabbreviated "$5,000,000" -- see fmtMoney()'s own module comment
    // and test_d3_why_surfaced_money.mjs for the full writeup of the
    // real negative-premium display bug this closes.
    assert.match(html, /\$5\.0M/);
    assert.doesNotMatch(html, /\$5,000,000/);
    assert.doesNotMatch(html, /not captured/);
  });
  check("Why Surfaced: a genuinely real ZERO value (checked, found nothing) renders as '0', never blank/placeholder", () => {
    const html = renderWhySurfacedBlock({ raw: { net_premium: 0, call_volume: 0, put_volume: 0, iv_rank: null, change_percent: null } });
    // put_volume: 0 is a REAL checked-and-found-zero value -- must render
    // the actual digit, not the "not captured" wording used for a truly
    // missing field.
    assert.match(html, /Put volume: <strong>0<\/strong>/);
  });
  check("Why Surfaced: a missing/None field renders VISIBLY DIFFERENT text+class than a real zero", () => {
    const html = renderWhySurfacedBlock({ raw: { net_premium: null, call_volume: 10, put_volume: 0, iv_rank: null, change_percent: null } });
    assert.match(html, /px-unavailable">not captured/); // net_premium: missing -> distinct class+wording
    assert.match(html, /Put volume: <strong>0<\/strong>/); // put_volume: real zero -> plain value, no px-unavailable class
  });
  check("Why Surfaced: entirely absent raw_json is itself distinct wording from any per-field placeholder", () => {
    const html = renderWhySurfacedBlock({ raw: null });
    assert.match(html, /no discovery details were captured for this snapshot/);
  });
}

console.log("\n=== Market Intelligence timestamp cleanup (item 3) ===");
{
  const { formatTimestampFull } = loadCoreOnly();
  check("formatTimestampFull never renders a raw ISO-with-microseconds string", () => {
    const out = formatTimestampFull("2026-10-16T14:53:37.980307+00:00");
    assert.doesNotMatch(out, /980307/);
    assert.doesNotMatch(out, /^\d{4}-\d{2}-\d{2}T/);
  });
}

console.log("\n=== renderSchedulerControls ===");
{
  const { renderSchedulerControls } = loadCoreOnly();
  check("shows all three mode buttons", () => {
    const html = renderSchedulerControls({ mode: "disabled", last_run_finished_at: null, next_run_at: null,
      last_cycle_candidates_evaluated: null, freshness_window_s: 1200, pause_reason: null });
    assert.match(html, /data-scheduler-mode="disabled"/);
    assert.match(html, /data-scheduler-mode="dry_run"/);
    assert.match(html, /data-scheduler-mode="enabled"/);
  });
  check("marks the current mode's button active", () => {
    const html = renderSchedulerControls({ mode: "dry_run", freshness_window_s: 1200 });
    assert.match(html, /radar-mode-btn active" data-scheduler-mode="dry_run"/);
  });
  check("shows a pause reason when present, never hides it", () => {
    const html = renderSchedulerControls({ mode: "enabled", freshness_window_s: 1200, pause_reason: "disk low" });
    assert.match(html, /Paused: disk low/);
  });
  check("never shows a pause note when there is none", () => {
    const html = renderSchedulerControls({ mode: "disabled", freshness_window_s: 1200, pause_reason: null });
    assert.doesNotMatch(html, /Paused:/);
  });
}

console.log("\n=== renderMarketWideCandidateRow / renderMarketWideRadar ===");
{
  const { renderMarketWideCandidateRow, renderMarketWideRadar } = loadCoreOnly();
  check("a current candidate shows the current freshness badge", () => {
    const html = renderMarketWideCandidateRow({
      ticker: "NVDA", freshness: "current", evaluation: { state: "WATCHING", confidence: "LOW" }, age_s: 100,
    });
    assert.match(html, /freshness-badge freshness-current/);
    assert.match(html, /Current/);
  });
  check("a stale candidate shows the stale badge, distinct from current", () => {
    const html = renderMarketWideCandidateRow({
      ticker: "TSLA", freshness: "stale", evaluation: { state: "WATCHING", confidence: "LOW" }, age_s: 5000,
    });
    assert.match(html, /freshness-badge freshness-stale/);
    assert.match(html, /Stale/);
  });
  check("an unevaluated candidate shows the unevaluated badge, never fabricates an evaluation", () => {
    const html = renderMarketWideCandidateRow({ ticker: "SPY", freshness: "unevaluated", evaluation: null, age_s: null });
    assert.match(html, /freshness-badge freshness-unevaluated/);
    assert.match(html, /no evaluation yet/);
  });
  check("UX remediation item 4: an index/derivative row suppresses View Details", () => {
    const html = renderMarketWideCandidateRow({
      ticker: "SPXW", freshness: "unevaluated", evaluation: null, age_s: null,
      raw: { is_index: true },
    });
    assert.doesNotMatch(html, /data-investigate-ticker="SPXW"/);
    assert.doesNotMatch(html, /View Details/);
    assert.match(html, /Not supported \(index\/derivative\)/);
  });
  check("UX remediation item 4: a non-index row still shows View Details as before", () => {
    const html = renderMarketWideCandidateRow({
      ticker: "NVDA", freshness: "unevaluated", evaluation: null, age_s: null,
      raw: { is_index: false },
    });
    assert.match(html, /data-investigate-ticker="NVDA"/);
    assert.match(html, /View Details/);
  });
  check("empty candidate list renders an honest note, never fabricated rows", () => {
    const html = renderMarketWideRadar({ candidates: [], note: "No discovery snapshot cataloged yet." });
    assert.match(html, /No discovery snapshot cataloged yet/);
  });
  check("a real candidate list renders one row per candidate plus the coverage note", () => {
    const html = renderMarketWideRadar({
      candidates: [
        { ticker: "NVDA", freshness: "current", evaluation: { state: "WATCHING", confidence: "LOW" }, age_s: 50 },
        { ticker: "TSLA", freshness: "unevaluated", evaluation: null, age_s: null },
      ],
      snapshot_at: "2026-10-15T14:00:00+00:00",
      coverage_note: "Freshness reflects the most recent cataloged evaluation.",
    });
    assert.match(html, /NVDA/);
    assert.match(html, /TSLA/);
    assert.match(html, /Freshness reflects the most recent cataloged evaluation\./);
  });
}

console.log("\n=== End-to-end over the REAL /api/scheduler/* + /api/radar/market-wide routes ===");
{
  let token;
  // Windows Docker Desktop QA round 2 (finding T1): falls back to the
  // pairing code (TIP_TEST_PAIRING_CODE) when bootstrap fails under
  // real Docker Desktop's port-publishing NAT -- see test_lib_auth.mjs.
  await check_async("a real X-TIP-Auth token is obtained (bootstrap, or pairing-code fallback under Docker Desktop)", async () => {
    token = await obtainAuthToken(BASE);
    assert.ok(token);
  });

  await check_async("unauthenticated GET /api/scheduler/status is rejected", async () => {
    const res = await fetch(`${BASE}/api/scheduler/status`);
    assert.equal(res.status, 401);
  });

  await check_async("authenticated GET /api/scheduler/status defaults to disabled", async () => {
    const res = await fetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.mode, "disabled");
  });

  await check_async("POST /api/scheduler/mode requires auth", async () => {
    const res = await fetch(`${BASE}/api/scheduler/mode`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "dry_run" }),
    });
    assert.equal(res.status, 401);
  });

  await check_async("authenticated POST /api/scheduler/mode sets dry_run and status reflects it", async () => {
    const res = await fetch(`${BASE}/api/scheduler/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
      body: JSON.stringify({ mode: "dry_run", confirm: "dry_run" }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.mode, "dry_run");
    const statusRes = await fetch(`${BASE}/api/scheduler/status`, { headers: { "X-TIP-Auth": token } });
    const status = await statusRes.json();
    assert.equal(status.mode, "dry_run");
  });

  await check_async("an invalid mode is rejected with 400, not silently accepted", async () => {
    const res = await fetch(`${BASE}/api/scheduler/mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
      body: JSON.stringify({ mode: "turbo" }),
    });
    assert.equal(res.status, 400);
  });

  await check_async("GET /api/radar/market-wide requires auth too", async () => {
    const res = await fetch(`${BASE}/api/radar/market-wide`);
    assert.equal(res.status, 401);
  });

  await check_async("GET /api/radar/market-wide returns the real honest shape (candidates array present)", async () => {
    const res = await fetch(`${BASE}/api/radar/market-wide`, { headers: { "X-TIP-Auth": token } });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.ok(Array.isArray(body.candidates));
  });

  // Restore to disabled (the safe default) so this test run never leaves
  // a real demo server with background mode on.
  await fetch(`${BASE}/api/scheduler/mode`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-TIP-Auth": token },
    body: JSON.stringify({ mode: "disabled" }),
  });
}

console.log(`\n${passed} check(s) passed.`);
