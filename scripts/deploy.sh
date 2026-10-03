#!/usr/bin/env bash
# Ship Kith to the Barn host, then prove it came up.
#
#   bash scripts/deploy.sh [tag]        # tag defaults to the version in the Makefile
#
# Three traps are encoded here on purpose, because each one cost an hour once:
#
#   1. Build as **barn**, never root. Rootless podman storage is per user, so a root build
#      lands somewhere the user unit can never see.
#   2. Run rootless podman through `sudo -iu barn` (a *login* shell), never `sudo -u barn`.
#      The latter inherits /home/fedora as its working directory, which barn cannot read,
#      and podman dies with "cannot chdir to /home/fedora" — which reads exactly like a
#      rejected password when it happens during set-password.
#   3. Extract the source *as barn* too. Files written into /home/barn by plain sudo end up
#      root-owned, and the container's `:Z` relabel then fails with EPERM.
#
# The script checks its own work: image, health version, schema, the migration the app
# logged, the login gate, and the new asset. A deploy that cannot show these is a failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${KITH_SSH:-fedora@host.containers.internal}"
REMOTE_SRC="${KITH_REMOTE_SRC:-/home/barn/kith}"
UNIT_DIR="/home/barn/.config/containers/systemd"

TAG="${1:-$(grep -oE 'localhost/kith:[0-9]+\.[0-9]+\.[0-9]+' "${ROOT}/Makefile" | head -1 | cut -d: -f2)}"
IMAGE="localhost/kith:${TAG}"
TARBALL="/tmp/kith-src-${TAG}.tgz"
RUN_AS_BARN="sudo -iu barn bash -lc"

if [[ -z "${TAG}" ]]; then
  echo "could not work out the version — pass one: bash scripts/deploy.sh 0.3.0" >&2
  exit 2
fi

say() { printf '\n==> %s\n' "$1"; }

say "syncing source to ${HOST}:${REMOTE_SRC} (${TAG})"
( cd "${ROOT}" && tar czf "${TARBALL}" \
    --exclude=./.venv --exclude=./data --exclude=./.pytest_cache --exclude=./.git \
    --exclude='*__pycache__*' --exclude='*.pyc' --exclude=./kith.env \
    . )
scp -q "${TARBALL}" "${HOST}:/tmp/"
ssh "${HOST}" "sudo -u barn mkdir -p ${REMOTE_SRC} && sudo -u barn tar -xzf /tmp/$(basename "${TARBALL}") -C ${REMOTE_SRC} && rm -f /tmp/$(basename "${TARBALL}")"

say "building ${IMAGE} as barn"
ssh "${HOST}" "${RUN_AS_BARN} 'cd ${REMOTE_SRC} && podman build --format docker -t ${IMAGE} -f Containerfile .'" | tail -5

say "installing the Quadlet unit and restarting"
ssh "${HOST}" "sudo -u barn mkdir -p ${UNIT_DIR} && sudo -u barn cp ${REMOTE_SRC}/kith.container ${UNIT_DIR}/kith.container"
ssh "${HOST}" "sudo -iu barn env XDG_RUNTIME_DIR=/run/user/1001 systemctl --user daemon-reload && sudo -iu barn env XDG_RUNTIME_DIR=/run/user/1001 systemctl --user restart kith"

say "checking"
sleep 4
running="$(ssh "${HOST}" "sudo -iu barn env XDG_RUNTIME_DIR=/run/user/1001 podman ps --format '{{.Names}} {{.Image}}' | grep -c 'kith ${IMAGE}'" || true)"
health="$(ssh "${HOST}" "curl -fsS http://127.0.0.1:8080/api/health" || echo '{}')"
gate="$(ssh "${HOST}" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/" || echo 000)"
asset="$(ssh "${HOST}" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/static/js/trainer.js" || echo 000)"
logged="$(ssh "${HOST}" "sudo -iu barn env XDG_RUNTIME_DIR=/run/user/1001 podman logs --tail 40 kith 2>&1 | grep -oE 'applied=[^ ]+' | tail -1" || true)"

python3 - "$running" "$health" "$gate" "$asset" "$logged" "$TAG" <<'CHECK'
import json, sys

running, health, gate, asset, logged, tag = sys.argv[1:7]
data = json.loads(health or "{}")

def ok(condition: bool) -> str:
    return "ok " if condition else "FAIL"

print(f"  [{ok(running.strip() not in ('', '0'))}] container running the new image   ({running.strip() or 'not found'})")
print(f"  [{ok(str(data.get('version')) == tag)}] health reports version {tag}   (got {data.get('version')!r})")
print(f"  [{ok(data.get('schema') == data.get('expected_schema'))}] schema at the expected version   ({data.get('schema')})")
print(f"  [{ok(gate == '303')}] login gate closed   (GET / -> {gate})")
print(f"  [{ok(asset == '200')}] trainer asset served   ({asset})")
print(f"      last migration line: {logged or '(none in the tail)'}")

bad = (
    not running.strip() or running.strip() == "0"
    or str(data.get("version")) != tag
    or data.get("schema") != data.get("expected_schema")
    or gate != "303"
    or asset != "200"
)
sys.exit(1 if bad else 0)
CHECK
