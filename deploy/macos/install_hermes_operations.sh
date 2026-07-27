#!/usr/bin/env bash
set -euo pipefail
umask 0077

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="$HOME/.local/share/hermes/operations-runtime"
RUNTIME_PARENT="$(dirname "$RUNTIME")"
AGENTS="$HOME/Library/LaunchAgents"
LOG_BACKUP="$HOME/Library/Logs/hermes-backup"
LOG_ALERTS="$HOME/Library/Logs/hermes-alerts"
UV="$HOME/.hermes/bin/uv"
UID_VALUE="$(id -u)"
STAGING=""

labels=(
  com.hermes.offsite-pull
  com.hermes.restore-drill
  com.hermes.alert-delivery
  com.hermes.alert-monitor
  com.hermes.alert-heartbeat
)

install -d -m 0700 "$RUNTIME_PARENT" "$AGENTS" "$LOG_BACKUP" "$LOG_ALERTS"
STAGING="$(mktemp -d "$RUNTIME_PARENT/.operations-runtime.XXXXXX")"
chmod 0700 "$STAGING"
trap 'rm -rf "$STAGING"' EXIT

# Build and validate the replacement before stopping a working scheduled job.
"$UV" venv --python "$REPO/.venv/bin/python" "$STAGING/venv"
"$UV" pip install --python "$STAGING/venv/bin/python" "PyYAML==6.0.3"

for package in hermes_alerts hermes_backup; do
  rsync -a --exclude __pycache__ --exclude '*.pyc' \
    "$REPO/$package/" "$STAGING/$package/"
done
install -m 0755 "$REPO/deploy/macos/hermes_operations_runtime.sh" "$STAGING/run.sh"
"$STAGING/run.sh" hermes_alerts \
  --config "$HOME/.hermes/config.yaml" --profile mac summary >/dev/null

plists=(
  com.hermes.offsite-pull.plist
  com.hermes.restore-drill.plist
  com.hermes.alert-delivery.plist
  com.hermes.alert-monitor.plist
  com.hermes.alert-heartbeat.plist
)
for plist in "${plists[@]}"; do
  plutil -lint "$REPO/deploy/macos/$plist" >/dev/null
done

restore_agents() {
  local rc="$?"
  if (( rc != 0 )); then
    for plist in "${plists[@]}"; do
      local label="${plist%.plist}"
      if [[ -f "$AGENTS/$plist" ]] \
        && ! launchctl print "gui/$UID_VALUE/$label" >/dev/null 2>&1; then
        launchctl bootstrap "gui/$UID_VALUE" "$AGENTS/$plist" || true
      fi
    done
  fi
  if [[ -n "$STAGING" ]]; then
    rm -rf "$STAGING"
  fi
  exit "$rc"
}
trap restore_agents EXIT

for label in "${labels[@]}"; do
  if launchctl print "gui/$UID_VALUE/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID_VALUE/$label"
  fi
done

rm -rf "$RUNTIME.previous"
if [[ -d "$RUNTIME" ]]; then
  mv "$RUNTIME" "$RUNTIME.previous"
fi
mv "$STAGING" "$RUNTIME"
STAGING=""

for plist in "${plists[@]}"; do
  install -m 0644 "$REPO/deploy/macos/$plist" "$AGENTS/$plist"
done

# Restore the established backup jobs first, then bring up alert delivery and
# monitoring.  The heartbeat has no RunAtLoad and therefore stays silent.
for plist in "${plists[@]}"; do
  launchctl bootstrap "gui/$UID_VALUE" "$AGENTS/$plist"
done
for label in "${labels[@]}"; do
  launchctl print "gui/$UID_VALUE/$label" >/dev/null
done

trap - EXIT
echo "hermes_operations_install_OK runtime=$RUNTIME agents=${#labels[@]}"
