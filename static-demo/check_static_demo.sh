#!/usr/bin/env bash
# Automated checks for the static public demo bundle: backend-dependency
# leaks, live API calls, secrets, and private infrastructure references.
# Run from static-demo/. Exits non-zero (and prints what it found) on any
# finding; intended to gate publishing, not just inform.
set -uo pipefail
cd "$(dirname "$0")"
FAIL=0

echo "== 1. No absolute http(s) fetch/XHR targets (every network call must be a relative static asset) =="
if grep -rnE "fetch\(\s*['\"]https?://" --include='*.js' .; then
  echo "FAIL: absolute network call found (should be relative static path only)"; FAIL=1
else
  echo "OK"
fi

echo "== 2. No reference to the real UW/MCP API hosts in JS (data must be pre-baked, not live-fetched) =="
if grep -rnE "api\.unusualwhales\.com" --include='*.js' .; then
  echo "FAIL: live UW API host referenced in JS"; FAIL=1
else
  echo "OK"
fi

echo "== 3. No private/internal infrastructure hostnames or IPs anywhere in the bundle =="
if grep -rnE "10\.10\.10\.[0-9]+|onyxhavenlabs\.com|cwwk-nas|proxmox|timescale|databento" --include='*.js' --include='*.html' --include='*.css' --include='*.json' --include='*.md' .; then
  echo "FAIL: private infrastructure reference found"; FAIL=1
else
  echo "OK"
fi

echo "== 4. No secret/API-key-shaped strings in any committed file =="
if grep -rnE "(api_key|apikey|secret|token|password)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']" --include='*.js' --include='*.html' --include='*.json' --include='*.md' .; then
  echo "FAIL: key-shaped literal found"; FAIL=1
else
  echo "OK"
fi

echo "== 5. No mutating/credential-bearing endpoints referenced anywhere (settings mode/key/investigate POST/DELETE) =="
if grep -rnE "/api/settings/(uw-key|ai-key|mode)|method:\s*[\"']POST[\"']|method:\s*[\"']DELETE[\"']" --include='*.js' .; then
  echo "FAIL: a mutating/backend API call pattern was found in the static bundle's JS"; FAIL=1
else
  echo "OK"
fi

echo "== 6. Every data/*.json file is valid JSON and explicitly reachable via a relative fetch() in app.js =="
for f in $(find data -name '*.json'); do
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" || { echo "FAIL: invalid JSON: $f"; FAIL=1; }
done
echo "OK (all JSON files parse)"

echo "== 7. index.html uses only relative asset paths (required for deployment under /hackathon) =="
if grep -nE 'href="/|src="/' index.html; then
  echo "FAIL: absolute-rooted asset path found (will break under a subpath like /hackathon)"; FAIL=1
else
  echo "OK"
fi

if [ "$FAIL" -eq 0 ]; then
  echo "=== ALL CHECKS PASSED ==="
else
  echo "=== ONE OR MORE CHECKS FAILED -- DO NOT PUBLISH ==="
fi
exit $FAIL
