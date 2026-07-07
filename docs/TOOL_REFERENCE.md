# Tool Reference

这份文档只回答两件事：

1. `om-agent` Tool Gateway 目前有哪些公开工具
2. 它们和人工 CLI `om` 的关系是什么

如果你只想跑产品，先看根目录 [README.md](../README.md)。

术语和架构边界统一维护在
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md)。Tool Gateway 与
Inbound Assistant 的能力边界、LLM 暴露面和验证方式统一维护在
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md)。这里不再复制能力地图。

---

## 1. 两套入口的区别

| 入口 | 面向对象 | 典型用途 |
|---|---|---|
| `om` | 人工操作 | 手动跑 pipeline、分阶段运行、命令行查询 |
| `om-agent` | 程序 / 外部 agent / Tool Gateway | JSON manifest、结构化 tool 调用 |

安装版默认提供全局 `om` / `om-agent` wrapper。源码目录内的 `./om` / `./om-agent` 是 fallback。

一句话：

- `om` 是人类 CLI
- `om-agent` 是 Tool Gateway，不是 OM 自己的 Agent

---

## 2. 如何查看工具清单

```bash
om-agent spec
```

它会输出当前环境下可用的工具 manifest。

注意：
- `spec` 不是绝对静态的
- 某些默认值会受环境变量影响，例如写工具门禁

---

## 3. 常见调用方式

```bash
om-agent run --tool <tool-name> --input-json '<json>'
```

也支持：

```bash
om-agent run --tool <tool-name> --input-file payload.json
```

`--input-file` 会覆盖 `--input-json`。

线上诊断时可以显式指定生产环境变量文件：

```bash
om-agent run --tool runtime_status --env-file /etc/options-monitor/options-monitor.env --input-json '{"config_key":"us"}'
```

---

## 4. Tool 与 `om` CLI 的关系

有些能力同时存在于：

- `om-agent` 的 tool
- `om` 的命令行入口

但名字不一定一样。

### 常见映射

| `om-agent` tool | `om` / CLI 对应能力 |
|---|---|
| `healthcheck` | `om doctor` / `om healthcheck` |
| `version_check` | `om version` |
| `version_update` | Tool Gateway-only local `VERSION` update helper |
| `config_validate` | `om config validate` |
| runtime config read | `om config get` |
| `scheduler_status` | `om scheduler` 的只读判定部分 |
| `scan_opportunities` | `om scan` / `om scan-pipeline` |
| `candidate_rank_explain` | Tool Gateway-only read existing candidate CSV ranking explanations |
| `preview_notification` | `om notify preview` |
| `notification_perception_read` | Tool Gateway-only read notification perception audit events |
| `runtime_status` | `om status` or raw assistant/runtime artifact summary |
| `runtime_runs` | `om runs` |
| `runtime_logs` | `om logs` |
| `assistant_trace` | `om assistant` inbound audit/session trace diagnostic |
| Research / Shadow Replay | `om research collect` / `om research shadow-replay ...` (not an `om-agent` tool) |
| `get_close_advice` | `om close-advice` |
| `query_cash_headroom` | `om sell-put-cash` / `src.application.cash_headroom_query::query_sell_put_cash(...)` |
| `monthly_income_report` | `om option-positions report monthly-income` |
| `option_positions_read` | `src.application.ledger.read_model` / `src.application.positions.inspection` 的只读部分 |

说明：
- `om-agent` 更适合给程序调
- `om` 更适合人工操作
- `om-agent` 的 CLI 由 `src/interfaces/agent/cli.py` 维护；工具实现和 manifest metadata 归属 `src/application/agent_tools/<domain>.py`，其中较重的历史实现位于同目录 `*_impl.py`；`src/application/agent_tool_registry.py` 只负责收集、去重和输出 manifest；根层 `src/application/agent_tool_*.py` 除 config / contract / registry helper 外只保留兼容 re-export；写入门禁归属 `src/application/agent_tools/permissions.py`；runtime config helper 由 `src/application/agent_tool_config.py` / `src/application/agent_tool_init_local.py` 维护。

配置优先级和 `config_validate` / `healthcheck` / `runtime_status` 的正式边界，请以根目录 `CONFIGURATION_GUIDE.md` 为准。这里只保留工具说明，不再重复完整配置规则。

### 远程消息入口

`om assistant handle` 是飞书、微信、Hermes 等消息入口调用 OM 的受控 Inbound 入口：

```bash
om assistant handle --text '/income <account> <YYYY-MM>' --sender ou_xxx --channel feishu --message-id msg_xxx
om inbound feishu --input-file feishu_event.json --format text
om inbound feishu-ws --check
om assistant capabilities
om assistant llm-check
om assistant model catalog
om assistant model list
om assistant model current
om assistant model check --active
```

它不是 `om-agent` manifest 里的工具，也不是 shell bridge。`inbound feishu`
只解析 Feishu 事件 payload，然后进入同一条 sender allowlist、message_id
幂等、SQLite audit 和工具白名单路径。Inbound command facade 默认开启；
当前 `assistant` config 只保留模型/profile 诊断和 legacy 兼容字段；自由问答执行
已禁用，不会触发工具调用、planner 或普通 LLM fallback。当前可见和可执行能力用
`om assistant capabilities` 查看；术语边界以
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md) 为准，能力边界以
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md) 为准。完整远程控制契约见
[INBOUND_CONTROL.md](INBOUND_CONTROL.md)。

### Tick 入口关系

`om-agent` 当前不提供“直接发送通知”的 tool。实时 tick / 扫描 / 通知运行使用人工 CLI：

```bash
om run tick --config config.us.json --accounts lx
om run tick --config config.us.json --accounts lx sy
```

这是一条统一链路，单账户只是传一个账户的特例。人工执行可直接调用
`om run tick`；生产 cron 使用带锁和 timeout 诊断的包装入口：

```bash
om run tick-cron --market hk --accounts lx sy --timeout 600
om run tick-cron --market us --accounts lx sy --timeout 600
om run tick-cron --market us --symbols PDD --timeout 600 --dry-run-command
```

`tick-cron` 会按 market 推导 canonical config、lock path 和 `OM_TRIGGER_*`
诊断环境变量；`--dry-run-command` 可只查看将执行的 tick 命令。带 `--symbols`
的单标运行会收窄扫描范围，并强制 `--no-send`。返回码语义：
`SKIP_LOCKED` 返回 `0`，表示上一轮还在跑；真实执行失败返回原始非零码并输出
`EXEC_FAILED_RC_<rc>`；超时返回 `124` 并输出 `EXEC_TIMEOUT_RC_124`。

正式 cron 前建议先跑：

```bash
om config validate --source yaml --market hk
om config validate --source yaml --market us
om config validate --config-path config.hk.json --market hk
om config validate --config-path config.us.json --market us
```

`tick-cron` 也会在真实 tick 前检查 runtime config 的 `_generated` 指纹；当 `config.yaml` 更新后未重新 `config build --source yaml`，会以 `[CONFIG_ERROR]` 失败并打印 YAML 重建命令。`--allow-stale-config` 只用于临时应急。

### Setup 入口关系

首次安装后先跑只读 setup 诊断：

```bash
om setup check
om setup check --no-local-env-file
```

`setup check` 只读，不写配置、不写 env-file、不启动服务、不创建 timer、不连接 OpenD/Feishu。它汇总安装、settings、runtime config、runtime root 和 service/timer 观察结果，并给出下一步命令。

推荐 authoring 入口是 `config.yaml`：

```bash
om config init --output config.yaml --runtime-output-dir .
om config validate --source yaml --market us
om config build --source yaml --market us --output config.us.json
om config symbol set --config-yaml config.yaml --market hk --symbol 09898 --covered-call-enabled true --covered-call-min-strike 85 --sell-put-enabled false
```

