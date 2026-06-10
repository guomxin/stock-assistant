#!/usr/bin/env bash
set -u

PROJECT_DIR="/mnt/ssd01/stocks"
cd "${PROJECT_DIR}" || exit 2

failures=0

check() {
  local name="$1"
  shift
  printf '[check] %s ... ' "${name}"
  if "$@" >/tmp/stocks_smoke_check.out 2>/tmp/stocks_smoke_check.err; then
    printf 'ok\n'
  else
    printf 'failed\n'
    sed 's/^/  /' /tmp/stocks_smoke_check.err
    sed 's/^/  /' /tmp/stocks_smoke_check.out
    failures=$((failures + 1))
  fi
}

check_file() {
  local name="$1"
  local path="$2"
  printf '[check] %s ... ' "${name}"
  if [ -e "${path}" ]; then
    printf 'ok\n'
  else
    printf 'missing: %s\n' "${path}"
    failures=$((failures + 1))
  fi
}

check "python venv" .venv/bin/python --version
check_file "env file" ".env"
check_file "duckdb file" "db/a_share_factors.duckdb"
check_file "h30269 combined report" "analysis/h30269/h30269_combined_report.md"
check_file "h30269 latest score" "analysis/h30269/h30269_latest_score.json"

check "query server process" pgrep -f "query_server.py"
check "query health" curl -fsS --max-time 5 "http://127.0.0.1:8088/health"
check "a-share query sample" curl -fsS --max-time 5 "http://127.0.0.1:8088/?q=600519"
check "hk query sample" curl -fsS --max-time 5 "http://127.0.0.1:8088/hk?q=00700"
check "h30269 page" curl -fsS --max-time 5 "http://127.0.0.1:8088/h30269"
check "kcb50 page" curl -fsS --max-time 5 "http://127.0.0.1:8088/kcb50"

check "duckdb latest dates" .venv/bin/python -c "import duckdb; con=duckdb.connect('db/a_share_factors.duckdb', read_only=True); print(con.execute(\"select 'factor_daily', max(snapshot_trade_date) from factor_daily union all select 'hk_factor_daily', max(snapshot_trade_date) from hk_factor_daily\").fetchall())"

printf '\n'
if [ -e "logs/xueqiu_cookie_alert.json" ]; then
  printf '[warn] xueqiu alert exists: logs/xueqiu_cookie_alert.json\n'
else
  printf '[check] xueqiu alert ... ok\n'
fi

if [ "${failures}" -eq 0 ]; then
  printf '[result] smoke check passed\n'
else
  printf '[result] smoke check failed: %s issue(s)\n' "${failures}"
fi

exit "${failures}"
