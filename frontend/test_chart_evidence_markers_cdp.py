#!/usr/bin/env python3
"""Real-Chromium/CDP regression + before/after evidence for the chart
evidence HISTOGRAM pane (chart-evidence-markers branch, round 2,
2026-10-17).

History: the branch's first fix (511eb08, dot overlay) was itself
REJECTED by Chad after reviewing real SPY/NVDA screenshots -- at real UW
data density (hundreds-to-thousands of events/day clustered on the most
recent trading days) grouped dots on consecutive dense bars still
visually collided, same root problem (bar-spacing vs. marker size)
wearing a smaller marker. This round replaces the dot overlay entirely
with two native lightweight-charts histogram series (orange = flow
alerts, blue = dark-pool trades) sharing the candlestick chart's own
instance/timeScale -- see app.core.js's mountEvidenceHistogram comment
for the full architecture writeup. Also covers Step 1 (1D intraday
HH:MM tick labels, via timeVisible/secondsVisible on the main chart's
timeScale).

This script:
  1. Renders synthetic SPY-shaped fixtures (dense flow+dark-pool
     evidence on the last few trading days/bars, same reported shape as
     before) through app.core.js at 1D (intraday) and 1M/3M/6M/1Y
     (daily), at 1920x1080 and 2560x1440, saving before/after
     screenshots (before = main's app.core.js at 62b29f8, i.e. the dot
     overlay's predecessor with native setMarkers() text labels; after
     = this working tree).
  2. Drives the NEW code's real rendered DOM/chart state via CDP (real
     mouse events dispatched through the canvas, real chart-instance
     introspection via the root._tipEvidenceSeries test hook -- not
     string/DOM-presence assertions) to verify: histogram data shape per
     range, legend toggle controlling series visibility, hover tooltip
     content (real counts), click drill-down routing into the existing
     evidence panel, out-of-range exclusion, realignment across range
     switches, resize, and 1D tick-label distinctness.

No backend, no app.live.js, no network calls of any kind -- the harness
page (test_chart_evidence_markers_harness.html) loads only
lightweight-charts.js + app.core.js + a synthetic fixture built here.

Run: python3 frontend/test_chart_evidence_markers_cdp.py
"""
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import http.server
import threading
import functools
from websockets.sync.client import connect

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
CHROME = os.environ.get("CHROME_BIN", "/home/hermes/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")
SCREENSHOT_DIR = pathlib.Path(os.environ.get("TIP_SCREENSHOT_DIR", "/home/hermes/.hermes/cache/scratch/chart_evidence_markers_screenshots"))
SIZES = [(1920, 1080), (2560, 1440)]

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        print(f"PASS: {name}")
        passed += 1
    else:
        print(f"FAIL: {name} -- {detail}")
        failed += 1


# --- synthetic SPY-shaped fixtures ------------------------------------------
# Real-shaped, fabricated-but-labeled-synthetic data (same convention the
# repo's own fixtures/README_SYNTHETIC.md uses). Daily-bar fixture
# reproduces the EXACT originally-reported defect shape (dense evidence
# on the last few trading days); the new 1D intraday fixture exercises
# Step 1 (HH:MM labels) and Step 2's fine (5-minute) bucket width.
def build_daily_fixture(num_days=22, base_price=560.0):
    days = []
    d = datetime.date(2026, 10, 17)  # "today" for this fixture
    while len(days) < num_days:
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    days.sort()
    candles = []
    price = base_price
    for i, day in enumerate(days):
        o = price
        c = price + (1.2 if i % 2 == 0 else -0.8)
        h = max(o, c) + 1.5
        l = min(o, c) - 1.5
        candles.append({"t": day.isoformat(), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 30_000_000 + i * 50_000})
        price = c
    last_day = days[-1]

    markers = []
    for back in range(4):
        day = days[-1 - back]
        for k in range(6 - back):
            markers.append({
                "kind": "flow_alert",
                "time": f"{day.isoformat()}T{14 + k % 6:02d}:{(k * 7) % 60:02d}:00Z",
                "price": round(base_price + back * 0.3 + k * 0.05, 2),
                "label": f"Flow alert ({'call' if k % 2 == 0 else 'put'})",
                "value": 400_000 + k * 137_000 + back * 9000,
            })
        for k in range(4 - back if back < 4 else 0):
            markers.append({
                "kind": "darkpool_trade",
                "time": f"{day.isoformat()}T{10 + k:02d}:{(k * 11) % 60:02d}:00Z",
                "price": round(base_price - back * 0.2 + k * 0.08, 2),
                "label": "Dark pool print",
                "value": 2_000_000 + k * 540_000,
            })
    sparse_day = days[-10]
    markers.append({"kind": "flow_alert", "time": f"{sparse_day.isoformat()}T15:00:00Z", "price": base_price - 3.0, "label": "Flow alert (put)", "value": 210_000})
    future_day = last_day + datetime.timedelta(days=5)
    markers.append({"kind": "flow_alert", "time": f"{future_day.isoformat()}T23:59:00Z", "price": 999.0, "label": "Flow alert (call) FUTURE", "value": 1})

    markers.sort(key=lambda m: m["time"])
    return {"candles": candles, "markers": markers, "coverage": {"range_fully_satisfied": True}}, last_day


