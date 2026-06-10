#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
MARKER="# kcb50-action-report"

CRON_AM_CLOSE="35 11 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/run_kcb50_action_report.sh morning-close >> ${PROJECT_DIR}/logs/kcb50_action_report.log 2>&1 ${MARKER}"
CRON_PM_CLOSE="30 18 * * 1-5 cd ${PROJECT_DIR} && ${PROJECT_DIR}/scripts/run_kcb50_action_report.sh afternoon-close >> ${PROJECT_DIR}/logs/kcb50_action_report.log 2>&1 ${MARKER}"

tmp_file="$(mktemp)"
crontab -l 2>/dev/null \
  | grep -v "${MARKER}" > "${tmp_file}" || true
printf '%s\n' "${CRON_AM_CLOSE}" >> "${tmp_file}"
printf '%s\n' "${CRON_PM_CLOSE}" >> "${tmp_file}"
crontab "${tmp_file}"
rm -f "${tmp_file}"

echo "Installed KCB50 action report crons:"
printf '%s\n' "${CRON_AM_CLOSE}"
printf '%s\n' "${CRON_PM_CLOSE}"
