#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ssd01/stocks
mkdir -p logs run

if [ -f run/query_server.pid ]; then
  old_pid="$(cat run/query_server.pid)"
  if kill -0 "${old_pid}" 2>/dev/null; then
    echo "Query server already running: pid=${old_pid}"
    exit 0
  fi
fi

setsid scripts/run_query_server.sh > logs/query_server.log 2>&1 < /dev/null &
pid="$!"
printf '%s\n' "${pid}" > run/query_server.pid
sleep 1

if ! kill -0 "${pid}" 2>/dev/null; then
  echo "Query server failed to start. Check logs/query_server.log"
  exit 1
fi

echo "Query server started: pid=${pid}"
echo "URL: http://$(hostname -I | awk '{print $1}'):8088/"
