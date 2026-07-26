#!/usr/bin/env bash
# The repository root is derived from this file's own location: a path in
# a plist's environment goes stale the moment the checkout moves.
set -euo pipefail
umask 0077
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
exec "$REPO/.venv/bin/python" -m hermes_backup.offsite_pull "$@"
