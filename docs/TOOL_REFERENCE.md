# Tool Reference

这份文档只回答两件事：

1. `om-agent` 目前有哪些公开工具
2. 它们和人工 CLI `om` 的关系是什么

如果你只想跑产品，先看根目录 [README.md](../README.md)。

---

## 1. 两套入口的区别

| 入口 | 面向对象 | 典型用途 |
|---|---|---|
| `om` | 人工操作 | 手动跑 pipeline、分阶段运行、命令行查询 |
| `om-agent` | 程序 / Agent | JSON manifest、结构化 tool 调用 |

安装版默认提供全局 `om` / `om-agent` wrapper。源码目录内的 `./om` / `./om-agent` 是 fallback。

一句话：

- `om` 是人类 CLI
- `om-agent` 是程序化工具入口

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
| `version_update` | Agent-only local `VERSION` update helper |
| `config_validate` | `om config validate` |
| runtime config read | `om config get` |
| `scheduler_status` | `om scheduler` 的只读判定部分 |
| `scan_opportunities` | `om scan` / `om scan-pipeline` |
| `candidate_rank_explain` | Agent-only read existing candidate CSV ranking explanations |
| `preview_notification` | `om notify preview` |
| `runtime_status` | `om status` or raw assistant/runtime artifact summary |
| `runtime_runs` | `om runs` |
| `runtime_logs` | `om logs` |
| `openclaw_readiness` | Agent-only OpenClaw readiness summary |
| `research` | `om research collect` |
| `get_close_advice` | `om close-advice` |
| `query_cash_headroom` | `om sell-put-cash` / `src.application.cash_headroom_query::query_sell_put_cash(...)` |
| `monthly_income_report` | `om option-positions report monthly-income` |
| `option_positions_read` | `src.application.ledger.read_model` / `src.application.positions.inspection` 的只读部分 |

说明：
- `om-agent` 更适合给程序调
- `om` 更适合人工操作
- `om-agent` 的 CLI 由 `src/interfaces/agent/cli.py` 维护；manifest 由 `src/application/agent_tool_registry.py` 维护，handler 由 `src/application/agent_tool_handlers.py` 维护，runtime config helper 由 `src/application/agent_tool_config.py` / `src/application/agent_tool_init_local.py` 维护。

配置优先级和 `config_validate` / `healthcheck` / `runtime_status` / `openclaw_readiness` 的正式边界，请以根目录 `CONFIGURATION_GUIDE.md` 为准。这里只保留工具说明，不再重复完整配置规则。

### 远程消息入口

`om assistant handle` 是飞书、微信、Hermes 等消息入口调用 OM 的受控入口：

```bash
om assistant handle --text '收益 <account> <YYYY-MM>' --sender ou_xxx --channel feishu --message-id msg_xxx
om inbound feishu --input-file feishu_event.json --format text
om inbound feishu-ws --check
om assistant capabilities
om assistant llm-check
om assistant model catalog
om assistant model list
om assistant model current
om assistant model check --active
```

它不是 `om-agent` manifest 里的工具，也不是 shell bridge。`inbound feishu` 只解析 Feishu 事件 payload，然后进入同一条 sender allowlist、message_id 幂等、SQLite audit 和工具白名单路径。Assistant command facade 默认开启；assistant config 可选择启用 LLM intent translation，但 LLM 只能从 `assistant capabilities` 暴露的只读可执行 capability 中产出结构化 intent，不能执行工具或改写事实输出。监控标的设置可用自然语言预览，例如 `设置 09898 covered call min strike 85`；确认监控后，如果 runtime JSON 带有 YAML 生成元数据，会写回 `config.yaml` 并重建同目录 runtime config。`assistant model` 只管理 `config.yaml` 里的 LLM model profile；聊天里对应 `/model`、`/model list`、`/model use <name>`，其中切换模型必须先 preview，再 `确认模型` 或 `/confirm model <operation_id>`。生成后的运行时 assistant config 仍然只包含一个 resolved `assistant.llm`。`inbound feishu-ws` 是长驻 Feishu App long-connection client：通过飞书 SDK 长连接接收消息、进入 assistant control，并使用同一个 Bot 自动回复；Assistant 由 `assistant.mode` 控制，reaction、reply、queue 行为配置在 assistant config 的 `inbound.feishu_ws` 下。完整边界见 [INBOUND_CONTROL.md](INBOUND_CONTROL.md)。

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
```

`tick-cron` 会按 market 推导 canonical config、lock path 和 `OM_TRIGGER_*`
诊断环境变量；`--dry-run-command` 可只查看将执行的 tick 命令。返回码语义：
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
重放已进入 `failed_deal_ids` 的单笔成交时，需要使用显式修复入口：
`om run trade-intake --config config.us.json --mode apply --confirm --deal-json <payload.json> --retry-failed`。
该入口只允许配合 `--deal-json` 使用，不会放开已成功处理成交的重复写入。
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
om-agent run --tool runtime_status --input-json '{"profile_path":"/var/lib/options-monitor/service.profile.json"}'
```

