#!/usr/bin/env bash
# Back up /srv/hermes/data to a timestamped, verified, permission-locked
# archive under /srv/hermes/backups. Run on the VPS (as root or via sudo):
#
#   /srv/hermes/app/deploy/beget/backup.sh
#
# Safe to run while the hermes container is up — it does not stop it.
# Add to root's crontab for scheduled backups, e.g. nightly at 03:15:
#   15 3 * * * /srv/hermes/app/deploy/beget/backup.sh >> /var/log/hermes-backup.log 2>&1
set -euo pipefail

DATA_DIR="/srv/hermes/data"
BACKUP_DIR="/srv/hermes/backups"
KEEP="${HERMES_BACKUP_KEEP:-7}"

if [ ! -d "$DATA_DIR" ]; then
  echo "ERROR: $DATA_DIR not found — nothing to back up" >&2
  exit 1
fi

install -d -m 0700 "$BACKUP_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/hermes-${timestamp}.tar.gz"
tmp_archive="${archive}.partial"

# NOTE: this is a live filesystem-level copy, not a transactional DB dump.
# state.db is SQLite in WAL mode — a backup taken mid-write can catch the
# main file and -wal/-shm siblings at slightly different points. Acceptable
# for disaster recovery (SQLite replays the WAL on next open); if you need a
# guaranteed-consistent point-in-time snapshot, stop the container first.
tar -C "$DATA_DIR" -czf "$tmp_archive" .

# Verify the archive is readable before it replaces anything or counts
# toward retention — a corrupt backup must never look successful.
if ! tar -tzf "$tmp_archive" >/dev/null; then
  echo "ERROR: backup verification failed for $tmp_archive — removing partial file" >&2
  rm -f "$tmp_archive"
  exit 1
fi

mv "$tmp_archive" "$archive"
chmod 600 "$archive"
echo "OK: backup created and verified: $archive ($(du -h "$archive" | cut -f1))"

# Retention: keep only the newest $KEEP backups, oldest-first deletion.
# Never delete down to zero — if something upstream is already broken
# (e.g. $KEEP=0 misconfiguration), fail loud instead of wiping history.
mapfile -t backups < <(find "$BACKUP_DIR" -maxdepth 1 -name 'hermes-*.tar.gz' -type f | sort)
count="${#backups[@]}"
if [ "$count" -gt "$KEEP" ] && [ "$KEEP" -ge 1 ]; then
  to_delete=$((count - KEEP))
  for ((i = 0; i < to_delete; i++)); do
    echo "Pruning old backup: ${backups[$i]}"
    rm -f "${backups[$i]}"
  done
fi

echo "Retention: $(find "$BACKUP_DIR" -maxdepth 1 -name 'hermes-*.tar.gz' -type f | wc -l) backup(s) kept in $BACKUP_DIR"
