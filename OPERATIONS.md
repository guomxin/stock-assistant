# 运维手册

项目目录：`/mnt/ssd01/stocks`  
主要依据：`CODEX_LOCAL_KNOWLEDGE.md`  
当前日期：2026-06-05

## 基本原则

- 手动执行项目脚本时优先使用 `scripts/*.sh` 包装入口；它们会激活 `.venv`。
- 不要打印 `.env`、雪球 cookie、Tushare token。
- `morning-close` 和 `afternoon-close` 的 H30269 任务会真实发雪球，调试前先确认是否需要发帖。
- 当前目录已经初始化为 git 仓库，默认分支为 `main`。生产数据和凭据由 `.gitignore` 保护。

## 查询服务

检查服务：

```bash
cd /mnt/ssd01/stocks
pgrep -af query_server.py
ss -ltnp | grep 8088
curl -s http://127.0.0.1:8088/health
```

启动服务：

```bash
cd /mnt/ssd01/stocks
scripts/start_query_server.sh
```

停止服务：

```bash
cd /mnt/ssd01/stocks
scripts/stop_query_server.sh
```

常用页面：

- A 股：`http://127.0.0.1:8088/`
- 港股：`http://127.0.0.1:8088/hk`
- H30269：`http://127.0.0.1:8088/h30269`
- 健康检查：`http://127.0.0.1:8088/health`

## 数据抓取

A 股：

```bash
cd /mnt/ssd01/stocks
scripts/run_fetch.sh
scripts/run_fetch.sh --trade-date YYYYMMDD
```

港股：

```bash
cd /mnt/ssd01/stocks
scripts/run_fetch_hk.sh
scripts/run_fetch_hk.sh --trade-date YYYYMMDD
scripts/run_fetch_hk.sh --limit 20
```

抓取后检查 DuckDB 最新日期：

```bash
cd /mnt/ssd01/stocks
.venv/bin/python - <<'PY'
import duckdb
con = duckdb.connect('db/a_share_factors.duckdb', read_only=True)
for table in ['factor_daily', 'hk_factor_daily']:
    print(table, con.execute(
        f"select max(snapshot_trade_date), count(*) from {table}"
    ).fetchone())
PY
```

## H30269

盘中报告：

```bash
cd /mnt/ssd01/stocks
scripts/run_h30269_action_report.sh 10:00
```

中午收盘报告，会发雪球：

```bash
cd /mnt/ssd01/stocks
scripts/run_h30269_action_report.sh morning-close
```

下午收盘报告，会等待官方日线并发雪球：

```bash
cd /mnt/ssd01/stocks
scripts/run_h30269_action_report.sh afternoon-close
```

调试收盘等待逻辑时可限制重试次数：

```bash
cd /mnt/ssd01/stocks
H30269_OFFICIAL_DAILY_MAX_ATTEMPTS=1 scripts/run_h30269_action_report.sh afternoon-close
```

## 雪球

只验证登录态和文本，不发帖：

```bash
cd /mnt/ssd01/stocks
source .venv/bin/activate
scripts/post_xueqiu_status.py \
  --text-file analysis/h30269/xueqiu_posts/<file>.txt \
  --session-label debug \
  --trade-date YYYYMMDD \
  --validate-only
```

遇到 `400019` 后刷新 WAF/Cookie：

```bash
cd /mnt/ssd01/stocks
source .venv/bin/activate
scripts/xueqiu_waf_refresh.py --write-env
```

## 定时任务

查看：

```bash
crontab -l
```

当前任务包含：

- 20:00 A 股因子抓取
- 20:45 港股因子抓取
- reboot 启动查询服务
- 10:00 H30269 盘中报告
- 11:35 H30269 中午报告并发雪球
- 18:30 H30269 收盘报告并发雪球

## 日志

```bash
cd /mnt/ssd01/stocks
tail -n 200 logs/query_server.log
tail -n 200 logs/cron.log
tail -n 200 logs/fetch_roe_pe.log
tail -n 200 logs/fetch_hk_roe_pe.log
tail -n 200 logs/h30269_action_report.log
tail -n 30 logs/xueqiu_post_history.jsonl
cat logs/xueqiu_cookie_alert.json 2>/dev/null || echo "no alert"
```

## 只读巡检

```bash
cd /mnt/ssd01/stocks
scripts/smoke_check.sh
```

这个脚本只读取本机状态，不抓取数据，不发雪球。
