#!/usr/bin/env bash
# Lightweight "build/export" step for the static public demo -- NOT a
# bundler/framework. frontend/app.core.js holds the one shared rendering
# implementation; this script copies it (and the shared styles.css)
# verbatim into static-demo/ so the two editions never hand-drift into
# two different implementations of how results are displayed. Run this
# any time frontend/app.core.js or frontend/styles.css changes, then
# commit the copied result.
set -euo pipefail
cd "$(dirname "$0")/.."

cp frontend/app.core.js static-demo/app.core.js
cp frontend/styles.css static-demo/styles.css
cp frontend/lightweight-charts.js static-demo/lightweight-charts.js
# UX round (2026-10-16), item 6: the new centralized timestamp-
# formatting utility is shared the same byte-identical way.
cp frontend/timefmt.js static-demo/timefmt.js

echo "Copied frontend/app.core.js -> static-demo/app.core.js"
echo "Copied frontend/styles.css  -> static-demo/styles.css"
echo "Copied frontend/lightweight-charts.js -> static-demo/lightweight-charts.js"
echo "Copied frontend/timefmt.js -> static-demo/timefmt.js"

PY=python3
if [ -x backend/.venv_run/bin/python ]; then
  PY=backend/.venv_run/bin/python
fi
"$PY" tools/capture_static_ohlc.py
"$PY" tools/capture_static_demo_data.py
