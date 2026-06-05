#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ssd01/stocks
source .venv/bin/activate
exec python scripts/query_server.py --host 0.0.0.0 --port "${PORT:-8088}"
