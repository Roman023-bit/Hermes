#!/usr/bin/env bash
# Thin launcher for the full local archive. All behaviour lives in
# hermes_backup.full_backup, where pytest covers it; this file only takes
# the shared lock and guarantees a machine-readable status line.
#
#   /srv/hermes/app/deploy/beget/backup.sh
#
# Safe to run while the hermes container is up — it does not stop it.
set -euo pipefail
umask 0077

APP=/srv/hermes/app
LOCK=/run/lock/hermes-backup.lock

# The trap only covers this wrapper failing before Python takes over: on a
# successful exec the shell — and this trap with it — is replaced, and the
# only status line comes from Python. Exit 75 is the documented "lock was
# busy" result and must not be reported as a failure.
trap 'code=$?; [ "$code" -eq 0 ] || [ "$code" -eq 75 ] ||
  echo "hermes_full_backup_FAILED wrapper exit=$code" >&2' EXIT

exec 9>"$LOCK"
if ! flock -n 9; then
  PYTHONPATH="$APP" exec /usr/bin/python3 -m hermes_backup.full_backup --record-skip "$@"
fi

PYTHONPATH="$APP" exec /usr/bin/python3 -m hermes_backup.full_backup "$@"
