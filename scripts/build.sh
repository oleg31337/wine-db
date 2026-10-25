#!/usr/bin/env bash
# Build the wine-db images, stamping the deploy with the current git commit SHA
# as APP_VERSION. The app stamps that version onto every static asset URL
# (/assets/x.js?v=SHA), so each rebuild forces the browser to fetch fresh JS
# instead of running a stale cached copy in a long-lived SPA tab.
set -euo pipefail
cd "$(dirname "$0")/.."

SHA="$(git rev-parse --short HEAD 2>/dev/null || echo local)"
# Provider-agnostic: Podman hosts have no `docker` CLI. Override with
#   COMPOSE="podman compose" ./scripts/build.sh
COMPOSE="${COMPOSE:-docker compose}"
echo "Building wine-db (APP_VERSION=${SHA}, ${COMPOSE})"
APP_VERSION="${SHA}" $COMPOSE build "$@"
