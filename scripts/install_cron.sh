#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
MARKER="# a-share-roe-pe-fetch"
CRON_LINE="0 20 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/run_fetch.sh >> ${PROJECT_DIR}/logs/cron.log 2>&1 ${MARKER}"

tmp_file="$(mktemp)"
crontab -l 2>/dev/null | grep -v "${MARKER}" > "${tmp_file}" || true
printf '%s\n' "${CRON_LINE}" >> "${tmp_file}"
crontab "${tmp_file}"
rm -f "${tmp_file}"

echo "Installed cron:"
echo "${CRON_LINE}"
