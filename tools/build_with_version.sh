#!/usr/bin/env bash
# Builds the Docker image with real BUILD_COMMIT/BUILD_VERSION/BUILD_DATE
# baked in (see Dockerfile + backend/app/version.py). Run this instead of
# a plain `docker compose build` when you want Settings -> Application
# Updates to show a real installed commit instead of "unknown".
#
# Usage: tools/build_with_version.sh [-p|--project-name NAME] [extra docker compose build args...]
#
#   -p NAME / --project-name NAME (or COMPOSE_PROJECT_NAME=NAME)
#       Build for that Compose project, so the image is tagged NAME-<service>:latest.
#       Compose otherwise derives the project from the checkout's directory name, which
#       is NOT the deployment's project when building from another checkout or worktree.
#       Release builds (docs/RELEASE3_RECOVERY_RUNBOOK.md, U6) always pass it.
#
# The stamp must describe exactly what is built, so a working tree with ANY tracked
# modification or untracked, non-ignored file is refused: the image would otherwise carry
# that uncommitted code under the clean commit's BUILD_COMMIT. For local development only,
# TIP_ALLOW_DIRTY_BUILD=1 builds anyway and stamps BUILD_COMMIT=<commit>-dirty, which never
# equals a release commit (a release acceptance with --expect-commit <commit> then fails, as
# it must). Never use the override for a release build.
#
# The base image is pinned by digest in the Dockerfile (python:3.11-slim@sha256:e88e9763...);
# the build never pulls a newer base implicitly. Files outside the build's needs are kept out
# of the image by .dockerignore.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
if [ "${1:-}" = "-p" ] || [ "${1:-}" = "--project-name" ]; then
  if [ -z "${2:-}" ]; then
    echo "usage: $0 [-p|--project-name NAME] [docker compose build args...]" >&2
    exit 64
  fi
  PROJECT_NAME="$2"
  shift 2
fi

if ! git rev-parse HEAD >/dev/null 2>&1; then
  echo "Not a git checkout (or git not installed) -- building with BUILD_COMMIT=unknown." >&2
  BUILD_COMMIT=unknown
  BUILD_VERSION=unknown
else
  BUILD_COMMIT="$(git rev-parse HEAD)"
  BUILD_VERSION="$(git describe --tags --always 2>/dev/null || echo unknown)"
  DIRTY="$(git status --porcelain --untracked-files=normal)"
  if [ -n "$DIRTY" ]; then
    if [ "${TIP_ALLOW_DIRTY_BUILD:-0}" != "1" ]; then
      echo "REFUSED: the working tree has uncommitted changes or untracked files:" >&2
      printf '%s\n' "$DIRTY" | head -n 20 >&2
      echo "A build from it would ship that code stamped as $BUILD_COMMIT. Commit or remove the" >&2
      echo "changes (or build from a clean checkout of the release commit). Development only:" >&2
      echo "TIP_ALLOW_DIRTY_BUILD=1 builds anyway with BUILD_COMMIT=$BUILD_COMMIT-dirty." >&2
      exit 1
    fi
    echo "WARNING: TIP_ALLOW_DIRTY_BUILD=1 -- development build of a dirty tree, stamped -dirty." >&2
    BUILD_COMMIT="$BUILD_COMMIT-dirty"
    BUILD_VERSION="$BUILD_VERSION-dirty"
  fi
fi
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

export BUILD_COMMIT BUILD_VERSION BUILD_DATE
COMPOSE=(docker compose)
if [ -n "$PROJECT_NAME" ]; then
  COMPOSE+=(-p "$PROJECT_NAME")
fi
echo "Building with BUILD_COMMIT=$BUILD_COMMIT BUILD_VERSION=$BUILD_VERSION BUILD_DATE=$BUILD_DATE" \
  "project=${PROJECT_NAME:-<directory default>}"
"${COMPOSE[@]}" build "$@"
if [ -n "$PROJECT_NAME" ]; then
  # Record this ID: deployment acceptance (--expect-image) and the deployment record use it.
  for svc in $("${COMPOSE[@]}" config --services 2>/dev/null); do
    id="$(docker image inspect "${PROJECT_NAME}-${svc}:latest" --format '{{.Id}}' 2>/dev/null || true)"
    if [ -n "$id" ]; then
      echo "Built image ${PROJECT_NAME}-${svc}:latest = $id"
    fi
  done
fi
exit 0
