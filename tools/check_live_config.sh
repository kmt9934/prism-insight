#!/usr/bin/env bash
# Read-only check that prism-insight-container sees the current host config files.
#
#   tools/check_live_config.sh
#
# Single-file bind mounts pin the inode that existed when the container was created.
# Editors that replace a file (new inode) leave the container reading the OLD content.
# Compares host md5 with the container's md5 for each single-file mount and prints
# only file names + OK/STALE (never values). Exit 1 if anything is STALE or unreadable.
# Environment: PRISM_CONTAINER (default "prism-insight-container").
set -uo pipefail

CONTAINER="${PRISM_CONTAINER:-prism-insight-container}"
CONTAINER_ROOT="/app/prism-insight"
FILES=(
    .env
    docker/crontab
    mcp_agent.config.yaml
    mcp_agent.secrets.yaml
    trading/config/kis_devlp.yaml
)

cd "$(dirname "$0")/.."

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "$CONTAINER is not running" >&2
    exit 1
fi

bad=0
for f in "${FILES[@]}"; do
    if [ ! -f "$f" ]; then
        printf '%-32s SKIP (not on host)\n' "$f"
        continue
    fi
    host_md5="$(md5sum < "$f" | cut -d' ' -f1)"
    cont_md5="$(docker exec "$CONTAINER" sh -c "md5sum < '$CONTAINER_ROOT/$f'" 2>/dev/null | cut -d' ' -f1)"
    if [ -z "$cont_md5" ]; then
        printf '%-32s UNREADABLE (in container)\n' "$f"
        bad=1
    elif [ "$host_md5" = "$cont_md5" ]; then
        printf '%-32s OK\n' "$f"
    else
        printf '%-32s STALE\n' "$f"
        bad=1
    fi
done

if [ "$bad" -ne 0 ]; then
    echo "container config differs from host; recreate it:" >&2
    echo "  docker compose up -d --force-recreate prism-insight" >&2
    exit 1
fi
