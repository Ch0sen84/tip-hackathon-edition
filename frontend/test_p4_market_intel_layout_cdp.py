#!/usr/bin/env python3
"""Real-Chromium regression for Market Intelligence row stretching."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.request
from websockets.sync.client import connect

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHROME = os.environ.get("CHROME_BIN", "/home/hermes/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")
BASE = os.environ.get("TIP_TEST_BASE", "http://127.0.0.1:8899")
SIZES = [(1920, 1080), (2560, 1440), (390, 844)]


def http_json(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def main():
    profile = tempfile.mkdtemp(prefix="tip-cdp-")
    try:
        proc = subprocess.Popen([
            CHROME, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
            "--remote-allow-origins=*", f"--user-data-dir={profile}", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = pathlib.Path(profile) / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                time.sleep(.05)
            port = int(port_file.read_text().splitlines()[0])
            target = http_json(f"http://127.0.0.1:{port}/json/new?{BASE}/?layout-test=r11", "PUT")
            with connect(target["webSocketDebuggerUrl"]) as ws:
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
                call("Page.enable")
                call("Runtime.enable")
                time.sleep(.8)
                for width, height in SIZES:
                    call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 500})
                    expression = """(() => {
                      const view = document.getElementById('view-marketwide');
                      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
                      view.classList.add('active');
                      const host = document.getElementById('marketWideCandidateList');
                      host.innerHTML = renderMarketWideCandidateRow({ticker:'QA', eval_freshness:'current', last_evaluated_at:'2026-10-16T12:00:00Z', raw_json:{net_premium:1,call_volume:2,put_volume:1,iv_rank:3,one_day_perc:1}, state:'NEW'});
                      const row = host.querySelector('.market-wide-row');
                      const main = row.querySelector('.market-wide-row-main');
                      const rr=row.getBoundingClientRect(), mr=main.getBoundingClientRect();
                      const style=getComputedStyle(row);
                      return {rowLeft:rr.left,rowRight:rr.right,rowWidth:rr.width,mainLeft:mr.left,mainRight:mr.right,mainWidth:mr.width,align:style.alignItems,padLeft:parseFloat(style.paddingLeft),padRight:parseFloat(style.paddingRight),leftGap:mr.left-rr.left,rightGap:rr.right-mr.right};
                    })()"""
                    result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})["result"]["value"]
                    assert result["align"] == "stretch", result
                    assert abs(result["leftGap"] - result["padLeft"]) <= 1, result
                    assert abs(result["rightGap"] - result["padRight"]) <= 1, result
                    expected_width = result["rowWidth"] - result["padLeft"] - result["padRight"]
                    assert abs(result["mainWidth"] - expected_width) <= 2, result
                    print(f"PASS {width}x{height}: align={result['align']} leftGap={result['leftGap']:.1f}px rightGap={result['rightGap']:.1f}px row={result['rowWidth']:.1f}px main={result['mainWidth']:.1f}px")
        finally:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill()
    finally:
        shutil.rmtree(profile, ignore_errors=True)

if __name__ == "__main__":
    main()
