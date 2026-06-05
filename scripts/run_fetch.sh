#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ssd01/stocks
source .venv/bin/activate
python scripts/fetch_roe_pe.py "$@"
