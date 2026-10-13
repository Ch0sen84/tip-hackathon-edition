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
# Usage:
#   tools/check_deployment_binding.sh <compose_project_name> [service_name]
#   ALLOW_PUBLIC_BIND=1 tools/check_deployment_binding.sh <project> [service]
#
# Exit codes: 0 = binding looks as intended, 1 = unexpected public bind
# detected, 2 = usage/lookup error (container not found, docker not
# available, etc).
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

UNEXPECTED_PUBLIC=0
while IFS= read -r addrport; do
  [ -z "$addrport" ] && continue
  echo "  published on: $addrport"
  case "$addrport" in
    0.0.0.0:*|\[::\]:*|:::*)
      echo "    -> BINDS EVERY INTERFACE (public-equivalent on a machine with any public IP/NAT)"
      UNEXPECTED_PUBLIC=1
      ;;
    *)
      echo "    -> scoped to a specific address, not every interface"
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
if [ "$UNEXPECTED_PUBLIC" -eq 1 ]; then
  if [ "${ALLOW_PUBLIC_BIND:-0}" = "1" ]; then
    echo "WARNING: a 0.0.0.0/:: binding was found, but ALLOW_PUBLIC_BIND=1 was set -- treating as intentional."
    exit 0
  fi
  echo "FAIL: container '$CONTAINER' is published on 0.0.0.0 or :: (every interface)."
  echo "      This also exposes the Settings UI's credential-management endpoints to"
  echo "      anything that can route to this host, since they have no auth of their own."
  echo "      Fix BIND_HOST in the deployment's .env (see .env.example), redeploy, and re-run"
  echo "      this check. If this is genuinely intentional, re-run with ALLOW_PUBLIC_BIND=1."
  exit 1
fi

echo "OK: no unexpected 0.0.0.0/:: binding detected for '$CONTAINER'."
exit 0
