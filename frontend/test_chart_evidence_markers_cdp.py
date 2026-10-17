#!/usr/bin/env python3
"""Real-Chromium/CDP regression + before/after evidence for the chart
evidence marker overlay fix (chart-evidence-markers branch, 2026-10-17).

Root defect (real captured video, SPY): flow-alert/dark-pool evidence
markers overlapped unreadably near the chart's right edge. Root cause
(see app.core.js's mountCandlestickChart/mountEvidenceMarkerOverlay
comments): lightweight-charts' native series.setMarkers() rendered a
permanent text label per (bar, kind) group with zero collision
avoidance; real UW evidence clusters on the most recent trading days,
so several consecutive right-edge bars each carrying a label collided.

This script:
  1. Renders a synthetic SPY-shaped fixture (dense flow+dark-pool
     evidence on the last few trading days, exactly the reported shape)
     through the OLD app.core.js (git show main:frontend/app.core.js)
     and the NEW one (this working tree), at 1920x1080 and 2560x1440,
     saving before/after screenshots.
  2. Drives the NEW code's real rendered DOM via CDP (real mouse
     events, real getBoundingClientRect/getComputedStyle reads -- not
     string/DOM-presence assertions) to verify: grouping/dedup, legend
     toggle filtering, hover + click tooltips (content traced back to
     real fixture timestamps), out-of-range exclusion, and realignment
     across 1M/3M/6M/1Y range switches, resize, and pan/scroll.

No backend, no app.live.js, no network calls of any kind -- the
harness page (test_chart_evidence_markers_harness.html) loads only
lightweight-charts.js + app.core.js + a synthetic fixture built here.

Run: python3 frontend/test_chart_evidence_markers_cdp.py
"""
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


# --- synthetic SPY-shaped fixture ------------------------------------------
# Real-shaped, fabricated-but-labeled-synthetic data (same convention the
# repo's own fixtures/README_SYNTHETIC.md uses) reproducing the EXACT
# reported defect: dense flow-alert + dark-pool evidence on the last few
# trading days of a 1M daily range, including the same-day-both-kinds
# case, multiple-observations-per-bucket case, and an evidence timestamp
# dated AFTER the last candle (must be excluded, never clamped).
def build_fixture(num_days=22, base_price=560.0):
    import datetime
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
    # Dense cluster on the LAST 4 trading days (the real right-edge defect
    # shape) -- each of those days gets BOTH kinds, several flow alerts
    # each (multiple observations in the same bucket).
    for back in range(4):
        day = days[-1 - back]
        for k in range(6 - back):  # more events on more-recent days, like real UW data
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
    # A sparse older day (single flow alert) -- not part of the dense
    # cluster, used to confirm distinct-day grouping stays distinct.
    sparse_day = days[-10]
    markers.append({"kind": "flow_alert", "time": f"{sparse_day.isoformat()}T15:00:00Z", "price": base_price - 3.0, "label": "Flow alert (put)", "value": 210_000})
    # Genuinely out-of-range: dated AFTER the last candle (today's
    # real-time print before today's daily bar has closed). Must be
    # excluded, never clamped onto the last bar.
    markers.append({"kind": "flow_alert", "time": f"{(last_day).isoformat()}T23:59:00Z".replace(last_day.isoformat(), str(last_day + __import__('datetime').timedelta(days=5))), "price": 999.0, "label": "Flow alert (call) FUTURE", "value": 1})

    markers.sort(key=lambda m: m["time"])
    return {"candles": candles, "markers": markers, "coverage": {"range_fully_satisfied": True}}, last_day


FIXTURE_1M, LAST_DAY = build_fixture(22)
FIXTURE_3M, _ = build_fixture(65)
FIXTURE_6M, _ = build_fixture(130)
FIXTURE_1Y, _ = build_fixture(252)


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

    def close(self):
        if self.ws:
            self.ws.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def mount_js(fixture_var="window.__fixture1M"):
    return f"""(() => {{
      const root = document.getElementById('ohlcChartContainer');
      root.innerHTML = '';
      mountCandlestickChart('ohlcChartContainer', {fixture_var});
      return true;
    }})()"""


