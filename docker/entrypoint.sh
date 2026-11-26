#!/bin/sh
# daemon            run `parcel_sync.py run` every PARCEL_SYNC_INTERVAL seconds (default)
# anything else     passed straight to parcel_sync.py (auth, run --dry-run, parse FILE)
set -eu
umask 077

DIR="${PARCEL_SYNC_DIR:-/data}"
if [ ! -w "$DIR" ]; then
  echo "$DIR is not writable by uid $(id -u). If it's a bind mount: sudo chown -R $(id -u) <host dir>" >&2
  exit 1
fi

cmd="${1:-daemon}"
if [ "$cmd" != "daemon" ]; then
  exec python /app/parcel_sync.py "$@"
fi

interval="${PARCEL_SYNC_INTERVAL:-1800}"
echo "parcel-sync: running every ${interval}s"
while true; do
  python /app/parcel_sync.py run || echo "parcel-sync: run failed (exit $?), retrying next cycle" >&2
  sleep "$interval"
done
