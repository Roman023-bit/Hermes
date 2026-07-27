#!/usr/bin/env bash
# Host-side operational alerts.  This intentionally bypasses the gateway:
# an unhealthy gateway is one of the things this process must report.
set -euo pipefail
umask 0077
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
exec "$REPO/.venv/bin/python" -m hermes_alerts \
  --config "$HOME/.hermes/config.yaml" --profile mac "$@"

