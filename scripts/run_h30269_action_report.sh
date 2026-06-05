#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/mnt/ssd01/stocks"
SESSION_LABEL="${1:-scheduled}"
TODAY="$(date +%Y%m%d)"
OFFICIAL_DAILY_MAX_ATTEMPTS="${H30269_OFFICIAL_DAILY_MAX_ATTEMPTS:-36}"
OFFICIAL_DAILY_SLEEP_SECONDS="${H30269_OFFICIAL_DAILY_SLEEP_SECONDS:-300}"

cd "${PROJECT_DIR}"
source .venv/bin/activate
mkdir -p logs

exec 9>"${PROJECT_DIR}/logs/h30269_action_report.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') ${SESSION_LABEL}: previous H30269 job is still running; skip."
  exit 0
fi

if ! python - "${TODAY}" <<'PY'
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
cal = pro.trade_cal(
    exchange="SSE",
    start_date=today,
    end_date=today,
    fields="cal_date,is_open",
)
if cal.empty or int(cal.iloc[0]["is_open"]) != 1:
    print(f"{today} is not an A-share trading day")
    raise SystemExit(1)
print(f"{today} is an A-share trading day")
PY
then
  echo "$(date '+%F %T') ${SESSION_LABEL}: non-trading day; skip H30269 action report."
  exit 0
fi

echo "$(date '+%F %T') ${SESSION_LABEL}: start H30269 action report refresh."
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
df = pro.index_daily(ts_code="H30269.CSI", start_date=today, end_date=today, fields="ts_code,trade_date,close")
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
  scripts/analyze_h30269.py --refresh
  echo "$(date '+%F %T') ${SESSION_LABEL}: using official daily data only; skip intraday estimate."
else
  scripts/analyze_h30269.py
  scripts/apply_h30269_intraday.py
fi
scripts/backtest_h30269_recommended_strategy.py
scripts/build_h30269_combined_report.py
if [[ "${SESSION_LABEL}" == "morning-close" || "${SESSION_LABEL}" == "afternoon-close" ]]; then
  post_info="$(scripts/build_h30269_xueqiu_post.py "${SESSION_LABEL}")"
  echo "${post_info}"
  post_file="$(printf '%s\n' "${post_info}" | tail -n 1)"
  post_trade_date="$(python - "${post_file}" <<'PY'
import json
import sys
from pathlib import Path

meta_path = Path(sys.argv[1]).with_suffix(".json")
if meta_path.exists():
    print(json.loads(meta_path.read_text(encoding="utf-8")).get("trade_date", ""))
PY
)"
  scripts/post_xueqiu_status.py \
    --text-file "${post_file}" \
    --session-label "${SESSION_LABEL}" \
    --trade-date "${post_trade_date:-${TODAY}}" \
    || echo "$(date '+%F %T') ${SESSION_LABEL}: Xueqiu post failed; report generation kept."
fi
if [[ "${SESSION_LABEL}" == "afternoon-close" ]]; then
  archive_date="$(python - <<'PY'
import json
from pathlib import Path

path = Path("/mnt/ssd01/stocks/analysis/h30269/h30269_latest_score.json")
if path.exists():
    print(json.loads(path.read_text(encoding="utf-8")).get("latest_date", ""))
PY
)"
  if [[ -n "${archive_date}" ]]; then
    archive_dir="${PROJECT_DIR}/analysis/h30269/daily_close_reports"
    mkdir -p "${archive_dir}"
    cp "${PROJECT_DIR}/analysis/h30269/h30269_combined_report.md" \
      "${archive_dir}/h30269_action_report_${archive_date}.md"
    python - "${archive_date}" "${SESSION_LABEL}" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

archive_date, session_label = sys.argv[1], sys.argv[2]
base = Path("/mnt/ssd01/stocks/analysis/h30269")
score = json.loads((base / "h30269_latest_score.json").read_text(encoding="utf-8"))
summary = json.loads((base / "h30269_recommended_strategy_summary.json").read_text(encoding="utf-8"))
signal = summary.get("current_signal", {})
metadata = {
    "index_code": "H30269.CSI",
    "report_type": "daily_close",
    "session_label": session_label,
    "trade_date": archive_date,
    "latest_close": score.get("latest_close"),
    "score": score.get("score"),
    "target_position": signal.get("target_position"),
    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}
(base / "daily_close_reports" / f"h30269_action_report_{archive_date}.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
    echo "$(date '+%F %T') ${SESSION_LABEL}: archived daily close report for ${archive_date}."
  else
    echo "$(date '+%F %T') ${SESSION_LABEL}: cannot archive daily close report; latest date is empty."
  fi
fi
echo "$(date '+%F %T') ${SESSION_LABEL}: finished H30269 action report refresh."
