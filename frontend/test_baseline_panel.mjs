// Settings "Historical baselines (Live)" panel (Phase 4): what the user sees
// about collected coverage, eligibility vs the 20-session requirement, and
// a running/paused/failed build. Pure render (app.core.js has no fetch).
//
//   node frontend/test_baseline_panel.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const corePath = fileURLToPath(new URL("./app.core.js", import.meta.url));
const src = fs.readFileSync(corePath, "utf8");
// eslint-disable-next-line no-new-func
const lib = new Function(`${src}\nreturn {renderBaselinePanel, renderBaselineJob, baselineJobIsActive};`)();

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); failed++; }
}

const ds = (n, why) => ({ complete_prior_sessions: n, required_sessions: 20, insufficient_reason: why });
const overview = (job = null) => ({
  mode: "live", required_sessions: 20, job, job_thread_alive: !!job && job.status === "running",
  tickers: [
    { ticker: "SPY", datasets: { darkpool: ds(7, "7 of 20 complete prior sessions collected -- build the baseline to collect more"),
                                 options_flow: ds(22, "history is sufficient") } },
    { ticker: "QQQ", datasets: { darkpool: ds(3, "3 of 20 complete prior sessions collected; provider history ends before 2026-09-30"),
                                 options_flow: ds(3, "3 of 20 complete prior sessions collected") } },
  ],
});

check("Demo Mode shows an explanation and no build controls", () => {
  const h = lib.renderBaselinePanel({ mode: "demo", tickers: [] });
  assert.match(h, /built only in Live Mode/);
  assert.doesNotMatch(h, /data-baseline-start|Build baselines/);
});

check("each ticker shows collected vs required sessions per dataset", () => {
  const h = lib.renderBaselinePanel(overview());
  assert.match(h, /data-baseline-ticker="SPY"/);
  assert.match(h, />7 \/ 20</);
  assert.match(h, />22 \/ 20</);
  assert.match(h, />3 \/ 20</);
});

check("the reason a ticker is still insufficient is shown, including a provider history cutoff", () => {
  const h = lib.renderBaselinePanel(overview());
  assert.match(h, /Dark pool: 7 of 20 complete prior sessions collected/);
  assert.match(h, /provider history ends before 2026-09-30/);
});

check("panel states that building makes real UW requests and only starts on a click", () => {
  const h = lib.renderBaselinePanel(overview());
  assert.match(h, /real, bounded UW requests/);
  assert.match(h, /only ever starts\s+when you click/);
  assert.match(h, /viewing them makes no UW requests/);
});

check("with no active job, per-ticker and whole-watchlist build buttons are enabled", () => {
  const h = lib.renderBaselinePanel(overview());
  assert.match(h, /data-baseline-start="SPY" >Build baseline/);
  assert.match(h, /id="baselineStartWatchlistBtn" >/);
  assert.match(h, /No baseline build has been started/);
});

const job = (status, extra = {}) => ({
  id: 4, status, tickers: ["SPY", "QQQ"], max_pages_per_ticker: 400, step_pages: 40, target_sessions: 20, pages_used: 120,
  last_error: null,
  progress: { SPY: { state: "running", pages_used: 80, datasets: { darkpool: { complete_prior_sessions: 12 }, options_flow: { complete_prior_sessions: 20 } } },
              QQQ: { state: "pending", pages_used: 0, datasets: {} } },
  ...extra,
});

check("a running job disables new builds and offers Pause and Cancel (not Resume)", () => {
  const h = lib.renderBaselinePanel(overview(job("running")));
  assert.match(h, /data-baseline-start="SPY" disabled/);
  assert.match(h, /id="baselineStartWatchlistBtn" disabled/);
  assert.match(h, /id="baselinePauseBtn"/);
  assert.match(h, /id="baselineCancelBtn"/);
  assert.doesNotMatch(h, /id="baselineResumeBtn"/);
});

check("progress shows per-ticker state, pages against budget and sessions collected", () => {
  const h = lib.renderBaselineJob(job("running"), true);
  assert.match(h, /in progress/);
  assert.match(h, /80 \/ 400/);
  assert.match(h, /<td>12<\/td><td>20<\/td>/);
  assert.match(h, /120 UW pages used, up to 40 per step, target 20 sessions/);
});

check("a paused job offers Resume and Cancel and shows the provider error that stopped it", () => {
  const h = lib.renderBaselineJob(job("paused", { last_error: "darkpool 2026-10-09: error (http 503)" }), false);
  assert.match(h, /id="baselineResumeBtn"/);
  assert.match(h, /id="baselineCancelBtn"/);
  assert.match(h, /Stopped: darkpool 2026-10-09: error \(http 503\)/);
});

check("a job left 'running' with no worker (after a restart) tells the user how to continue", () => {
  const h = lib.renderBaselineJob(job("running"), false);
  assert.match(h, /worker is not running/);
});

check("finished and cancelled jobs are not active and offer no controls", () => {
  for (const s of ["completed", "cancelled"]) {
    assert.equal(lib.baselineJobIsActive(job(s)), false);
    const h = lib.renderBaselineJob(job(s), false);
    assert.doesNotMatch(h, /baselinePauseBtn|baselineResumeBtn|baselineCancelBtn/);
  }
  assert.equal(lib.baselineJobIsActive(job("paused")), true);
});

check("panel explains cooperative Pause/Cancel without promising a time or request bound", () => {
  const h = lib.renderBaselinePanel(overview());
  assert.match(h, /cooperative/);
  assert.match(h, /at most 10 pages by default/);
  assert.match(h, /Retry-After/);
  assert.match(h, /no fixed time limit/);
  assert.doesNotMatch(h, /few seconds|at most 10 more UW requests/);
  assert.match(h, /at most 400 UW pages per ticker; click Build again/);
});

check("a requested pause/cancel is shown as requested, distinct from completed", () => {
  for (const [s, done] of [["pause_requested", "Paused"], ["cancel_requested", "Cancelled"]]) {
    const h = lib.renderBaselineJob(job(s, { step_pages: 10 }), true);
    assert.match(h, /requested -- not yet/);
    assert.match(h, /step in progress finishes first \(up to 10 pages/);
    assert.match(h, /Retry-After/);
    assert.doesNotMatch(h, new RegExp(`>${done}<`));
  }
  assert.match(lib.renderBaselineJob(job("paused"), false), />Paused</);
  assert.match(lib.renderBaselineJob(job("cancelled"), false), />Cancelled</);
  assert.doesNotMatch(lib.renderBaselineJob(job("running"), true), /baselineStepNote/);
});

check("ticker names are HTML-escaped", () => {
  const ov = overview();
  ov.tickers[0].ticker = "<img src=x>";
  assert.doesNotMatch(lib.renderBaselinePanel(ov), /<img src=x>/);
});

check("app.core.js still makes no network calls", () => {
  assert.doesNotMatch(src.replace(/\/\/.*$/gm, ""), /(^|[^\w.-])fetch\(/m);
});

console.log(`\n${passed} check(s) passed, ${failed} failed.`);
if (failed) process.exit(1);
