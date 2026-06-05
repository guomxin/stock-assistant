#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
BACKUP_DIR="${STOCKS_BACKUP_DIR:-${PROJECT_DIR}/backups}"
RETENTION_DAYS="${STOCKS_BACKUP_RETENTION_DAYS:-30}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
ARCHIVE_NAME="stocks_backup_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_DIR}/${ARCHIVE_NAME}"
SHA_PATH="${ARCHIVE_PATH}.sha256"
STAGING_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${STAGING_DIR}"
}
trap cleanup EXIT

cd "${PROJECT_DIR}"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

copy_if_exists() {
  local path="$1"
  if [ -e "${path}" ]; then
    mkdir -p "${STAGING_DIR}/$(dirname "${path}")"
    cp -a "${path}" "${STAGING_DIR}/${path}"
    printf 'included %s\n' "${path}"
  else
    printf 'missing %s\n' "${path}"
  fi
}

{
  printf 'backup_created_at=%s\n' "$(date '+%F %T %Z %z')"
  printf 'project_dir=%s\n' "${PROJECT_DIR}"
  printf 'backup_dir=%s\n' "${BACKUP_DIR}"
  printf 'retention_days=%s\n' "${RETENTION_DAYS}"
  printf 'hostname=%s\n' "$(hostname)"
  printf '\n[files]\n'
  copy_if_exists ".env"
  copy_if_exists "db/a_share_factors.duckdb"
  copy_if_exists "analysis/h30269/daily_close_reports"
  copy_if_exists "logs/xueqiu_post_history.jsonl"
  copy_if_exists "logs/xueqiu_cookie_alert.json"
  copy_if_exists "CODEX_LOCAL_KNOWLEDGE.md"
  copy_if_exists "OPERATIONS.md"
  copy_if_exists "README.md"
  printf '\n[crontab]\n'
  crontab -l 2>/dev/null || printf 'no crontab\n'
} > "${STAGING_DIR}/BACKUP_MANIFEST.txt"

tar -C "${STAGING_DIR}" -czf "${ARCHIVE_PATH}" .
sha256sum "${ARCHIVE_PATH}" > "${SHA_PATH}"
chmod 600 "${ARCHIVE_PATH}" "${SHA_PATH}"

find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'stocks_backup_*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'stocks_backup_*.tar.gz.sha256' -mtime "+${RETENTION_DAYS}" -delete

printf 'backup archive: %s\n' "${ARCHIVE_PATH}"
printf 'sha256 file: %s\n' "${SHA_PATH}"
