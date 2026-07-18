# Options Monitor Runbook

运维文档只覆盖：日常巡检、值班排障、应急操作。

## 文档边界

- 快速上手与常用命令：`README.md`
- Linux / Mac 服务化部署：`DEPLOY.md` / `docs/DEPLOY_LINUX_MAC.md`
- 配置来源与同步：`CONFIGS.md`
- option positions 状态检查与错账修复：`docs/OPTION_POSITIONS_REPAIR.md`
- 发布/回滚：仅在本地私有运维仓执行（本仓不公开流程细节）

## Option Positions 状态与修账入口

当前期权持仓模型固定为：

```text
trade_events -> projection -> position_lots
```

旧 `option_positions_v2`、Feishu `option_positions` 镜像、自动 bootstrap 都不是当前运行时事实源。本仓不再维护单独的一次性升级迁移文档。

只读排查优先用：

```bash
./om option-positions store inspect --config config.us.json
./om option-positions history --record-id <record_id>
./om option-positions events --account lx
```

需要修账时先看 `docs/OPTION_POSITIONS_REPAIR.md`。如果旧环境只剩 legacy SQLite / Feishu 历史表，先离线整理为 canonical `trade_events`，再进入 `rebuild` / `verify-projection`；不要把旧表重新接成当前运行时来源。

## 日常运行（prod）

```bash
cd /var/lib/options-monitor/current
./om run tick --config config.us.json --accounts lx sy
```

- 运行入口配置：`config.us.json`（US）/ `config.hk.json`（HK）
- 产出：`<report_dir>/symbols_*` 与每标的 `*_sell_put_* / *_sell_call_*`（默认 `report_dir=output_shared/reports`）
- 服务化部署时，所有运行产物应位于 `runtime_root`，而不是 repo 根目录；详见 `DEPLOY.md`

服务化环境的只读巡检：

```bash
./om service status --profile-path /var/lib/options-monitor/service.profile.json --include-service-status
./om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
```

## 命令副作用总表（先看这个）

| 命令 / 工具 | 写本地状态 | 写远端 | 发通知 | 备注 |
|---|---:|---:|---:|---|
| `./om-agent run --tool config_validate ...` | 否 | 否 | 否 | 只做纯配置语义校验 |
| `./om-agent run --tool healthcheck ...` | 否 | 否 | 否 | 检查 runtime readiness |
| `./om-agent run --tool runtime_status ...` | 否 | 否 | 否 | 只读汇总现有输出 |
| `./om run tick --config ... --no-send` | 是 | 可能 | 否 | 会写本地运行产物，但禁发通知 |
| `./om run tick --config ...` | 是 | 可能 | 是 | 正式扫描/通知入口 |
| `./.venv/bin/python -m src.application.trades.auto_intake --mode apply --yes` | 是 | 否 | 是 | 会写本地 option_positions / intake state/status，并默认发送入账回执 |
| `./om option-positions auto-close-expired --config ... --apply` | 是 | 否 | 是 | 专用过期自动平仓入口；先跑 `--dry-run`；需要静默时加 `--no-send` |

判断原则：
- 只想确认配置或状态时，优先 `config_validate` / `healthcheck` / `runtime_status`
- 只要命令会写本地、写远端或发通知，就不要把它当成“只读检查”来使用

## 定时任务（systemd / launchd）

Linux / Mac 部署使用 `./om service render` 生成 systemd / launchd 服务；OpenClaw cron/readiness/profile 路径已退役，不再作为推荐运行面。

生产 service profile 固定为 `$RUNTIME/service.profile.json`。只读状态优先看：

```bash
./om service status --profile-path "$RUNTIME/service.profile.json" --include-service-status
./om-agent run --tool runtime_status --input-json "{\"profile_path\":\"$RUNTIME/service.profile.json\"}"
```

“过期自动平仓维护”cron 应触发专用入口，不再借用 tick。例如每天 `00:10` 唤醒一次：

```bash
flock -n /tmp/om-auto-close-expired.lock bash -lc 'set -euo pipefail; cd "$REPO_ROOT"; timeout 600s ./om option-positions auto-close-expired --config "$RUNTIME/config.hk.json" --accounts lx sy --apply --quiet' || { rc=$?; if [ "$rc" -eq 1 ]; then echo SKIP_LOCKED; exit 0; else echo EXEC_FAILED_RC_$rc; exit $rc; fi; }
```

专用入口会写入 `output_runs/<run_id>/accounts/<account>/state/expired_position_maintenance.json` 和 `output_shared/state/auto_close_expired.json`；回执按账户、券商、业务日和平仓记录生成 `receipt_key`，同一天已确认发送的回执不会因为人工重跑或 cron 重试而重复发送，未确认回执会按 `option_positions.auto_close.receipt.retry_unconfirmed` 重试。

线上定时执行入口：`./om run tick --config config.us.json --accounts lx sy`

旧的 `scripts/send_if_needed.py` / `scripts/send_if_needed_multi.py` 兼容 wrapper 已删除；任何老定时任务都应直接调用 `./om run tick`。

统一 tick 手动/可选定时入口：

```bash
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy
```

传一个账户就是单账户运行，传多个账户就是多账户运行；二者使用同一条
`src.application.multi_account_tick.run_tick` 链路。统一 tick 会复用共享运行数据，
但通知按账户逐条发送到同一目标；每个账户一条消息，发送失败按账户隔离。

## 值班三步检查（先做这个）

Agent 优先使用只读入口：

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

如果生产路径不想每次手填，使用 service profile：

```bash
./om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
```

人工直接查看文件时，再用下面三步：

1. 查看是否在跑：

```bash
./om service status --profile-path /var/lib/options-monitor/service.profile.json --include-service-status
```

2. 查看上次运行结果（最重要）：

```bash
cat /var/lib/options-monitor/output_shared/state/last_run.json
```

3. 查看最新通知内容：

```bash
cat /var/lib/options-monitor/output_shared/reports/symbols_notification.txt
```

统一 tick 的账户级状态和报告位于 `output_accounts/<account>/`，共享运行状态位于 `output_runs/<run_id>/`。

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

## 应急控制

- 立即停定时监控：
  - systemd: `systemctl stop 'options-monitor*.timer'`
  - launchd: `launchctl bootout gui/$UID ~/Library/LaunchAgents/com.options-monitor.*.plist`

## 维护入口（手动）

运行产物清理：

```bash
cd ~/apps/options-monitor

# 预览（dry-run）
./om service cleanup \
  --repo-root ~/apps/options-monitor \
  --runtime-root /var/lib/options-monitor \
  --cleanup-output-runs \
  --output-runs-keep-days 14 \
  --output-runs-keep-count 200 \
  --cleanup-runtime-logs \
  --runtime-logs-keep-days 14

# 执行删除
./om service cleanup \
  --repo-root ~/apps/options-monitor \
  --runtime-root /var/lib/options-monitor \
  --cleanup-output-runs \
  --output-runs-keep-days 14 \
  --output-runs-keep-count 200 \
  --cleanup-runtime-logs \
  --runtime-logs-keep-days 14 \
  --confirm
```

辅助诊断工具位于 `scripts/tools/`。
