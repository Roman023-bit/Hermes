# Hermes on Beget Cloud VPS

Deployment layout for running Hermes Agent as a single, persistently
supervised Docker container on a Beget Cloud VPS. Data (config, secrets,
sessions, memories, skills) lives outside the container in a host bind
mount and survives image rebuilds/upgrades.

```
/srv/hermes/app/      git checkout of this repo (this directory is
                       deploy/beget/ inside it)
/srv/hermes/data/     -> mounted into the container at /opt/data
/srv/hermes/backups/  timestamped tar.gz backups of /srv/hermes/data
```

See `CLAUDE_BEGET_DEPLOY.md` at the repo root for the full deployment
runbook this directory was built from (stage-by-stage, including the
Mac-side preflight and data-transfer steps).

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

```sh
deploy/beget/backup.sh
```

Creates a verified, timestamped, `chmod 600` archive in
`/srv/hermes/backups/`, prunes down to the newest `HERMES_BACKUP_KEEP`
copies (default 7), and never deletes the last remaining backup. Safe to
run while the container is up. Wire it into root's crontab for scheduled
backups (see the comment at the top of the script).

Off-site copies should be encrypted before leaving the VPS — the archive
contains `.env` secrets. Beget VPS snapshots are a useful supplement, not a
replacement for these backups.

## Dashboard access

The dashboard stays disabled (`HERMES_DASHBOARD=0`) until an auth provider
is configured. Ports 8642/9119 are published to `127.0.0.1` only — reach
them from your Mac via an SSH tunnel, never by opening them on the public
interface:

```sh
ssh -i ~/.ssh/beget_hermes -L 9119:127.0.0.1:9119 root@<VPS_IP>
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
