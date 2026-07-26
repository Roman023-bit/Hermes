#!/usr/bin/env bash
# Thin launcher: all behaviour lives in hermes_backup.essential_backup,
# where it is covered by pytest. Takes the shared backup lock so the full
# archive can never run at the same time.
set -euo pipefail
umask 0077

APP=/srv/hermes/app
LOCK=/run/lock/hermes-backup.lock

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "hermes_essential_backup_SKIPPED locked"
  exit 75
fi

PYTHONPATH="$APP" exec /usr/bin/python3 -m hermes_backup.essential_backup "$@"
