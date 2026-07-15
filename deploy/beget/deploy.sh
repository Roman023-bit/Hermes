#!/usr/bin/env bash
# Safe update/deploy for the Beget VPS. Run on the VPS from /srv/hermes/app:
#
#   deploy/beget/deploy.sh
#
# Sequence: verify clean checkout -> backup data -> remember rollback point
# -> pull -> build -> up -> verify -> roll back the image/commit on failure.
# Never touches the data volume and never force-resets git history.
set -euo pipefail

APP_DIR="/srv/hermes/app"
COMPOSE_FILE="deploy/beget/compose.yaml"

cd "$APP_DIR"

echo "== 1/9: verifying clean checkout =="
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: $APP_DIR has local changes — refusing to deploy over them:" >&2
  git status --short >&2
  exit 1
fi

echo "== 2/9: backing up /srv/hermes/data =="
"$APP_DIR/deploy/beget/backup.sh"

echo "== 3/9: recording rollback point =="
prev_commit="$(git rev-parse HEAD)"
prev_tag="$(git rev-parse --short=12 HEAD)"
echo "Rollback point: commit=$prev_commit image_tag=$prev_tag"

rollback() {
  echo "!! Deploy failed — rolling back to commit=$prev_commit image_tag=$prev_tag" >&2
  git checkout --quiet "$prev_commit" || true
  HERMES_GIT_SHA="$prev_commit" HERMES_IMAGE_TAG="$prev_tag" \
    docker compose -f "$COMPOSE_FILE" up -d --build || true
  echo "!! Rolled back. Data volume was not touched. Investigate before retrying." >&2
}
trap rollback ERR

echo "== 4/9: git fetch origin main =="
git fetch origin main

echo "== 5/9: git pull --ff-only origin main =="
git pull --ff-only origin main

new_commit="$(git rev-parse HEAD)"
if [ "$new_commit" = "$prev_commit" ]; then
  echo "Already up to date at $prev_commit — rebuilding/redeploying anyway."
fi

export HERMES_GIT_SHA="$new_commit"
export HERMES_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
echo "== 6/9: building image (HERMES_GIT_SHA=$HERMES_GIT_SHA HERMES_IMAGE_TAG=$HERMES_IMAGE_TAG) =="
docker compose -f "$COMPOSE_FILE" build --pull

echo "== 7/9: docker compose up -d =="
docker compose -f "$COMPOSE_FILE" up -d

echo "== 8/9: verifying container =="
sleep 5
docker compose -f "$COMPOSE_FILE" ps
status="$(docker inspect -f '{{.State.Status}}' hermes)"
if [ "$status" != "running" ]; then
  echo "ERROR: container status is '$status', expected 'running'" >&2
  exit 1
fi
docker logs --tail=100 hermes
docker exec hermes hermes --version
if ! docker exec hermes hermes gateway status; then
  echo "ERROR: 'hermes gateway status' failed inside the container" >&2
  exit 1
fi

trap - ERR
echo "== 9/9: deploy complete =="
echo "commit=$new_commit image_tag=$HERMES_IMAGE_TAG"
