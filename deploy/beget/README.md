# Hermes on the Aeza VPS

Deployment layout for running Hermes Agent as a single, persistently
supervised Docker container. Data (config, secrets, sessions, memories,
skills) lives outside the container in a host bind mount and survives image
rebuilds/upgrades.

Production runs on **Aeza**. Only the directory name `deploy/beget` is
historical — it predates the migration and was kept so the paths baked into
the VPS (`/srv/hermes/app/deploy/beget/…`), systemd units and this
repository would not all have to move at once.

```
/srv/hermes/app/      git checkout of this repo (this directory is
                       deploy/beget/ inside it)
/srv/hermes/data/     -> mounted into the container at /opt/data
/srv/hermes/backups/  full tar.gz archives, plus essential/daily-<UTC>/
```

`MIGRATION_BEGET_TO_AEZA.md` at the repo root is the current runbook — it
describes the host this directory actually deploys to.
`CLAUDE_BEGET_DEPLOY.md` is the original Beget runbook this layout was built
from; keep it for the stage-by-stage detail, not for host facts.

## First-time setup

Already covered by the runbook (Этапы 0–7). Summary once `/srv/hermes/data`
is populated from the Mac and `deploy/beget/.env` exists (copy from
`.env.example`, `chmod 600`):

```sh
cd /srv/hermes/app
export HERMES_GIT_SHA="$(git rev-parse HEAD)"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose -f deploy/beget/compose.yaml config   # validate first
docker compose -f deploy/beget/compose.yaml build --pull
docker compose -f deploy/beget/compose.yaml up -d
```

## Day-to-day operations

```sh
# Status
cd /srv/hermes/app
docker compose -f deploy/beget/compose.yaml ps
docker exec hermes hermes gateway status
docker exec hermes hermes doctor

# Logs (live)
docker logs --tail=200 -f hermes
# Logs (persisted across restarts, per profile)
tail -F /srv/hermes/data/logs/gateways/default/current

# Restart
docker restart hermes

# Stop / start
docker compose -f deploy/beget/compose.yaml stop
docker compose -f deploy/beget/compose.yaml up -d

# Shell into the container (drops to the hermes user automatically)
docker exec -it hermes hermes

# Version
docker exec hermes hermes --version
```

## Updating

Use the safe update script — it backs up data, pulls, rebuilds, verifies,
and rolls back the image/commit (never the data volume) on failure:

```sh
cd /srv/hermes/app
deploy/beget/deploy.sh
```

Manual equivalent, if you need to run the steps by hand:

```sh
cd /srv/hermes/app
git status --short
git pull --ff-only origin main
export HERMES_GIT_SHA="$(git rev-parse HEAD)"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
docker compose -f deploy/beget/compose.yaml build --pull
docker compose -f deploy/beget/compose.yaml up -d
docker exec hermes hermes --version
docker exec hermes hermes gateway status
```

Never run `git reset --hard` with unknown local changes present, and never
run `docker compose down -v` or `docker system prune -a` — the former can
destroy the data volume, the latter is unnecessary and can remove images
you still need for rollback.

### Importing a new upstream Hermes release

This repository is a flattened snapshot: its local history has no merge-base
with the official Hermes history. Consequently, a normal merge cannot detect
when an upstream release overwrites a local modification. Treat every upstream
release as an explicit tree import and run the snapshot guard before changing
the tree.

The file `deploy/beget/upstream-base.txt` names the upstream release represented
by the current snapshot. With a clean worktree and the new tag already fetched:

```sh
BASE="$(cat deploy/beget/upstream-base.txt)"
TARGET=vYYYY.M.P
BUNDLE="$(mktemp -d /tmp/hermes-update.XXXXXX)"
python -m hermes_update.snapshot_guard prepare \
  --baseline "$BASE" --target "$TARGET" --output "$BUNDLE"
```

`hermes_upstream_guard_BLOCKED` means upstream and the local layer touch at
least one identical path. Stop and resolve every listed path deliberately; do
not apply the generated patch. A successful preparation records every local
path, blob ID, file mode and a digest of the binary patch.

Build the candidate on a temporary branch. The restore below changes only the
index and worktree; the current commit remains the rollback point:

```sh
git switch -c codex/update-hermes-vYYYY.M.P
git restore --source="$TARGET" --staged --worktree -- .
git apply --index "$BUNDLE/local-overlay.patch"
python -m hermes_update.snapshot_guard verify --bundle "$BUNDLE"
```