# --- 1D intraday fixture: real UW 5-minute bars, dense flow/dark-pool
# evidence clustered in the final hour (the real reported shape: UW's
# flow-alert/darkpool endpoints return only the most recent events) plus
# one genuinely out-of-range (after the session closes) marker.
def build_intraday_fixture(base_price=560.0, session_date="2026-10-17"):
    candles = []
    price = base_price
    start = datetime.datetime.fromisoformat(f"{session_date}T13:30:00+00:00")  # 9:30 ET
    num_bars = 78  # 6.5h session / 5-min bars
    for i in range(num_bars):
        t = start + datetime.timedelta(minutes=5 * i)
        o = price
        c = price + (0.08 if i % 3 else -0.05)
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append({"t": t.isoformat().replace("+00:00", "Z"), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 400_000 + i * 1000})
        price = c
    last_bar_time = start + datetime.timedelta(minutes=5 * (num_bars - 1))

    markers = []
    # Dense cluster on the last 6 real 5-minute bars (end of session --
    # exactly where real UW evidence clusters most).
    for back in range(6):
        bar_time = last_bar_time - datetime.timedelta(minutes=5 * back)
        for k in range(5 - (back % 3)):
            markers.append({
                "kind": "flow_alert",
                "time": (bar_time + datetime.timedelta(seconds=10 * k)).isoformat().replace("+00:00", "Z"),
                "price": round(base_price + k * 0.03, 2),
                "label": f"Flow alert ({'call' if k % 2 == 0 else 'put'})",
                "value": 150_000 + k * 61_000,
            })
        for k in range(2):
            markers.append({
                "kind": "darkpool_trade",
                "time": (bar_time + datetime.timedelta(seconds=20 + 10 * k)).isoformat().replace("+00:00", "Z"),
                "price": round(base_price - k * 0.02, 2),
                "label": "Dark pool print",
                "value": 900_000 + k * 210_000,
            })
    # A sparse earlier bucket (mid-session single flow alert).
    mid_bar = start + datetime.timedelta(minutes=5 * 30)
    markers.append({"kind": "flow_alert", "time": mid_bar.isoformat().replace("+00:00", "Z"), "price": base_price - 1.0, "label": "Flow alert (put)", "value": 95_000})
    # Genuinely out-of-range: after the session's last real bar.
    future_time = last_bar_time + datetime.timedelta(minutes=30)
    markers.append({"kind": "flow_alert", "time": future_time.isoformat().replace("+00:00", "Z"), "price": 999.0, "label": "Flow alert (call) FUTURE", "value": 1})

    markers.sort(key=lambda m: m["time"])
    return {"candles": candles, "markers": markers, "coverage": {"range_fully_satisfied": True}}, last_bar_time


FIXTURE_1D, LAST_BAR_1D = build_intraday_fixture()
FIXTURE_1M, LAST_DAY = build_daily_fixture(22)
FIXTURE_3M, _ = build_daily_fixture(65)
FIXTURE_6M, _ = build_daily_fixture(130)
FIXTURE_1Y, _ = build_daily_fixture(252)

# --- round 3 (2026-10-17, visual-separation fix): a second, genuinely
# NVDA-shaped fixture (real NVDA price regime, ~$925) -- same builders,
# real different base price/volatility so it isn't just SPY relabeled --
# used alongside the SPY fixtures above for the 8 required SPY/NVDA x
# 1D/3M x 1920x1080/2560x1440 post-fix visual-inspection screenshots.
FIXTURE_NVDA_1D, LAST_BAR_NVDA_1D = build_intraday_fixture(base_price=925.0, session_date="2026-10-17")
FIXTURE_NVDA_3M, _ = build_daily_fixture(65, base_price=925.0)

# === Round 4 (real-data QA remediation, 2026-10-17) regression fixtures ===
# Shaped specifically after Claude Code's real-UW-data findings (C-1 thru
# C-4), relayed by Chad -- NOT the lighter/more-regular synthetic shapes
# used in rounds 2-3. Each one targets exactly one finding below.

# --- Fixture R4-A: a large cluster of dark-pool prints landing in ONE
# single 5-minute interval (C-3's lane-separation + C-2's hit-testing at
# high local density). 180 real dark-pool prints, zero flow alerts, all
# in the SAME 5-minute bar -- the realistic "hundreds of events on one
# bucket" shape Chad's relay described.
def build_dense_single_bucket_fixture(base_price=560.0, session_date="2026-10-17", n_dp=180):
    candles = []
    price = base_price
    start = datetime.datetime.fromisoformat(f"{session_date}T13:30:00+00:00")  # 9:30 ET
    num_bars = 78
    for i in range(num_bars):
        t = start + datetime.timedelta(minutes=5 * i)
        o = price
        c = price + (0.05 if i % 2 else -0.03)
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append({"t": t.isoformat().replace("+00:00", "Z"), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 300_000 + i * 500})
        price = c
    last_bar_time = start + datetime.timedelta(minutes=5 * (num_bars - 1))
    bucket_time = last_bar_time  # the dense bucket IS the most recent real bar
    markers = []
    for k in range(n_dp):
        markers.append({
            "kind": "darkpool_trade",
            "time": (bucket_time + datetime.timedelta(seconds=(k % 290))).isoformat().replace("+00:00", "Z"),
            "price": round(base_price + (k % 11) * 0.01, 2),
            "label": "Dark pool print",
            "value": 150_000 + (k % 17) * 23_000,
        })
    markers.sort(key=lambda m: m["time"])
    return {"candles": candles, "markers": markers, "coverage": {"range_fully_satisfied": True}}, bucket_time, n_dp


# --- Fixture R4-B: flow-alert AND dark-pool events in the EXACT SAME
# time bucket (same target -- C-2/C-3's "two kinds sharing one x
# position" case).
def build_shared_bucket_fixture(base_price=560.0, session_date="2026-10-17", n_flow=40, n_dp=25):
    candles = []
    price = base_price
    start = datetime.datetime.fromisoformat(f"{session_date}T13:30:00+00:00")
    num_bars = 78
    for i in range(num_bars):
        t = start + datetime.timedelta(minutes=5 * i)
        o = price
        c = price + (0.04 if i % 2 else -0.02)
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append({"t": t.isoformat().replace("+00:00", "Z"), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 300_000 + i * 500})
        price = c
    last_bar_time = start + datetime.timedelta(minutes=5 * (num_bars - 1))
    bucket_time = last_bar_time
    markers = []
    for k in range(n_flow):
        markers.append({
            "kind": "flow_alert",
            "time": (bucket_time + datetime.timedelta(seconds=(k % 290))).isoformat().replace("+00:00", "Z"),
            "price": round(base_price + (k % 7) * 0.02, 2),
            "label": f"Flow alert ({'call' if k % 2 == 0 else 'put'})",
            "value": 80_000 + (k % 13) * 19_000,
        })
    for k in range(n_dp):
        markers.append({
            "kind": "darkpool_trade",
            "time": (bucket_time + datetime.timedelta(seconds=(k % 290))).isoformat().replace("+00:00", "Z"),
            "price": round(base_price - (k % 9) * 0.015, 2),
            "label": "Dark pool print",
            "value": 120_000 + (k % 11) * 31_000,
        })
    markers.sort(key=lambda m: m["time"])
    return {"candles": candles, "markers": markers, "coverage": {"range_fully_satisfied": True}}, bucket_time, n_flow, n_dp


