#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
CRON_LINE="45 20 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/run_fetch_hk.sh >> ${PROJECT_DIR}/logs/hk_fetch.log 2>&1 # hk-roe-pe-fetch"

(
  crontab -l 2>/dev/null | grep -v "hk-roe-pe-fetch" || true
  echo "${CRON_LINE}"
) | crontab -

echo "Installed cron:"
echo "${CRON_LINE}"
