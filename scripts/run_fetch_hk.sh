#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ssd01/stocks
source .venv/bin/activate
exec python scripts/fetch_hk_roe_pe.py "$@"
