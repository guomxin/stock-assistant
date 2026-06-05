#!/usr/bin/env bash
set -euo pipefail

project_dir="/mnt/ssd01/stocks"
source_file="${HOME}/.bashrc"

line="$(grep -m1 -E '^[[:space:]]*(export[[:space:]]+)?TUSHARE_TOKEN=' "${source_file}" || true)"
if [ -z "${line}" ]; then
  echo "TUSHARE_TOKEN_NOT_FOUND_IN_BASHRC"
  exit 1
fi

line="${line#export }"
printf '%s\n' "${line}" > "${project_dir}/.env"
chmod 600 "${project_dir}/.env"

echo "PROJECT_ENV_WRITTEN"
ls -l "${project_dir}/.env"