`config init` 默认写 `config.yaml` 并生成 `config.us.json` / `config.hk.json`；`--dry-run` 只预览 YAML，`--force` 才覆盖已有 starter/runtime 文件。
`config build` / `config explain` 读取 YAML authoring config。
`config symbol set` 默认 dry-run；`--apply` 写入 `config.yaml` 并生成备份，`--rebuild-runtime-root <dir>` 可在同一步重建 `config.us.json` / `config.hk.json` / `resolved/config.assistant.json`。

写入命令的语义统一为：默认只读或 dry-run；`--apply` 允许本地文件/状态写入；`--confirm` 允许交易事件、Feishu、服务变更这类高风险写入；`--yes` 用于非交互脚本，等价显式确认并在输出里带 `audit_id`。结构化输出统一包含 `dry_run`、`write_applied`、`backup_path`、`audit_id`、`rollback_hint`。

共享 multiplier cache 可以显式 seed，默认 dry-run，属于本地状态写入，写入使用 `--apply`：

```bash
om multiplier-cache seed --runtime-root /var/lib/options-monitor --symbol 0883.HK --multiplier 1000
om multiplier-cache seed --runtime-root /var/lib/options-monitor --symbol 0883.HK --multiplier 1000 --apply
```

manual trade / trade-intake 会优先使用 runtime root 或 runtime config 路径推导出的
`output_shared/state/multiplier_cache.json`，cache miss 时再按场景实时向 OpenD 刷新；
`trade-intake --mode apply` 会写交易事件并可能发送回执，必须同时带 `--confirm` 或非交互用的 `--yes`。
当入站成交是股票卖出（`option_type=null`, `side=sell`）且能唯一匹配开放的 Sell Put
assigned-stock lot 时，trade-intake 会返回 `action=assigned_stock_sale`，只写本地
`assigned_stock_events`，并用券商 `deal_id` 作为 `source_deal_id` 幂等。没有匹配
assigned-stock lot 的普通股票卖出仍返回 `skipped/not_option_deal`；多个候选 lot 或
数量/时间无法安全匹配时返回 unresolved，需要人工指定目标 lot。多个候选 lot 的回执会展示
候选项并标记为待确认，确认前不会自动写入。
重放已进入 `failed_deal_ids` 的单笔成交时，需要使用显式修复入口：
`om run trade-intake --config config.us.json --mode apply --confirm --deal-json <payload.json> --retry-failed`。
该入口只允许配合 `--deal-json` 使用，不会放开已成功处理成交的重复写入。
对已经进入 `waiting_settlement_evidence` 的 0 价期权生命周期腿，如果人工确认没有股票交割腿、
属于到期未指派/未行权，先用受控入口写 canonical `expire_close`；默认 dry-run，写入必须显式确认：

```bash
om option-positions lifecycle confirm-expired --deal-id <deal-id>
om option-positions lifecycle confirm-expired --deal-id <deal-id> --confirm
```

如果随后 trade-intake state 仍残留该 deal，再执行 `--reconcile-state` 清理本地 intake 状态。
如果账本已经通过手工 repair/expire_close 订正，但 trade-intake state 仍残留
`failed_deal_ids` / `unresolved_deal_ids`，先 dry-run 对账，再显式应用本地 state 修复：

```bash
om run trade-intake --config config.us.json --reconcile-state --deal-id <deal-id>
om run trade-intake --config config.us.json --reconcile-state --deal-id <deal-id> --apply
```

`--reconcile-state` 只读取账本和 audit 证据，只写 `auto_trade_intake_state.json`，
不会写 `trade_events` / `position_lots`。

### Service 入口关系

Linux / Mac 长期运行建议先渲染服务文件，再由系统服务管理器安装：

```bash
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --include-opend \
  --opend-root /home/liuxie/apps/futu-opend/current \
  --include-feishu-ws \
  --output-dir /tmp/options-monitor-service

om service render \
  --target launchd \
  --runtime-root "$HOME/Library/Application Support/options-monitor" \
  --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$HOME/Library/Application Support/options-monitor/config.yaml" \
  --config-us "$HOME/Library/Application Support/options-monitor/config.us.json" \
  --config-hk "$HOME/Library/Application Support/options-monitor/config.hk.json" \
  --output-dir /tmp/options-monitor-service

om service preflight --runtime-root /var/lib/options-monitor --env-file /etc/options-monitor/options-monitor.env --config-us config.us.json --config-hk config.hk.json --accounts lx sy
om settings doctor
```

只读检查：

```bash
om service status --profile-path /var/lib/options-monitor/service.profile.json --include-service-status
om service drift --runtime-root /var/lib/options-monitor
om channel status --runtime-root /var/lib/options-monitor --profile-path /var/lib/options-monitor/service.profile.json --env-file /etc/options-monitor/options-monitor.env
om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
```

`service render` 只生成 service/timer/plist/profile 文件和安装命令，不会自动安装或启动。systemd 的 `--env-file` 会写入 `EnvironmentFile=...`；launchd 的 `--env-file` 会写入 `EnvironmentVariables.OM_ENV_FILE=...`，两者都用于加载本机 Feishu 凭证环境变量。systemd unit 始终写入 `OM_RUNTIME_ROOT`；只有显式传 `--deploy-user` 或设置 `OM_DEPLOY_USER` / `DEPLOY_USER` 时，才会写入 `User=<deploy_user>` 和默认 `HOME=/home/<deploy_user>`。HOME 不在默认位置时用 `--deploy-home` 覆盖。启用 `--include-opend` 时会额外生成 `options-monitor-opend.service` / `com.options-monitor.opend.plist`，默认读取 `<deploy_home>/apps/futu-opend/current/FutuOpenD`，也可以用 `--opend-root` 和 `--opend-executable` 指定；trade-intake 的 systemd unit 会声明 `After/Wants=options-monitor-opend.service`。启用 `--include-feishu-ws` 时会额外生成 `options-monitor-feishu-ws.service` / `com.options-monitor.feishu-ws.plist`，通过飞书长连接接收消息，不需要公网 HTTPS callback、反向代理或 tunnel，并会使用 runtime locks 目录下的 `feishu-ws.lock` 防止多实例抢同一个 App。启用 `--include-wechat-clawbot` 时会额外生成 `options-monitor-wechat-clawbot.service` / `com.options-monitor.wechat-clawbot.plist`，通过 ClawBot 轮询接收微信消息，并要求在 `config.yaml` 的 `inbound.wechat_clawbot.allowed_senders` 或 render 参数 `--wechat-clawbot-allowed-senders` 中显式声明 allowlist，没有通配默认值；YAML 来源的 allowlist 不会在 profile 中重复明文保存。传入 `--config-yaml` 后，Feishu WS 和 WeChat ClawBot service 会同时拿到 runtime config 的 `--config-path` 和 `$RUNTIME/resolved/config.assistant.json` 的 `--assistant-config`。启用 `--include-auto-upgrade` 时，`--repo-root` 会保留传入的 symlink 路径，默认 config path 会使用 runtime root 下的 `config.us.json` / `config.hk.json`，避免生产配置随 release 目录漂移。传入 `--config-yaml` 后，profile 会记录 YAML authoring source；后续 `update apply` 会用 `config build --source yaml --config-yaml <path>` 重建 runtime config，并用 `config build-assistant --source yaml --config-yaml <path>` 重建 assistant config。`service preflight` 是只读部署前检查。`healthcheck` 和 `runtime_status` 也会暴露同一份 `channel_health` 只读摘要。

WeChat ClawBot 的聊天回复和主动通知使用不同上下文。`serve` 对当前入站消息的回复使用该消息携带的 `context_token`；tick、trade receipt 和维护告警的主动通知使用 `notifications.target` 指向的持久化 binding。若 run 的 `tick_metrics.notify_failures` 出现 `SEND_UNCONFIRMED`，且 `provider_response_code=-2` / `stdout_tail={"ret": -2}`，优先判断为主动通知 binding 上下文不可用；不要把“聊天能回复”误判为主动通知 target 仍然健康。只读诊断顺序：

```bash
om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
om channel status --runtime-root /var/lib/options-monitor --profile-path /var/lib/options-monitor/service.profile.json
cat /var/lib/options-monitor/output_runs/<run_id>/state/tick_metrics.json
```