Verification is fail-closed. It rejects unresolved collisions, a modified
bundle, missing or extra overlay paths, content changes, mode changes, and
unstaged edits. Only after `hermes_upstream_guard_OK` should you run the full
test/build checks, replace `deploy/beget/upstream-base.txt` with the exact new
tag, commit the snapshot, create a pre-update backup ref, fast-forward `main`,
push, and run `deploy/beget/deploy.sh`. Keep the generated bundle until the new
container and the local backup jobs have both been verified.

## Backups

Two tiers, both scheduled by systemd timers on the VPS. `deploy/beget` is a
historical directory name — this is the Aeza host, not Beget.

**Essential backup — the one you restore from.** Daily at 03:15 UTC via
`hermes-essential-backup.timer`. It stages everything under
`/srv/hermes/data` except explicitly named recoverable debris (caches,
logs, lock and pid files, cron output, debug request dumps), replaces the
live SQLite files with consistent snapshots, and publishes a directory of
exactly five files:

```
/srv/hermes/backups/essential/daily-<UTC>/
  essential.tar.gz   STATE   INVENTORY.jsonl   EXCLUSIONS.jsonl   SHA256SUMS
```

Publication is atomic: everything is built in `.daily-<UTC>.partial`,
self-checked, and only then renamed. A failure leaves the previous backup
untouched.

**Full archive — the local safety net.** Daily at 04:15 UTC via
`hermes-full-backup.timer`. Keeps everything, caches included, for files no
classification rule anticipated. It is not a second restore path.

**Consistent SQLite snapshots.** Neither tier tars a live `state.db`.
Both take `VACUUM INTO` snapshots through an unprivileged child
(`setpriv` to the database owner), because a root connection can create
`-wal`/`-shm` owned by root and lock Hermes out of its own data. Ownership
is verified before and after; a mismatch fails the run.

```sh
deploy/beget/hermes_essential_backup.sh   # essential, manual run
deploy/beget/backup.sh                    # full archive, manual run
```

Both are thin wrappers around `hermes_backup.*`, which is where the logic
lives and where pytest covers it. They share `/run/lock/hermes-backup.lock`,
so the two tiers can never run together; a busy lock exits 75, which the
units treat as success.

**Retention** comes from `config.yaml`, not from the environment:

```yaml
backup:
  retention_server: 7      # on the VPS: essential daily-* and full .tar.gz,
                           # counted independently of each other
  retention_mac: 7         # daily-* on the Mac
  retention_mac_floor: 2   # never prune below this
  freshness_hours: 26
  drill_staleness_hours: 48
```

### Off-site copy on the Mac

The Mac pulls; the VPS has no route into it.

| What | When |
|---|---|
| Pull + freshness check | daily 06:00 local (`com.hermes.offsite-pull`) |
| Restore drill | Sundays 11:00 local (`com.hermes.restore-drill`) |

The pull refuses to run unless FileVault is active — the archive carries
`auth.json`, provider keys and live session tokens, and `0700/0600` protects
against other accounts but not against a stolen laptop. Transport is SSH, so
the archive is not separately encrypted before leaving the VPS; at rest on
the Mac it is protected by FileVault.

Both pullers — Hermes and Knowledge Factory — share one `fcntl.flock` file
at `~/Library/Application Support/offsite-sync/network.lock`, because the
uplink is narrow and a Knowledge Factory transfer can run for hours.

The Hermes pull is pinned to the Mac's `en0` interface by default, so SSH and
rsync do not move the backup through an active VPN tunnel. The binding is
deliberately fail-closed: if `en0` is down or disappears, the pull fails and
the emitted `hermes_offsite_pull_FAILED` status names the selected interface;
it never retries over an unbound route. Before replacing the Mac, changing
Ethernet/Wi-Fi hardware, or renaming interfaces, verify the route with
`ifconfig en0` and override it for an operational test when needed:

```sh
deploy/macos/hermes_pull_offsite.sh --bind-interface en7
```

Once the correct persistent interface is known, update
`MAC_SSH_BIND_INTERFACE` in `hermes_backup/config.py` and reinstall/restart the
LaunchAgent through the normal operations runbook.