`service render` 只生成 service/timer/plist/profile 文件和安装命令，不会自动安装或启动。systemd 的 `--env-file` 会写入 `EnvironmentFile=...`；launchd 的 `--env-file` 会写入 `EnvironmentVariables.OM_ENV_FILE=...`，两者都用于加载本机 Feishu 凭证环境变量。systemd unit 始终写入 `OM_RUNTIME_ROOT`；只有显式传 `--deploy-user` 或设置 `OM_DEPLOY_USER` / `DEPLOY_USER` 时，才会写入 `User=<deploy_user>` 和默认 `HOME=/home/<deploy_user>`。HOME 不在默认位置时用 `--deploy-home` 覆盖。启用 `--include-feishu-ws` 时会额外生成 `options-monitor-feishu-ws.service` / `com.options-monitor.feishu-ws.plist`，通过飞书长连接接收消息，不需要公网 HTTPS callback、反向代理或 tunnel，并会使用 runtime locks 目录下的 `feishu-ws.lock` 防止多实例抢同一个 App。传入 `--config-yaml` 后，Feishu WS service 会同时拿到 runtime config 的 `--config-path` 和 `$RUNTIME/resolved/config.assistant.json` 的 `--assistant-config`。启用 `--include-auto-upgrade` 时，`--repo-root` 会保留传入的 symlink 路径，默认 config path 会使用 runtime root 下的 `config.us.json` / `config.hk.json`，避免生产配置随 release 目录漂移。传入 `--config-yaml` 后，profile 会记录 YAML authoring source；后续 `update apply` 会用 `config build --source yaml --config-yaml <path>` 重建 runtime config，并用 `config build-assistant --source yaml --config-yaml <path>` 重建 assistant config。`service preflight` 是只读部署前检查。

`service drift` 会用当前 release 的 `render_service_bundle()` 重新生成期望 service/timer，再和 `$RUNTIME/service.profile.json` 以及 systemd unit 文件对比。默认只读；带 `--confirm` 或 `--yes` 时只写入缺失 unit/profile、执行 `systemctl daemon-reload`，并 `enable --now` 缺失 timer，不会自动启用或重启新增长期 service。`runtime_status` 同样会暴露 service drift 摘要；缺失 `options-monitor-projection-verify.timer` 这类维护 timer 会作为 warning/error 返回。

`om update verify --repo-root <current> --runtime-root <runtime>` 是发布/升级后的 compact 只读复核入口，汇总 current symlink、版本、runtime config freshness、事件源配置、最近 upgrade status 和长期 service health。`--no-check-latest` 可跳过 git tag 查询，用于已经确认 release 发布成功后的远端快速收口。

systemd 的 US/HK tick timer 使用 market timezone 的 `OnCalendar` 在 10 分钟整数边界唤醒：US 为 `Mon..Fri *-*-* 09..16:00/10:00 America/New_York`，HK 为 `Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong`。业务 run point 是否执行仍由 `tick-cron` scheduler 决定。

