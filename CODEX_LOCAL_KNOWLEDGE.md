# Codex 本机接手知识库

更新时间：2026-06-05 12:57 CST  
项目根目录：`/mnt/ssd01/stocks`  
主机名：`xingm-AI`  
本机局域网 IP：`192.168.0.16`，同时存在 docker/虚拟网卡地址。  

这份文档是基于 `CODEX_HANDOFF.md` 和本机实测环境整理的后续开发支撑文档。交接文档仍有价值，但它原本面向“远端 Ubuntu 机器”；当前工作已经切到本机，后续以本文件和真实文件状态为准。

## 1. 当前结论

- 本目录已在 2026-06-05 初始化为 git 仓库，默认分支为 `main`。初始化前曾有一个空的只读 `.git` 占位导致 `git init` 失败于 `.git/hooks/: Read-only file system`；该空占位已移除，真实仓库已创建。`.gitignore` 已保护 `.env`、`.venv`、analysis/data/db/logs/run 等生产凭据和运行产物。
- 项目已经具备运行态：`.venv`、`.env`、DuckDB、Parquet 数据、日志、pid 文件和 crontab 均存在。
- 查询服务正在运行：`python scripts/query_server.py --host 0.0.0.0 --port 8088`，pid 见 `run/query_server.pid`。
- 本机服务监听：`0.0.0.0:8088`。
- 查询服务健康检查：`http://127.0.0.1:8088/health` 返回 `ok`。
- 外部映射沿用交接信息：`http://guomxin.imwork.net:8088/`。后续如公网访问异常，需单独核对映射是否仍指向本机。

## 2. 敏感信息边界

不要在日志、回答或文档里打印这些值：

- `.env` 中的 `TUSHARE_TOKEN`
- `.env` 中的 `XUEQIU_COOKIE_B64`
- 雪球 cookie/token 中的 `xq_a_token`、`xq_id_token`、`xqat`

本机 `.env` 实测只有以下键名：

```text
TUSHARE_TOKEN
XUEQIU_COOKIE_B64
```

`config/env.example` 当前只包含 `TUSHARE_TOKEN=put_your_tushare_token_here`，没有雪球 cookie 示例。

## 3. Python 环境

系统默认 `python3` 是 `Python 3.7.0`，项目虚拟环境是 `Python 3.10.12`。后续手动执行项目脚本时，优先使用包装脚本，或显式使用 `.venv/bin/python`。

已安装核心包版本：

```text
tushare 1.4.29
pandas 2.3.3
pyarrow 24.0.0
duckdb 1.5.3
python-dotenv 1.2.2
```

`requirements.txt`：

```text
tushare>=1.4.21
pandas>=2.0
pyarrow>=14.0
duckdb>=0.10
python-dotenv>=1.0
```

## 4. 目录结构

核心目录：

```text
/mnt/ssd01/stocks
├── scripts/              # 所有抓取、分析、服务、发帖脚本
├── data/raw/             # Tushare 原始缓存
├── data/factors/         # A 股和港股因子快照 parquet/csv
├── db/                   # DuckDB 文件
├── analysis/h30269/      # H30269 评分、策略、报告和雪球文案
├── logs/                 # cron、抓取、服务、H30269、雪球日志
├── run/                  # query server pid
├── config/               # env 示例
├── .venv/                # Python 3.10 虚拟环境
└── .env                  # 生产 token/cookie，不能打印
```

关键文件：

- `scripts/query_server.py`：LAN 查询服务和 H30269 页面。
- `scripts/fetch_roe_pe.py`：A 股 ROE/PE 因子抓取。
- `scripts/fetch_hk_roe_pe.py`：港股 ROE/PE 因子抓取。
- `scripts/run_h30269_action_report.sh`：H30269 定时任务主入口。
- `scripts/fetch_nasdaq100.py`：从 Yahoo Finance 抓取纳斯达克100指数 `^NDX` 日线，缓存为 `data/raw/index_daily/NDX_YAHOO_*.parquet`。
- `scripts/analyze_h30269.py`：H30269 评分分析。
- `scripts/apply_h30269_intraday.py`：盘中估算覆盖。
- `scripts/backtest_h30269_recommended_strategy.py`：推荐策略回测。
- `scripts/build_h30269_combined_report.py`：用户可读行动报告。
- `scripts/build_h30269_xueqiu_post.py`：生成雪球文案。
- `scripts/post_xueqiu_status.py`：雪球 HTTP 发帖。
- `scripts/xueqiu_waf_refresh.py`：雪球 WAF/Cookie 刷新。
- `scripts/smoke_check.sh`：只读巡检脚本。
- `scripts/run_backup.sh`：关键凭据、数据库、H30269 收盘归档和雪球发帖历史备份。
- `scripts/install_maintenance_cron.sh`：安装巡检和备份 cron。