The drill proves the pulled copy restores **without starting anything**: it
never launches the container, the gateway or Telegram, since the archive
holds live tokens and a second poller would answer twice. It checks the
directory structure and manifest, both databases by integrity, checksum and
page count, and the recomputed inventory against `STATE`.

Secrets are checked by type as well as mode. `auth.json`, `config.yaml` and
its historical copies, `.env*` and `sessions/sessions.json` must each be a
regular file — never a symlink, a dangling link or a directory — no wider
than `0600`. Inside `mcp-tokens/` the directories must be exactly `0700`,
since a readable one leaks the file names and those name the providers a
token exists for; the regular files must be no wider than `0600`; and every
`*.json` must still parse.

One command answers whether all of it is healthy:

```sh
deploy/macos/hermes_backup_status.sh
```

It reads status files rather than parsing logs, and reports the age of the
newest backup from its `CREATED_AT` — not from the local mtime, which would
only say when it was downloaded.

### Telegram operational alerts

Alerts are host-side and deliberately do not use the Hermes gateway,
container or agent loop. A failed gateway must not silence its own alert.
Both Aeza and the Mac write private JSON events to an atomic outbox; a
separate delivery job retries Telegram every two minutes until the API
confirms each configured recipient.

The monitor sends one `FAILED` transition, a reminder every six hours while
the failure remains, and one `RECOVERED` transition. It also detects silence
when a timer or LaunchAgent stops running. Aeza and the Mac each send a
weekly heartbeat, so a broken alert path cannot look like a quiet healthy
week.

| Host | Monitor | Delivery retry | Heartbeat |
|---|---|---|---|
| Aeza | every 5 min (`hermes-alert-monitor.timer`) | every 2 min (`hermes-alert-delivery.timer`) | Sunday 09:00 UTC |
| Mac | every 5 min (`com.hermes.alert-monitor`) | every 2 min (`com.hermes.alert-delivery`) | Sunday 12:00 local |

`TELEGRAM_ALERT_BOT_TOKEN` in the existing secret `.env` is preferred.
Until a dedicated alert bot is created, `config.yaml` must explicitly allow
fallback to `TELEGRAM_BOT_TOKEN`. Recipients are explicit `chat_ids` in
`config.yaml`; they are not inferred from the gateway allowlist.

Useful checks:

```sh
# Aeza
PYTHONPATH=/srv/hermes/app /usr/bin/python3 -m hermes_alerts \
  --config /srv/hermes/data/config.yaml --profile aeza summary
systemctl list-timers 'hermes-alert-*'

# Mac
~/.local/share/hermes/operations-runtime/run.sh hermes_alerts \
  --config ~/.hermes/config.yaml --profile mac summary
launchctl list | grep 'com.hermes.alert'
```

The Mac LaunchAgents execute the installed private runtime under
`~/.local/share/hermes/operations-runtime`, not the checkout in
`~/Documents`. This is required by macOS privacy controls: a loaded agent can
otherwise fail only when it actually runs, with exit 126. Reinstall all five
Hermes operations agents after updating their code:

```sh
deploy/macos/install_hermes_operations.sh
```

## Dashboard access

The dashboard stays disabled (`HERMES_DASHBOARD=0`) until an auth provider
is configured. Ports 8642/9119 are published to `127.0.0.1` only — reach
them from your Mac via an SSH tunnel, never by opening them on the public
interface:

```sh
ssh -i ~/.ssh/aeza_hermes -L 9119:127.0.0.1:9119 root@<VPS_IP>
# then open http://127.0.0.1:9119 locally
```

See `website/docs/user-guide/features/web-dashboard.md` for the auth
provider options (basic auth, Nous Portal OAuth, self-hosted OIDC).

## Security notes

- `deploy/beget/.env` (this directory) holds only non-secret Compose
  parameters and is gitignored. It is **not** the same file as
  `/srv/hermes/data/.env`, which holds Hermes's actual secrets (model
  provider keys, messaging bot tokens) and is never placed inside the git
  checkout.
- Do not bind-mount `/var/run/docker.sock` into this container.
- Do not set `HERMES_DASHBOARD_INSECURE=1` on this VPS.
- Do not publish ports 8642/9119 beyond `127.0.0.1` without a reverse
  proxy + auth in front.
- Never run two Hermes gateway containers (or a container + a local
  `hermes gateway run`) against the same bot token or the same data
  directory at the same time.
