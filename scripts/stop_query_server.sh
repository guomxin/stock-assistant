#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ssd01/stocks
if [ ! -f run/query_server.pid ]; then
  echo "No pid file."
  exit 0
fi

pid="$(cat run/query_server.pid)"
if kill -0 "${pid}" 2>/dev/null; then
  kill "${pid}"
  echo "Stopped query server: pid=${pid}"
else
  echo "Process already stopped: pid=${pid}"
fi
rm -f run/query_server.pid
