#!/usr/bin/env bash
# Smoke-test the Docker deployment WITHOUT connecting to Discord: build with
# build.sh, export and load the archive, then run it through compose.docker.yaml
# in a throwaway folder and prove every hardening setting is really in effect.
#
#   scripts/docker-smoke.sh                 against the Docker daemon (docker group)
#   sudo scripts/docker-smoke.sh            if your user isn't in the docker group
#   DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock scripts/docker-smoke.sh
#                                           Docker CLI against Podman's Docker API
#
# Never moves localhost/tenman-bot:latest (builds :smoke with TAG_LATEST=0).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
IMG=localhost/tenman-bot:smoke
FAKE_TOKEN="SMOKEaaaaaaaaaaaaaaaaaaaa.bbbbbb.ccccccccccccccccccccccccccc"
trap 'cd /; docker compose -p tenman-smoke -f "$WORK/compose.docker.yaml" down -v >/dev/null 2>&1 || true;
      docker run --rm --user 0 -v "$WORK:/w" "$IMG" sh -c "rm -rf /w/*" >/dev/null 2>&1 || true;
      docker image rm -f "$IMG" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

ok() { echo "  ok  $*"; }
fail() { echo "  FAIL $*"; exit 1; }
PODMAN_API=0
if docker version --format '{{range .Server.Components}}{{.Name}} {{end}}' 2>/dev/null | grep -qi podman; then
  PODMAN_API=1
  echo "  note: Docker CLI talking to Podman's Docker API (rootless); real-root refusal is checked on Docker only"
fi

echo "==> docker: $(docker version --format '{{.Server.Platform.Name}} {{.Server.Version}}')"
echo "==> build (build.sh, CONTAINER_ENGINE=docker, TAG_LATEST=0)"
TAG_LATEST=0 CONTAINER_ENGINE=docker "$HERE/build.sh" smoke >/dev/null
docker image inspect "$IMG" >/dev/null && ok "$IMG built (:latest untouched)"
test -s "$HERE/dist/tenman-bot-smoke.tar" && ok "exported dist/tenman-bot-smoke.tar"
[[ "$(docker image inspect "$IMG" --format '{{.Config.User}}')" == "10001:10001" ]] \
  && ok "image's default user is 10001 (not root)" || fail "image default user is not 10001"

echo "==> load the exported archive (what the server does)"
docker image rm -f "$IMG" >/dev/null
docker load -i "$HERE/dist/tenman-bot-smoke.tar" | tail -1
docker image inspect "$IMG" >/dev/null && ok "archive loads"
rm -f "$HERE/dist/tenman-bot-smoke.tar"

