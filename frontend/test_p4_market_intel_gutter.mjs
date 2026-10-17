// Real-browser layout regression. Unlike the previous source-only test,
// this launches Chromium through CDP and asserts rendered rectangles at all
// three required viewport sizes.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const test = fileURLToPath(new URL("./test_p4_market_intel_layout_cdp.py", import.meta.url));
const python = fileURLToPath(new URL("../.venv/bin/python", import.meta.url));
const run = spawnSync(python, [test], { encoding: "utf8", env: process.env });
process.stdout.write(run.stdout || "");
process.stderr.write(run.stderr || "");
if (run.status !== 0) process.exitCode = run.status || 1;
else console.log("\n3 check(s) passed.");
