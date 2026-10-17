// Real-browser layout regression. Unlike the previous source-only test,
// this launches Chromium through CDP and asserts rendered rectangles at all
// three required viewport sizes.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const test = fileURLToPath(new URL("./test_p4_market_intel_layout_cdp.py", import.meta.url));
// Round 12 (ux-nav-watchlist-reliability) portability fix: the previous
// hard-coded `../.venv/bin/python` is the Linux/macOS venv layout only --
// Windows venvs put the interpreter at `.venv\Scripts\python.exe`, so this
// could never run there at all. Reuses the SAME env-var-override
// convention backend/app/ai/mcp_tool_client.py's MCP_VENV_PYTHON and this
// very test's own CHROME_BIN already establish ("env var overrides exist
// only so tests/unusual local layouts can point at a different
// interpreter without touching the real default"): an explicit
// PYTHON_BIN wins when set (e.g. a Windows CI runner exporting the
// Scripts\python.exe path), otherwise this falls back to auto-detecting
// the repo's own .venv for the current OS's real layout -- never a new
// dependency, never a weakened test.
const repoRoot = fileURLToPath(new URL("..", import.meta.url));
function defaultVenvPython() {
  const isWindows = process.platform === "win32";
  const candidate = isWindows
    ? `${repoRoot}.venv\\Scripts\\python.exe`
    : `${repoRoot}.venv/bin/python`;
  return candidate;
}
const python = process.env.PYTHON_BIN || defaultVenvPython();
const run = spawnSync(python, [test], { encoding: "utf8", env: process.env });
process.stdout.write(run.stdout || "");
process.stderr.write(run.stderr || "");
if (run.status !== 0) process.exitCode = run.status || 1;
else console.log("\n3 check(s) passed.");