渲染出的长期服务包含每天北京时间 05:30 执行的 expired auto-close，以及每天北京时间 06:00 执行的 option-position projection verify。auto-close 会以非交互方式运行 `auto-close-expired --apply --yes --quiet`；projection verify 使用 `om option-positions --data-config <runtime_root>/portfolio.runtime.json verify-projection --mode auto`。systemd 分别使用 `OnCalendar=*-*-* 05:30:00 Asia/Shanghai` 和 `OnCalendar=*-*-* 06:00:00 Asia/Shanghai`；launchd 使用本机时区的 `Hour=5, Minute=30` 和 `Hour=6, Minute=0`。

自动升级是显式 opt-in：`om service render --include-auto-upgrade ...` 会额外生成每天北京时间 06:10 的 upgrade timer。升级命令默认 dry-run；只有 `om update apply --confirm` 才会用本机 `_cache/git/options-monitor.git` 增量 fetch 并 archive 出 release、准备新 release `.venv`、安装 runtime/server 依赖、校验新 release、切换 `current` symlink、补齐当前 release 新增的缺失 timer/unit，并按 reconcile 后的 profile 重启长期服务。`OM_UPGRADE_CACHE_ROOT` / `--cache-root` 可覆盖默认 `_cache/`；依赖下载缓存复用 `_cache/uv` 和 `_cache/pip`。release 目录不保留 `.git`，升级检查和下一次升级会在当前 release 没有 git remote 时从 git cache 读取 remote 与 release tags。uv 只作为宿主机工具检测和使用，升级不会自动安装 uv，uv 模式使用 `uv venv --python python3 .venv`。systemd 使用非 root `User=` 运行时，profile 会使用 `sudo -n systemctl restart ...` 重启 `options-monitor-trade-intake.service` 和已渲染的 `options-monitor-feishu-ws.service`，需要部署用户具备对应 NOPASSWD sudoers；重启后会检查长期服务 `is-active` / `is-enabled`，并对 Feishu WS 执行 `om inbound feishu-ws --check`。release/config 已切换但服务重启、reconcile 或 health check 失败时，upgrade status 会记录 `upgraded_restart_failed`、`upgraded_service_reconcile_failed` 或 `upgraded_service_health_failed` 以及 remediation。`om update rollback` 同样默认 dry-run。升级公开入口统一为 `om update check/apply/rollback`。

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

## 5.5.2 Offline Shadow Replay Evidence

用途：
- 通过 `research collect --scope candidate` 从已有候选 CSV、reject log 和 `candidate_filter_trace.jsonl` 收集离线 shadow replay readiness
- 输出在 `candidate_evidence.shadow_replay`，用于检查 accepted/rejected universe 是否完整
- `research shadow-replay status` / `list` 是只读 dataset readiness dashboard，会列出每个 dataset 的样本、mark、outcome 覆盖、采样新鲜度、路径采样点数量和下一步建议；`data_plan` 只包含 `collect_marks` / `settle`，`review_queue` 只提示可人工 `analyze` 的 dataset
- `research shadow-replay run-data-plan` 消费 `status.data_plan`；默认 dry-run 且不写 receipt，显式 `--write` 才执行本地采样 / settle 并写本地 receipt；人工复盘仍走 `analyze`
- `research shadow-replay collect-marks` 是数据采样入口，可从本地 required-data cache 或显式 OpenD 当前报价追加 mark path
- `research shadow-replay mark` 可从 `required_data/parsed/*_required_data.csv` 为本地 dataset 生成 mark path
- `research shadow-replay settle` 可从可用 mark 推导 mark-to-market outcome，也可在到期日/到期后用 spot/strike 推导到期 outcome
- `research shadow-replay analyze` 会在已有可用 mark path / outcome facts 时输出路径风险、outcome stats 和按 DTE/Delta/IV/Spread/集中度分桶的 outcome-by-bucket 表现
- `research shadow-replay parameter-backtest` 用历史 run artifacts 或已有 dataset 做 short-vol 参数反事实回放，比较生产实际结果和 variants，不重建历史期权链、不修改 runtime config
- 缺少被拒样本、mark path 或 outcome facts 时返回 `not_ready` / `evidence_incomplete`，防止幸存者偏差
- 只输出人工评审用建议，不自动改配置