## 5. 查询服务

启动、停止、重启：

```bash
cd /mnt/ssd01/stocks
scripts/start_query_server.sh
scripts/stop_query_server.sh
scripts/start_query_server.sh
```

底层启动命令：

```bash
cd /mnt/ssd01/stocks
source .venv/bin/activate
python scripts/query_server.py --host 0.0.0.0 --port 8088
```

路由：

- `/health`：健康检查，返回 `ok`。
- `/`：A 股 ROE / PE 查询。
- `/stocks`：A 股查询的别名。
- `/hk`：港股 ROE / PE 查询。
- `/h30269`：红利低波 H30269 行动报告。
- `/h30269?date=YYYYMMDD`：按日期查看收盘归档。
- `/h30269_score`、`/h30269_strategy`：当前都渲染同一个 H30269 报告页。
- `/h30269_research`：策略研究报告。

查询服务代码会从 `db/a_share_factors.duckdb` 读取最新 `snapshot_trade_date`，并在进程内按数据库文件 mtime 做缓存。更新 DuckDB 后，服务通常不需要重启；mtime 变化会触发重新加载。

页面显示字段：

- A 股：排序、代码、名称、行业、股价、PE(TTM)、ROE(%)、市赚率、ROE公告日、ROE报告期、总市值(亿元)。
- 港股：排序、代码、名称、市场、股价、PE(TTM)、ROE(%)、市赚率、PB(TTM)、股息率(%)、报告期、标准报告期、总市值(亿港元)。

默认过滤：

- A 股默认只显示 PE 和 ROE 都为正的股票；搜索时展示匹配结果。
- 港股默认还会隐藏 ROE 高于 50%、PB 异常、10 亿港元以下市值等明显异常项；搜索时展示匹配结果。

实测查询例子：

- `http://127.0.0.1:8088/?q=600519` 可查到贵州茅台。
- `http://127.0.0.1:8088/hk?q=00700` 可查到腾讯控股。

### 市赚率口径

市赚率以本机代码口径为准：

```python
return pe / roe
```

其中 `roe` 是 Tushare ROE 百分点字段，例如 `10` 表示 `10%`，所以页面口径是：

```text
市赚率 = PE(TTM) / ROE百分点
```

页面实测示例：贵州茅台 `19.16 / 10.57 = 1.8130`。旧交接文档中 `PE(TTM) / ROE(%) / 100` 的写法已废弃。

## 6. 数据与数据库

数据库文件：

```text
db/a_share_factors.duckdb
```

表：

- `factor_daily`：A 股因子快照。
- `hk_factor_daily`：港股因子快照。

实测数据范围：

```text
factor_daily:
  snapshot_trade_date: 20260529 至 20260604
  distinct dates: 5
  rows: 27543
  latest 20260604 rows: 5511

hk_factor_daily:
  snapshot_trade_date: 20260529 至 20260604
  distinct dates: 5
  rows: 13715
  latest 20260604 rows: 2743
```

`data/raw/` 实测缓存概况：

```text
daily_basic: 5 files，最新 daily_basic_20260604.parquet
fina_indicator: 5 files，最新 fina_indicator_asof_20260604.parquet
hk_basic: 5 files，最新 hk_basic_20260604.parquet
hk_daily: 4 files，最新 hk_daily_20260604.parquet
hk_fina_indicator: 2743 files，按港股代码缓存
index_daily: 18 files，包含 H30269/000300/000922 到 20260605 的缓存
```

`data/factors/` 实测有：

- `a_share_roe_pe_20260529.csv`
- `a_share_roe_pe_20260529.parquet`
- `a_share_roe_pe_20260601.parquet` 至 `a_share_roe_pe_20260604.parquet`
- `hk_roe_pe_20260529.csv`
- `hk_roe_pe_20260529.parquet`
- `hk_roe_pe_20260601.parquet` 至 `hk_roe_pe_20260604.parquet`

## 7. 数据抓取

A 股抓取：

```bash
cd /mnt/ssd01/stocks
scripts/run_fetch.sh
```