echo "==> prepare a deploy folder exactly as the guide says"
sed "s|image: localhost/tenman-bot:latest|image: $IMG|" "$HERE/compose.docker.yaml" > "$WORK/compose.docker.yaml"
grep -q "$IMG" "$WORK/compose.docker.yaml" || fail "could not point compose at the smoke image"
mkdir -p "$WORK"/{data,logs,backups,secrets}
printf '%s' "$FAKE_TOKEN" > "$WORK/secrets/discord_token"
: > "$WORK/secrets/discord_webhook_url"
chmod 700 "$WORK/secrets"; chmod 600 "$WORK"/secrets/*
printf 'TIMEZONE=America/New_York\n' > "$WORK/.env"    # no GUILD_ID: the bot stops before reaching Discord
# chown to the bot's user without sudo: a root container does it (rootless: inside your namespace)
docker run --rm --user 0 -v "$WORK:/w:z" "$IMG" chown -R 10001:10001 /w/data /w/logs /w/backups /w/secrets
cd "$WORK"
run() { docker compose -p tenman-smoke -f compose.docker.yaml run --rm -T "$@"; }

echo "==> identity and privileges"
set +e; run bot >/dev/null 2>&1; code=$?; set -e
[[ $code -eq 7 ]] && ok "refuses to start without settings (exit 7)" || fail "expected exit 7, got $code"
[[ "$(run bot id -un)" == "tenman" && "$(run bot id -u)" == "10001" ]] && ok "runs as tenman (uid 10001)" || fail "wrong user"
run bot sh -c 'grep -q "^CapEff:[[:space:]]*0000000000000000$" /proc/self/status' && ok "no Linux capabilities (cap_drop ALL)" || fail "capabilities present"
run bot sh -c 'grep -q "^NoNewPrivs:[[:space:]]*1$" /proc/self/status' && ok "no-new-privileges in effect" || fail "NoNewPrivs not set"
if [[ $PODMAN_API -eq 0 ]]; then
  set +e; docker compose -p tenman-smoke -f compose.docker.yaml run --rm -T --user 0 bot >/dev/null 2>&1; code=$?; set -e
  [[ $code -eq 7 ]] && ok "refuses to run as real root (exit 7)" || fail "ran as root (exit $code)"
fi

echo "==> filesystem"
run bot sh -c 'touch /app/bot.py' 2>/dev/null && fail "code is writable" || ok "code is read-only"
run bot sh -c 'for d in data logs backups; do touch /app/$d/.w && rm /app/$d/.w; done' && ok "data/ logs/ backups/ writable by tenman" || fail "volumes not writable"
run bot sh -c 'cp /bin/true /tmp/t && /tmp/t' 2>/dev/null && fail "/tmp can run programs" || ok "/tmp is noexec"
run bot sh -c 'test ! -e /app/.env' && ok "no .env baked into the image" || fail ".env in image"
mv data data.off
set +e; run bot >/dev/null 2>&1; code=$?; set -e
docker run --rm --user 0 -v "$WORK:/w:z" "$IMG" rm -rf /w/data >/dev/null 2>&1 || rm -rf data
mv data.off data
[[ $code -ne 0 ]] && ok "a missing data folder fails closed (exit $code), the bot never runs as root" || fail "bot started without its data folder"

echo "==> secrets"
run bot python -c "
import sys; sys.path.insert(0, '/app')
from utils.config import read_secret
assert read_secret('DISCORD_TOKEN') == '$FAKE_TOKEN'" && ok "token read from /run/secrets/discord_token" || fail "secret not readable"
run bot sh -c "! tr '\0' '\n' < /proc/self/environ | grep -q SMOKEaaaa" && ok "token not in the environment" || fail "token in environment"
docker compose -p tenman-smoke -f compose.docker.yaml config 2>/dev/null | grep -q SMOKEaaaa && fail "token visible in compose config" || ok "token not in the compose config"

echo "==> resource limits"
run bot sh -c 'test "$(cat /sys/fs/cgroup/pids.max 2>/dev/null)" = 128' && ok "pids limit 128" || echo "  skip pids.max not visible here (cgroup v1?)"
run bot sh -c 'test "$(cat /sys/fs/cgroup/memory.max 2>/dev/null)" = 536870912' && ok "memory limit 512 MB" || echo "  skip memory.max not visible here (cgroup v1?)"

echo "==> database, backup and restore as tenman"
run bot python -c "
import sys; sys.path.insert(0, '/app')
from utils.db_queue import DBQueue; from utils import database as db
q = DBQueue('/app/data/tenman.db', init=db.ensure_schema); q.start()
sid = q.call(db.create_session, guild_id=1, channel_id=2, title='Smoke', start_at=4102444800, created_by=1)
q.call(db.set_status, sid, 'a', 'A', db.IN); q.stop()" && ok "database created"
archive=$(run bot python -m utils.backup 2>/dev/null | tail -1)
run bot python -m utils.backup --verify "$archive" >/dev/null && ok "backup written and verified"
run bot python -m utils.backup --restore "$archive" /app/data/restored.db >/dev/null && ok "restore works"
run bot python3 /app/healthcheck.py >/dev/null 2>&1 && fail "healthcheck passed with no heartbeat" \
  || ok "healthcheck fails without a heartbeat (as it should)"

echo "==> docker smoke test passed"
