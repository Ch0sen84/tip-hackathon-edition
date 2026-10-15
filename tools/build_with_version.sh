#!/usr/bin/env bash
# Builds the Docker image with real BUILD_COMMIT/BUILD_VERSION/BUILD_DATE
# baked in (see Dockerfile + backend/app/version.py). Run this instead of
# a plain `docker compose build` when you want Settings -> Application
# Updates to show a real installed commit instead of "unknown".
#
# Usage: tools/build_with_version.sh [extra docker compose build args...]
set -euo pipefail
cd "$(dirname "$0")/.."

if ! git rev-parse HEAD >/dev/null 2>&1; then
  echo "Not a git checkout (or git not installed) -- building with BUILD_COMMIT=unknown." >&2
  BUILD_COMMIT=unknown
  BUILD_VERSION=unknown
else
  BUILD_COMMIT="$(git rev-parse HEAD)"
  BUILD_VERSION="$(git describe --tags --always 2>/dev/null || echo unknown)"
fi
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

export BUILD_COMMIT BUILD_VERSION BUILD_DATE
echo "Building with BUILD_COMMIT=$BUILD_COMMIT BUILD_VERSION=$BUILD_VERSION BUILD_DATE=$BUILD_DATE"
docker compose build "$@"
