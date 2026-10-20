#!/usr/bin/env python3
"""Rendered-UI end-to-end check of Live Mode on RECORDED UW data
(signal-integrity remediation, requirement J).

Expects:
  * tools/uw_replay_server.py running at TIP_REPLAY_URL (e.g. --as-of
    2026-10-16T15:02:30Z), and
  * a real uvicorn backend at TIP_TEST_BASE started with
    UW_BASE_URL=$TIP_REPLAY_URL and TIP_REPLAY_CLOCK set (fresh temp DBs).
This script then drives the real Settings flow over HTTP (bootstrap token ->
save key -> Test Connection -> Live Mode -> watchlist -> Refresh), opens the
real frontend in headless Chrome and asserts what a user actually SEES, then
injects a UW outage and checks the rendered "Data collection failed" state.

  CHROME_BIN=... TIP_TEST_BASE=http://127.0.0.1:18766 TIP_REPLAY_URL=http://127.0.0.1:18901 \
    python frontend/test_e2e_live_replay_cdp.py
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.request

from websockets.sync.client import connect

CHROME = os.environ.get("CHROME_BIN", "chromium")
BASE = os.environ.get("TIP_TEST_BASE", "http://127.0.0.1:18766")
REPLAY = os.environ.get("TIP_REPLAY_URL", "http://127.0.0.1:18901")
SHOTS = pathlib.Path(os.environ.get("TIP_SCREENSHOT_DIR", tempfile.mkdtemp(prefix="tip-e2e-shots-")))
# Tickers come from the replay itself (/__control/info): the PUBLIC synthetic
# fixture (SYNP/SYNC/SYNQ) or, when present locally, the PRIVATE recorded one.
INFO = {}

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name} -- {str(detail)[:400]}")


def http(method, url, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-TIP-Auth", token)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def setup():
    token = http("GET", f"{BASE}/api/auth/bootstrap")["token"]
    http("POST", f"{BASE}/api/settings/uw-key", {"api_key": "replay-ui-key"}, token)
    assert http("POST", f"{BASE}/api/settings/uw-key/test", {}, token)["result"] == "success"
    http("POST", f"{BASE}/api/settings/mode", {"mode": "live"}, token)
    http("PUT", f"{BASE}/api/settings/watchlist", {"watchlist": INFO["watch"]}, token)
    radar = http("POST", f"{BASE}/api/radar/refresh", {}, token)
    http("POST", f"{BASE}/api/ticker/{INFO['chart']}/ohlc/refresh?range=1M", {}, token)
    return token, radar


def main():
    info = http("GET", f"{REPLAY}/__control/info")
    tick = info["tickers"]
    INFO.update(watch=tick[:5],
                detail="PLTR" if "PLTR" in tick else "SYNP",
                chart="NVDA" if "NVDA" in info["bar_tickers"] else info["bar_tickers"][0],
                outage="SPY" if "SPY" in tick else "SYNC")
    print(f"replay fixture: {'SYNTHETIC (public)' if info['synthetic'] else 'RECORDED (private)'}; tickers {INFO['watch']}")
    health = http("GET", f"{BASE}/api/health")
    check("backend is pinned to the replay clock (recorded data, loopback UW)", bool(health.get("replay_clock")), health)
    token, radar = setup()
    check("API: nothing qualifies on the recorded data", radar["highest"] == [], radar["highest"])

    profile = tempfile.mkdtemp(prefix="tip-e2e-cdp-")
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                             "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
                             "--remote-allow-origins=*", f"--user-data-dir={profile}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = pathlib.Path(profile) / "DevToolsActivePort"
        for _ in range(200):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.05)
        port = int(port_file.read_text().splitlines()[0])
        target = json.load(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT"), timeout=10))
        with connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            seq = 0

            def call(method, params=None):
                nonlocal seq
                seq += 1
                ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == seq:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            def text_of(selector):
                r = call("Runtime.evaluate", {"expression":
                         f"(document.querySelector({json.dumps(selector)})||{{}}).innerText||''", "returnByValue": True})
                return r["result"].get("value", "")

            def wait_for(selector, contains, timeout=20):
                end = time.time() + timeout
                txt = ""
                while time.time() < end:
                    txt = text_of(selector)
                    if contains in txt:
                        return txt
                    time.sleep(0.25)
                return txt

            def shot(name):
                data = call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})["data"]
                import base64
                (SHOTS / f"{name}.png").write_bytes(base64.b64decode(data))

            call("Page.enable")
            call("Runtime.enable")
            call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1800, "deviceScaleFactor": 1, "mobile": False})
            call("Page.navigate", {"url": f"{BASE}/#view=radar"})
            body = wait_for("body", "No active qualification")
            check("radar renders 'No active qualification' for evaluated tickers (not 'Not evaluated')",
                  "No active qualification" in body and "Not evaluated" not in body, body[:600])
            check("radar shows the insufficient-evidence note", "insufficient evidence" in body, body[:600])
            check("radar never shows HIGH/CONFIRMED on recorded data", "CONFIRMED" not in body and "HIGH" not in body)
            shot("radar")

            call("Page.navigate", {"url": f"{BASE}/#view=ticker&ticker={INFO['detail']}"})
            detail = wait_for("#tickerDetail", "Last successful data collection")
            check("ticker header distinguishes Last evaluated vs Last successful data collection",
                  "Last evaluated" in detail and "Last successful data collection" in detail, detail[:600])
            check("ticker header names the evidence session", "evidence session: 2026-10-16" in detail, detail[:600])
            check("confidence reads insufficient, not a probability", "confidence: insufficient" in detail, detail[:400])
            check("chart caption no longer claims markers are the gates' exact inputs",
                  "not necessarily the exact snapshot" in detail, detail[:800])
            call("Runtime.evaluate", {"expression": "document.querySelectorAll('details').forEach(d=>d.open=true)"})
            detail = text_of("#tickerDetail")
            check("gate diagnostics show 'insufficient history' with the real reason",
                  "insufficient history" in detail.lower() and "verified from the open" in detail, detail[-1500:])
            check("synthetic percentile is not presented as a trailing-window percentile",
                  "trailing 30-day window" not in detail and "Not computed in Live Mode" in detail)
            check("no 'trustworthy' or 'statistical anomaly' claims rendered",
                  "trustworthy" not in detail and "statistical anomaly" not in detail)
            shot("ticker_detail")

            call("Page.navigate", {"url": f"{BASE}/#view=ticker&ticker={INFO['chart']}"})
            prov = wait_for("#chartMarkerProvenance", "Markers:")
            check("chart marker provenance line is rendered", prov.startswith("Markers:"), prov)

            # --- inject a UW outage and refresh through the real UI button ---
            # stock-state is requested by EVERY scan (flow/dark-pool coverage may
            # already be current at the pinned replay clock, in which case the
            # collector correctly makes no new request for them).
            http("POST", f"{REPLAY}/__control/fail?path=stock-state&status=503")
            call("Page.navigate", {"url": f"{BASE}/#view=ticker&ticker={INFO['outage']}"})
            wait_for("#tickerDetail", "Refresh this ticker")   # rendered, not the "Loading SPY..." placeholder
            call("Runtime.evaluate", {"expression": "document.getElementById('tickerRefreshBtn') && document.getElementById('tickerRefreshBtn').click()"})
            failed_txt = wait_for("#tickerDetail", "Data collection failed", timeout=60)
            head = failed_txt.split("Last evaluated")[0]
            check("an outage renders as 'Data collection failed', never as Current",
                  "Data collection failed" in failed_txt and "Current" not in head, failed_txt[:600])
            call("Runtime.evaluate", {"expression": "document.querySelectorAll('details').forEach(d=>d.open=true)"})
            failed_txt = text_of("#tickerDetail")
            check("data quality names the failed source and the scan is held",
                  "Collection failed for: stock state" in failed_txt and "state held" in failed_txt, failed_txt[-1500:])
            check("price confirmation reports unavailable data, not a negative observation",
                  "Price data could not be collected this scan" in failed_txt, failed_txt[-1500:])
            shot("ticker_outage")
            http("POST", f"{REPLAY}/__control/reset")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    print(f"\n{passed} check(s) passed, {failed} failed. Screenshots: {SHOTS}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
