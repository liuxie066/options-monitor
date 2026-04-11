# Options Monitor Runbook

## ✅ 日常使用（唯一入口）

```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
./run_watchlist.sh
```

- 读取：`config.us.json`（US）或 `config.hk.json`（HK）
- 生成：`<report_dir>/symbols_*` 以及每标的 `*_sell_put_* / *_sell_call_*`（默认 report_dir=output/reports）

> 提醒：不要用系统 python 直接跑脚本，统一用项目 `.venv`（脚本已自动处理）。

---

## 🤖 线上定时监控（OpenClaw cron）

Cron Job:
- name: `options-monitor auto tick`
- id: `9cba60f7-407b-4427-9120-0a176b818de9`
- schedule: `*/10 9-16 * * 1-5` @ `America/New_York`（只在美股交易时段附近跑；不追求严格从 09:30 开始）

常用命令：
```bash
openclaw cron list
openclaw cron runs
openclaw cron disable 9cba60f7-407b-4427-9120-0a176b818de9
openclaw cron enable  9cba60f7-407b-4427-9120-0a176b818de9
openclaw cron run 9cba60f7-407b-4427-9120-0a176b818de9 --expect-final --timeout 120000
```

线上定时执行入口（固化脚本）：
- `scripts/send_if_needed.py`

### Dev -> Prod 自动发布

- 自动发布脚本：`/home/node/.openclaw/workspace/options-monitor/scripts/auto_deploy_from_main.py`
- cron：每 2 分钟检查一次 `origin/main`，有新 commit 才发布
- 日志：`/home/node/.openclaw/workspace/options-monitor/logs/auto_deploy_from_main.log`
- 回滚/停用：删除对应 cron 条目即可立即停止自动发布

它会：
- `scan_scheduler` 判断是否到点/是否允许推送
- 到点则跑 pipeline
- 允许推送且内容有意义则发飞书私聊，并在成功后 `--mark-notified`
- 每次写 `output/state/last_run.json`

---

## 🔐 SSH / Deploy Key 自检（强烈推荐）

当你发现 **重启 OpenClaw / 重启容器 / 换机器** 后突然无法 push/PR，先跑自检：

```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
scripts/ssh_selfcheck.sh
```

它会检查：
- 本机 deploy key 私钥/公钥是否存在（路径：`/home/node/.openclaw/secrets/ssh/options-monitor/`）
- `ssh -T git@github.com` 是否能通过认证
- `git ls-remote` 是否能访问远端

如果失败，会直接打印：你应该去 GitHub 仓库 **Settings → Deploy keys** 更新哪一行 pubkey。

---

## 🔎 排障（只看三处）

1) 是否在跑：
```bash
openclaw cron runs
```

2) 上一次运行结果（最重要）：
```bash
cat /home/node/.openclaw/workspace/options-monitor-prod/output/state/last_run.json
```

3) 最新通知内容：
```bash
cat /home/node/.openclaw/workspace/options-monitor-prod/<report_dir>/symbols_notification.txt  # 默认 report_dir=output/reports
```

## Archived scripts

Some legacy/unused helper scripts were moved to `scripts/_archive/` to reduce confusion.
If you need them for manual debugging, you can still run them from there, but they are not part of the production cron pipeline.

## Tools (manual)

Manual diagnostics/helpers live under `scripts/tools/`:
- doctor_opend_telnet.py: basic OpenD connectivity/status checks
- doctor_required_data_schema.py: validate required_data.csv schema
- snip_sell_put_headroom.py: extract headroom summary
- sell_put_cash_and_notify.py: standalone cash warning helper

## 🧹 历史运行产物清理（手动）

仅实现手动/cron 可调用脚本；是否创建 cron 任务由你自行决定。

```bash
cd /home/node/.openclaw/workspace/options-monitor

# 预览（默认 dry-run）
.venv/bin/python scripts/cleanup_runtime_artifacts.py --keep-days 7

# 执行删除（仅 output_runs）
.venv/bin/python scripts/cleanup_runtime_artifacts.py --keep-days 7 --apply

# 可选：同时清理 output_accounts/*/raw 下旧 JSON（默认关闭，保守）
.venv/bin/python scripts/cleanup_runtime_artifacts.py --keep-days 7 --cleanup-account-raw --apply

# 可选：同时清理 logs/ 与 audit/ 旧文件（默认关闭，保守）
.venv/bin/python scripts/cleanup_runtime_artifacts.py --keep-days 7 --cleanup-logs-audit --apply
```

安全策略：
- 永不删除“最近一次成功 run”目录
- 仅删除仓库白名单路径：`output_runs/`、（可选）`output_accounts/*/raw/*.json`、（可选）`logs/**`、`audit/**`
- 每次输出扫描/删除汇总、空间估算与保护目录