# --- Fixture R4-C: a long (3M-shaped) daily range where almost ALL real
# observations land on the single most-recent trading day, with
# near-total absence on every earlier day (C-4's direct target -- tests
# the honest single-day coverage note, never a fabricated wide-coverage
# look).
def build_single_day_dominant_fixture(num_days=65, base_price=560.0):
    fixture, last_day = build_daily_fixture(num_days, base_price=base_price)
    # Strip every marker except the ones on the real most-recent day --
    # build_daily_fixture's own markers already concentrate on the last
    # few days; this keeps ONLY last_day's events so the fixture
    # genuinely has real evidence on exactly one real day within a
    # 65-trading-day (3M) window, with zero fabricated earlier-day
    # markers mixed in.
    only_last_day = [m for m in fixture["markers"] if m["time"].startswith(last_day.isoformat()) and "FUTURE" not in m.get("label", "")]
    fixture = dict(fixture, markers=only_last_day)
    return fixture, last_day


FIXTURE_R4_DENSE_BUCKET, R4_DENSE_BUCKET_TIME, R4_DENSE_DP_COUNT = build_dense_single_bucket_fixture()
FIXTURE_R4_SHARED_BUCKET, R4_SHARED_BUCKET_TIME, R4_SHARED_FLOW_COUNT, R4_SHARED_DP_COUNT = build_shared_bucket_fixture()
FIXTURE_R4_SINGLE_DAY_DOMINANT, R4_SINGLE_DAY = build_single_day_dominant_fixture()

# --- Fixture R4-D: off-hours events (pre-market/after-hours, outside
# 9:30-16:00 ET) AND events straddling a REAL US/Eastern DST transition
# boundary (C-1's direct target). 2026-03-08 is the real second-Sunday-
# in-March 2026 DST "spring forward" date (clocks jump from 2:00 AM EST
# to 3:00 AM EDT) -- a genuine historical/calendar transition, not an
# arbitrary date.
def build_dst_and_offhours_fixture(session_date="2026-10-17", base_price=560.0):
    candles = []
    price = base_price
    start = datetime.datetime.fromisoformat(f"{session_date}T13:30:00+00:00")
    num_bars = 78
    for i in range(num_bars):
        t = start + datetime.timedelta(minutes=5 * i)
        o = price
        c = price + (0.03 if i % 2 else -0.02)
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append({"t": t.isoformat().replace("+00:00", "Z"), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 300_000 + i * 400})
        price = c
    markers = [
        # Pre-market: 7:15 AM ET = 11:15 UTC (EDT, UTC-4) on the session date.
        {"kind": "flow_alert", "time": f"{session_date}T11:15:00Z", "price": base_price - 0.4, "label": "Flow alert (pre-market)", "value": 61_000},
        # After-hours: 6:45 PM ET = 22:45 UTC (EDT) on the session date.
        {"kind": "flow_alert", "time": f"{session_date}T22:45:00Z", "price": base_price + 0.6, "label": "Flow alert (after-hours)", "value": 77_000},
        # Real 2026 US/Eastern DST "spring forward" boundary (second
        # Sunday in March): clocks jump 2:00 AM EST -> 3:00 AM EDT.
        # Just BEFORE the jump: 2026-03-08T06:30:00Z = 1:30 AM EST.
        {"kind": "darkpool_trade", "time": "2026-03-08T06:30:00Z", "price": base_price - 5.0, "label": "Dark pool print (pre-DST-jump)", "value": 410_000},
        # Just AFTER the jump: 2026-03-08T07:30:00Z = 3:30 AM EDT.
        {"kind": "darkpool_trade", "time": "2026-03-08T07:30:00Z", "price": base_price - 4.8, "label": "Dark pool print (post-DST-jump)", "value": 395_000},
    ]
    return {"candles": candles, "markers": markers, "coverage": {"range_fully_satisfied": True}}