示例：

```bash
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
om research shadow-replay parameter-backtest --profile-path /var/lib/options-monitor/service.profile.json --start-date 2026-06-01 --end-date 2026-06-02 --account lx --market hk --params params.json --min-sample 30
om research shadow-replay parameter-backtest --dataset output_shared/research/shadow_replay/datasets/us-20260515 --params params.json --format markdown --output backtest.md
om research shadow-replay collect-marks --dataset output_shared/research/shadow_replay/datasets/us-20260515 --source local --required-data-root output_shared/required_data --write
om research shadow-replay collect-marks --dataset output_shared/research/shadow_replay/datasets/us-20260515 --source opend --required-data-root output_shared/required_data --opend-host 127.0.0.1 --opend-port 11111 --write
om research shadow-replay mark --dataset output_shared/research/shadow_replay/datasets/us-20260515 --required-data-root output_shared/required_data --write
om research shadow-replay settle --dataset output_shared/research/shadow_replay/datasets/us-20260515 --write
om research shadow-replay analyze --dataset output_shared/research/shadow_replay/datasets/us-20260515 --min-sample 30
```

边界：
- 只读源数据，来源可以是 `run_id`、`run_dir`、`candidate_report_dir`、`candidate_path`、`trace_path`、`reject_log_path`、`mark_path` 或 `outcome_path`。
- `--profile-path` / `--runtime-root` 只用于解析 runtime `output_runs`、dataset root、required-data root 和 receipt root；`--latest-scanned-run` 只选择最新已有 replay 证据的 run，不运行扫描。
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
- Agent 查询 Sell Put 现金占用与余量的标准入口
- 包装 `src.application.cash_headroom_query` 的 `query_sell_put_cash(...)`
- 返回账户现金、Sell Put 担保占用、剩余可用现金
- 支持按账户筛选，并按可用汇率折算到 CNY

示例：

```bash
om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"sy"}'
```

注意：
- Agent payload 使用 `broker` 表示券商口径；未传时读取 runtime config 的 `portfolio.broker`
- Agent 工具输入统一使用 `broker`
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
  `return_basis=current_cash_secured` 表示这不是账户总资产收益率。
  如果缺少汇率，相关 CNY 和收益率字段为 `null`，并在 `warnings` 中说明。
- `diagnostics`：按 `month + account` 输出收益统计诊断，包括匹配到的
  `trade_events`、position lots、已平仓行、premium 行、现金担保可用性和
  `missing_fields`。入站 `收益` 命令会用它解释“暂无可计算收益”或数据不完整的原因。

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

示例：

```bash
om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"history","record_id":"rec_xxx"}'
```

注意：
- 这个工具只开放读和诊断动作
- `add` / `buy-close` / `void-event` / `adjust-lot` / `rebuild` 不在此工具中开放

---

## 5.9 `get_portfolio_context`

用途：
- 获取账户持仓 / 现金 context

示例：

```bash
om-agent run --tool get_portfolio_context --input-json '{"config_key":"us","account":"lx"}'
```

---

## 5.10 `prepare_close_advice_inputs`

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
- 推荐给 Agent 使用；内部会准备输入并运行 `close_advice`

示例：

```bash
om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

这是更推荐的 Agent 入口。

---

## 5.12.1 `close_advice_read`

用途：
- 只读已有 `close_advice.csv`，按 account/symbol/option type/side/strike/expiration 过滤平仓建议行
- 给 Assistant 的 `position_exit_analysis` 使用，例如“分析 long call 是不是应该平仓”
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
- 只读汇总现有 runtime / OpenClaw 输出文件
- 暴露 `config_authority`，用于确认 `config.yaml` 到 `config.<market>.json` 的生成来源、sha256、身份和 freshness
- 不运行 pipeline
- 不发送通知
- 可读取 `openclaw.profile.json` / `.openclaw-profile.json` 或 payload 里的
  `profile_path` 作为 OpenClaw 或 service profile 路径、账户和 freshness 阈值
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
om-agent run --tool runtime_status --input-json '{"profile_path":"openclaw.profile.json"}'
om-agent run --tool runtime_runs --input-json '{"limit":10}'
om-agent run --tool runtime_logs --input-json '{"run_id":"20260515T182459Z-474761","kind":"tool","lines":20}'
```