常用参数：

```bash
scripts/run_fetch.sh --trade-date YYYYMMDD
scripts/run_fetch.sh --trade-date YYYYMMDD --csv
scripts/run_fetch.sh --allow-missing-roe
```

A 股默认逻辑：

- `daily_basic.pe_ttm` 作为 PE(TTM)。
- `fina_indicator_vip` 作为 ROE 等财务指标。
- 默认 ROE 字段为 `roe_yearly`。
- 按 `ann_date <= trade_date` 选取已公告财报，避免未来函数。
- 输出到 `data/raw/daily_basic/`、`data/raw/fina_indicator/`、`data/factors/`，并写入 DuckDB。

港股抓取：

```bash
cd /mnt/ssd01/stocks
scripts/run_fetch_hk.sh
```

常用参数：

```bash
scripts/run_fetch_hk.sh --trade-date YYYYMMDD
scripts/run_fetch_hk.sh --limit 20
scripts/run_fetch_hk.sh --force-refresh
scripts/run_fetch_hk.sh --csv
```

港股默认逻辑：

- `hk_fina_indicator` 获取 ROE/PE 等指标。
- `hk_daily` 获取交易数据。
- per-stock 原始财务缓存写入 `data/raw/hk_fina_indicator/`。
- 默认 ROE 字段为 `roe_yearly`。
- 输出 `data/factors/hk_roe_pe_YYYYMMDD.parquet` 并写入 DuckDB。

## 8. H30269 行动报告

指数：

```text
H30269.CSI
红利低波
```

主入口：

```bash
cd /mnt/ssd01/stocks
scripts/run_h30269_action_report.sh 10:00
scripts/run_h30269_action_report.sh morning-close
scripts/run_h30269_action_report.sh afternoon-close
```

运行逻辑：

- 先用 Tushare `trade_cal` 判断当天是否 A 股交易日，非交易日直接跳过。
- 通过 `logs/h30269_action_report.lock` 做 `flock` 防重入。
- `10:00` 和 `morning-close`：运行 `analyze_h30269.py` 后再运行 `apply_h30269_intraday.py`，用成分股盘中数据估算指数。
- `afternoon-close`：必须等待 Tushare 官方 `index_daily` 返回当天 H30269 数据；默认最多 36 次，每 300 秒重试一次。未就绪则保留旧报告，不生成伪当天收盘报告。
- 之后运行推荐策略回测、合并报告。
- `morning-close` 和 `afternoon-close` 会生成雪球文案并调用雪球 HTTP 发帖。
- `afternoon-close` 还会归档每日收盘报告到 `analysis/h30269/daily_close_reports/`。

可调环境变量：

```bash
H30269_OFFICIAL_DAILY_MAX_ATTEMPTS=36
H30269_OFFICIAL_DAILY_SLEEP_SECONDS=300
```

当前 H30269 最新状态：

- 最新生成时间：2026-06-05 11:35。
- 最新报告：`analysis/h30269/h30269_combined_report.md`。
- 盘中评分报告：`analysis/h30269/h30269_score_report_latest.md`。
- 推荐策略报告：`analysis/h30269/h30269_recommended_strategy_report.md`。
- 最新交易日：2026-06-05。
- 当前评分：2.93 / 10。
- 下一交易日目标仓位：100.00%。
- 当日中午雪球发帖成功。

H30269 归档：

- 已有收盘归档：20260601、20260602、20260603、20260604。
- 已有雪球文案：20260603 morning/afternoon、20260604 morning/afternoon、20260605 morning。

## 9. H30269 策略口径

评分含义：

- 分数越低越偏向买入。
- `<=3` 是买入区。
- `3-7` 是中性区。
- `>=7` 是卖出区或降仓风险区。

当前主提示不是早期的“3 分买入、7 分卖出”简单规则。行动报告以推荐策略为主：

- 评分 `<= 4.0`：目标仓位 100%。
- 否则，收盘价 `> MA15 * 1.03`：目标仓位 100%。
- 否则，评分 `> 4.0` 且收盘价 `< MA15 * 0.97`：目标仓位 30%。
- 其余情况：保持上一目标仓位。

回测口径：

- 信号收盘后确认，下一交易日生效。
- 默认扣除 0.10% 的仓位变动成本。
- 必须同时看策略年化、持有年化、累计收益、最大回撤、分段表现、换手和成本敏感性。

2026-06-05 11:35 报告中的全样本结果：

