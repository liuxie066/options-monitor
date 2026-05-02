# Options Monitor Runbook

运维文档只覆盖：日常巡检、值班排障、应急操作。

## 文档边界

- 快速上手与常用命令：`README.md`
- 配置来源与同步：`CONFIGS.md`
- 发布/回滚：仅在本地私有运维仓执行（本仓不公开流程细节）

## 日常运行（prod）

```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
./run_watchlist.sh
```

- 运行入口配置：`config.us.json`（US）/ `config.hk.json`（HK）
- 产出：`<report_dir>/symbols_*` 与每标的 `*_sell_put_* / *_sell_call_*`（默认 `report_dir=output/reports`）

## 定时任务（OpenClaw cron）

Cron Job:
- name: `options-monitor auto tick`
- id: `9cba60f7-407b-4427-9120-0a176b818de9`
- schedule: `*/10 9-16 * * 1-5` @ `America/New_York`

常用命令：

```bash
openclaw cron list
openclaw cron runs
openclaw cron disable 9cba60f7-407b-4427-9120-0a176b818de9
openclaw cron enable  9cba60f7-407b-4427-9120-0a176b818de9
openclaw cron run 9cba60f7-407b-4427-9120-0a176b818de9 --expect-final --timeout 120000
```

线上定时执行入口：`./om run tick --config config.us.json --accounts lx sy`

`scripts/send_if_needed.py` 仅保留为兼容 wrapper，会转调统一 tick 链路；不要再把它当作单账户业务实现维护。

多账户手动/可选定时入口：

```bash
./om run tick --config config.us.json --accounts lx sy
```

多账户链路会复用共享运行数据，但通知按账户逐条发送到同一目标；每个账户一条消息，发送失败按账户隔离。

## 值班三步检查（先做这个）

Agent / OpenClaw 优先使用只读聚合入口：

```bash
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

人工直接查看文件时，再用下面三步：

1. 查看是否在跑：

```bash
openclaw cron runs
```

2. 查看上次运行结果（最重要）：

```bash
cat /home/node/.openclaw/workspace/options-monitor-prod/output/state/last_run.json
```

3. 查看最新通知内容：

```bash
cat /home/node/.openclaw/workspace/options-monitor-prod/<report_dir>/symbols_notification.txt
```

多账户运行的账户级状态和报告位于 `output_accounts/<account>/`，共享运行状态位于 `output_runs/<run_id>/`。

## 高频故障处理

### OpenD 不可用 / 登录失效

1. 先确认 OpenD 进程与端口。
2. 检查 `output/*/opend_metrics.json` 是否大量失败。
3. 恢复后手动触发一次 cron run 观察 `last_run.json`。

### 字段缺失 / 源不可用

1. 不要硬跑 pipeline。
2. 先打印缺失字段并确认数据源是否支持。
3. 必要时切换到人工核验流程。

### “非交易时段：不监控”误判

1. 确认运行命令的 `--market-config` 与配置文件市场一致。
2. 检查是否误用 US/HK 配置。

## SSH / Deploy Key 自检

```bash
cd /home/node/.openclaw/workspace/options-monitor-prod
scripts/ssh_selfcheck.sh
```

脚本检查：
- `/home/node/.openclaw/secrets/ssh/options-monitor/` 私钥/公钥是否存在
- `ssh -T git@github.com` 认证
- `git ls-remote` 远端访问

## 应急控制

- 立即停自动发布：
  - 创建 `options-monitor-prod/disable_autodeploy.flag`
- 立即停定时监控：
  - `openclaw cron disable 9cba60f7-407b-4427-9120-0a176b818de9`

## 维护脚本（手动）

运行产物清理：

```bash
cd /home/node/.openclaw/workspace/options-monitor

# 预览（dry-run）
.venv/bin/python scripts/cleanup_runtime_artifacts.py --keep-days 7

# 执行删除（仅 output_runs）
.venv/bin/python scripts/cleanup_runtime_artifacts.py --keep-days 7 --apply
```

辅助诊断工具位于 `scripts/tools/`。
