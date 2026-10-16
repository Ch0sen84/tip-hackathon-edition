// Shared token-acquisition helper for the .mjs integration tests that
// hit a REAL running backend (test_navigation.mjs, test_p5_p6_p7.mjs,
// test_manual_bugfixes.mjs, test_investigate_safeguards.mjs,
// test_discovery.mjs, test_market_wide_radar.mjs).
//
// Windows Docker Desktop QA round 2 -- finding T1: these 6 files used to
// call ONLY `GET /api/auth/bootstrap` and hard-fail (`throw`) on
// anything but a 200. That endpoint is gated on `is_loopback(request.
// client.host)` (see backend/app/auth.py / app/main.py's auth_bootstrap
// docstring) -- a real loopback TCP peer check that genuinely holds
// against the dev uvicorn server a Node process on the same machine
// talks to directly, but is a DOCUMENTED, PRE-EXISTING failure mode
// under real Docker Desktop (Windows/Mac): Docker Desktop's port-
// publishing proxy (vpnkit/WSL2) re-sources the connection, so
// `request.client.host` arrives as the Docker bridge-gateway address,
// not a literal loopback peer -- see app/main.py's `auth_pair` route
// docstring ("a genuine Docker Desktop browser whose request arrives at
// is_loopback() as the bridge-gateway address... so GET
// /api/auth/bootstrap 403s") and the startup-time pairing-code banner
// that exists FOR EXACTLY THIS CASE (QA remediation D1). That mechanism
// was already correct; these 6 test files just never used it, so they
// failed hard under Docker instead of falling back the way the actual
// product UI (and INSTALL_AGENT.md's documented operator flow) already
// does.
//
// Fix: fall back to `POST /api/auth/pair` with a pairing code the
// caller supplies via TIP_TEST_PAIRING_CODE -- the exact same code an
// operator/agent would scrape from `docker logs <container>` (see
// auth_pair's docstring) or, for a local dev-server run, from this
// process's own stdout banner. This is a TEST-HARNESS-ONLY change --
// backend/app/auth.py, app/main.py's bootstrap/pair routes, and the
// pairing-code store are untouched, preserving Network Mode's existing
// token/pairing/bootstrap flow exactly as-is (constraint #9).
//
// Usage: node frontend/test_navigation.mjs http://127.0.0.1:8000
//        TIP_TEST_PAIRING_CODE=<code from `docker logs`/startup banner> \
//          node frontend/test_navigation.mjs http://127.0.0.1:8000

/**
 * @param {string} base
 * @returns {Promise<string>} a valid X-TIP-Auth token
 */
export async function obtainAuthToken(base) {
  const bootstrapRes = await fetch(`${base}/api/auth/bootstrap`);
  if (bootstrapRes.ok) {
    const { token } = await bootstrapRes.json();
    return token;
  }

  const pairingCode = process.env.TIP_TEST_PAIRING_CODE;
  if (!pairingCode) {
    throw new Error(
      `GET /api/auth/bootstrap -> HTTP ${bootstrapRes.status}, and no TIP_TEST_PAIRING_CODE ` +
        "env var was set to fall back to POST /api/auth/pair. Under real Docker Desktop " +
        "(Windows/Mac) this bootstrap 403 is EXPECTED (see app/main.py's auth_pair docstring) " +
        "-- set TIP_TEST_PAIRING_CODE to the code from `docker logs <container>` (or this " +
        "process's own startup banner for a local dev-server run) before re-running this test."
    );
  }
  const pairRes = await fetch(`${base}/api/auth/pair`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pairing_code: pairingCode }),
  });
  if (!pairRes.ok) {
    throw new Error(
      `GET /api/auth/bootstrap -> HTTP ${bootstrapRes.status}, and the TIP_TEST_PAIRING_CODE ` +
        `fallback via POST /api/auth/pair also failed -> HTTP ${pairRes.status} (code wrong, ` +
        "expired, or already used -- codes are single-use and rotate every 15 minutes)."
    );
  }
  const { token } = await pairRes.json();
  return token;
}
