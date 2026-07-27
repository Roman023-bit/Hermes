#!/usr/bin/env bash
# Installed under ~/.local/share so LaunchAgents never need macOS TCC access
# to the checkout in ~/Documents.
set -euo pipefail
umask 0077
RUNTIME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$RUNTIME"
exec "$RUNTIME/venv/bin/python" -m "$@"