```text
样本区间：20070116 至 20260605
策略年化收益：10.86%
持有不动年化收益：8.46%
策略累计收益：637.33%
持有不动累计收益：382.89%
策略最大回撤：-58.14%
持有不动最大回撤：-66.79%
平均仓位：87.20%
仓位变化：96 次，约 5.0 次/年
```

## 10. 雪球发帖

发帖脚本：

```bash
cd /mnt/ssd01/stocks
source .venv/bin/activate
scripts/post_xueqiu_status.py \
  --text-file analysis/h30269/xueqiu_posts/<file>.txt \
  --session-label morning-close \
  --trade-date YYYYMMDD
```

只验证登录态/文本，不发布：

```bash
cd /mnt/ssd01/stocks
source .venv/bin/activate
scripts/post_xueqiu_status.py \
  --text-file analysis/h30269/xueqiu_posts/<file>.txt \
  --session-label debug \
  --trade-date YYYYMMDD \
  --validate-only
```

干跑：

```bash
cd /mnt/ssd01/stocks
source .venv/bin/activate
scripts/post_xueqiu_status.py \
  --text-file analysis/h30269/xueqiu_posts/<file>.txt \
  --session-label debug \
  --trade-date YYYYMMDD \
  --dry-run
```

正确 HTTP 流程：

1. `GET https://xueqiu.com/provider/session/token.json?api_path=%2Fstatuses%2Fupdate.json` 取 `session_token`。
2. `POST https://xueqiu.com/statuses/update.json`，表单包含 `status`、`allow_reward=false`、`ai_disclose=0`、`session_token`。

`post_xueqiu_status.py` 已内置上述流程。遇到 `400019` 时会尝试调用 `xueqiu_waf_refresh.py --write-env` 后重试一次。

相关文件：

- `logs/xueqiu_post_history.jsonl`
- `logs/xueqiu_cookie_alert.json`
- `logs/xueqiu_waf_refresh.json`
- `analysis/h30269/xueqiu_posts/`

重复保护：

- 所有以 `posted` 开头的状态都应视为已发布，包括历史 `posted_via_chrome`。
- 当前生产方向是 HTTP 接口发帖，不应依赖本机 Chrome。

最新实测：

- 2026-06-04 morning-close：HTTP 发帖成功。
- 2026-06-04 afternoon-close：HTTP 发帖成功。
- 2026-06-05 morning-close：HTTP 发帖成功。
- 2026-06-03 曾出现 `400019`，后续已通过正确 session token 流程解决。

## 11. 定时任务

本机 `crontab -l` 实测：

```cron
0 20 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_fetch.sh >> /mnt/ssd01/stocks/logs/cron.log 2>&1 # a-share-roe-pe-fetch
@reboot cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/start_query_server.sh >> /mnt/ssd01/stocks/logs/query_server_boot.log 2>&1 # a-share-query-server
45 20 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_fetch_hk.sh >> /mnt/ssd01/stocks/logs/hk_fetch.log 2>&1 # hk-roe-pe-fetch
0 10 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_h30269_action_report.sh 10:00 >> /mnt/ssd01/stocks/logs/h30269_action_report.log 2>&1 # h30269-action-report
35 11 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_h30269_action_report.sh morning-close >> /mnt/ssd01/stocks/logs/h30269_action_report.log 2>&1 # h30269-action-report
30 18 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_h30269_action_report.sh afternoon-close >> /mnt/ssd01/stocks/logs/h30269_action_report.log 2>&1 # h30269-action-report
15 9 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/smoke_check.sh >> /mnt/ssd01/stocks/logs/smoke_check.log 2>&1 # stocks-smoke-check
50 19 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/smoke_check.sh >> /mnt/ssd01/stocks/logs/smoke_check.log 2>&1 # stocks-smoke-check
30 22 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_backup.sh >> /mnt/ssd01/stocks/logs/backup.log 2>&1 # stocks-backup
```

时间区：本机 `date` 显示 `CST +0800`。这些 cron 时间按本机时区执行。

## 12. 常用检查

服务检查：

```bash
cd /mnt/ssd01/stocks
pgrep -af query_server.py
ss -ltnp | grep 8088
curl -s http://127.0.0.1:8088/health
curl -s http://127.0.0.1:8088/ | head
curl -s http://127.0.0.1:8088/h30269 | head
```

日志：

