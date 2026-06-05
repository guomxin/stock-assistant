#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
SMOKE_MARKER="# stocks-smoke-check"
BACKUP_MARKER="# stocks-backup"

SMOKE_AM="15 9 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/smoke_check.sh >> ${PROJECT_DIR}/logs/smoke_check.log 2>&1 ${SMOKE_MARKER}"
SMOKE_PM="50 19 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/smoke_check.sh >> ${PROJECT_DIR}/logs/smoke_check.log 2>&1 ${SMOKE_MARKER}"
BACKUP_DAILY="30 22 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/run_backup.sh >> ${PROJECT_DIR}/logs/backup.log 2>&1 ${BACKUP_MARKER}"

tmp_file="$(mktemp)"
crontab -l 2>/dev/null \
  | grep -v "${SMOKE_MARKER}" \
  | grep -v "${BACKUP_MARKER}" > "${tmp_file}" || true
printf '%s\n' "${SMOKE_AM}" >> "${tmp_file}"
printf '%s\n' "${SMOKE_PM}" >> "${tmp_file}"
printf '%s\n' "${BACKUP_DAILY}" >> "${tmp_file}"
crontab "${tmp_file}"
rm -f "${tmp_file}"

echo "Installed maintenance crons:"
printf '%s\n' "${SMOKE_AM}"
printf '%s\n' "${SMOKE_PM}"
printf '%s\n' "${BACKUP_DAILY}"
