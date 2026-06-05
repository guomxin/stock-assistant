#!/usr/bin/env bash
set -euo pipefail

found=0
for file in ~/.bashrc ~/.profile ~/.bash_profile ~/.zshrc /mnt/ssd01/stocks/.env; do
  if [ -f "${file}" ] && grep -q "TUSHARE_TOKEN" "${file}"; then
    found=1
    echo "FOUND_IN ${file}"
    grep -n "TUSHARE_TOKEN" "${file}" | sed -E 's/(TUSHARE_TOKEN=).*/\1***MASKED***/'
  fi
done

if [ "${found}" -eq 0 ]; then
  echo "NO_TUSHARE_TOKEN_CONFIG_FOUND"
fi