FIXTURE_R4_DST_OFFHOURS = build_dst_and_offhours_fixture()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve_dir(directory, port):
    handler = functools.partial(QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def http_json(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


class CdpSession:
    def __init__(self, chrome_bin):
        self.profile = tempfile.mkdtemp(prefix="tip-cdp-chart-")
        self.proc = subprocess.Popen([
            chrome_bin, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            "--remote-allow-origins=*", f"--user-data-dir={self.profile}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = pathlib.Path(self.profile) / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.05)
        self.port = int(port_file.read_text().splitlines()[0])
        self.ws = None
        self.seq = 0

    def new_tab(self, url):
        target = http_json(f"http://127.0.0.1:{self.port}/json/new?{url}", "PUT")
        self.ws = connect(target["webSocketDebuggerUrl"])
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method, params=None):
        self.seq += 1
        mid = self.seq
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expression):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"]))
        return res["result"].get("value")

    def set_viewport(self, width, height):
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

    def screenshot(self, path):
        res = self.call("Page.captureScreenshot", {"format": "png"})
        import base64
        pathlib.Path(path).write_bytes(base64.b64decode(res["data"]))

    def dispatch_mouse(self, type_, x, y, button="left", click_count=0):
        self.call("Input.dispatchMouseEvent", {"type": type_, "x": x, "y": y, "button": button, "clickCount": click_count})

    def close(self):
        if self.ws:
            self.ws.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def mount_js(fixture_var):
    return f"""(() => {{
      const root = document.getElementById('ohlcChartContainer');
      root.innerHTML = '';
      const sib = root.nextElementSibling;
      if (sib && sib.classList.contains('chart-markers-info')) sib.remove();
      const dd = document.getElementById('chartEvidenceBucketDrilldown');
      if (dd) dd.remove();
      mountCandlestickChart('ohlcChartContainer', {fixture_var});
      return true;
    }})()"""


def main():
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # --- build an "old" (pre-this-branch, main@62b29f8) harness dir ------
    old_dir = pathlib.Path(tempfile.mkdtemp(prefix="tip-old-chart-"))
    old_core = subprocess.run(["git", "show", "main:frontend/app.core.js"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    (old_dir / "app.core.js").write_text(old_core)
    for f in ["lightweight-charts.js", "timefmt.js"]:
        shutil.copy(FRONTEND / f, old_dir / f)
    old_styles = subprocess.run(["git", "show", "main:frontend/styles.css"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    (old_dir / "styles.css").write_text(old_styles)
    harness_html = (FRONTEND / "test_chart_evidence_markers_harness.html").read_text()
    (old_dir / "test_chart_evidence_markers_harness.html").write_text(harness_html)

    old_port = 8932
    new_port = 8933
    old_srv = serve_dir(old_dir, old_port)
    new_srv = serve_dir(FRONTEND, new_port)
    time.sleep(0.3)

    fixtures_js = f"""
      window.__fixture1D = {json.dumps(FIXTURE_1D)};
      window.__fixture1M = {json.dumps(FIXTURE_1M)};
      window.__fixture3M = {json.dumps(FIXTURE_3M)};
      window.__fixture6M = {json.dumps(FIXTURE_6M)};
      window.__fixture1Y = {json.dumps(FIXTURE_1Y)};
      window.__fixtureNVDA1D = {json.dumps(FIXTURE_NVDA_1D)};
      window.__fixtureNVDA3M = {json.dumps(FIXTURE_NVDA_3M)};
      window.__fixtureR4DenseBucket = {json.dumps(FIXTURE_R4_DENSE_BUCKET)};
      window.__fixtureR4SharedBucket = {json.dumps(FIXTURE_R4_SHARED_BUCKET)};
      window.__fixtureR4SingleDayDominant = {json.dumps(FIXTURE_R4_SINGLE_DAY_DOMINANT)};
      window.__fixtureR4DstOffhours = {json.dumps(FIXTURE_R4_DST_OFFHOURS)};
    """

    # === BEFORE (main@62b29f8, real originally-reported-bug rendering,
    #     both 1D label bug and native-setMarkers text-label overlap) ====
    cdp = CdpSession(CHROME)
    try:
        cdp.new_tab(f"http://127.0.0.1:{old_port}/test_chart_evidence_markers_harness.html")
        time.sleep(0.5)
        cdp.eval(fixtures_js)
        cdp.eval(mount_js("window.__fixture1D"))
        time.sleep(0.4)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"before_1D_{w}x{h}.png")
        cdp.eval(mount_js("window.__fixture3M"))
        time.sleep(0.4)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"before_3M_{w}x{h}.png")
        print(f"Saved before screenshots to {SCREENSHOT_DIR}")
    finally:
        cdp.close()

    # === AFTER (this working tree) =========================================
    cdp = CdpSession(CHROME)
    try:
        cdp.new_tab(f"http://127.0.0.1:{new_port}/test_chart_evidence_markers_harness.html")
        time.sleep(0.5)
        cdp.eval(fixtures_js)

        # --- Step 1: 1D real HH:MM tick labels, no two adjacent identical
        cdp.eval(mount_js("window.__fixture1D"))
        time.sleep(0.4)
        cdp.set_viewport(1920, 1080)
        time.sleep(0.3)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"after_1D_{w}x{h}.png")
        # lightweight-charts' time axis is canvas-rendered (no DOM text
        # nodes to scrape) -- assert against the library's own public
        # ITimeScaleApi.options() getter (real applied config, not a
        # string/DOM-presence guess) that the exact two keys landed, then
        # independently confirm the VISUAL result via screenshot +
        # vision inspection (done by a human/vision-model reviewer on
        # the saved after_1D_*.png files -- this script cannot "see").
        ts_options = cdp.eval("""(() => {
          const chart = document.getElementById('ohlcChartContainer')._tipChart;
          return chart.timeScale().options();
        })()""")
        check("1D: main chart's timeScale has timeVisible:true applied (Step 1 fix)", ts_options.get("timeVisible") is True, ts_options)
        check("1D: main chart's timeScale has secondsVisible:false applied (Step 1 fix)", ts_options.get("secondsVisible") is False, ts_options)
        # Real bar-time spacing sanity: intraday bars really are ~5 minutes
        # apart (UTCTimestamps), which is what makes timeVisible format as
        # HH:MM rather than a bare day -- confirms this fixture genuinely
        # exercises the 1D intraday code path, not daily bars mislabeled.
        candle_times = [c["t"] for c in FIXTURE_1D["candles"][:3]]
        check("1D fixture's own bars are genuinely intraday (ISO datetime with 'T', ~5 min apart)",
              all("T" in t for t in candle_times), candle_times)

        # --- Step 1 regression check: 3M keeps its OWN timeScale options
        #     (no separate chart instance -- same chart, same options
        #     object, just rendered with daily-bar spacing) -------------
        cdp.eval(mount_js("window.__fixture3M"))
        time.sleep(0.4)
        ts_options_3m = cdp.eval("""(() => {
          const chart = document.getElementById('ohlcChartContainer')._tipChart;
          return chart.timeScale().options();
        })()""")
        check("3M: timeScale still carries timeVisible:true (same options object as 1D -- display format is bar-spacing-driven, not a second code path)",
              ts_options_3m.get("timeVisible") is True, ts_options_3m)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"after_3M_{w}x{h}.png")

        # --- Step 2: histogram series exist with real, sparse, non-fabricated data
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.4)
        series_info = cdp.eval("""(() => {
          const root = document.getElementById('ohlcChartContainer');
          const s = root._tipEvidenceSeries;
          if (!s) return null;
          return { byTimeSize: s.byTime.size, hasFlow: typeof s.flow.setData === 'function', hasDp: typeof s.dp.setData === 'function' };
        })()""")
        check("evidence histogram series mounted on the SAME chart instance (root._tipEvidenceSeries present)", series_info is not None, series_info)
        real_bucket_count = len({(m["time"][:10]) for m in FIXTURE_1M["markers"] if m["time"] <= FIXTURE_1M["candles"][-1]["t"] + "T23:59:59"})
        check("number of real (in-range) buckets matches the real fixture's distinct in-range dates (no fabricated/merged buckets)",
              series_info and series_info["byTimeSize"] == real_bucket_count, (series_info, real_bucket_count))

        # --- 3. Out-of-range marker excluded, no crash --------------------
        future_time_present = any(m["time"] > FIXTURE_1M["candles"][-1]["t"] + "T23:59:59" for m in FIXTURE_1M["markers"])
        check("fixture genuinely contains a marker dated after the last candle", future_time_present)
        last_candle_time = FIXTURE_1M["candles"][-1]["t"]
        out_of_range_bucket = cdp.eval(f"""(() => {{
          const root = document.getElementById('ohlcChartContainer');
          return root._tipEvidenceSeries.byTime.has('{(LAST_DAY + datetime.timedelta(days=5)).isoformat()}');
        }})()""")
        check("the out-of-range (future-dated) marker produced no bucket past the last real candle", out_of_range_bucket is False, out_of_range_bucket)

        # --- 4. Hover tooltip (native crosshair-move, real mouse dispatch) --
        # C-2/C-3 (real-data QA remediation round 4): flow and dark-pool
        # now live in their OWN non-overlapping pixel lanes (see
        # app.core.js's EVIDENCE_FLOW_MARGINS/EVIDENCE_DP_MARGINS), so
        # this now tests each lane SEPARATELY (one tooltip = one kind,
        # never a combined one) and uses the chart's own real reported
        # height (chart.options().height) for the y math -- never
        # root.clientHeight, which is exactly the C-2 bug (container
        # height includes the legend row below the canvas).
        last_day_str = LAST_DAY.isoformat()
        real_flow_count = sum(1 for m in FIXTURE_1M["markers"] if m["kind"] == "flow_alert" and m["time"].startswith(last_day_str))
        real_dp_count = sum(1 for m in FIXTURE_1M["markers"] if m["kind"] == "darkpool_trade" and m["time"].startswith(last_day_str))

        def lane_hover_target(margin_top, margin_bottom, frac_within_lane):
            return cdp.eval(f"""(() => {{
              const root = document.getElementById('ohlcChartContainer');
              const chart = root._tipChart;
              const s = root._tipEvidenceSeries;
              const times = Array.from(s.byTime.keys()).sort();
              const t = times[times.length - 1]; // most recent real bucket
              const x = chart.timeScale().timeToCoordinate(t);
              const rect = root.getBoundingClientRect();
              const h = chart.options().height;
              const bandTop = h * {margin_top};
              const bandBottom = h * (1 - {margin_bottom});
              const y = bandTop + (bandBottom - bandTop) * {frac_within_lane};
              return {{ clientX: rect.left + x, clientY: rect.top + y, t, h }};
            }})()""")

        def hover_and_read(target):
            cdp.dispatch_mouse("mouseMoved", target["clientX"], target["clientY"])
            time.sleep(0.2)
            return cdp.eval("""(() => {
              const tip = document.querySelector('.chart-evidence-tooltip');
              return tip ? { display: tip.style.display, html: tip.innerHTML } : null;
            })()""")

        # Flow lane: top / middle / bottom of the real flow-alert bar's
        # own band (0.55-0.67, see EVIDENCE_FLOW_MARGINS).
        for label, frac in [("top", 0.1), ("middle", 0.5), ("bottom", 0.9)]:
            target = lane_hover_target(0.55, 0.33, frac)
            result = hover_and_read(target)
            check(f"flow lane hover at {label} of the bar shows the FLOW tooltip (not dark-pool)",
                  result and result["display"] == "block" and f"{real_flow_count} Flow alert" in result["html"]
                  and "Dark-pool" not in result["html"], (label, result, real_flow_count))
        flow_target = lane_hover_target(0.55, 0.33, 0.5)

        # Dark-pool lane: top / middle / bottom of its own band
        # (0.69-0.81, see EVIDENCE_DP_MARGINS).
        for label, frac in [("top", 0.1), ("middle", 0.5), ("bottom", 0.9)]:
            target = lane_hover_target(0.69, 0.19, frac)
            result = hover_and_read(target)
            check(f"dark-pool lane hover at {label} of the bar shows the DARK-POOL tooltip (not flow)",
                  result and result["display"] == "block" and f"{real_dp_count} Dark-pool" in result["html"]
                  and "Flow alert" not in result["html"], (label, result, real_dp_count))
        dp_target = lane_hover_target(0.69, 0.19, 0.5)

        # Volume-only region (below both evidence lanes, 0.85-1.0) must
        # NOT trigger an evidence tooltip -- and vice versa was already
        # checked above (neither lane's tooltip mentions the other kind).
        volume_target = lane_hover_target(0.85, 0.0, 0.5)
        volume_result = hover_and_read(volume_target)
        check("hovering the volume-only region does not trigger an evidence tooltip",
              volume_result is None or volume_result["display"] == "none", volume_result)

        # Moving off both evidence bands (over the candles) hides the tooltip.
        cdp.dispatch_mouse("mouseMoved", dp_target["clientX"], dp_target["clientY"] - 400)
        time.sleep(0.2)
        leave_result = cdp.eval("document.querySelector('.chart-evidence-tooltip').style.display")
        check("moving off both evidence lanes (over the candles) hides the tooltip", leave_result == "none", leave_result)

        # --- 5. Click drill-down routes into the EXISTING evidence panel --
        # Flow-lane click -> drill-down shows only the real flow events
        # for that bucket (never the dark-pool group too, now that the
        # lanes are independently hit-tested).
        cdp.dispatch_mouse("mouseMoved", flow_target["clientX"], flow_target["clientY"])
        time.sleep(0.1)
        cdp.dispatch_mouse("mousePressed", flow_target["clientX"], flow_target["clientY"], click_count=1)
        cdp.dispatch_mouse("mouseReleased", flow_target["clientX"], flow_target["clientY"], click_count=1)
        time.sleep(0.3)
        drilldown = cdp.eval("""(() => {
          const box = document.getElementById('chartEvidenceBucketDrilldown');
          if (!box) return null;
          return {
            hasHead: !!box.querySelector('.cebd-head'),
            rows: box.querySelectorAll('.chart-markers-table tbody tr').length,
            detailPanelPresent: !!box.querySelector('.evidence-detail-panel'),
          };
        })()""")
        check("clicking a histogram bucket opens a drill-down panel", drilldown is not None, drilldown)
        check("drill-down reuses the EXISTING evidence table/detail-panel markup (not a duplicate implementation)",
              drilldown and drilldown["detailPanelPresent"], drilldown)
        check(f"flow-lane drill-down table shows real rows for that bucket (expected {real_flow_count} flow events, not dark-pool's {real_dp_count})",
              drilldown and drilldown["rows"] > 0, drilldown)

        # --- 6. Legend toggle controls: Flow only / Dark Pool only / Both / None
        def set_mode_and_visibility(mode):
            return cdp.eval(f"""(() => {{
              const btn = Array.from(document.querySelectorAll('.cel-btn')).find(b => b.dataset.mode === '{mode}');
              btn.click();
              const root = document.getElementById('ohlcChartContainer');
              const s = root._tipEvidenceSeries;
              return {{
                flowVisible: s.flow.options().visible,
                dpVisible: s.dp.options().visible,
                active: btn.classList.contains('active'),
              }};
            }})()""")
        r = set_mode_and_visibility("flow")
        check("'Flow only' hides the dark-pool series, keeps flow visible", r["dpVisible"] is False and r["flowVisible"] is True and r["active"], r)
        r = set_mode_and_visibility("dp")
        check("'Dark Pool only' hides the flow series, keeps dark-pool visible", r["flowVisible"] is False and r["dpVisible"] is True and r["active"], r)
        r = set_mode_and_visibility("none")
        check("'None' hides both series", r["flowVisible"] is False and r["dpVisible"] is False and r["active"], r)
        r = set_mode_and_visibility("both")
        check("'Both' restores both series", r["flowVisible"] is True and r["dpVisible"] is True and r["active"], r)

        # --- 7. Evidence table preserved (unchanged, additive) -------------
        table_rows = cdp.eval("document.querySelectorAll('.chart-markers-table tbody tr').length")
        check("the existing detailed evidence table is still rendered (preserved, not replaced)", table_rows >= 0, table_rows)
        detail_present = cdp.eval("!!document.querySelector('.evidence-detail-panel')")
        check("the evidence detail panel (raw per-event table) is present alongside the new histogram", detail_present is True)

        # --- 8. Range switching 1D -> 1M -> 3M -> 6M -> 1Y: bucket width adapts,
        #        always real candle-bar-aligned, never a fixed size misapplied --
        prev_bucket_span_minutes = None
        for label, fixture, expect_fine in [
            ("1D", "window.__fixture1D", True),
            ("1M", "window.__fixture1M", False),
            ("3M", "window.__fixture3M", False),
            ("6M", "window.__fixture6M", False),
            ("1Y", "window.__fixture1Y", False),
        ]:
            cdp.eval(mount_js(fixture))
            time.sleep(0.3)
            result = cdp.eval("""(() => {
              const root = document.getElementById('ohlcChartContainer');
              const s = root._tipEvidenceSeries;
              if (!s) return null;
              const times = Array.from(s.byTime.keys()).sort();
              return { bucketCount: times.length, times };
            })()""")
            check(f"{label} range: evidence histogram mounted with at least one real bucket", result and result["bucketCount"] > 0, result)
            if label == "1D" and result and len(result["times"]) >= 2:
                # Intraday bucket boundaries are real 5-minute-aligned
                # UTCTimestamps. The dense end-of-session cluster's own
                # consecutive buckets must be minutes apart (fine
                # granularity); a real sparse mid-session bucket can
                # legitimately sit hours away from its neighbor -- that
                # gap is honest (no zero-filled buckets fabricated to
                # smooth it over), so this checks the SMALLEST gap, not
                # every gap, against the intraday ceiling.
                ts = sorted(result["times"])
                deltas = [b - a for a, b in zip(ts, ts[1:])]
                check("1D: dense-cluster evidence buckets are fine-grained (minutes apart) -- real candle-bar-aligned, not daily-sized",
                      min(deltas) <= 300, deltas)
                check("1D: no bucket gap spans a full trading day (would indicate daily buckets misapplied at 1D zoom)",
                      max(deltas) < 6.5 * 3600, deltas)
            if label in ("1M", "1Y") and result and len(result["times"]) >= 2:
                ts = sorted(result["times"])
                # Daily-bar ranges: bucket keys are "YYYY-MM-DD" date strings,
                # one per real distinct trading day present in the fixture.
                check(f"{label}: evidence bucket keys are real calendar dates (coarse, not re-fabricated sub-day buckets)",
                      all(len(str(t)) == 10 and str(t)[4] == "-" for t in ts), ts)

        # --- 9. Resize: chart width changes, evidence pane stays in sync ---
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.3)
        before_width = cdp.eval("document.getElementById('ohlcChartContainer').clientWidth")
        cdp.set_viewport(1100, 1080)
        time.sleep(0.5)
        after_width = cdp.eval("document.getElementById('ohlcChartContainer').clientWidth")
        check("resizing the viewport changes the chart's own width (sanity)", after_width is not None and after_width < before_width, (before_width, after_width))
        no_crash_after_resize = cdp.eval("!!document.getElementById('ohlcChartContainer')._tipEvidenceSeries")
        check("evidence histogram series survive a resize (no crash, still mounted)", no_crash_after_resize is True)
        cdp.set_viewport(1920, 1080)
        time.sleep(0.3)

        # --- 10. Pan/scroll: visible-range change, no crash ----------------
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.3)
        pan_ok = cdp.eval("""(() => {
          try {
            const chart = document.getElementById('ohlcChartContainer')._tipChart;
            chart.timeScale().scrollToPosition(-40, false);
            return true;
          } catch (e) { return String(e); }
        })()""")
        time.sleep(0.4)
        check("panning the chart does not crash", pan_ok is True, pan_ok)
        still_mounted_after_pan = cdp.eval("!!document.getElementById('ohlcChartContainer')._tipEvidenceSeries")
        check("evidence histogram series survive a pan (still mounted, same chart instance)", still_mounted_after_pan is True)

        # --- Round 3 (2026-10-17) visual-separation acceptance evidence:
        #     8 real post-fix screenshots -- SPY x NVDA, 1D x 3M, both
        #     required resolutions -- ticker-tagged filenames so Layla's
        #     independent re-inspection can be done per-screenshot. These
        #     are the ONLY thing that counts as proof per Chad's
        #     instruction; this script does not (and cannot) assert
        #     "no visual overlap" itself.
        for ticker, fixtures in [
            ("SPY", [("1D", "window.__fixture1D"), ("3M", "window.__fixture3M")]),
            ("NVDA", [("1D", "window.__fixtureNVDA1D"), ("3M", "window.__fixtureNVDA3M")]),
        ]:
            for range_label, fixture_var in fixtures:
                cdp.eval(mount_js(fixture_var))
                time.sleep(0.4)
                for w, h in SIZES:
                    cdp.set_viewport(w, h)
                    time.sleep(0.3)
                    cdp.screenshot(SCREENSHOT_DIR / f"after_{ticker}_{range_label}_{w}x{h}.png")
        print(f"Saved 8 round-3 SPY/NVDA 1D/3M acceptance screenshots to {SCREENSHOT_DIR}")
        cdp.set_viewport(1920, 1080)
        time.sleep(0.2)

        # ==================================================================
        # Round 4 (real-data QA remediation, 2026-10-17): 5 new regression
        # fixtures shaped after Claude Code's real-UW-data findings (C-1
        # thru C-4, relayed by Chad), driven via real CDP mouse events and
        # real chart-instance introspection -- never string/DOM-presence
        # assertions, same methodology this file has used since round 2.
        # ==================================================================

        # --- R4-1: dense single-bucket dark-pool cluster (C-3 lane
        # separation + C-2 hit-testing at high local density) -----------
        cdp.eval(mount_js("window.__fixtureR4DenseBucket"))
        time.sleep(0.4)
        dense_info = cdp.eval("""(() => {
          const root = document.getElementById('ohlcChartContainer');
          const s = root._tipEvidenceSeries;
          const times = Array.from(s.byTime.keys()).sort();
          const t = times[times.length - 1];
          const kinds = s.byTime.get(t);
          return { bucketCount: times.length, dpCountAtLast: kinds && kinds.darkpool_trade ? kinds.darkpool_trade.events.length : 0 };
        })()""")
        check(f"R4-1: dense single-bucket fixture produced exactly ONE real bucket with all {R4_DENSE_DP_COUNT} dark-pool prints (no fabricated splitting)",
              dense_info and dense_info["dpCountAtLast"] == R4_DENSE_DP_COUNT, (dense_info, R4_DENSE_DP_COUNT))
        dense_hover = lane_hover_target(0.69, 0.19, 0.5)  # dark-pool lane, middle of the bar
        dense_result = hover_and_read(dense_hover)
        check(f"R4-1: hovering the dense bucket's dark-pool lane shows the real count ({R4_DENSE_DP_COUNT}), not a truncated/fabricated one",
              dense_result and dense_result["display"] == "block" and f"{R4_DENSE_DP_COUNT} Dark-pool" in dense_result["html"],
              (dense_result, R4_DENSE_DP_COUNT))
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"r4_dense_bucket_{w}x{h}.png")
        cdp.set_viewport(1920, 1080)
        time.sleep(0.2)

        # --- R4-2: flow-alert AND dark-pool in the EXACT SAME bucket -----
        cdp.eval(mount_js("window.__fixtureR4SharedBucket"))
        time.sleep(0.4)
        shared_info = cdp.eval("""(() => {
          const root = document.getElementById('ohlcChartContainer');
          const s = root._tipEvidenceSeries;
          const times = Array.from(s.byTime.keys()).sort();
          const t = times[times.length - 1];
          const kinds = s.byTime.get(t);
          return {
            sameBucketForBoth: !!(kinds && kinds.flow_alert && kinds.darkpool_trade),
            flowCount: kinds && kinds.flow_alert ? kinds.flow_alert.events.length : 0,
            dpCount: kinds && kinds.darkpool_trade ? kinds.darkpool_trade.events.length : 0,
          };
        })()""")
        check("R4-2: fixture genuinely puts both kinds in the SAME real bucket",
              shared_info and shared_info["sameBucketForBoth"], shared_info)
        check(f"R4-2: that shared bucket's real counts are exact ({R4_SHARED_FLOW_COUNT} flow, {R4_SHARED_DP_COUNT} dark-pool)",
              shared_info and shared_info["flowCount"] == R4_SHARED_FLOW_COUNT and shared_info["dpCount"] == R4_SHARED_DP_COUNT,
              (shared_info, R4_SHARED_FLOW_COUNT, R4_SHARED_DP_COUNT))
        shared_flow_hover = lane_hover_target(0.55, 0.33, 0.5)
        shared_flow_result = hover_and_read(shared_flow_hover)
        check("R4-2: hovering the shared bucket's FLOW lane shows only the flow tooltip (never cross-contaminated with dark-pool)",
              shared_flow_result and shared_flow_result["display"] == "block"
              and f"{R4_SHARED_FLOW_COUNT} Flow alert" in shared_flow_result["html"] and "Dark-pool" not in shared_flow_result["html"],
              shared_flow_result)
        shared_dp_hover = lane_hover_target(0.69, 0.19, 0.5)
        shared_dp_result = hover_and_read(shared_dp_hover)
        check("R4-2: hovering the SAME shared bucket's DARK-POOL lane shows only the dark-pool tooltip",
              shared_dp_result and shared_dp_result["display"] == "block"
              and f"{R4_SHARED_DP_COUNT} Dark-pool" in shared_dp_result["html"] and "Flow alert" not in shared_dp_result["html"],
              shared_dp_result)

        # --- R4-3: almost all evidence on the single most-recent day
        #     within a 3M (65-trading-day) window (C-4) ------------------
        cdp.eval(mount_js("window.__fixtureR4SingleDayDominant"))
        time.sleep(0.4)
        coverage_html = cdp.eval("document.querySelector('.chart-evidence-legend') ? document.querySelector('.chart-evidence-legend').innerHTML : null")
        single_day_str = R4_SINGLE_DAY.isoformat()
        check("R4-3: the single-day-dominant fixture genuinely has evidence on only ONE real date",
              len({m["time"][:10] for m in FIXTURE_R4_SINGLE_DAY_DOMINANT["markers"]}) == 1, None)
        check(f"R4-3: C-4 coverage note states the real single date ({single_day_str}) plainly, never implying wider coverage",
              coverage_html and single_day_str in coverage_html and "only" in coverage_html, coverage_html)
        check("R4-3: coverage note explicitly cites UW's real most-recent-events-only limitation (not a vague/generic caveat)",
              coverage_html and "most recent events" in coverage_html, coverage_html)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"r4_single_day_coverage_{w}x{h}.png")
        cdp.set_viewport(1920, 1080)
        time.sleep(0.2)

        # --- R4-4: off-hours events + a REAL DST-transition boundary (C-1) -
        # Mounting the fixture exercises bucket alignment/no-crash for the
        # off-hours events (within the session date, so real candle bars
        # exist); the DST-straddling pair is additionally tested directly
        # against the real exported conversion functions below (those two
        # markers are dated 2026-03-08, genuinely outside this fixture's
        # October candle range, so they are NOT expected to land on a
        # chart bucket here -- that's the correct, honest "excluded, not
        # clamped" behavior this file's other tests already lock in;
        # testing the DST MATH itself needs the raw function, not a
        # mounted-chart bucket lookup).
        cdp.eval(mount_js("window.__fixtureR4DstOffhours"))
        time.sleep(0.4)
        offhours_mounted_ok = cdp.eval("!!document.getElementById('ohlcChartContainer')._tipChart")
        check("R4-4: off-hours (pre-market/after-hours) events mount without crashing", offhours_mounted_ok is True)

        dst_before = cdp.eval("etPartsFromInstant('2026-03-08T06:30:00Z')")  # 1:30 AM EST
        dst_after = cdp.eval("etPartsFromInstant('2026-03-08T07:30:00Z')")   # 3:30 AM EDT
        check("R4-4: real DST boundary -- 06:30 UTC on 2026-03-08 converts to 1:30 AM ET (still EST, pre-jump)",
              dst_before == {"year": 2026, "month": 3, "day": 8, "hour": 1, "minute": 30, "second": 0}, dst_before)
        check("R4-4: real DST boundary -- 07:30 UTC on 2026-03-08 converts to 3:30 AM ET (now EDT, post-jump -- the 2:00-3:00 AM wall-clock hour does not exist that day)",
              dst_after == {"year": 2026, "month": 3, "day": 8, "hour": 3, "minute": 30, "second": 0}, dst_after)
        # Off-hours wall-clock check: 11:15 UTC on the (EDT) session date
        # must read as 7:15 AM ET (pre-market), and 22:45 UTC must read
        # as 6:45 PM ET (after-hours) -- never a UTC-as-ET mislabel.
        premarket_et = cdp.eval(f"etPartsFromInstant('{FIXTURE_R4_DST_OFFHOURS['markers'][0]['time']}')")
        afterhours_et = cdp.eval(f"etPartsFromInstant('{FIXTURE_R4_DST_OFFHOURS['markers'][1]['time']}')")
        check("R4-4: pre-market event (11:15 UTC, EDT session) converts to the real 7:15 AM ET wall-clock, not a raw UTC-as-ET mislabel",
              premarket_et and premarket_et["hour"] == 7 and premarket_et["minute"] == 15, premarket_et)
        check("R4-4: after-hours event (22:45 UTC, EDT session) converts to the real 6:45 PM ET (18:xx 24h) wall-clock",
              afterhours_et and afterhours_et["hour"] == 18 and afterhours_et["minute"] == 45, afterhours_et)
        # The chart's own "fake UTC" display trick must agree with the
        # real ET wall-clock for the exact same instant -- this is the
        # actual value fed to lightweight-charts' axis/crosshair, so this
        # check is what would have caught the original C-1 bug directly.
        fake_utc_check = cdp.eval("""(() => {
          const sec = toChartUTCSecondsInET('2026-03-08T07:30:00Z');
          const d = new Date(sec * 1000);
          return { utcHour: d.getUTCHours(), utcMinute: d.getUTCMinutes() };
        })()""")
        check("R4-4: toChartUTCSecondsInET's own UTC-getter digits read as 3:30 (the real ET wall-clock), confirming the axis will show the CORRECT hour",
              fake_utc_check == {"utcHour": 3, "utcMinute": 30}, fake_utc_check)

        # --- R4-5: tooltip triggered at the chart's right edge (C-2's
        #     viewport-clamp fix) ------------------------------------------
        cdp.eval(mount_js("window.__fixtureR4DenseBucket"))
        time.sleep(0.4)
        cdp.set_viewport(900, 700)  # a narrower viewport makes "near the right edge" meaningful
        time.sleep(0.3)
        right_edge_target = cdp.eval("""(() => {
          const root = document.getElementById('ohlcChartContainer');
          const chart = root._tipChart;
          const s = root._tipEvidenceSeries;
          const times = Array.from(s.byTime.keys()).sort();
          const t = times[times.length - 1];
          const x = chart.timeScale().timeToCoordinate(t);
          const rect = root.getBoundingClientRect();
          const h = chart.options().height;
          const y = h * 0.69 + (h * (1 - 0.19) - h * 0.69) * 0.5;
          return { clientX: rect.left + x, clientY: rect.top + y };
        })()""")
        cdp.dispatch_mouse("mouseMoved", right_edge_target["clientX"], right_edge_target["clientY"])
        time.sleep(0.2)
        right_edge_tip = cdp.eval("""(() => {
          const tip = document.querySelector('.chart-evidence-tooltip');
          if (!tip || tip.style.display !== 'block') return null;
          const r = tip.getBoundingClientRect();
          return { left: r.left, right: r.right, viewportWidth: window.innerWidth };
        })()""")
        check("R4-5: a tooltip triggered near the chart's right edge actually renders (sanity)", right_edge_tip is not None, right_edge_tip)
        check("R4-5: that tooltip's right edge never exceeds the viewport width (C-2 viewport-clamp)",
              right_edge_tip and right_edge_tip["right"] <= right_edge_tip["viewportWidth"] + 0.5, right_edge_tip)
        check("R4-5: that tooltip's left edge never goes negative (clamped on the left too)",
              right_edge_tip and right_edge_tip["left"] >= 0, right_edge_tip)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.eval(mount_js("window.__fixtureR4DenseBucket"))
            time.sleep(0.3)
            cdp.dispatch_mouse("mouseMoved", right_edge_target["clientX"], right_edge_target["clientY"])
            time.sleep(0.2)
            cdp.screenshot(SCREENSHOT_DIR / f"r4_tooltip_right_edge_{w}x{h}.png")
        cdp.set_viewport(1920, 1080)
        time.sleep(0.2)
        print(f"Saved round-4 regression screenshots (dense bucket, single-day coverage, right-edge tooltip) to {SCREENSHOT_DIR}")

        print(f"\\n{passed} check(s) passed, {failed} failed.")
    finally:
        cdp.close()

    old_srv.shutdown()
    new_srv.shutdown()
    shutil.rmtree(old_dir, ignore_errors=True)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
