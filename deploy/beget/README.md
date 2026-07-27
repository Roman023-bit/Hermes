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
