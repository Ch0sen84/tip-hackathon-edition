#!/usr/bin/env bash
# Post-deployment binding assertion for TIP Hackathon Edition.
#
# Verifies the running container's host-published port is NOT exposed on
# every interface (0.0.0.0 or ::) unless the operator explicitly opts in
# via ALLOW_PUBLIC_BIND=1. This exists because of a real incident: a
# docker-compose override merge (ports lists concatenate, they don't
# replace) left a container simultaneously bound to both an intended
# LAN-only address and 0.0.0.0 -- this script would have caught that
# immediately instead of relying on someone noticing by hand.
#
# It also cross-checks the publish addresses against the auth mode the app
# itself is in: when the zero-token Local Mode is in effect inside the
# container (TIP_LOCAL_MODE=1, or "auto"/unset with a loopback BIND_HOST),
# the port must be published on loopback addresses ONLY. Local Mode trusts
# the browser's Sec-Fetch-Site/Origin headers, which any client that can
# reach the port can forge, so a LAN or every-interface publish while Local
# Mode is on is a FAIL that ALLOW_PUBLIC_BIND cannot override. (The typical
# cause: a hand-edited `ports:` entry or override, e.g. a bare "8080:8000",
# while BIND_HOST inside the container is still 127.0.0.1. Set BIND_PORT /
# BIND_HOST in .env instead -- see INSTALL_AGENT.md, Troubleshooting.)
#
# Only BIND_HOST and TIP_LOCAL_MODE are read from the container's
# environment; no other variable (and no secret) is ever printed.
#
# Usage:
#   tools/check_deployment_binding.sh <compose_project_name> [service_name]
#   ALLOW_PUBLIC_BIND=1 tools/check_deployment_binding.sh <project> [service]
#
# Exit codes: 0 = binding looks as intended, 1 = unexpected public bind or
# a non-loopback publish while Local Mode is in effect, 2 = usage/lookup
# error (container not found, docker not available, etc).
set -euo pipefail

PROJECT="${1:-}"
SERVICE="${2:-tip-hackathon}"

if [ -z "$PROJECT" ]; then
  echo "usage: $0 <compose_project_name> [service_name]" >&2
  exit 2
fi

CONTAINER="${PROJECT}-${SERVICE}-1"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "ERROR: container '$CONTAINER' not found (is it running? right project/service name?)" >&2
  exit 2
fi

echo "== docker port $CONTAINER =="
PORT_OUTPUT="$(docker port "$CONTAINER" 2>&1)"
echo "$PORT_OUTPUT"

echo
echo "== actual listening sockets on the host for this container's published ports =="
# Extract every host-side "ADDR:PORT" the container is actually published on.
PUBLISHED="$(echo "$PORT_OUTPUT" | sed -n 's/.*-> \(.*\)$/\1/p')"
if [ -z "$PUBLISHED" ]; then
  echo "ERROR: container has no published ports at all -- nothing to verify, but that's" >&2
  echo "       also probably not what you intended if you're running this check." >&2
  exit 2
fi

# The app's own auth-mode decision (same rule as backend/app/core/local_mode.py):
# TIP_LOCAL_MODE 1/true/yes/on forces Local Mode, 0/false/no/off forces Network
# Mode, anything else ("auto", unset) means Local Mode iff BIND_HOST is loopback.
# Only these two variables are extracted; the environment itself is never printed.
ENV_LINES="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" 2>/dev/null \
  | grep -E '^(BIND_HOST|TIP_LOCAL_MODE)=' || true)"
IN_BIND_HOST="$(printf '%s\n' "$ENV_LINES" | sed -n 's/^BIND_HOST=//p' | tail -n 1)"
IN_LOCAL_MODE="$(printf '%s\n' "$ENV_LINES" | sed -n 's/^TIP_LOCAL_MODE=//p' | tail -n 1)"
IN_BIND_HOST="${IN_BIND_HOST:-127.0.0.1}"     # the app's own default when unset

is_loopback_host() {
  case "$1" in
    127.0.0.1|::1|localhost|::ffff:127.0.0.1) return 0 ;;
    *) return 1 ;;
  esac
}

case "$(printf '%s' "${IN_LOCAL_MODE:-auto}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) LOCAL_MODE=1 ;;
  0|false|no|off) LOCAL_MODE=0 ;;
  *) if is_loopback_host "$IN_BIND_HOST"; then LOCAL_MODE=1; else LOCAL_MODE=0; fi ;;
esac
if [ "$LOCAL_MODE" -eq 1 ]; then MODE_WORD=ON; else MODE_WORD=OFF; fi
echo "  in-container BIND_HOST=$IN_BIND_HOST TIP_LOCAL_MODE=${IN_LOCAL_MODE:-<unset>} -> Local Mode $MODE_WORD"

UNEXPECTED_PUBLIC=0
NON_LOOPBACK=0
while IFS= read -r addrport; do
  [ -z "$addrport" ] && continue
  echo "  published on: $addrport"
  host="${addrport%:*}"
  host="${host#[}"; host="${host%]}"
  case "$addrport" in
    0.0.0.0:*|\[::\]:*|:::*)
      echo "    -> BINDS EVERY INTERFACE (public-equivalent on a machine with any public IP/NAT)"
      UNEXPECTED_PUBLIC=1
      NON_LOOPBACK=1
      ;;
    *)
      if is_loopback_host "$host" || [ "${host#127.}" != "$host" ]; then
        echo "    -> loopback only"
      else
        echo "    -> scoped to a specific non-loopback address (reachable from that network)"
        NON_LOOPBACK=1
      fi
      ;;
  esac
done <<< "$PUBLISHED"

echo
echo "== cross-check against live host sockets (ss -tlnp, if permitted) =="
if command -v ss >/dev/null 2>&1; then
  ss -tlnp 2>/dev/null | grep -E ':[0-9]+' || true
else
  echo "(ss not available on this host -- skipped; docker port output above is still authoritative)"
fi

echo
if [ "$LOCAL_MODE" -eq 1 ] && [ "$NON_LOOPBACK" -eq 1 ]; then
  echo "FAIL: container '$CONTAINER' runs in the zero-token Local Mode but is published on a"
  echo "      non-loopback address. Local Mode trusts browser headers that any client on that"
  echo "      network can forge, so settings and credential writes would need no token."
  echo "      ALLOW_PUBLIC_BIND does not override this. Fix: publish via BIND_HOST/BIND_PORT in the"
  echo "      deployment's .env only (never a hand-edited or override 'ports:' entry without the"
  echo "      127.0.0.1: prefix); for LAN access set BIND_HOST to the LAN address, which switches the"
  echo "      app to Network Mode (token required). Redeploy and re-run this check."
  exit 1
fi
if [ "$UNEXPECTED_PUBLIC" -eq 1 ]; then
  if [ "${ALLOW_PUBLIC_BIND:-0}" = "1" ]; then
    echo "WARNING: a 0.0.0.0/:: binding was found, but ALLOW_PUBLIC_BIND=1 was set -- treating as intentional."
    exit 0
  fi
  echo "FAIL: container '$CONTAINER' is published on 0.0.0.0 or :: (every interface)."
  echo "      The X-TIP-Auth token still protects settings and credential writes (Network Mode),"
  echo "      but every network that can route to this host can reach the app and try it."
  echo "      Fix BIND_HOST in the deployment's .env (see .env.example), redeploy, and re-run"
  echo "      this check. If this is genuinely intentional, re-run with ALLOW_PUBLIC_BIND=1."
  exit 1
fi

echo "OK: no unexpected 0.0.0.0/:: binding detected for '$CONTAINER'."
exit 0