恢复优先级是让允许名单内用户发送一条普通聊天消息；poller 收到该消息后会先刷新当前 `wechat_clawbot` 通知路由指向的既有 binding，不依赖同条消息的回复是否成功。实现必须只刷新配置里的既有 target，不创建新 target，不补发历史通知，并在 binding 状态中留下 `refreshed_from_inbound_at_utc`、`last_inbound_message_id` 等审计字段；若同条消息回复成功，还会补充 `refreshed_from_reply_at_utc`、`reply_message_id`，方便区分入站刷新和回复确认。重新扫码绑定只是备用恢复方案。

`service drift` 会用当前 release 的 `render_service_bundle()` 重新生成期望 service/timer，再和 `$RUNTIME/service.profile.json` 以及 systemd unit 文件对比。默认只读；带 `--confirm` 或 `--yes` 时只写入缺失 unit/profile、执行 `systemctl daemon-reload`，并 `enable --now` 缺失 timer，不会自动启用或重启新增长期 service。`runtime_status` 同样会暴露 service drift 摘要；缺失 `options-monitor-projection-verify.timer` 这类维护 timer 会作为 warning/error 返回。

`om update verify --repo-root <current> --runtime-root <runtime>` 是发布/升级后的 compact 只读复核入口，汇总 current symlink、版本、runtime config freshness、事件源配置、最近 upgrade status 和长期 service health。`--no-check-latest` 可跳过 git tag 查询，用于已经确认 release 发布成功后的远端快速收口。

systemd 的 US/HK tick timer 使用 market timezone 的 `OnCalendar` 在 10 分钟整数边界唤醒：US 为 `Mon..Fri *-*-* 09..16:00/10:00 America/New_York`，HK 为 `Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong`。业务 run point 是否执行仍由 `tick-cron` scheduler 决定。

渲染出的长期服务包含每天北京时间 09:00 执行的 expired auto-close，以及每天北京时间 09:30 执行的 option-position projection verify。auto-close 会以非交互方式运行 `auto-close-expired --apply --yes --quiet`；projection verify 使用 `om option-positions --data-config <runtime_root>/portfolio.runtime.json verify-projection --mode auto`。systemd 分别使用 `OnCalendar=*-*-* 09:00:00 Asia/Shanghai` 和 `OnCalendar=*-*-* 09:30:00 Asia/Shanghai`；launchd 使用本机时区的 `Hour=9, Minute=0` 和 `Hour=9, Minute=30`。

自动升级是显式 opt-in：`om service render --include-auto-upgrade ...` 会额外生成每天北京时间 06:10 的 upgrade timer。升级命令默认 dry-run；只有 `om update apply --confirm` 才会用本机 `_cache/git/options-monitor.git` 增量 fetch 并 archive 出 release、准备新 release `.venv`、安装 runtime/server 依赖、校验新 release、切换 `current` symlink、补齐当前 release 新增的缺失 timer/unit，并按 reconcile 后的 profile 重启长期服务。`OM_UPGRADE_CACHE_ROOT` / `--cache-root` 可覆盖默认 `_cache/`；依赖下载缓存复用 `_cache/uv` 和 `_cache/pip`。release 目录不保留 `.git`，升级检查和下一次升级会在当前 release 没有 git remote 时从 git cache 读取 remote 与 release tags。uv 只作为宿主机工具检测和使用，升级不会自动安装 uv，uv 模式使用 `uv venv --python python3 .venv`。systemd 使用非 root `User=` 运行时，profile 会使用 `sudo -n systemctl restart ...` 重启 `options-monitor-opend.service`、`options-monitor-trade-intake.service`、已渲染的 `options-monitor-feishu-ws.service` 和 `options-monitor-wechat-clawbot.service`，需要部署用户具备对应 NOPASSWD sudoers；重启后会检查长期服务 `is-active` / `is-enabled`，并对 Feishu WS 执行 `om inbound feishu-ws --check`，对 WeChat ClawBot 执行 `om channel wechat-clawbot serve --check`。release/config 已切换但服务重启、reconcile 或 health check 失败时，upgrade status 会记录 `upgraded_restart_failed`、`upgraded_service_reconcile_failed` 或 `upgraded_service_health_failed` 以及 remediation。`om update rollback` 同样默认 dry-run。升级公开入口统一为 `om update check/apply/rollback`。

---

## 5. 当前公开工具列表

## 5.1 `doctor` / `healthcheck`

用途：
- 校验 runtime config
- 检查账户路径
- 检查 OpenD / SQLite / 通知前置条件
- 可选检查候选证据文件是否具备诊断所需的候选、trace/reject 样本

示例：

```bash
om doctor --config-key us
om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
om doctor --config-key us \
  --candidate-path output_shared/reports/sell_put_candidates.csv \
  --candidate-trace-path output_shared/reports/candidate_filter_trace.jsonl \
  --candidate-evidence-min-sample 5
```

### Support Bundle

用途：
- 生成给维护者排查问题的脱敏 JSON 诊断包
- 汇总 setup/settings/config/runtime status 快照
- 默认不连接 OpenD；需要 OpenD readiness 时显式加 `--include-healthcheck`

示例：

```bash
om support bundle --config-key us
om support bundle --config-key us --include-healthcheck
om support bundle --config-key us --env-file /etc/options-monitor/options-monitor.env --output-dir /tmp/options-monitor-support
```

输出文件会脱敏 secret、token、webhook URL 和长数字账号。该命令只写 support bundle 文件，不修改配置、env-file、服务或业务状态。

---

## 5.2 `version_check`

用途：
- 检查本地 `VERSION` 与 git 远端发布 tag
- 不运行监控流程

示例：

```bash
om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
```

---

## 5.2.1 `version_update`

用途：
- 预览或更新本地 `VERSION`
- 默认 dry-run；写入需要 `apply=true`、`confirm=true` 和 `OM_AGENT_ENABLE_WRITE_TOOLS=true`
- 不创建 git tag、不 commit、不 push、不运行发布流程

示例：

```bash
om-agent run --tool version_update --input-json '{"bump":"patch"}'
OM_AGENT_ENABLE_WRITE_TOOLS=true om-agent run --tool version_update --input-json '{"target_version":"1.2.3","apply":true,"confirm":true}'
```

`apply=true` 是本地写入动作，还需要 `confirm=true` 和
`OM_AGENT_ENABLE_WRITE_TOOLS=true`。固定频率任务只应使用 dry-run 预览或版本检查。

---

## 5.3 `config_validate`

用途：
- 只校验 runtime config
- 不检查 OpenD
- 不运行 pipeline

示例：

