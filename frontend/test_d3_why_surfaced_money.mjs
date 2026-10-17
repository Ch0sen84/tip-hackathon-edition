// D3 regression test (ux-nav-watchlist-reliability round 9): Market
// Intelligence's "Why TIP surfaced this" net_premium field must use the
// SAME fmtMoney() helper (proper Unicode minus + abbreviated magnitude)
// as every other money display in this app, never a raw signed number.
//
// Real reported defect: a negative net_premium rendered as the raw
// "$-89,231,250" (sign stuck inside "$", full unabbreviated digit count)
// instead of "\u2212$89.2M". Loads the REAL frontend/app.core.js.
//
//   node frontend/test_d3_why_surfaced_money.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
(0, eval)(src + "\nglobalThis.__exports = { renderWhySurfacedBlock, fmtMoney, WHY_SURFACED_FIELDS };");
const { renderWhySurfacedBlock, fmtMoney } = globalThis.__exports;

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

check("fmtMoney formats a large negative premium with Unicode minus + abbreviated magnitude", () => {
  assert.equal(fmtMoney(-89231250), "\u2212$89.2M");
});

check("renderWhySurfacedBlock's Net premium field uses fmtMoney, not raw toLocaleString", () => {
  const html = renderWhySurfacedBlock({
    raw: { net_premium: -89231250, call_volume: 100, put_volume: 50, iv_rank: 20.5, change_percent: -1.2 },
  });
  assert.ok(html.includes("\u2212$89.2M"), html);
  assert.ok(!html.includes("$-89"), `raw signed-number form must not appear: ${html}`);
  assert.ok(!/\$-[\d,]+/.test(html), `no sign-inside-dollar form anywhere: ${html}`);
});

check("a positive net_premium still renders plainly (no spurious minus)", () => {
  const html = renderWhySurfacedBlock({ raw: { net_premium: 5230000, call_volume: 10, put_volume: 5, iv_rank: 30, change_percent: 2.1 } });
  assert.ok(html.includes("$5.2M"), html);
  assert.ok(!html.includes("\u2212"), html);
});

console.log(`\n${passed} check(s) passed.`);
