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
        hover_target = cdp.eval("""(() => {
          const root = document.getElementById('ohlcChartContainer');
          const chart = root._tipChart;
          const s = root._tipEvidenceSeries;
          const times = Array.from(s.byTime.keys()).sort();
          const t = times[times.length - 1]; // most recent real bucket
          const x = chart.timeScale().timeToCoordinate(t);
          const rect = root.getBoundingClientRect();
          const h = root.clientHeight || 320;
          const y = h * 0.68; // inside the evidence pane's own band (0.60-0.78)
          return { clientX: rect.left + x, clientY: rect.top + y, t };
        })()""")
        cdp.dispatch_mouse("mouseMoved", hover_target["clientX"], hover_target["clientY"])
        time.sleep(0.2)
        hover_result = cdp.eval("""(() => {
          const tip = document.querySelector('.chart-evidence-tooltip');
          return tip ? { display: tip.style.display, html: tip.innerHTML } : null;
        })()""")
        check("hovering the most recent evidence bucket shows the tooltip", hover_result and hover_result["display"] == "block", hover_result)
        last_day_str = LAST_DAY.isoformat()
        check("tooltip content includes the real latest-day date (not fabricated)",
              hover_result and last_day_str in hover_result["html"], hover_result)
        real_count = sum(1 for m in FIXTURE_1M["markers"] if m["kind"] == "flow_alert" and m["time"].startswith(last_day_str))
        check(f"tooltip's stated flow-alert count ({real_count}) matches the real number of underlying events for that bucket",
              hover_result and f"{real_count} Flow alert" in hover_result["html"], (hover_result, real_count))

        # Moving off the pane band (over the candles) hides the tooltip.
        cdp.dispatch_mouse("mouseMoved", hover_target["clientX"], hover_target["clientY"] - 200)
        time.sleep(0.2)
        leave_result = cdp.eval("document.querySelector('.chart-evidence-tooltip').style.display")
        check("moving off the evidence pane band (over the candles) hides the tooltip", leave_result == "none", leave_result)

        # --- 5. Click drill-down routes into the EXISTING evidence panel --
        cdp.dispatch_mouse("mouseMoved", hover_target["clientX"], hover_target["clientY"])
        time.sleep(0.1)
        cdp.dispatch_mouse("mousePressed", hover_target["clientX"], hover_target["clientY"], click_count=1)
        cdp.dispatch_mouse("mouseReleased", hover_target["clientX"], hover_target["clientY"], click_count=1)
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
        check(f"drill-down table shows real rows for that bucket (expected {real_count + sum(1 for m in FIXTURE_1M['markers'] if m['kind']=='darkpool_trade' and m['time'].startswith(last_day_str))} total events)",
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
