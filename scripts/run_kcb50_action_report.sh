#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
SESSION_LABEL="${1:-scheduled}"
TODAY="$(date +%Y%m%d)"
OFFICIAL_DAILY_MAX_ATTEMPTS="${KCB50_OFFICIAL_DAILY_MAX_ATTEMPTS:-36}"
OFFICIAL_DAILY_SLEEP_SECONDS="${KCB50_OFFICIAL_DAILY_SLEEP_SECONDS:-300}"

cd "${PROJECT_DIR}"
source .venv/bin/activate
mkdir -p logs

exec 9>"${PROJECT_DIR}/logs/kcb50_action_report.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') ${SESSION_LABEL}: previous KCB50 job is still running; skip."
  exit 0
fi

set +e
python - "${TODAY}" <<'PY'
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import tushare as ts

today = sys.argv[1]
base_dir = Path("/mnt/ssd01/stocks")
load_dotenv(base_dir / ".env")
token = os.getenv("TUSHARE_TOKEN")
if not token:
    print("missing TUSHARE_TOKEN", file=sys.stderr)
    raise SystemExit(2)

pro = ts.pro_api(token)
cal = pro.trade_cal(
    exchange="SSE",
    start_date=today,
    end_date=today,
    fields="cal_date,is_open",
)
if cal.empty or int(cal.iloc[0]["is_open"]) != 1:
    print(f"{today} is not an A-share trading day")
    raise SystemExit(10)
print(f"{today} is an A-share trading day")
PY
trade_status=$?
set -e
if [[ "${trade_status}" -eq 10 ]]; then
  echo "$(date '+%F %T') ${SESSION_LABEL}: non-trading day; skip KCB50 action report."
  exit 0
elif [[ "${trade_status}" -ne 0 ]]; then
  echo "$(date '+%F %T') ${SESSION_LABEL}: trading calendar check failed."
  exit "${trade_status}"
fi

echo "$(date '+%F %T') ${SESSION_LABEL}: start KCB50 action report refresh."
if [[ "${SESSION_LABEL}" == "afternoon-close" ]]; then
  attempt=1
  while true; do
    latest_date="$(python - "${TODAY}" <<'PY'
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import tushare as ts

today = sys.argv[1]
base_dir = Path("/mnt/ssd01/stocks")
load_dotenv(base_dir / ".env")
token = os.getenv("TUSHARE_TOKEN")
if not token:
    raise SystemExit("missing TUSHARE_TOKEN")

pro = ts.pro_api(token)
df = pro.index_daily(ts_code="000688.SH", start_date=today, end_date=today, fields="ts_code,trade_date,close")
if not df.empty:
    print(str(df["trade_date"].max()))
PY
)"
    if [[ "${latest_date}" == "${TODAY}" ]]; then
      echo "$(date '+%F %T') ${SESSION_LABEL}: official daily data is ready for ${TODAY}."
      break
    fi
    if (( attempt >= OFFICIAL_DAILY_MAX_ATTEMPTS )); then
      echo "$(date '+%F %T') ${SESSION_LABEL}: official daily data for ${TODAY} is still unavailable after ${attempt} attempts; latest_date=${latest_date:-none}; keep previous report."
      exit 0
    fi
    echo "$(date '+%F %T') ${SESSION_LABEL}: official daily data for ${TODAY} is not ready yet; latest_date=${latest_date:-none}; retry ${attempt}/${OFFICIAL_DAILY_MAX_ATTEMPTS} after ${OFFICIAL_DAILY_SLEEP_SECONDS}s."
    attempt=$((attempt + 1))
    sleep "${OFFICIAL_DAILY_SLEEP_SECONDS}"
  done
fi

if [[ "${SESSION_LABEL}" == "afternoon-close" ]]; then
  scripts/analyze_kcb50_strategy.py --refresh
else
  scripts/analyze_kcb50_strategy.py --refresh --intraday
fi

archive_date="$(python - <<'PY'
import json
from pathlib import Path

path = Path("/mnt/ssd01/stocks/analysis/kcb50/kcb50_latest_signal.json")
if path.exists():
    print(json.loads(path.read_text(encoding="utf-8")).get("latest_date", ""))
PY
)"
if [[ -n "${archive_date}" ]]; then
  archive_dir="${PROJECT_DIR}/analysis/kcb50/daily_close_reports"
  mkdir -p "${archive_dir}"
  cp "${PROJECT_DIR}/analysis/kcb50/kcb50_strategy_report.md" \
    "${archive_dir}/kcb50_action_report_${archive_date}.md"
  python - "${archive_date}" "${SESSION_LABEL}" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

archive_date, session_label = sys.argv[1], sys.argv[2]
base = Path("/mnt/ssd01/stocks/analysis/kcb50")
signal = json.loads((base / "kcb50_latest_signal.json").read_text(encoding="utf-8"))
summary = json.loads((base / "kcb50_strategy_research_summary.json").read_text(encoding="utf-8"))
metadata = {
    "index_code": "000688.SH",
    "index_name": "科创50",
    "report_type": "daily_close",
    "session_label": session_label,
    "trade_date": archive_date,
    "latest_close": signal.get("latest_close"),
    "score": signal.get("score"),
    "target_position": signal.get("target_position"),
    "action": signal.get("action"),
    "data_source": signal.get("data_source"),
    "quote_time": signal.get("quote_time"),
    "execution_note": signal.get("execution_note"),
    "selected_strategy": summary.get("selected_name"),
    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}
(base / "daily_close_reports" / f"kcb50_action_report_{archive_date}.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
  echo "$(date '+%F %T') ${SESSION_LABEL}: archived daily close report for ${archive_date}."
else
  echo "$(date '+%F %T') ${SESSION_LABEL}: cannot archive daily close report; latest date is empty."
fi

echo "$(date '+%F %T') ${SESSION_LABEL}: finished KCB50 action report refresh."
