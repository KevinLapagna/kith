#!/bin/sh
# Kith entrypoint: preflight the data directory, then hand over to uvicorn.
#
# Migrations deliberately DO NOT run here — they run in the application lifespan
# (app/main.py), so that the container and the test suite apply migrations through
# exactly the same code path. This script only fails loudly on the one mistake that
# is otherwise confusing to debug: a data directory the container cannot write to.
set -eu

: "${KITH_DATA_DIR:=/data}"
: "${KITH_HOST:=0.0.0.0}"
: "${KITH_PORT:=8080}"

if ! mkdir -p "$KITH_DATA_DIR" 2>/dev/null; then
    echo "FATAL: cannot create data directory $KITH_DATA_DIR" >&2
    exit 1
fi

if [ ! -w "$KITH_DATA_DIR" ]; then
    echo "FATAL: data directory $KITH_DATA_DIR is not writable by uid $(id -u):$(id -g)." >&2
    echo "       This is a bind-mount ownership or SELinux label problem, not an app bug." >&2
    echo "       See 02-architecture.md §16: create it with 'sudo install -d -o <user> -g <user>'" >&2
    echo "       and mount it with the :Z suffix." >&2
    exit 1
fi

exec uvicorn app.main:app \
    --host "$KITH_HOST" \
    --port "$KITH_PORT" \
    --proxy-headers \
    --no-server-header
