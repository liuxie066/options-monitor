# Options Monitor Runbook

运维文档只覆盖：日常巡检、值班排障、应急操作。

## 文档边界

- 快速上手与常用命令：`README.md`
- Linux / Mac 服务化部署：`DEPLOY.md` / `docs/DEPLOY_LINUX_MAC.md`
- 配置来源与同步：`CONFIGS.md`
- option positions 状态检查与错账修复：`docs/OPTION_POSITIONS_REPAIR.md`
- 发布/回滚：`docs/RELEASE_PROCESS.md`

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
REPO_ROOT="$HOME/apps/options-monitor/current"
RUNTIME_ROOT="/var/lib/options-monitor"

cd "$REPO_ROOT"
./om run tick-cron \
  --market us \
  --config "$RUNTIME_ROOT/config.us.json" \
  --accounts lx sy \
  --timeout 600
```

- 运行入口配置：`config.us.json`（US）/ `config.hk.json`（HK）
- 统一 tick 的账户报告写入
  `output_runs/<run_id>/accounts/<account>/`；`output_accounts/<account>/`
  保存账户级稳定状态/投影，`output_shared/` 保存共享状态
- `repo_root` 是代码 release/current symlink；`runtime_root` 保存配置、状态和报告，不要把两者混成同一路径
- 服务化部署时，所有运行产物应位于 `runtime_root`，而不是 release 目录；详见 `DEPLOY.md`
- `tick-cron` 是正式定时入口，负责锁、超时和触发证据；`run tick` 只用于明确的人工运行

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
| `./om run tick --config ...` | 是 | 可能 | 否 | 人工扫描入口；普通 Tick 通知只由 cron trigger 发送 |
| `./om run tick-cron --market ... --config ...` | 是 | 可能 | 是 | 正式定时扫描/通知入口 |
| `./om run trade-intake --config ... --mode apply --yes` | 是 | 否 | 是 | 会写 canonical ledger、intake 状态；启用 `holdings_sync` 时异步触发 PM 绝对持仓同步，并默认发送入账回执 |
| `./om run trade-intake --config ... --reconcile-state --dry-run` | 否 | 否 | 否 | 默认逐一核对所有启用账户的独立 state；可用 `--account lx` 缩小范围 |
| `./om run trade-intake --config ... --reconcile-state --account lx --apply` | 是 | 否 | 否 | 只更新指定账户 intake state；输出对应 backup path，执行前必须先看 dry-run |
| `./om option-positions auto-close-expired --config ... --apply --yes` | 是 | 否 | 是 | 专用过期自动平仓入口；先跑 `--dry-run`；需要静默时加 `--no-send` |

判断原则：
- 只想确认配置或状态时，优先 `config_validate` / `healthcheck` / `runtime_status`
- 只要命令会写本地、写远端或发通知，就不要把它当成“只读检查”来使用

trade-intake 的每账户运行目录同时保存 durable inbox 与 backfill checkpoint。push
回调在进入解析器前先落 inbox；backfill 从上次成功窗口继续并保留一小时重叠，因此
服务中断超过配置的快速 lookback 后不会直接形成永久盲区。

## 定时任务（systemd / launchd）

Linux / Mac 部署使用 `./om service render` 生成 systemd / launchd 服务；OpenClaw cron/readiness/profile 路径已退役，不再作为推荐运行面。

生产 service profile 固定为 `<runtime_root>/service.profile.json`。以 `/var/lib/options-monitor` 为例：

```bash
./om service status --profile-path /var/lib/options-monitor/service.profile.json --include-service-status
./om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
```

“过期自动平仓维护”使用 `service render` 生成的独立 timer，不借用 tick。人工预览与明确执行分别为：

```bash
./om option-positions auto-close-expired \
  --config /var/lib/options-monitor/config.hk.json \
  --accounts lx sy \
  --dry-run

./om option-positions auto-close-expired \
  --config /var/lib/options-monitor/config.hk.json \
  --accounts lx sy \
  --apply \
  --yes
```

专用入口会写入 `output_runs/<run_id>/accounts/<account>/state/expired_position_maintenance.json` 和 `output_shared/state/auto_close_expired.json`；回执按账户、券商、业务日和平仓记录生成 `receipt_key`，同一天已确认发送的回执不会因为人工重跑或 cron 重试而重复发送，未确认回执会按 `option_positions.auto_close.receipt.retry_unconfirmed` 重试。

线上定时执行入口：

```bash
./om run tick-cron --market us --config /var/lib/options-monitor/config.us.json --accounts lx sy --timeout 600
./om run tick-cron --market hk --config /var/lib/options-monitor/config.hk.json --accounts lx sy --timeout 600
```

已删除：`scripts/send_if_needed.py`。已删除：`scripts/send_if_needed_multi.py`。传一个账户就是单账户运行，传多个账户就是多账户运行；两者最终进入同一条 `multi_account_tick.run_tick` 链路。共享扫描数据可复用，通知与失败按账户隔离。

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
./om daily-brief latest --account lx --market US
```

Daily Brief 是普通调度通知的权威读取面。`symbols_notification.txt` 仅保留作兼容报告，
不能证明通知已发送。本次运行的账户报告和 run-scoped state 位于
`output_runs/<run_id>/accounts/<account>/`；账户级稳定状态/当前投影位于
`output_accounts/<account>/`，跨账户共享状态位于 `output_shared/`。

## 高频故障处理

### OpenD 不可用 / 登录失效

1. 先确认 OpenD 进程与端口。
2. 检查 `<runtime_root>/output_shared/state/opend_metrics.json` 是否连续失败。
3. 恢复后手动触发一次 cron run 观察 `last_run.json`。

### 字段缺失 / 源不可用

1. 不要硬跑 pipeline。
2. 先打印缺失字段并确认数据源是否支持。
3. 必要时切换到人工核验流程。

### “非交易时段：不监控”误判

1. 确认 `tick-cron --market us|hk` 与传入的 `config.us.json` / `config.hk.json` 一致。
2. 检查 runtime config 中的市场、时区与 scheduler run points。
3. 用 `scheduler_status` 只读检查账户当前调度判断。

## 应急控制

- 立即停定时监控：
  - systemd: `systemctl stop 'options-monitor*.timer'`
  - launchd: `launchctl bootout gui/$UID ~/Library/LaunchAgents/com.options-monitor.*.plist`

## 维护入口（手动）

运行产物清理：

```bash
REPO_ROOT="$HOME/apps/options-monitor/current"
cd "$REPO_ROOT"

# 预览（dry-run）
./om service cleanup \
  --repo-root "$REPO_ROOT" \
  --runtime-root /var/lib/options-monitor \
  --cleanup-output-runs \
  --output-runs-keep-days 14 \
  --output-runs-keep-count 200 \
  --cleanup-runtime-logs \
  --runtime-logs-keep-days 14

# 执行删除
./om service cleanup \
  --repo-root "$REPO_ROOT" \
  --runtime-root /var/lib/options-monitor \
  --cleanup-output-runs \
  --output-runs-keep-days 14 \
  --output-runs-keep-count 200 \
  --cleanup-runtime-logs \
  --runtime-logs-keep-days 14 \
  --confirm
```

辅助诊断优先使用 `./om-agent` 和 `./om` 的只读入口。
