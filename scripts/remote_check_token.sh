#!/usr/bin/env bash
set -euo pipefail

cd /mnt/ssd01/stocks

if [ -f .env ]; then
  echo "PROJECT_ENV_EXISTS"
else
  echo "PROJECT_ENV_MISSING"
fi

if [ -n "${TUSHARE_TOKEN:-}" ]; then
  echo "CURRENT_SHELL_TOKEN_VISIBLE"
else
  echo "CURRENT_SHELL_TOKEN_MISSING"
fi

if bash -lc 'test -n "${TUSHARE_TOKEN:-}"'; then
  echo "LOGIN_SHELL_TOKEN_VISIBLE"
else
  echo "LOGIN_SHELL_TOKEN_MISSING"
fi