```bash
tail -n 200 logs/query_server.log
tail -n 200 logs/cron.log
tail -n 200 logs/fetch_roe_pe.log
tail -n 200 logs/fetch_hk_roe_pe.log
tail -n 200 logs/h30269_action_report.log
tail -n 30 logs/xueqiu_post_history.jsonl
cat logs/xueqiu_cookie_alert.json 2>/dev/null || echo "no alert"
```

DuckDB 检查：

```bash
cd /mnt/ssd01/stocks
.venv/bin/python - <<'PY'
import duckdb
con = duckdb.connect('db/a_share_factors.duckdb', read_only=True)
print(con.execute('show tables').fetchall())
for t in ['factor_daily', 'hk_factor_daily']:
    print(t, con.execute(f'''
        select min(snapshot_trade_date), max(snapshot_trade_date),
               count(distinct snapshot_trade_date), count(*)
        from {t}
    ''').fetchall()[0])
PY
```

H30269 手动验证：

```bash
scripts/run_h30269_action_report.sh 10:00
scripts/run_h30269_action_report.sh morning-close
H30269_OFFICIAL_DAILY_MAX_ATTEMPTS=1 scripts/run_h30269_action_report.sh afternoon-close
```

注意：真实 `morning-close` 和 `afternoon-close` 会发雪球。调试时如不想发帖，单独跑下游脚本或使用 `post_xueqiu_status.py --dry-run/--validate-only`。

备份脚本：

```bash
cd /mnt/ssd01/stocks
scripts/run_backup.sh
```

默认备份目录为 `backups/`，包含 `.env`、`db/a_share_factors.duckdb`、H30269 收盘归档、雪球发帖历史和 crontab 快照。归档文件权限为 `600`，备份目录权限为 `700`，默认保留 30 天。

维护 cron：

```bash
cd /mnt/ssd01/stocks
scripts/install_maintenance_cron.sh
```

安装后会在交易日 09:15 和 19:50 运行巡检，在交易日 22:30 运行备份。

## 13. 排障优先级

查询页打不开：

1. `pgrep -af query_server.py`
2. `ss -ltnp | grep 8088`
3. `curl -s http://127.0.0.1:8088/health`
4. 查 `logs/query_server.log`
5. 查公网映射 `guomxin.imwork.net:8088` 是否仍指向本机

A 股/港股数据没更新：

1. 确认当日是否交易日。
2. 确认 `.env` 有 `TUSHARE_TOKEN`，但不要打印值。
3. 查 `logs/cron.log`、`logs/fetch_roe_pe.log`、`logs/hk_fetch.log`、`logs/fetch_hk_roe_pe.log`。
4. 查 `data/factors/*YYYYMMDD*` 是否生成。
5. 查 DuckDB 最新 `snapshot_trade_date`。

H30269 收盘日期不对：

1. 查 `logs/h30269_action_report.log`。
2. 对 `afternoon-close`，必须看到 `official daily data is ready for YYYYMMDD` 才应生成当天收盘报告。
3. 如果官方日线没更新，脚本应保留上一份报告并退出。

雪球发帖失败：

1. 查 `logs/xueqiu_post_history.jsonl`。
2. 查 `logs/xueqiu_cookie_alert.json`。
3. 如果是 `400019`，先运行：

   ```bash
   cd /mnt/ssd01/stocks
   source .venv/bin/activate
   scripts/xueqiu_waf_refresh.py --write-env
   ```

4. 再用 `post_xueqiu_status.py --validate-only` 验证。
5. 不要把解决方案退回到本机 Chrome；当前生产脚本应走 HTTP 接口。

## 14. 后续开发习惯

- 修改前先读真实文件；`CODEX_HANDOFF.md` 只当历史背景。
- 手动跑命令优先使用 `scripts/*.sh` 包装入口，避免系统 Python 3.7。
- 涉及 `.env`、雪球发帖、删除帖、crontab、覆盖历史报告前，先确认用户意图。
- 代码里已有 `__pycache__`，但目录不是 git 仓库，暂时不需要清理，除非后续建立版本管理规则。
- 现在日志里有外部 `CONNECT` 探测请求，`query_server.py` 会返回 501；这不一定是业务故障，但如果公网暴露继续使用，后续可考虑加反向代理、访问控制或更明确的日志降噪。
- 如要做较大改动，建议先补一个最小 smoke test 清单：健康检查、A 股代码查询、港股代码查询、H30269 页面、DuckDB 最新日期、雪球 validate-only。