---

## 5.16 `openclaw_readiness`

用途：
- 面向 OpenClaw 的一站式 readiness 摘要
- 组合 `runtime_status`、`healthcheck` 和本地 `openclaw` 命令可用性
- 读取可选 OpenClaw profile，输出 `next_actions.safe_next_actions` 和
  `next_actions.blocked_actions`
- profile 或 payload 提供 `cron_jobs` / `include_cron_status=true` 时，会运行只读
  `openclaw cron list` / `openclaw cron runs`
- 检查通知 route 是否已配置，且不会返回完整通知 target

示例：

```bash
om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
om-agent run --tool openclaw_readiness --input-json '{"profile_path":"openclaw.profile.json"}'
```

---

## 5.17 `research`

用途：
- 收集线上运行证据，生成给 MacBook Codex 阅读的 redacted bundle / handoff
- 诊断 runtime 质量、账本质量、多账户策略影响和策略证据完整性
- 内嵌与 `om runs` / `om logs` 同源的 run 列表和 audit tail 摘要
- 可选嵌入 `healthcheck` snapshot，但不取代 `healthcheck` 的 readiness 职责
- 默认不写文件、不调用在线 AI、不发送通知

示例：

```bash
om-agent run --tool research --input-json '{"config_key":"us","scope":"full","output":"both","write_outputs":false}'
om research collect --config-key us --scope full --output both --no-write-outputs
om research collect --config-key us --scope candidate --run-id 20260515T182459Z-474761 --output json --no-write-outputs --shadow-replay-min-sample 30
```

带线上调度证据：

```bash
om-agent run --tool research --input-json '{
  "config_key": "us",
  "scope": "full",
  "output": "both",
  "write_outputs": false,
  "scheduler_evidence": {
    "provider": "cron",
    "job_name": "us-tick",
    "last_run_id": "20260518T095446Z-2e7d54",
    "last_triggered_at": "2026-05-18T09:54:46Z",
    "last_status": "success",
    "last_exit_code": 0
  }
}'
```

Scope：
- `ledger`：交易入账、持仓维护和账本质量
- `candidate`：多账户候选、排名样本、filter trace 和 shadow replay readiness
- `quality`：runtime freshness、最新 run、调度证据和可选 healthcheck
- `full`：默认全量证据

写报告需要三层条件：
- `write_outputs=true`
- `confirm=true`
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`

默认写入位置：

```text
output_shared/research/
output_shared/state/current/research.current.json
```

注意：
- 它是证据打包工具，不是线上 AI 推理功能。
- `scheduler_evidence` 来自线上调度系统；尽量提供 `last_run_id` 和 `last_triggered_at`，否则本地 runtime 文件不能完整证明线上 cron 是否按时触发。
- `include_healthcheck=true` 只在 `quality` / `full` scope 下有意义。
- `--shadow-replay-min-sample` 只影响 Research bundle 里的 `candidate_evidence.shadow_replay` 样本充足性判断，不会改生产策略参数。

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

`om version` 仍然保留为人工 CLI 能力。Agent 使用 `version_check`，二者读取同一个本地 `VERSION` 和远端 `v*` tags。

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

Agent 工具输入统一使用 `broker`。数据表里的 `market` 字段不作为工具 payload 字段。

---

## 9. 相关文档

- Agent 合同：[`AGENT_INTEGRATION.md`](AGENT_INTEGRATION.md)
- 快速开始：[`GETTING_STARTED.md`](GETTING_STARTED.md)
- Agent 快速开始：[`AGENT_GETTING_STARTED.md`](AGENT_GETTING_STARTED.md)
- 配置说明：[`../CONFIGURATION_GUIDE.md`](../CONFIGURATION_GUIDE.md)
