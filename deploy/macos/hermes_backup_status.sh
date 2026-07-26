#!/usr/bin/env bash
# The repository root is derived from this file's own location, exactly as
# the pull and drill wrappers do.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
exec "$REPO/.venv/bin/python" -m hermes_backup.backup_status "$@"
