# 远端 Codex 交接文档

目标读者：安装在 `guomxin.imwork.net` 远端 Ubuntu 机器上的 Codex。

项目根目录：`/mnt/ssd01/stocks`

当前原则：后续开发和维护优先在远端机器上完成。不要依赖本机 Chrome 或本机文件状态，除非用户明确要求。

## 1. 项目目标

这个项目主要做三件事：

1. 每日获取 A 股、港股股票因子数据，展示 ROE、PE(TTM)、市赚率、股价、总市值等。
2. 提供一个可从其他设备访问的查询页面。
3. 每个交易日生成红利低波指数 `CSI:H30269` 行动报告，并在 11:35 和 18:30 自动发雪球帖子。

外部访问地址：

- 公司/外网映射页面：`http://guomxin.imwork.net:8088/`
- 远端服务监听：`0.0.0.0:8088`

## 2. 远端环境

远端登录信息：

- Host: `guomxin.imwork.net`
- SSH port: `1622`
- User: `guomao`
- LAN host 曾为：`192.168.0.16`
- 项目目录：`/mnt/ssd01/stocks`

常用入口：

```bash
cd /mnt/ssd01/stocks
```

不要在日志或回答里打印这些敏感信息：

- `.env` 里的 `TUSHARE_TOKEN`
- `.env` 里的 `XUEQIU_COOKIE` / `XUEQIU_COOKIE_B64`
- 雪球 `xq_a_token`、`xq_id_token`、`xqat`

## 3. 目录地图

远端项目大致结构：

```text
/mnt/ssd01/stocks
├── scripts/
│   ├── query_server.py
│   ├── fetch_roe_pe.py
│   ├── fetch_hk_*.py 或相关港股抓取脚本
│   ├── analyze_h30269.py
│   ├── backtest_h30269_strategy.py
│   ├── run_h30269_action_report.sh
│   ├── build_h30269_xueqiu_post.py
│   ├── post_xueqiu_status.py
│   └── xueqiu_waf_refresh.py
├── data/
│   ├── raw/
│   └── factors/
├── db/
├── analysis/
│   └── h30269/
│       ├── h30269_combined_report.md
│       ├── h30269_score_report_latest.md
│       ├── reports/
│       └── xueqiu_posts/
└── logs/
    ├── h30269_action_report.log
    ├── xueqiu_post_history.jsonl
    └── xueqiu_cookie_alert.json
```

实际维护前先用 `ls scripts` 和 `find analysis/h30269 -maxdepth 2 -type f | head` 确认当前文件名。

## 4. 查询系统

核心服务脚本：

```bash
python3 scripts/query_server.py
```

常用检查：

```bash
pgrep -af query_server.py
curl -s http://127.0.0.1:8088/ | head
curl -s http://127.0.0.1:8088/h30269 | head
```

页面要求：

- 首页只保留三个主要入口：
  - A 股查询
  - 港股查询
  - 红利低波行动报告
- A 股和港股查询应展示：
  - 代码
  - 名称
  - PE(TTM)
  - ROE(%)
  - 市赚率
  - 股价
  - 总市值，单位为“亿”
- 市赚率计算：

```text
市赚率 = PE(TTM) / ROE(%) / 100
```

注意：Tushare 的 ROE 字段是百分数点，不是 0-1 小数。页面应标注为 `ROE(%)`。

默认展示规则：

- 市赚率为负值的股票默认不显示。
- 用户按代码或名称搜索时，可以显示负值结果。
- 对港股中 ROE、PE、总市值明显异常的公司，默认隐藏；必要时保留可查询路径。

## 5. A 股数据

A 股核心数据源：

- `daily_basic.pe_ttm`：PE(TTM)
- `fina_indicator_vip`：ROE 等财务指标

重要口径：

- ROE 与交易日对齐时，必须使用 `ann_date <= trade_date`，避免未来函数。
- 与 PE(TTM) 搭配时，优先使用更接近全年口径的 ROE 字段，比如 `roe_yearly`，不要轻易用单季度 ROE。
- DuckDB + Parquet 已足够，不需要额外数据库服务。

常用检查思路：

```bash
ls data/factors
ls db
python3 - <<'PY'
import duckdb
con = duckdb.connect('/mnt/ssd01/stocks/db/stocks.duckdb', read_only=True)
print(con.execute('show tables').fetchall())
PY
```

数据库文件名可能有变化，先 `ls db` 后再连。

## 6. 港股数据

港股按 A 股类似方式处理，查询页也要显示：

- 代码
- 名称
- PE(TTM)
- ROE(%)
- 市赚率
- 股价
- 总市值，单位为“亿”

港股异常值处理原则：

