#!/usr/bin/env bash
# Deploy one full, verified commit to the live Prism containers.
#
#   tools/deploy_live.sh [--with-bot] [REF]      (REF default: fork/main)
#
# Steps: refuse a dirty tracked tree -> fetch -> detached checkout of REF ->
# build a candidate image -> run tools/deploy_preflight.py inside that image
# with this tree mounted and no network -> only then retag it as
# prism-insight:latest and recreate prism-insight-container
# (prism-telegram-bot only with --with-bot).
#
# Never copy individual files into the live tree; deploy a whole commit.
# Environment: PRISM_DEPLOY_REMOTE (default "fork").
set -euo pipefail

REMOTE="${PRISM_DEPLOY_REMOTE:-fork}"
WITH_BOT=0
REF=""

usage() {
    sed -n '2,13p' "$0"
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --with-bot) WITH_BOT=1 ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $1" >&2; usage 2 ;;
        *) [ -z "$REF" ] || { echo "only one REF allowed" >&2; exit 2; }; REF="$1" ;;
    esac
    shift
done
REF="${REF:-${REMOTE}/main}"

cd "$(dirname "$0")/.."

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "docker compose is not available" >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "REFUSED: tracked files are modified. Commit them on mini1 and deploy a commit instead:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi

PREVIOUS="$(git rev-parse HEAD)"
git fetch "$REMOTE"
git checkout --detach "$REF"
COMMIT="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
CANDIDATE="prism-insight:candidate-${SHORT}"
echo "deploy: ${PREVIOUS:0:7} -> ${SHORT} (${REF})"

rollback_checkout() {
    echo "preflight/build failed; restoring previous checkout ${PREVIOUS:0:7}. Containers were not touched." >&2
    git checkout --detach "$PREVIOUS" >/dev/null 2>&1 || true
}

if ! docker build -t "$CANDIDATE" .; then
    rollback_checkout
    exit 1
fi

if ! docker run --rm --network none \
        -v "$PWD:/app/prism-insight" -w /app/prism-insight \
        --entrypoint python3 "$CANDIDATE" tools/deploy_preflight.py; then
    rollback_checkout
    exit 1
fi

docker tag "$CANDIDATE" prism-insight:latest

SERVICES=(prism-insight)
if [ "$WITH_BOT" -eq 1 ]; then
    SERVICES+=(prism-telegram-bot)
fi
"${COMPOSE[@]}" up -d --no-build --no-deps --force-recreate "${SERVICES[@]}"

echo "deployed ${COMMIT}"
echo "running containers:"
docker ps --filter name=prism --format '  {{.Names}}  {{.Image}}  {{.Status}}'
echo "rollback: tools/deploy_live.sh ${PREVIOUS}"

echo "live config check:"
tools/check_live_config.sh