```bash
om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

---

## 5.4 `scheduler_status`

用途：
- 读取现有 scheduler state
- 返回当前调度判定、下次运行时间、是否处于通知窗口
- 不执行 `run-if-due`
- 不写 `mark-scanned` / `mark-notified`

示例：

```bash
om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
```

---

## 5.5 `scan_opportunities`

用途：
- 跑扫描流程
- 返回候选摘要

示例：

```bash
om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":3}'
```

---

## 5.5.1 `candidate_rank_explain`

用途：
- 读取已有候选 CSV
- 返回 Top N 的排序分数、分数组件、输入指标、主要排序原因和风险提示
- 可用 `compare_baseline=true` 对比“收益率优先”的基线排序

示例：

```bash
om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
om-agent run --tool candidate_rank_explain --input-json '{"run_id":"20260515T182459Z-474761","account":"lx","mode":"put","top_n":5}'
om-agent run --tool candidate_rank_explain --input-json '{"candidate_path":"output_shared/reports/sell_call_candidates.csv","mode":"call","top_n":5}'
om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","score_weights":{"liquidity":0.02},"compare_baseline":true}'
```

注意：
- 该工具只读本地 CSV，不重新扫描、不发通知、不写 Feishu、不写报告。
- 默认先找 `output_shared/reports`，再找 `output_shared/agent_tools/reports`；也可传 `run_id`、`run_dir`、`account`、`report_dir`、`output_dir` 或 `candidate_path`。
- `score_weights` 只影响本次解释输出，不修改配置，也不改变生产排序默认值。

---

## 5.5.2 `candidate_filter_explain`

用途：
- 解释单个标的为什么被候选过滤、后置过滤、接受，或为什么缺少 trace 证据
- 支持 canonical symbol、中文名、Futu code 和 alias，例如 `9992.HK` 或 `泡泡玛特`
- `account` 只是扫描/run 范围，不是标的身份或业务归属

默认 trace 发现：
- 显式 `trace_path` / `trace_paths` 优先。
- 否则从 `runtime_root`、`config_path` 所在目录、`OM_RUNTIME_ROOT`、service profile
  和 repo root 推导 runtime roots。
- 未传 `run_id` 时，先读 `output_shared/state/last_run_dir.txt`，再扫描最近
  `output_runs/*/accounts/*/candidate_filter_trace.jsonl`，最后回退到旧的
  `output_shared/reports` 和 `output_shared/agent_tools/reports`。
- `candidate_filter_diagnostics` view 使用同一套 trace 发现逻辑；窄工具用于单标的
  问答，view 用于聚合、对比、趋势和跨 run/account/rule 分析。

示例：

```bash
om-agent run --tool candidate_filter_explain --input-json '{"config_key":"hk","symbol":"泡泡玛特"}'
om-agent run --tool candidate_filter_explain --input-json '{"run_id":"20260515T182459Z-474761","account":"sy","symbol":"9992.HK"}'
om-agent run --tool candidate_filter_explain --input-json '{"trace_path":"output_shared/reports/candidate_filter_trace.jsonl","symbol":"NVDA"}'
```

注意：
- 该工具只读已有 trace，不重新扫描、不发通知、不写报告。
- 当 trace 文件缺失或没有匹配行时，只能报告诊断缺失，不能推断确定过滤原因。

---

## 5.5.3 Offline Shadow Replay Evidence

用途：
- 通过 `research collect --scope candidate` 从已有候选 CSV、reject log 和 `candidate_filter_trace.jsonl` 收集离线 shadow replay readiness
- 输出在 `candidate_evidence.shadow_replay`，用于检查 accepted/rejected universe 是否完整
- `research shadow-replay status` / `list` 是只读 dataset readiness dashboard，会列出每个 dataset 的样本、mark、outcome 覆盖、采样新鲜度、路径采样点数量和下一步建议；`data_plan` 只包含 `collect_marks` / `settle`，`review_queue` 只提示可人工 `analyze` 的 dataset
- `research shadow-replay run-data-plan` 消费 `status.data_plan`；默认 dry-run 且不写 receipt，显式 `--write` 才执行本地采样 / settle 并写本地 receipt；人工复盘仍走 `analyze`
- `research shadow-replay collect-marks` 是数据采样入口，可从本地 required-data cache 或显式 OpenD 当前报价追加 mark path
- `research shadow-replay mark` 可从 `required_data/parsed/*_required_data.csv` 为本地 dataset 生成 mark path
- `research shadow-replay settle` 可从可用 mark 推导 mark-to-market outcome，也可在到期日/到期后用 spot/strike 推导到期 outcome
- `research shadow-replay analyze` 会在已有可用 mark path / outcome facts 时输出路径风险、outcome stats、review_readiness 和按 DTE/Delta/IV/Spread/集中度分桶的 outcome-by-bucket 表现
- `research shadow-replay candidate-impact` 用历史 run artifacts 或已有 dataset 做 `insurance_underwriting` 候选影响对比，比较 production observed 与阈值 variants 会新增/移除哪些候选，不重建历史期权链、不修改 runtime config
- `research shadow-replay candidate-impact-report` 是用户常用报告入口，会一次写出 JSON + Markdown 候选影响报告；参数仍来自 `--params` 或 `--params-dir`，不会自动生成参数、不修改 runtime config
- `research archive pull/verify/inventory/build-datasets/prune-remote` 是远端空间有限时的证据归档链路：先把远端 runtime 证据 rsync 到本地 `output_shared/research/remote_archive/<remote>/`，再从 verified archive 生成 Shadow Replay dataset
- 缺少被拒样本、mark path 或 outcome facts 时返回 `not_ready` / `evidence_incomplete`，防止幸存者偏差
- 只输出人工复盘证据和候选影响对比，不自动改配置

示例：

```bash
om research archive pull --remote prod --ssh-target deploy@example --since-days 7
om research archive pull --remote prod --ssh-target deploy@example --since-days 7 --write
om research archive pull --remote prod --ssh-target deploy@example --require-replay-evidence --write
om research archive verify --remote prod
om research archive build-datasets --remote prod --market us --write
om research archive prune-remote --remote prod --ssh-target deploy@example --keep-days 3 --keep-count 30
om research collect --config-key us --scope candidate --run-id 20260515T182459Z-474761 --output json --no-write-outputs --shadow-replay-min-sample 30
om research collect --config-key us --scope candidate --candidate-report-dir output_shared/reports --output json --no-write-outputs
om research collect --config-key us --scope candidate --candidate-path candidates.csv --trace-path candidate_filter_trace.jsonl --mark-path mark_path_snapshots.jsonl --outcome-path outcome_facts.jsonl --output json --no-write-outputs
om research shadow-replay build --run-id 20260515T182459Z-474761 --dataset-id us-20260515
om research shadow-replay build --profile-path /var/lib/options-monitor/service.profile.json --latest-scanned-run --dataset-id us-20260515
om research shadow-replay build --runs-root /var/lib/options-monitor/output_runs --run-id 20260515T182459Z-474761 --dataset-root /var/lib/options-monitor/output_shared/research/shadow_replay/datasets --dataset-id us-20260515
om research shadow-replay status --min-sample 30 --min-mark-points 2 --mark-stale-hours 24
om research shadow-replay status --profile-path /var/lib/options-monitor/service.profile.json --min-sample 30 --min-mark-points 2 --mark-stale-hours 24
om research shadow-replay run-data-plan --min-sample 30 --min-mark-points 2
om research shadow-replay run-data-plan --profile-path /var/lib/options-monitor/service.profile.json --min-sample 30 --min-mark-points 2
om research shadow-replay run-data-plan --min-sample 30 --min-mark-points 2 --source local --write
om research shadow-replay run-data-plan --min-sample 30 --min-mark-points 2 --source opend --write --max-datasets 3
om research shadow-replay candidate-impact --profile-path /var/lib/options-monitor/service.profile.json --start-date 2026-06-01 --end-date 2026-06-02 --account lx --market hk --params params.json --min-sample 30
om research shadow-replay candidate-impact-report --runtime-root /var/lib/options-monitor --start-date 2026-06-03 --end-date 2026-06-03 --account lx --account sy --market us --params-dir /var/lib/options-monitor/output_shared/research/shadow_replay/backtests/<params-dir> --min-sample 30
om research shadow-replay candidate-impact --dataset output_shared/research/shadow_replay/datasets/us-20260515 --params params.json --format markdown --output candidate-impact.md
om research shadow-replay collect-marks --dataset output_shared/research/shadow_replay/datasets/us-20260515 --source local --required-data-root output_shared/required_data --write
om research shadow-replay collect-marks --dataset output_shared/research/shadow_replay/datasets/us-20260515 --source opend --required-data-root output_shared/required_data --opend-host 127.0.0.1 --opend-port 11111 --write
om research shadow-replay mark --dataset output_shared/research/shadow_replay/datasets/us-20260515 --required-data-root output_shared/required_data --write
om research shadow-replay settle --dataset output_shared/research/shadow_replay/datasets/us-20260515 --write
om research shadow-replay analyze --dataset output_shared/research/shadow_replay/datasets/us-20260515 --min-sample 30
```

边界：
- 只读源数据，来源可以是 `run_id`、`run_dir`、`candidate_report_dir`、`candidate_path`、`trace_path`、`reject_log_path`、`mark_path` 或 `outcome_path`。
- `--profile-path` / `--runtime-root` 只用于解析 runtime `output_runs`、dataset root、required-data root 和 receipt root；`--latest-scanned-run` 只选择最新已有 replay 证据的 run，不运行扫描。
- `research archive pull` 默认 dry-run；`--write` 只写本地归档和 manifest，不删除远端文件。`--require-replay-evidence` 只拉含候选 CSV、reject log 或 `candidate_filter_trace.jsonl` 的 run，跳过 scheduler skip / tick 心跳目录。`prune-remote --confirm` 会先解析远端 cleanup dry-run 计划，并要求每个待删 run 都已经在本地 `inventory.latest.json` 中 verified。
- `research archive build-datasets --market us|hk --write` 会按归档 run 的候选/reject 文件名推断市场并过滤样本；不传 `--market` 才会保留所有市场。dataset build 默认会从归档 run 自带的 `required_data/parsed` 生成第一批 scan-time `mark_path_snapshots.jsonl`；这不是最终 outcome，后续仍需 path mark / expiry mark 后再 `settle --write`。用 `--no-mark-from-run-required-data` 可关闭这个本地 mark 步骤。
- 默认 `--no-write-outputs` 不写文件；显式 `--write-outputs --confirm` 时只写本地 research bundle/handoff。
- `research shadow-replay status` / `list` 只读本地 dataset root，不采样、不 settle、不写输出文件；`next_suggested_action` 只会是 `collect_marks`、`settle`、`analyze` 或 `wait`。输出里的 `data_plan` 只是数据维护建议命令清单，不会自动执行；人工复盘提示在 `review_queue`。
- `research shadow-replay run-data-plan` 不挂 tick / tick-cron；默认不写，`--write` 只写 replay dataset、local receipt，以及在显式 `--source opend` 时写本地 required-data / OpenD cache。它不执行 `analyze`，不写 Feishu、不写 broker、不写 trade state、不写 runtime config、不发送通知。
- `research shadow-replay collect-marks --source local` 只读本地 required-data cache；`--source opend --write` 会读取 OpenD、刷新本地 required-data cache，并维护本地 OpenD 限流状态和 option-chain cache；不带 `--write` 时使用临时目录预览，不持久化这些文件。OpenD 只能提供当前采样点，不能恢复过去没有保存的 option mark。
- `research shadow-replay mark --write` 只写本地 replay dataset 的 `mark_path_snapshots.jsonl`，缺报价记录为 `missing_quote`，不视为可用 mark；到期 spot-only mark 可作为到期结算证据。
- `research shadow-replay settle --write` 只写本地 replay dataset 的 `outcome_facts.jsonl`，可输出 `expired_worthless`、`assigned_at_expiry`、`called_away_at_expiry` 或 mark-to-market outcome，不写交易状态。
- 不运行扫描、不发通知、不写 Feishu、不写交易状态、不写 runtime config。
- 样本、被拒样本、mark path 或 outcome facts 不足时不会产生可落地配置。

---

## 5.6 `query_cash_headroom`

用途：
- Tool Gateway 查询 Sell Put 现金占用与余量的标准入口
- 包装 `src.application.cash_headroom_query` 的 `query_sell_put_cash(...)`
- 返回账户现金、Sell Put 担保占用、剩余可用现金
- 支持按账户筛选，并按可用汇率折算到 CNY

示例：

```bash
om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"sy"}'
```

注意：
- Tool Gateway payload 使用 `broker` 表示券商口径；未传时读取 runtime config 的 `portfolio.broker`
- Tool Gateway 工具输入统一使用 `broker`
- 该工具不会发送通知或写 Feishu；它会把查询产物写到本地 agent 输出目录

---

## 5.7 `monthly_income_report`

用途：
- 读取本地 option positions
- 返回月度期权收益的三类统计口径
- 默认只返回 summary；`include_rows=true` 时返回资金流、实现收益、开仓归因明细

核心字段：
- `net_cashflow_gross`：资金流口径，按交易发生月统计；short 开仓收款为正，
  long 开仓成本和平仓买回支出为负，long 平仓卖出为正。
- `realized_pnl_gross`：已实现口径，按平仓/到期月统计；short 为开仓权利金减平仓成本，
  long 为平仓卖出减开仓成本。
- `open_basis_lifecycle_pnl_gross`：开仓归因口径，按开仓月回填生命周期收益，
  公式为：
  `sell_open_premium - sell_close_cost_actual - enhancement_call_buy_cost + enhancement_call_sell_proceeds_actual`。
- `yield_enhancement_realized_pnl_gross`：收益增强 call 腿按实现口径统计，
  只有带 `yield_enhancement` / `enhancement_call` 标记的 long call 平仓收益进入该字段。
- `premium_received_gross`：short 开仓收到的权利金。
- `realized_gross`：平仓/到期实现收益，和 `realized_pnl_gross` 同口径。
- `return_summary`：按 `month + account` 输出账户级收益率摘要，不按币种拆行。
  分母为当前 open position lots 的 `cash_secured_amount` 折 CNY 后合计，
  字段包括 `cash_secured_by_ccy`、`cash_secured_cny`、`net_income_cny`、
  `premium_income_cny`、`net_return_rate`、`premium_return_rate`、
  `annualized_*_return_rate` 和 `annualized_basis_days`。
  `net_income_*` 会排除 Sell Put assignment 形成的正股交割本金现金流；
  这类本金流出仍保留在 `summary.net_cashflow_gross` 和
  `assignment_stock_net_cashflow_gross` 中。
  `return_basis=current_cash_secured` 表示这不是账户总资产收益率。
  如果缺少汇率，相关 CNY 和收益率字段为 `null`，并在 `warnings` 中说明。
- `combined_return_summary`：当未指定 `account` 时按 `month` 输出全账户合并收益摘要。
  合并收益率按 `sum(net_income_cny) / sum(cash_secured_cny)` 计算，不平均各账户收益率。
- `diagnostics`：按 `month + account` 输出收益统计诊断，包括匹配到的
  `trade_events`、position lots、已平仓行、premium 行、现金担保可用性和
  `missing_fields`。入站 `收益` 命令会用它解释“暂无可计算收益”或数据不完整的原因。
- `include_rows=true` 时，若存在 Sell Put assignment 的 `stock_settlement`，
  额外返回 `stock_settlement_rows`、`assigned_stock_lots`、
  `assignment_lifecycle_rows`、`assigned_stock_sale_rows` 和
  `assigned_stock_review_rows`。其中 `assignment_lifecycle_pnl` 是组合口径：
  option premium attribution + assigned stock realized/unrealized PnL；正股成本
  `stock_cost_per_share` 仍按真实交割价记录，不扣除权利金。缺 spot 时
  `quote_status=missing_quote`，浮盈亏和 lifecycle PnL 为 `null`。

示例：

```bash
om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx","month":"2026-04"}'
```

---

## 5.8 `option_positions_read`

用途：
- `action=list`：读取 position lots
- `action=events`：读取 canonical trade events
- `action=history`：读取单个 lot 的事件链
- `action=inspect`：读取投影诊断状态
- `action=assigned-stock`：读取 Sell Put 被指派后形成的正股 lot、已记录 sale events、
  lifecycle PnL 和 review rows。默认只消费本地账本和可选 `quote_snapshots`，
  不主动连接 OpenD；只有显式传 `refresh_quotes=true` 时，才通过 OpenD
  获取开放 assigned-stock lot 的实时 spot，并返回 `quote_refresh` 诊断。
  指定 `as_of_ms` 的历史查询不会使用实时 spot 回填，只会使用传入的
  `quote_snapshots` 或返回 `missing_quote`。
- Inbound 用户入口：`/assigned-stock [lx|sy|all] [symbol] [open|partially_sold|closed|all]`。
  自然语言的“指派正股持仓盈亏 / 被指派正股浮盈亏 / assigned stock PnL”也应路由到
  `action=assigned-stock`。当前持仓盈亏默认使用 `refresh_quotes=true` 获取实时
  spot；若取价失败，响应必须展示 `quote_refresh` / `quote_status`，不能编造浮盈亏。
- 口径：`assigned_stock_unrealized_pnl` 和 `assigned_stock_realized_pnl` 是正股自身
  PnL，不含 Sell Put 权利金；`assignment_lifecycle_pnl` 才包含权利金归因。
  `stock_cost_per_share` 按真实交割价记录，不扣除权利金。
- 用户回执：自然语言入口由 Agent Composer 基于工具证据生成简洁摘要，并由系统追加
  deterministic 数据来源/口径；deterministic assigned-stock renderer 作为 LLM 不可用
  或 answer guard 不通过时的 fallback。正常 `fresh` 报价不逐行重复展示；缺价或异常
  quote 状态必须在回答、fallback 明细、`检查提示` 或 provenance 中显式出现。

示例：

```bash
om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"history","record_id":"rec_xxx"}'
om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"assigned-stock","account":"lx"}'
om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"assigned-stock","account":"lx","refresh_quotes":true}'
om option-positions assigned-stock-sale --target-stock-lot-id assigned-stock-assign_xxx --shares 100 --price 105 --trade-time-ms 1780000000000 --dry-run
```

注意：
- 这个工具只开放读和诊断动作
- `add` / `buy-close` / `void-event` / `adjust-lot` / `rebuild` 不在此工具中开放
- 被指派正股卖出的事实来源可以是 trade-intake 的 broker stock sell 自动匹配，
  也可以是 `om option-positions assigned-stock-sale` 的人工确认写入；两者都写
  `assigned_stock_events`，不写 canonical option `trade_events`

---

## 5.9 `analysis_catalog` / `analysis_query`

用途：
- Inbound Assistant 可使用的通用只读分析工作区。
- `analysis_catalog` 返回可查询 view、字段说明、行粒度、聚合策略、join
  策略、示例和只读 SQL 边界。
- `analysis_query` 在内存 SQLite 中执行单条 SELECT/CTE，只能读取白名单 view，
  用于对比、排名、趋势、组成、分组、差额、收益率差、排障等开放式问题。
- 这是通用工具，不是收益专用 API。view 覆盖收益、现金流、已实现明细、
  指派正股生命周期、option exposure、trade events、策略配置、候选过滤诊断、
  close advice、runtime 状态和 quote freshness。

关键约束：
- 只允许单条 `SELECT` 或 `WITH ... SELECT`。
- 禁止 `INSERT`、`UPDATE`、`DELETE`、DDL、`ATTACH`、`DETACH`、`PRAGMA`、
  `VACUUM` 等非只读操作。
- SQLite authorizer 会拒绝非白名单表读取。
- SQL 函数走白名单，例如 `sum`、`avg`、`count`、`min`、`max`、`round`、
  `coalesce`、`substr`、`lower`、`upper` 等；不允许 `load_extension` 等越界函数。
- 工具会限制用户可见输出行数，并返回 `truncated=true|false`；内部读取源数据时使用
  更高的物化上限，避免把分析源数据误截断到展示上限。
- `analysis_query` 只懒加载 SQL 实际引用的 view；`select 1` 不会加载业务源。
- 输出 schema 为 `analysis.query.output.v2`，保留旧字段兼容；新增
  `query_explain`、`preflight`、`evidence.coverage`、`evidence.freshness`、
  `evidence.aggregation_policy`、`evidence.diagnostics` 等证据。
- 输出包含 `columns`、`rows`、`cell_refs`、`views_used`、`source_label` 和
  `fallback_text`。`cell_refs` 和 `evidence` 是只读查询结果的结构化证据。
- 当前 Inbound Assistant 不会自动为开放式自然语言调用 `analysis_query` 或合成答案；
  这些 view 仍作为 Tool Gateway / 显式命令 / 未来任务系统的只读证据基础。
- 对诊断 view，`evidence.diagnostics` 会区分 observed rejection、
  no matching rows、diagnostic missing、empty artifact、read error、runtime
  skip/failure、quote freshness gap 等状态；缺失或无匹配诊断不能被回答成
  “没有问题”或确定 root cause。

语义 view：
- P0 收益/指派正股：
  `account_monthly_performance`、`account_monthly_income_components`、
  `assigned_stock_position_pnl`、`assigned_stock_sale_events`
- P1 exposure/归因/配置：
  `open_option_exposure`、`expiration_risk_buckets`、
  `symbol_income_attribution`、`strategy_config_by_symbol_account`
- P2 诊断：
  `candidate_filter_diagnostics`、`close_advice_snapshot`、
  `runtime_tick_status`、`quote_freshness`
- 兼容 view：
  `monthly_income_summary`、`monthly_income_return_summary`、
  `monthly_income_combined_return_summary`、`monthly_income_cashflow_rows`、
  `monthly_income_realized_rows`、`monthly_income_premium_rows`、
  `assigned_stock_lifecycle`、`assigned_stock_sales`、`assigned_stock_review`、
  `position_lots`、`trade_events`、`symbol_strategy_config`

P2 诊断 view 读取已有本地 artifact 或只读状态面。缺失 artifact 时返回 warning
和空结果，不启动 broker、OpenD、cron 或其他生产服务。

当前约束：
- 显式工具调用仍必须遵守 SELECT-only、白名单 view、只读 artifact 读取和数据新鲜度
  边界。
- 自由问答重建前，不新增硬编码自然语言触发、业务模板或隐式 follow-up 规则。
- 当 `analysis_query` preflight 返回 `UNKNOWN_COLUMN` / `UNKNOWN_VIEW` 且包含
  catalog 建议时，Agent 可以用建议字段或建议 view 做一次只读修复查询；原失败
  observation 会保留在 trace，正常回执不展示内部 SQL 修复细节。
- 当 follow-up 判定账户、月份、标的、market 或 run 范围无法安全推断时，
  Agent 会以 `ask_clarification` 停止并向用户提出一个简短问题。
- follow-up 只能使用 `analysis_catalog` / `analysis_query`，且必须命中 evidence gap
  建议的 view；不允许借 follow-up 扩大到写工具或生产操作。
- follow-up 决策会以 `om-agent-loop-followup-decision-v1` 写入
  `assistant.tool_loop.followup_decisions` 和
  `AgentSession.answer_trace.followup_decisions`，记录
  `call_tool`、`stop_with_gap`、rejected duplicate 等状态。

示例：

```bash
om-agent run --tool analysis_catalog --input-json '{"config_key":"us"}'
om-agent run --tool analysis_query --input-json '{"config_key":"us","sql":"select month, account, net_income_cny, net_return_rate from account_monthly_performance order by month, account","limit":50}'
om-agent run --tool analysis_query --input-json '{"config_key":"us","sql":"select month, account, symbol, component, amount_cny from symbol_income_attribution order by month, account, amount_cny desc","limit":50}'
```

---

## 5.10 `get_portfolio_context`

用途：
- 获取账户持仓 / 现金 context

示例：

```bash
om-agent run --tool get_portfolio_context --input-json '{"config_key":"us","account":"lx"}'
```

---

## 5.11 `prepare_close_advice_inputs`

用途：
- 预先刷新 close advice 依赖的本地输入

通常与 `close_advice` 搭配使用。

---

## 5.11 `close_advice`

用途：
- 基于本地 position lots、required data、quotes 和 lot 策略快照构建平仓建议
- 输出 deterministic exit state：`profit_capture`、`risk_exit`、`take_profit`、`salvage`、`let_expire`、`hold`、`not_evaluable`
- 收益增强腿会输出专用动作：`close_put_keep_call`、`hold_put_keep_call`、`sell_call_take_profit`、`hold_call_as_convexity` 等
- `not_evaluable` 行会进入待补数据链路，不会被当成已定价建议
- `optimizer_switch` 是 advisory-only redeploy 建议，必须携带 `alternative_symbol` / `alternative_contract_symbol` / `alternative_source_path` 等替代候选证据；没有候选报告证据时不会产生换仓建议

示例：

```bash
om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

---

## 5.12 `get_close_advice`

用途：
- 一次性执行 close advice 推荐路径
- 推荐给 Tool Gateway 调用方使用；内部会准备输入并运行 `close_advice`

示例：

```bash
om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

这是更推荐的 Tool Gateway 入口。

---

## 5.12.1 `close_advice_read`

用途：
- 只读已有 `close_advice.csv`，按 account/symbol/option type/side/strike/expiration 过滤平仓建议行
- 给 Inbound 的 `position_exit_analysis` 使用，例如“分析 long call 是不是应该平仓”
- 不刷新行情、不连接 OpenD、不重新生成 close advice、不写报告

示例：

```bash
om-agent run --tool close_advice_read --input-json '{"config_key":"us","query":{"option_type":"call","side":"long"}}'
om-agent run --tool close_advice_read --input-json '{"config_key":"hk","run_id":"<run-id>","query":{"symbol":"9992.HK","option_type":"call","side":"long"}}'
```

如果没有找到已有报告，会返回 `DEPENDENCY_MISSING`；这时应先运行扫描或显式生成 close advice，再查询。

---

## 5.13 `manage_symbols`

用途：
- 读取或修改 `symbols[]`

示例：

```bash
om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

注意：
- `list` 永远是只读
- 真正写操作需要：
  - `OM_AGENT_ENABLE_WRITE_TOOLS=true`
  - `confirm=true`

---

## 5.14 `preview_notification`

用途：
- 只生成通知内容，不发送

示例：

```bash
om-agent run --tool preview_notification --input-json '{"alerts_path":"output_shared/reports/symbols_alerts.txt","changes_path":"output_shared/reports/symbols_changes.txt","account_label":"lx"}'
```

---

## 5.15 `runtime_status`

用途：
- 只读汇总现有 runtime / service 输出文件
- 暴露 `config_authority`，用于确认 `config.yaml` 到 `config.<market>.json` 的生成来源、sha256、身份和 freshness
- 不运行 pipeline
- 不发送通知
- 可读取 payload 里的 `profile_path` 作为 service profile 路径、账户和 freshness 阈值
- service profile 会提供 `service_provider`、`repo_root`、`runtime_root`、
  `config_paths` 和 `services` 摘要
- 可读取可选的外层任务上下文，例如 `trigger_source`、`trigger_job_id`、
  `delivery.mode` / `delivery_mode`、`timeoutSeconds`，用于区分“代码没有发送”
  和“外层任务没有 announce”

示例：

```bash
om status --config-key us
om status --config-key us --json
om runs --limit 10
om runs --run-id 20260515T182459Z-474761
om logs --run-id 20260515T182459Z-474761 --kind tool --lines 20
om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
om-agent run --tool runtime_runs --input-json '{"limit":10}'
om-agent run --tool runtime_logs --input-json '{"run_id":"20260515T182459Z-474761","kind":"tool","lines":20}'
```

---

## 5.15.1 `assistant_trace`

用途：
- 只读 Inbound Assistant 的 SQLite `agent_sessions` trace
- 诊断 `./om assistant` 如何选择能力、收集证据、判断进度、请求澄清或停在权限预览
- 读取 `capability_selection`、`progress`、`progress.blocked_by`、
  `answer.clarification_request` 等派生 session 字段
- `response_text` 是给操作者看的 compact trace，会隐藏原始内部细节；JSON 结果保留
  结构化诊断字段，例如 blocker 的 `tool_name`

注意：
- `assistant_trace` 通过 `om-agent` Tool Gateway 读取，但它诊断的是
  `./om assistant` session；这不表示 `om-agent` 自身是 Assistant 或 Planner
- 它不创建 session schema，不执行 planner，不补跑工具，也不修改 pending operation

示例：

```bash
om-agent run --tool assistant_trace --input-json '{"limit":10}'
om-agent run --tool assistant_trace --input-json '{"command_id":"<command-id>","include_snapshot":true}'
```

---

## 5.15.2 `notification_perception_read`

用途：
- 只读 tick audit 里的 `assistant_perception` 通知感知事件
- 诊断最近一次通知准备、no-account 早退、delivery decision、quiet-hours skip 或发送完成摘要
- 返回压缩事件：`run_id`、账户/标的摘要、delivery action/reason、消息长度和 sha256
- 不返回原始通知正文、webhook、token 或 credentials
- 不运行 tick，不发送通知，不创建 Assistant session

示例：

```bash
om-agent run --tool notification_perception_read --input-json '{"limit":10}'
om-agent run --tool notification_perception_read --input-json '{"run_id":"20260515T182459Z-474761"}'
om-agent run --tool notification_perception_read --input-json '{"conversation_id":"wechat:<chat_key>","limit":3}'
```

---

## 5.16 Research / Shadow Replay / Strategy Lab

产品分层：Research 是证据基础设施，Shadow Replay 是反事实复盘引擎，Strategy Lab 是策略进化产品入口。Strategy Lab 会按 strategy domain adapter 区分 Sell Put、Covered Call 和 Combo Yield；统一的是证据和实验 workflow，分开的是决策单元、目标函数、可调参数、硬约束和 proposal target。Combo Yield 必须按 group-level decision instance 评估，不能被拆成独立单腿参数实验。当前 Strategy Lab 已提供 update、readiness、experiment、proposal 和 llm-context 入口；`update` 默认 dry-run，显式 `--build-dataset --write` 时从 latest scanned run 构建本地 replay dataset，显式 `--write` 时才执行本地 mark / settle data-plan。完整模块设计和安全边界见 [STRATEGY_LAB_DESIGN.md](STRATEGY_LAB_DESIGN.md)。

用途：
- 收集线上运行证据，生成给 MacBook Codex 阅读的 redacted bundle / handoff
- 诊断 runtime 质量、账本质量、多账户策略影响和策略证据完整性
- 内嵌与 `om runs` / `om logs` 同源的 run 列表和 audit tail 摘要
- 可选嵌入 `healthcheck` snapshot，但不取代 `healthcheck` 的 readiness 职责
- Shadow Replay 基于已有候选、reject、trace、mark、outcome 和归档 run 证据做离线复盘
- `review_readiness` 判断是否具备人工策略复盘条件；`candidate-impact` / `candidate-impact-report` 比较显式阈值 variants 对候选集合的影响
- Strategy Lab update 默认 dry-run 汇总 Shadow Replay status / data-plan；显式 `--build-dataset --write` 才从 latest scanned run 构建本地 replay dataset，显式 `--write` 才执行本地 collect / settle 维护动作
- Strategy Lab readiness 把 replay dataset 归一成 `decision_instance`，按 Sell Put / Covered Call / Combo Yield 输出 domain readiness 和 blocker
- Strategy Lab experiment 自动生成 Sell Put / Covered Call 受控 hypotheses，复用 candidate-impact evaluator，并输出 observed-universe scorecard；Combo Yield 输出独立的 group-level observed-universe experiment
- Strategy Lab proposal 从 experiment artifact 生成 advisory-only proposal 和 Markdown；Sell Put / Covered Call 只有 `closed_replay` gate 通过才输出 dry-run patch，Combo Yield 只输出 group advisory，不应用生产配置
- Strategy Lab llm-context 从 experiment / proposal artifact 生成脱敏本地 LLM 上下文，不调用在线 AI，不应用 patch
- `service render --include-strategy-lab-recorder` 是远端持续记录证据的 opt-in 部署入口，会生成 latest-run dataset build、mark sampler 和 outcome settler timers
- Combo Yield 第一版输出 group evidence readiness、组合级 variants 和 group scorecard，不输出单腿化参数 patch
- 默认不写文件、不调用在线 AI、不发送通知
- 这是独立离线侧线，不是 `om-agent` manifest tool

示例：

```bash
om research collect --config-key us --scope full --output both --no-write-outputs
om research collect --config-key us --scope candidate --run-id 20260515T182459Z-474761 --output json --no-write-outputs --shadow-replay-min-sample 30
om research shadow-replay status --min-sample 30
om research shadow-replay candidate-impact-report --params params.json --market us --start-date 2026-06-03 --account lx --min-sample 30
om research shadow-replay analyze --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30
om research strategy-lab update --latest
om research strategy-lab update --latest --build-dataset --write
om research strategy-lab readiness --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30
om research strategy-lab readiness --market us --account lx --start-date 2026-06-03 --end-date 2026-06-03 --min-sample 30
om research strategy-lab experiment --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30 --auto
om research strategy-lab experiment --market us --account lx --start-date 2026-06-03 --end-date 2026-06-03 --min-sample 30 --auto
om research strategy-lab proposal --experiment output_shared/research/strategy_lab/experiment.json --markdown-output output_shared/research/strategy_lab/proposal.md
om research strategy-lab llm-context --experiment output_shared/research/strategy_lab/experiment.json --proposal output_shared/research/strategy_lab/proposal.json --output output_shared/research/strategy_lab/llm_context.json
om service render --target systemd --include-strategy-lab-recorder --strategy-lab-recorder-source opend
```

带线上调度证据：

```bash
om research collect --config-key us --scope full --output both --no-write-outputs \
  --scheduler-evidence-json '{"provider":"cron","job_name":"us-tick","last_run_id":"20260518T095446Z-2e7d54","last_triggered_at":"2026-05-18T09:54:46Z","last_status":"success","last_exit_code":0}'
```

Scope：
- `ledger`：交易入账、持仓维护和账本质量
- `candidate`：多账户候选、排名样本、filter trace 和 shadow replay readiness
- `quality`：runtime freshness、最新 run、调度证据和可选 healthcheck
- `full`：默认全量证据

写 Research 报告需要：
- `--write-outputs`
- `--confirm`

Shadow Replay 的 dataset build、mark、settle、run-data-plan 和 archive pull /
build-datasets 使用各自命令的 `--write`。这些写入只限本地 research/replay
artifact，不能修改 runtime config、通知、Feishu、ledger/trade state 或
broker-facing data。

Strategy Lab recorder 是同一条本地 artifact 写入路径的服务化封装：

- latest-run build timer 默认用 scanned run id 作为 dataset id；目标 dataset 已存在时跳过，避免覆盖后续 mark path。
- mark sampler timer 默认每 2 小时维护最近 dataset，`--strategy-lab-recorder-source opend` 需要可用 OpenD。
- outcome settler timer 默认每天北京时间 07:20 维护 `outcome_facts.jsonl`。
- recorder opt-in 会写入 `service.profile.json.strategy_lab_recorder`，让 upgrade/service drift reconcile 保留这些 timer；默认 `service render` 不启用。

默认写入位置：

```text
output_shared/research/
output_shared/state/current/research.current.json
output_shared/research/shadow_replay/
output_shared/research/strategy_lab/
```

注意：
- 它是证据打包工具，不是线上 AI 推理功能。
- `scheduler_evidence` 来自线上调度系统；尽量提供 `last_run_id` 和 `last_triggered_at`，否则本地 runtime 文件不能完整证明线上 cron 是否按时触发。
- `include_healthcheck=true` 只在 `quality` / `full` scope 下有意义。
- `--shadow-replay-min-sample` 只影响 Research bundle 里的 `candidate_evidence.shadow_replay` 样本充足性判断，不会改生产策略参数。
- Candidate-impact 报告只能说明 observed run universe 内候选集合变化，不能自动生成最优参数，也不能修改 runtime config、交易状态或通知。
- `strategy-lab update` 默认 dry-run；显式 `--build-dataset --write` 时只从 latest scanned run 构建本地 replay dataset，显式 `--write` 时只执行已有 Shadow Replay `collect_marks` / `settle` data-plan 和本地 receipt，不会执行 analyze、生成参数建议或修改生产状态。
- 未显式传 `--dataset-id` 时，`strategy-lab update --latest --build-dataset --write` 默认使用 latest scanned run id 作为 dataset id；同名 dataset 已存在时返回 `dataset_build_reason=dataset_already_exists` 并跳过构建。
- `strategy-lab readiness` 支持已有 dataset 输入，也支持通过 `--start-date` / `--end-date` / `--market` / `--account` 聚合 scanned-run window；显式 `--output` 只写本地 JSON artifact，不会采样 mark、settle outcome 或生成生产参数 patch。
- `strategy-lab experiment` 支持已有 dataset 输入，也支持通过 `--start-date` / `--end-date` / `--market` / `--account` 聚合 scanned-run window；它生成的 scorecard 只用于人工复盘，不是生产参数建议；Combo Yield 结果位于 `group_experiments.combo_yield`。
- `strategy-lab proposal` 接收 experiment JSON 文件或包含 `experiment.json` 的目录；显式 `--output` / `--markdown-output` 只写本地 artifact，不会应用 patch。`filter_only` / `path_only` 只返回 evidence gap；Combo Yield 结果通过 `group_advisory` 表达，不生成单腿 patch。
- `strategy-lab llm-context` 接收 experiment JSON、proposal JSON 或对应目录；显式 `--output` 只写本地脱敏 JSON，不会调用在线 AI，不会应用 dry-run patch。
- Sell Put / Covered Call 可以在第一阶段复用单腿 candidate-impact；Covered Call 缺少持仓覆盖或 cost-basis 证据时不能输出生产参数建议。
- Combo Yield 必须有 `strategy_group_id`、`leg_role`、同组 legs 和组合 metrics 才能进入组合 optimizer；证据不足时只输出 blocker 和下一步数据需求。

---

## 6. 人工 CLI：事件源探针

`om event-source probe` 是只读事件风险数据源探针。它不写 runtime state、不发送通知、不运行扫描。

```bash
om event-source probe --provider futu --symbols NVDA 0700.HK --host 127.0.0.1 --port 11111
om event-source probe --provider yfinance --symbols NVDA
om event-source probe --provider all --symbols NVDA 0700.HK
om event-source probe --provider all --symbols NVDA 0700.HK --summary-only
```

Futu/OpenD provider 读取财报、分红和拆合股事件，需要 `futu-api>=10.6.6608` 以及可用 OpenD quote 连接。
`--provider all` 会并行展示 Futu 与 yfinance 的只读探针结果，便于比较数据源可用性；它不会写事件缓存。`--summary-only` 会省略原始事件 payload，只保留 provider health、计数和错误码，适合远端发布验证。

线上事件预取由 `runtime.event_risk_source` 控制。单源配置仍兼容：

```yaml
runtime:
  event_risk_source:
    provider: futu
    futu:
      host: 127.0.0.1
      port: 11111
```

多源 fallback 配置示例：

```yaml
runtime:
  event_risk_source:
    mode: primary_fallback
    default_provider: futu
    providers:
      futu:
        enabled: true
        role: primary
        host: 127.0.0.1
        port: 11111
      yfinance:
        enabled: true
        role: fallback
    market_rules:
      hk:
        chain: [futu]
      us:
        chain: [futu, yfinance]
```

扫描和 close-advice 只消费 resolved event snapshot；数据源选择、fallback、provider cooldown 和 stale cache 都在事件源子系统内完成。

---

## 7. 人工 CLI：版本检查

`om version` 仍然保留为人工 CLI 能力。Tool Gateway 调用方使用 `version_check`，二者读取同一个本地 `VERSION` 和远端 `v*` tags。

示例：

```bash
om version
```

---

## 8. 字段口径

### 工具输入
- `broker`

### 数据表字段
- `market`

Tool Gateway 工具输入统一使用 `broker`。数据表里的 `market` 字段不作为工具 payload 字段。

---

## 9. 相关文档

- 当前架构术语：[`OM_ASSISTANT_ARCHITECTURE.md`](OM_ASSISTANT_ARCHITECTURE.md)
- Tool Gateway 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 快速开始：[`GETTING_STARTED.md`](GETTING_STARTED.md)
- Tool Gateway 快速开始：[`AGENT_GETTING_STARTED.md`](AGENT_GETTING_STARTED.md)
- 配置说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)