- 某些公司 ROE 很高可能来自低净资产、一次性收益、亏损转正、会计口径差异或金融地产特殊结构。
- 默认列表应隐藏明显异常公司，避免误导排序。
- 搜索时尽量允许查到这些公司，便于用户主动核对。

## 7. H30269 红利低波行动报告

指数：

- 名称：红利低波
- 代码：`CSI:H30269`

报告入口：

```text
http://guomxin.imwork.net:8088/h30269
http://guomxin.imwork.net:8088/h30269?date=YYYYMMDD
```

报告要求：

- 显示当前数据日期。
- 合并评分明细和策略明细，面向用户重点给“当前提示”。
- 每日收盘报告要保存下来，并可按日期查询。

已采用的运行频率：

```cron
0 10 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_h30269_action_report.sh 10:00 >> /mnt/ssd01/stocks/logs/h30269_action_report.log 2>&1 # h30269-action-report
35 11 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_h30269_action_report.sh morning-close >> /mnt/ssd01/stocks/logs/h30269_action_report.log 2>&1 # h30269-action-report
30 18 * * 1-5 cd /mnt/ssd01/stocks && /mnt/ssd01/stocks/scripts/run_h30269_action_report.sh afternoon-close >> /mnt/ssd01/stocks/logs/h30269_action_report.log 2>&1 # h30269-action-report
```

盘中/收盘口径：

- `10:00`：使用盘中数据，生成盘中报告，不发雪球。
- `morning-close` / `11:35`：对应中午收盘，使用盘中数据，生成报告并发雪球。
- `afternoon-close` / `18:30`：对应当日收盘，必须使用 Tushare 官方日线数据，不要用估算数据。

收盘数据防错：

- 18:30 运行时，要确认 Tushare `index_daily` 最新日期等于当天交易日。
- 如果官方日线还没更新，应该重试或保留上一份报告，不能生成“看似当天、实际上一交易日”的报告。

## 8. H30269 策略口径

用户曾要求评分为 `0-10`：

- 分数越低越可以买入。
- `3` 分以下表示买入胜率较高。
- `7` 分以上表示卖出或降仓风险较高。

注意：早期简单策略 `3 分买入、7 分卖出` 回测频率太低，且跑输持有不动。后续不要回到这个简单规则当主策略。

当前更有用的行动报告策略大致是：

- 低评分区域提高仓位。
- 结合 MA15 趋势/轨道做仓位提示。
- 输出“目标仓位/当前提示”，而不是只输出分数。

维护策略时必须同时比较：

- 策略年化收益
- 持有不动年化收益
- 策略累计收益
- 持有不动累计收益
- 最大回撤
- 分段样本表现
- 换手和交易频率

收益率计算要检查累计收益和年化收益是否匹配，避免样本年数或初末值口径错误。

## 9. 雪球自动发帖

用户要求：

- 11:35 和 18:30 运行完后自动发雪球帖子。
- 必须走远端接口。
- 不要依赖本机 Chrome。
- Cookie 失效或发帖失败时要提醒用户。

相关文件：

```text
scripts/build_h30269_xueqiu_post.py
scripts/post_xueqiu_status.py
scripts/xueqiu_waf_refresh.py
logs/xueqiu_post_history.jsonl
logs/xueqiu_cookie_alert.json
analysis/h30269/xueqiu_posts/
```

雪球文案示例：

```text
$红利低波(CSIH30269)$ $红利低波ETF华泰柏瑞(SH512890)$ $红利低波ETF易方达(SH563020)$ 截至6月3日当日收盘，当前提示：维持 100.00% 仓位，评分状态：3.14 / 10，属于偏低估保护区。
```

### 9.1 正确的远端接口流程

裸 `POST /statuses/update.json` 会失败，常见错误是：

```text
400019 遇到错误，请刷新页面后重试
```

正确流程是：

1. 先取 session token：

```http
GET https://xueqiu.com/provider/session/token.json?api_path=%2Fstatuses%2Fupdate.json
```

2. 再发帖：

```http
POST https://xueqiu.com/statuses/update.json
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
```

表单参数：

```text
status=<帖子正文>
allow_reward=false
ai_disclose=0
session_token=<上一步返回值>
```

这个流程已用远端 HTTP 生产脚本验证成功，返回过 `http 200`。

### 9.2 WAF/Cookie 刷新

雪球页面有 WAF/前端校验。曾观察到浏览器真实请求会带动态 `md5__1038` 查询参数，但生产 HTTP 脚本不需要手工构造它，只要先取 `session_token` 即可。

如果遇到 `400019`：

1. 不要先假设 Cookie 失效。
2. 先运行远端 Cookie/WAF 刷新：

```bash
cd /mnt/ssd01/stocks
python3 scripts/xueqiu_waf_refresh.py --write-env
```