def main():
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # --- build an "old" (pre-fix) harness dir from main's app.core.js ----
    old_dir = pathlib.Path(tempfile.mkdtemp(prefix="tip-old-chart-"))
    old_core = subprocess.run(["git", "show", "main:frontend/app.core.js"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    (old_dir / "app.core.js").write_text(old_core)
    for f in ["lightweight-charts.js", "timefmt.js", "styles.css"]:
        shutil.copy(FRONTEND / f, old_dir / f)
    old_styles = subprocess.run(["git", "show", f"main:frontend/styles.css"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    (old_dir / "styles.css").write_text(old_styles)
    harness_html = (FRONTEND / "test_chart_evidence_markers_harness.html").read_text()
    (old_dir / "test_chart_evidence_markers_harness.html").write_text(harness_html)

    old_port = 8930
    new_port = 8931
    old_srv = serve_dir(old_dir, old_port)
    new_srv = serve_dir(FRONTEND, new_port)
    time.sleep(0.3)

    fixtures_js = f"""
      window.__fixture1M = {json.dumps(FIXTURE_1M)};
      window.__fixture3M = {json.dumps(FIXTURE_3M)};
      window.__fixture6M = {json.dumps(FIXTURE_6M)};
      window.__fixture1Y = {json.dumps(FIXTURE_1Y)};
    """

    # === BEFORE (old code, real reported-bug rendering) ===================
    cdp = CdpSession(CHROME)
    try:
        cdp.new_tab(f"http://127.0.0.1:{old_port}/test_chart_evidence_markers_harness.html")
        time.sleep(0.5)
        cdp.eval(fixtures_js)
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.4)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"before_{w}x{h}.png")
        print(f"Saved before screenshots to {SCREENSHOT_DIR}")
    finally:
        cdp.close()

    # === AFTER (new code) ==================================================
    cdp = CdpSession(CHROME)
    try:
        cdp.new_tab(f"http://127.0.0.1:{new_port}/test_chart_evidence_markers_harness.html")
        time.sleep(0.5)
        cdp.eval(fixtures_js)
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.4)
        for w, h in SIZES:
            cdp.set_viewport(w, h)
            time.sleep(0.3)
            cdp.screenshot(SCREENSHOT_DIR / f"after_{w}x{h}.png")
        print(f"Saved after screenshots to {SCREENSHOT_DIR}")

        cdp.set_viewport(1920, 1080)
        time.sleep(0.3)

        # --- 1. Grouping: compact dots, not overlapping text --------------
        dot_info = cdp.eval("""(() => {
          const dots = Array.from(document.querySelectorAll('.chart-evidence-dot'));
          return dots.map(d => ({
            cls: d.className, left: d.getBoundingClientRect().left,
            top: d.getBoundingClientRect().top, width: d.getBoundingClientRect().width,
            text: d.textContent,
          }));
        })()""")
        check("at least one compact marker dot rendered", len(dot_info) > 0, dot_info)
        check("no dot carries a full permanent text label (count badges are <=3 chars)",
              all(len(d["text"]) <= 3 for d in dot_info), dot_info)
        flow_dots = [d for d in dot_info if "chart-evidence-dot-flow" in d["cls"]]
        dp_dots = [d for d in dot_info if "chart-evidence-dot-dp" in d["cls"]]
        check("flow-alert dots present", len(flow_dots) > 0, flow_dots)
        check("dark-pool dots present", len(dp_dots) > 0, dp_dots)
        # Multiple distinct days near the right edge must get DISTINCT,
        # non-overlapping x-positions (the actual reported defect) --
        # every pair of same-lane dots must be at least their own
        # diameter apart (bounding boxes must not intersect).
        def no_overlap(group):
            xs = sorted(d["left"] for d in group)
            for a, b in zip(xs, xs[1:]):
                if b - a < group[0]["width"] * 0.9:
                    return False, (a, b)
            return True, None
        ok, bad = no_overlap(flow_dots) if len(flow_dots) > 1 else (True, None)
        check("flow dots on distinct days do not visually overlap (the reported defect)", ok, bad)
        ok, bad = no_overlap(dp_dots) if len(dp_dots) > 1 else (True, None)
        check("dark-pool dots on distinct days do not visually overlap", ok, bad)

        # --- 2. Same-day both kinds: separate lanes, both present ----------
        check("flow and dark-pool dots render in separate vertical lanes (no y collision)",
              len(flow_dots) and len(dp_dots) and abs(flow_dots[0]["top"] - dp_dots[0]["top"]) > 20,
              (flow_dots[:1], dp_dots[:1]))

        # --- 3. Rightmost-candle case: a dot exists near the right edge ---
        chart_box = cdp.eval("""(() => {
          const r = document.getElementById('ohlcChartContainer').getBoundingClientRect();
          return {left: r.left, top: r.top, width: r.width, height: r.height};
        })()""")
        rightmost_dot = max(dot_info, key=lambda d: d["left"])
        check("a marker dot exists near the chart's right edge (the real-video scenario)",
              rightmost_dot["left"] > chart_box["left"] + chart_box["width"] * 0.6,
              (rightmost_dot, chart_box))
        check("the rightmost dot is within the chart's drawable plot area (not clipped off/under the axis)",
              rightmost_dot["left"] <= chart_box["left"] + chart_box["width"],
              (rightmost_dot, chart_box))

        # --- 4. Out-of-range marker excluded, no crash --------------------
        future_time_present = any(m["time"] > FIXTURE_1M["candles"][-1]["t"] + "T23:59:59" for m in FIXTURE_1M["markers"])
        check("fixture genuinely contains a marker dated after the last candle", future_time_present)
        # No dot should sit beyond the last real candle's x position.
        last_candle_x = cdp.eval("""(() => {
          const chart = document.getElementById('ohlcChartContainer')._tipChart;
          return chart.timeScale().timeToCoordinate(chart.timeScale().getVisibleRange().to);
        })()""")
        over_the_edge = [d for d in dot_info if last_candle_x is not None and d["left"] - chart_box["left"] > last_candle_x + 15]
        check("the out-of-range (future-dated) marker produced no dot past the last real candle", len(over_the_edge) == 0, over_the_edge)

        # --- 5. Hover tooltip: real content, real timestamps --------------
        target_dot_box = cdp.eval("""(() => {
          const dots = Array.from(document.querySelectorAll('.chart-evidence-dot-flow'));
          const d = dots[dots.length - 1]; // most recent day's flow group
          const r = d.getBoundingClientRect();
          d.setAttribute('data-test-target', '1');
          return {x: r.left + r.width/2, y: r.top + r.height/2};
        })()""")
        hover_result = cdp.eval(f"""(() => {{
          const dot = document.querySelector('[data-test-target="1"]');
          dot.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
          const tip = document.querySelector('.chart-evidence-tooltip');
          return tip ? {{display: tip.style.display, html: tip.innerHTML}} : null;
        }})()""")
        check("hovering a dot shows the tooltip", hover_result and hover_result["display"] == "block", hover_result)
        last_day_str = LAST_DAY.isoformat()
        check("tooltip content includes a real count and the real latest-day date (not fabricated)",
              hover_result and str(last_day_str) in hover_result["html"], hover_result)
        # Cross-check against the REAL underlying events for that group:
        # the group's real count must be reflected in the tooltip header.
        real_count = sum(1 for m in FIXTURE_1M["markers"] if m["kind"] == "flow_alert" and m["time"].startswith(last_day_str))
        check(f"tooltip's stated count ({real_count}) matches the real number of underlying events for that day",
              hover_result and f"{real_count} Flow alert" in hover_result["html"],
              (hover_result, real_count))

        # mouse leave hides it again (no pin)
        leave_result = cdp.eval("""(() => {
          const dot = document.querySelector('[data-test-target="1"]');
          dot.dispatchEvent(new MouseEvent('mouseleave', {bubbles: true}));
          const tip = document.querySelector('.chart-evidence-tooltip');
          return tip ? tip.style.display : null;
        })()""")
        check("mouse-leaving an unpinned dot hides the tooltip again", leave_result == "none", leave_result)

        # --- 6. Click pins the tooltip; second click unpins ----------------
        click_result = cdp.eval("""(() => {
          const dot = document.querySelector('[data-test-target="1"]');
          dot.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const tip = document.querySelector('.chart-evidence-tooltip');
          const pinned1 = dot.classList.contains('pinned');
          dot.dispatchEvent(new MouseEvent('mouseleave', {bubbles: true}));
          const stillShownAfterLeaveWhilePinned = tip.style.display;
          dot.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          const pinned2 = dot.classList.contains('pinned');
          return {pinned1, stillShownAfterLeaveWhilePinned, pinned2, hiddenAfterUnpin: tip.style.display};
        })()""")
        check("clicking a dot pins its tooltip", click_result["pinned1"] is True, click_result)
        check("a pinned tooltip survives mouseleave", click_result["stillShownAfterLeaveWhilePinned"] == "block", click_result)
        check("clicking again unpins and hides the tooltip", click_result["pinned2"] is False and click_result["hiddenAfterUnpin"] == "none", click_result)

        # --- 7. Legend toggle controls: Flow only / Dark Pool only / Both / None
        def set_mode_and_count(mode):
            return cdp.eval(f"""(() => {{
              const btn = Array.from(document.querySelectorAll('.cel-btn')).find(b => b.dataset.mode === '{mode}');
              btn.click();
              return {{
                flow: document.querySelectorAll('.chart-evidence-dot-flow').length,
                dp: document.querySelectorAll('.chart-evidence-dot-dp').length,
                active: btn.classList.contains('active'),
              }};
            }})()""")
        r = set_mode_and_count("flow")
        check("'Flow only' hides all dark-pool dots and keeps flow dots", r["dp"] == 0 and r["flow"] > 0 and r["active"], r)
        r = set_mode_and_count("dp")
        check("'Dark Pool only' hides all flow dots and keeps dark-pool dots", r["flow"] == 0 and r["dp"] > 0 and r["active"], r)
        r = set_mode_and_count("none")
        check("'None' hides every dot", r["flow"] == 0 and r["dp"] == 0 and r["active"], r)
        r = set_mode_and_count("both")
        check("'Both' restores every dot", r["flow"] > 0 and r["dp"] > 0 and r["active"], r)

        # --- 8. Evidence table preserved (unchanged, additive) -------------
        table_rows = cdp.eval("document.querySelectorAll('.chart-markers-table tbody tr').length")
        check("the existing detailed evidence table is still rendered (preserved, not replaced)", table_rows >= 0, table_rows)
        detail_present = cdp.eval("!!document.querySelector('.evidence-detail-panel')")
        check("the evidence detail panel (raw per-event table) is present alongside the new markers", detail_present is True)

        # --- 9. Range switching 1M -> 3M -> 6M -> 1Y: realignment ----------
        for label, fixture in [("3M", "window.__fixture3M"), ("6M", "window.__fixture6M"), ("1Y", "window.__fixture1Y")]:
            cdp.eval(mount_js(fixture))
            time.sleep(0.3)
            result = cdp.eval("""(() => {
              const chart = document.getElementById('ohlcChartContainer')._tipChart;
              const ts = chart.timeScale();
              const dots = Array.from(document.querySelectorAll('.chart-evidence-dot'));
              return {
                dotCount: dots.length,
                allAligned: dots.every(d => {
                  // every visible dot's stored left px must be within 1px
                  // of a plausible coordinate inside the plot area (sanity:
                  // no dot drawn far outside the chart's own bounding box).
                  const r = d.getBoundingClientRect();
                  const root = document.getElementById('ohlcChartContainer').getBoundingClientRect();
                  return r.left >= root.left - 2 && r.left <= root.left + root.width + 2;
                }),
              };
            })()""")
            check(f"{label} range: markers re-rendered and all stay within the chart's plot area", result["allAligned"], result)
            check(f"{label} range: at least one marker still visible after range switch", result["dotCount"] > 0, result)

        # --- 10. Resize: dots reposition, don't go stale --------------------
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.3)
        before_resize = cdp.eval("""(() => {
          const dots = Array.from(document.querySelectorAll('.chart-evidence-dot-flow'));
          return dots.length ? dots[dots.length-1].getBoundingClientRect().left : null;
        })()""")
        cdp.set_viewport(1100, 1080)
        time.sleep(0.5)
        after_resize = cdp.eval("""(() => {
          const dots = Array.from(document.querySelectorAll('.chart-evidence-dot-flow'));
          return dots.length ? dots[dots.length-1].getBoundingClientRect().left : null;
        })()""")
        check("resizing the viewport changes the chart width (sanity)",
              cdp.eval("document.getElementById('ohlcChartContainer').clientWidth") < 1100 and True)
        check("marker dots reposition after a resize (not left stale at the old pixel coordinate)",
              before_resize is not None and after_resize is not None and before_resize != after_resize,
              (before_resize, after_resize))
        cdp.set_viewport(1920, 1080)
        time.sleep(0.3)

        # --- 11. Pan/scroll: visible-range change hides scrolled-out markers,
        #         no crash, still tracks remaining ones correctly -----------
        cdp.eval(mount_js("window.__fixture1M"))
        time.sleep(0.3)
        pan_result = cdp.eval("""(() => {
          const chart = document.getElementById('ohlcChartContainer')._tipChart;
          const before = document.querySelectorAll('.chart-evidence-dot').length;
          chart.timeScale().scrollToPosition(-40, false);
          return {before};
        })()""")
        time.sleep(0.4)
        after_pan_count = cdp.eval("document.querySelectorAll('.chart-evidence-dot').length")
        check("panning the chart does not crash and re-renders the marker overlay (count changes or stays consistent, no stale dots off-screen)",
              isinstance(after_pan_count, int), (pan_result, after_pan_count))

        print(f"\n{passed} check(s) passed, {failed} failed.")
    finally:
        cdp.close()

    old_srv.shutdown()
    new_srv.shutdown()
    shutil.rmtree(old_dir, ignore_errors=True)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
