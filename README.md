# 股票因子与红利低波报告

这个项目用于维护 A 股、港股 ROE/PE 因子查询页，并生成红利低波指数 `H30269.CSI` 的行动报告和雪球发布文案。

当前本机维护入口：

- 接手知识库：`CODEX_LOCAL_KNOWLEDGE.md`
- 运维手册：`OPERATIONS.md`
- 查询服务：`scripts/query_server.py`
- H30269 主任务：`scripts/run_h30269_action_report.sh`
- 纳斯达克100数据抓取：`scripts/fetch_nasdaq100.py`
- 纳斯达克100评分与策略研究：`scripts/analyze_nasdaq100_strategy.py`
- 只读巡检：`scripts/smoke_check.sh`
- 关键数据备份：`scripts/run_backup.sh`
- 维护 cron 安装：`scripts/install_maintenance_cron.sh`

## 快速检查

```bash
cd /mnt/ssd01/stocks
scripts/smoke_check.sh
```

## 服务入口

- A 股查询：`http://127.0.0.1:8088/`
- 港股查询：`http://127.0.0.1:8088/hk`
- H30269 行动报告：`http://127.0.0.1:8088/h30269`
- 健康检查：`http://127.0.0.1:8088/health`

## 注意事项

- `.env` 保存 Tushare 和雪球生产凭据，不要打印、提交或同步到不可信位置。
- `morning-close` 和 `afternoon-close` H30269 任务会真实发雪球。
- 当前目录已经初始化为 git 仓库，默认分支为 `main`。生产数据和凭据由 `.gitignore` 保护。
- 本机备份默认写入 `backups/`，归档内包含 `.env`，目录权限应保持仅当前用户可读写。