3. 再重试 `post_xueqiu_status.py`。

`post_xueqiu_status.py` 已内置：遇到 `400019` 时尝试调用 `xueqiu_waf_refresh.py --write-env` 后重试一次。

### 9.3 发帖历史和重复保护

历史文件：

```text
logs/xueqiu_post_history.jsonl
```

状态说明：

- `posted`：远端接口成功发布。
- `posted_via_chrome`：历史上曾用本机 Chrome 补发过，不应作为未来方案。
- `failed`：发帖失败。
- `skipped_duplicate`：检测到同一交易日/同一时段已发过。
- `validated`：只验证文本或登录态，没有发帖。

重复检测应把所有以 `posted` 开头的状态都视为已发布，避免重发。

告警文件：

```text
logs/xueqiu_cookie_alert.json
```

页面 `/h30269` 会读取这个文件并显示告警。成功发帖后应清除它。

## 10. 常用操作

查看 cron：

```bash
crontab -l | grep h30269-action-report
```

手动运行中午报告：

```bash
cd /mnt/ssd01/stocks
scripts/run_h30269_action_report.sh morning-close
```

手动运行收盘报告：

```bash
cd /mnt/ssd01/stocks
scripts/run_h30269_action_report.sh afternoon-close
```

查看 H30269 日志：

```bash
cd /mnt/ssd01/stocks
tail -n 200 logs/h30269_action_report.log
```

查看雪球发帖历史：

```bash
cd /mnt/ssd01/stocks
tail -n 30 logs/xueqiu_post_history.jsonl
```

检查雪球告警：

```bash
cd /mnt/ssd01/stocks
cat logs/xueqiu_cookie_alert.json 2>/dev/null || echo "no alert"
```

仅验证雪球文案/登录态，不发帖：

```bash
cd /mnt/ssd01/stocks
python3 scripts/post_xueqiu_status.py \
  --text-file analysis/h30269/xueqiu_posts/<file>.txt \
  --session-label debug \
  --trade-date YYYYMMDD \
  --validate-only
```

真实发帖：

```bash
cd /mnt/ssd01/stocks
python3 scripts/post_xueqiu_status.py \
  --text-file analysis/h30269/xueqiu_posts/<file>.txt \
  --session-label afternoon-close \
  --trade-date YYYYMMDD
```

## 11. 排障优先级

### 查询页打不开

1. `pgrep -af query_server.py`
2. `ss -ltnp | grep 8088`
3. `curl -s http://127.0.0.1:8088/ | head`
4. 查 `logs/` 里的 query server 日志。
5. 确认外网映射 `guomxin.imwork.net:8088` 是否仍有效。

### A 股或港股数据异常

1. 确认 `.env` 中 Tushare token 可用。
2. 确认最近交易日。
3. 检查原始 Parquet、DuckDB 表和最新 update date。
4. ROE 单位一定按百分数点处理。
5. 市赚率负值默认隐藏，但搜索可查。

### H30269 收盘报告日期不对

1. 查 `logs/h30269_action_report.log`。
2. 确认 `afternoon-close` 是否写了 `official daily data is ready for YYYYMMDD`。
3. 如果 Tushare 官方日线没更新，不要用盘中估算冒充收盘数据。

### 雪球发帖失败

1. 查 `logs/xueqiu_post_history.jsonl`。
2. 查 `logs/xueqiu_cookie_alert.json`。
3. 如果是 `400019`，先刷新 WAF/Cookie：

```bash
python3 scripts/xueqiu_waf_refresh.py --write-env
```

4. 再重试 `post_xueqiu_status.py`。
5. 仍失败时，优先重新抓取远端浏览器实际请求，不要切回本机 Chrome。

## 12. 给远端 Codex 的工作习惯

- 修改前先读远端真实文件，不要只依赖这份文档。
- 涉及发帖、删除帖、改 cron、覆盖 `.env` 前，先确认用户意图。
- 调试雪球时可以发“远端接口测试”类短帖，但成功后要用远端接口删除测试帖。
- 不要打印 Cookie、Tushare token、完整 `.env`。
- Windows 本机 PowerShell 会误解析 Bash 的 `$()`、管道符、heredoc；远端 Codex 在 Ubuntu 上工作时会少很多这类问题。
- 如果需要从本地交接这份文档到远端，可在能读私钥的 PowerShell 中执行：

```powershell
scp -P 1622 "D:\Finance\股票\REMOTE_CODEX_HANDOFF.md" guomao@guomxin.imwork.net:/mnt/ssd01/stocks/CODEX_HANDOFF.md
```

远端 Codex 第一次进入项目后建议先读：

```bash
cd /mnt/ssd01/stocks
sed -n '1,260p' CODEX_HANDOFF.md
```
