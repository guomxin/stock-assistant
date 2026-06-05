#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
MARKER="# a-share-query-server"
CRON_LINE="@reboot cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/start_query_server.sh >> ${PROJECT_DIR}/logs/query_server_boot.log 2>&1 ${MARKER}"

tmp_file="$(mktemp)"
crontab -l 2>/dev/null | grep -v "${MARKER}" > "${tmp_file}" || true
printf '%s\n' "${CRON_LINE}" >> "${tmp_file}"
crontab "${tmp_file}"
rm -f "${tmp_file}"

echo "Installed query server autostart:"
echo "${CRON_LINE}"
