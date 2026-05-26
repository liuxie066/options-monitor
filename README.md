# options-monitor

`options-monitor` 是一个本地运行的期权监控、筛选、报告和通知工具，主要服务四个用户功能：

- `Sell Put`：扫描 Put 期权候选，结合现金、策略阈值和已有风险暴露，筛出适合人工复核的卖 Put 机会。
- `Covered Call`：基于已有正股持仓扫描 Call 期权候选，辅助 covered call 收益管理。
- `Sell Put` 收益增强 `yield_enhancement`：在 Sell Put 候选基础上评估收益增强组合，寻找权利金、现金占用和 long call 成本之间更合适的组合。
- `close_advice`：检查已有期权仓位，给出止盈、继续持有或需要关注的平仓参考。

它不是自动交易系统，也不会替你下单。它的职责是把行情、持仓、现金、期权仓位、策略阈值和通知串起来，给出便于人工复核的结果。

## 入口

| 入口 | 面向对象 | 适合做什么 |
|---|---|---|
| `om` | 人工 CLI | 配置构建、手动运行、持仓维护、只读查询 |
| `om-agent` | Agent / 程序 | JSON manifest、结构化工具调用、只读诊断 |

源码目录内也可以直接使用 fallback：`./om` / `./om-agent`。

推荐顺序：

1. 首次启用先完成安装，然后运行 `om setup check`。
2. 日常人工操作优先 `om`。
3. Agent 接入、排障和结构化读取优先 `om-agent`。

## 它做什么，不做什么

做什么：

- 为 `Sell Put` 扫描和筛选候选。
- 为 `Covered Call` 生成已有持仓的 covered call 候选。
- 为 `yield_enhancement` 评估 Sell Put 收益增强组合。
- 为 `close_advice` 生成已有仓位的平仓参考。

不做什么：

- 不自动下单
- 不替你决定仓位
- 不默认发送通知、写 Feishu 或修改生产配置
- 不建议把有副作用的命令当成“先看看会发生什么”的探针

## 快速开始

### 1. 安装

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash

om setup check
```

无参数安装会解析并安装最新 GitHub release，例如 `v1.2.118`；不会安装浮动 `main` 分支。需要复现、回滚或固定生产版本时，显式指定 release tag：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh --version v1.2.118 --prefix "$HOME/apps/options-monitor"
```

安装脚本会下载代码、checkout 指定 release、创建 `.venv`、安装依赖、更新 `current` symlink，并默认在 `$HOME/.local/bin` 创建 `om` / `om-agent` 用户级 wrapper。它不会写配置、不会写 secrets、不会启动服务、不会创建定时任务。
如果 `$HOME/.local/bin` 尚未加入 `PATH`，按安装输出提示先加入 PATH，或使用 fallback：`$HOME/apps/options-monitor/current/om setup check`。

手动安装、server/dev 依赖和目录布局见 [docs/INSTALL.md](docs/INSTALL.md)。

平台默认值：

| 平台 | 推荐 runtime root | 推荐 env-file | 服务管理器 |
|---|---|---|---|
| Linux | `/var/lib/options-monitor` | `/etc/options-monitor/options-monitor.env` | `systemd` |
| macOS | `$HOME/Library/Application Support/options-monitor` | `$HOME/Library/Application Support/options-monitor/options-monitor.env` | `launchd` |

如果要从飞书 long-connection 接收远端命令，安装时加 `--with-server`。

### 2. 初始化配置

当前推荐配置模型：

- 代码内 `DEFAULT_CONFIG` 提供系统默认值，用户不用维护系统默认文件。
- `config.yaml` 只保存用户 override，包括 accounts、markets、symbols 和非 secret 行为配置。
- env-file 只保存 secrets、Feishu 凭证和写入开关。
- `config build` 生成 market-specific runtime config，实际运行仍读取 JSON 快照。
- `config build` / `config explain` 默认读取 YAML；legacy JSON 必须显式传 `--source legacy`。

源码 checkout 或本地手动运行可以直接在 repo root 维护忽略文件 `config.yaml`：

```bash
om config init --output config.yaml --runtime-output-dir .
$EDITOR config.yaml

om config validate --source yaml --market us
om config build --source yaml --market us --output config.us.json
om config validate --config-path config.us.json --market us
```

HK 同理：

```bash
om config validate --source yaml --market hk
om config build --source yaml --market hk --output config.hk.json
om config validate --config-path config.hk.json --market hk
```

生产服务建议把 `config.yaml` 和生成后的 runtime config 放在 `runtime_root` 或其他 release 外的持久路径，再显式传路径：

```bash
om config init --output /var/lib/options-monitor/config.yaml --runtime-output-dir /var/lib/options-monitor
om config build --source yaml --market us --config-yaml /var/lib/options-monitor/config.yaml --output /var/lib/options-monitor/config.us.json
om config build --source yaml --market hk --config-yaml /var/lib/options-monitor/config.yaml --output /var/lib/options-monitor/config.hk.json
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json
```

已有 `configs/user.*.json` 的旧安装可以先 dry-run 迁移：

```bash
om config migrate-yaml --output config.yaml
om config migrate-yaml --output config.yaml --apply
```

历史兼容入口 `om setup init` 仍保留，但已 deprecated。新安装不要再用它生成人工维护的
runtime JSON；需要从旧 `configs/user.*.json` 迁移时使用 `om config migrate-yaml`。

```bash
om setup init --market us --account lx --futu-acc-id <futu-account-id>
om setup init --market hk --account lx --futu-acc-id <futu-account-id>
```

这条路径只作为旧脚本兼容面存在，输出会带 deprecation warning。

生成的 runtime config 会记录 `_generated` 指纹。`config.yaml` 更新后需要重新
`config build --source yaml`；旧安装的 legacy 分层配置仍可兼容 rebuild，但该 authoring
路径已 deprecated。`run tick` / `run tick-cron` 会在陈旧 runtime config 上提前失败并给出重建命令。

完整首次运行流程见 [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)。

### 3. 先做只读验证

先检查配置本身是否合法：

```bash
om config validate --source yaml --market us
om config validate --config-path config.us.json --market us
om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

再检查本机前置条件、OpenD、SQLite 和通知配置：

```bash
om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
om doctor --config-key us
om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
```

解释某个配置值来自哪里：

```bash
om config explain --source yaml --market us --key option_positions.auto_close.enabled
om config explain --source yaml --market us --key symbol_defaults.fetch.limit_expirations
```

直接查看 runtime config 时，使用 `config get`。`config set` 仍保留为兼容/应急入口，但它修改的是生成后的 runtime JSON，不是 durable authoring source；持久配置变更应改 `config.yaml` 后重新 `config build --source yaml`。写入语义统一为：
默认只读或 dry-run；`--apply` 允许本地文件/状态写入；`--confirm` 允许交易事件、Feishu、服务变更这类高风险写入；`--yes` 用于非交互脚本，等价显式确认并在输出里带 `audit_id`。写命令的 JSON 输出都会带 `dry_run`、`write_applied`、`backup_path`、`audit_id`、`rollback_hint`。

`config set` 属于本地 runtime snapshot 写入，默认只预览，`--apply` 后会先校验修改后的配置再写入：

```bash
om config get --config-key us --key runtime.prefetch.max_workers
om config set --config-key us --key runtime.prefetch.max_workers --json-value 4
om config set --config-key us --key runtime.prefetch.max_workers --json-value 4 --apply
```

### 4. 第一轮真实运行

先禁发通知：

```bash
om run tick --config config.us.json --accounts lx sy --no-send
```

确认输出、候选和通知预览都合理，再进行正式运行：

```bash
om run tick --config config.us.json --accounts lx
om run tick --config config.us.json --accounts lx sy
```

### 5. Linux / Mac 服务化部署

长期运行时不要让运行产物散落在仓库目录。统一使用 `runtime_root`：

```text
<runtime_root>/output_runs/
<runtime_root>/output_shared/
<runtime_root>/output_accounts/
<runtime_root>/output/
```

期权持仓 SQLite 固定在：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

渲染服务文件：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --output-dir /tmp/options-monitor-service

./om service render \
  --target launchd \
  --runtime-root "$HOME/Library/Application Support/options-monitor" \
  --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env" \
  --markets us hk \
  --accounts lx sy \
  --config-yaml "$HOME/Library/Application Support/options-monitor/config.yaml" \
  --config-us "$HOME/Library/Application Support/options-monitor/config.us.json" \
  --config-hk "$HOME/Library/Application Support/options-monitor/config.hk.json" \
  --output-dir /tmp/options-monitor-service
```

完整步骤见 [`DEPLOY.md`](DEPLOY.md)。

## 常用工作流

### 监控与扫描

统一 tick 入口只有一条链路：

```bash
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy
```

单账户只是传一个账户的特例；多账户直接把多个账户标签传给 `--accounts`。旧的 `scripts/send_if_needed.py` 和 `scripts/send_if_needed_multi.py` 已移除。

只做扫描时：

```bash
./om scan --config-key us --symbols NVDA,TSLA --top-n 5
./om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":5}'
```

### 候选排序解释

解释已有候选为什么这样排序：

```bash
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
./om-agent run --tool candidate_rank_explain --input-json '{"candidate_path":"output_shared/reports/sell_call_candidates.csv","mode":"call","top_n":5}'
```

这个工具只读已有 CSV，不重跑扫描，不发送通知，也不改配置。

### 为什么某个 symbol 被过滤掉

如果线上有人反馈“某个 symbol 没进候选”或者“为什么这个账户没有看到它”，先看 `candidate_filter_explain`。

按 run/account 查：

```bash
./om-agent run --tool candidate_filter_explain --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
```

按 trace 文件查：

```bash
./om-agent run --tool candidate_filter_explain --input-json '{"trace_path":"output_shared/reports/candidate_filter_trace.jsonl","symbol":"NVDA"}'
```

它会把 trace 汇总到这几个函数维度：

- `sell_put`
- `sell_call`（trace/runtime 内部 key；`config.yaml` authoring 使用 `covered_call`，对外显示为 Covered Call）
- `close_advice`
- `yield_enhancement`
- `cash_reserve`
- `share_coverage`

适合回答这些问题：

- 这个 symbol 根本没被观测到，还是被某条规则拒掉了
- 是扫描阶段被拒，还是账户级后过滤被拒
- 是现金覆盖、持股覆盖，还是流动性、DTE、收益阈值导致的

### Sell Put 现金余量

人工 CLI：

```bash
./om sell-put-cash --market 富途 --account lx
./om sell-put-cash --market 富途 --account sy
```

Agent：

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
```

### 平仓建议

人工 CLI：

```bash
./om close-advice --config-key us
```

推荐的 Agent 一站式入口：

```bash
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

如果要拆成两步排查输入准备和建议生成：

```bash
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

### Symbols

`symbols` CLI operates on an explicit generated runtime snapshot. Normal config authoring should still happen in `config.yaml`, followed by `om config build --source yaml`.

```bash
./om symbols --config config.us.json list
./om symbols --config config.us.json add TCOM --put --dry-run
./om symbols --config config.us.json add TCOM --put --apply
./om symbols --config config.us.json edit TCOM --set sell_put.max_strike=45 --apply
./om symbols --config config.us.json rm TCOM --apply
```

Agent 只读列出：

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

写入 `symbols[]` 时，需要显式确认和 `OM_AGENT_ENABLE_WRITE_TOOLS=true`。

### Option Positions

查看本地期权仓位：

```bash
./om option-positions list --broker 富途 --account lx --status open
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

新增仓位先用 `--dry-run`：

```bash
./om option-positions add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --premium-per-share 1.2 --dry-run
./om option-positions add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --premium-per-share 1.2 --confirm
```

手工成交文本入账使用 runtime config 路径；确认写入前会打印目标 SQLite，发现 active/default store 已经漂移时会拒绝写入：

```bash
python3 -m src.application.option_intake --config /var/lib/options-monitor/config.hk.json --text "/om -sy open ..." --dry-run
python3 -m src.application.option_intake --config /var/lib/options-monitor/config.hk.json --text "/om -sy open ..." --confirm
```

过期自动平仓使用专用入口，不随 tick 扫描执行：

```bash
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --dry-run
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm
./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm --no-send
```

月度收益报表：

```bash
./om option-positions report monthly-income --broker 富途 --account lx --month 2026-04
./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx","month":"2026-04"}'
```

### 通知预览

不发送通知，只看最终文本：

```bash
./om notify preview
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output_shared/reports/symbols_alerts.txt","changes_path":"output_shared/reports/symbols_changes.txt","account_label":"lx"}'
```

## 策略模型

### Sell Put

默认策略已经从“年化收益优先”切到 short-vol 视角。Sell Put 不再被当成“折价买股”，而是先确认是否有足够的波动率边际和组合容量：

- `min_dte` / `max_dte`
- `min_strike` / `max_strike`
- `short_vol.min_iv_rv_ratio`
- `short_vol.min_iv_minus_rv`
- `short_vol.min_abs_delta` / `short_vol.max_abs_delta`
- `short_vol.reject_event_risk` / `short_vol.event_source_fail_closed`
- `short_vol.max_put_sigma_stress_loss_nav_pct`
- `short_vol.max_put_gap_down_loss_nav_pct`
- `concentration.max_single_trade_nav_pct`
- `concentration.max_symbol_nav_pct`
- `concentration.max_total_short_put_nav_pct`
- `min_open_interest`
- `min_volume`
- `max_spread_ratio`

在 `short_vol` profile 下，收益率和单笔净收入参与排序，不再作为扫描入口的第一道硬过滤。

除了链上候选过滤，最终还会叠加账户现金维度的 `cash_reserve` 后过滤，以及基于全局 holdings / option positions 的组合集中度后过滤。事件源不可用、expiry 前存在财报等事件、缺少 IV、RV、Delta、NAV、FX、strike、spot 或 multiplier 等关键风险输入时，short-vol 策略会 fail closed。Sell Put 还会检查 2σ 下跌和默认 10% gap-down 压力亏损占 NAV 的比例。

### Covered Call

Covered Call 依赖真实持仓上下文。它在风险结构上和 Sell Put 同属 short vol / short gamma，只是现金、持仓和行权方向不同：

- `shares` / `avg_cost` 来自 holdings，不再建议手写在 symbol 配置里
- 已被 short call 锁定的股票会从可卖数量里扣掉
- `min_strike_cost_multiplier` 会抬高有效 strike 下限，避免推荐明显低于成本底线的 call
- 默认同样走 `short_vol` profile，会检查 IV/RV、IV-RV、delta band、事件风险和组合集中度，并输出短 gamma/vega、covered notional 与 gap-up 右尾机会成本字段；右尾机会成本会通过 `path_risk` 进入排序
- gap-up 右尾机会成本同时是硬预算：默认要求机会成本不超过 NAV 的 2%，且不超过本次 premium 的 3 倍

注意：`config.yaml` 里使用 `covered_call`；生成后的 runtime JSON、CSV 和 trace 里的内部策略 key 仍是 `sell_call`。这是兼容性标识；用户可见名称统一为 `Covered Call`。

### Yield Enhancement

`yield_enhancement` 是顶层 symbol 维度配置，不再挂在 `sell_put` 下面。它的目标是基于 Sell Put 候选，寻找“short put 权利金足够覆盖 long call 成本”的增强组合。

要点：

- 依赖 `sell_put.enabled=true`
- 即使 `covered_call.enabled=false`（runtime 内部为 `sell_call.enabled=false`），启用收益增强后也可能拉取 call 侧期权链
- 重点看 `min_combo_net_credit`、`max_call_cost_to_put_credit`、`scenario_weights`、`min_scenario_score`

### Close Advice

`close_advice` 基于本地 `option_positions`、required data 和报价生成建议，属于 advisory-only 逻辑，不应被当成自动平仓器。

## 配置心智模型

### Runtime config 与 authoring config

运行时入口：

- `config.us.json`
- `config.hk.json`

推荐编辑源：

- `config.yaml`

兼容编辑源：

- `configs/system.json`
- `configs/user.common.json`
- `configs/user.us.json`
- `configs/user.hk.json`

持仓和本地仓位相关数据配置：

- 默认不需要单独配置文件；SQLite 固定在 `<runtime_root>/output_shared/state/option_positions.sqlite3`
- `portfolio.runtime.json` 默认不需要；只在 external_holdings 需要声明 Feishu 表引用 env 名或执行 legacy 迁移时使用

原则上：

- 编辑 `config.yaml`
- 用 `om config build --source yaml --market us|hk` 生成 runtime config
- 用 `om config validate --source yaml --market us|hk` 检查 YAML override 与代码默认值合并后的配置
- 用 `om config validate --config-path ... --market us|hk` 检查 runtime config、市场时区契约和生成指纹
- 用 `config_validate` 做不含生成指纹检查的基础只读配置校验
- 用 `om settings doctor` 检查 env-file、Feishu Bot 和写入开关
- 遇到安装或运行问题时，用 `om support bundle --config-key us` 生成脱敏诊断包

### 数据来源

默认最小组合通常是：

- 行情与期权链：OpenD / Futu API
- 持仓与现金：OpenD / Futu API
- `option_positions`：本地 SQLite
- 通知：默认关闭，按需显式配置

Feishu 常见只用于这些场景：

- `external_holdings` / `holdings` 数据源
- 飞书通知

### 多账户

多账户的基本约定：

- 账户标签使用小写，例如 `lx`、`sy`
- 默认账户列表来自 runtime config 顶层 `accounts`
- 单账户和多账户走同一条 tick 链路
- 多账户问题先按账户维度排查，不要默认认为是全局 gate

## Agent 使用指南

这个仓库把文档拆成两层：

- [AGENTS.md](AGENTS.md)：给 agent 首先加载的短说明书，记录安全红线、入口层级和模块归属
- [docs/AGENT_GETTING_STARTED.md](docs/AGENT_GETTING_STARTED.md)：Agent 接入的最短路径
- [docs/AGENT_WIKI.md](docs/AGENT_WIKI.md)：给 agent 深入执行任务时看的手册，包含工具选择、Research、排障 playbook 和验证矩阵

安装 Agent 插件：

```bash
bash scripts/install_agent_plugin.sh
./om-agent spec
```

常用只读工具：

```bash
./om status --config-key us
./om runs --limit 10
./om logs --run-id <run-id> --lines 50
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_runs --input-json '{"limit":10}'
./om-agent run --tool runtime_logs --input-json '{"run_id":"<run-id>","kind":"tool","lines":50}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
```

受控远程消息入口：

```bash
./om assistant handle --text '持仓 sy' --sender local --channel local --message-id local-1
./om inbound feishu --input-file feishu_event.json --format text
./om inbound feishu-ws --check
./om assistant commands --format text
./om assistant capabilities
./om assistant llm-check
```

只读查询会直接执行；写操作必须先返回预览并等待确认。链路带 sender allowlist、message_id 幂等和 SQLite audit。Assistant command facade 默认开启，例如 `/status`、`/positions sy`、`/income 2026-05`；Feishu WS 使用同一层 command facade，由 assistant config 的 `assistant.mode` 控制。LLM 默认关闭；显式把 `assistant.mode` 设为 `llm_router` 或 `agent_loop`，并配置 `assistant.llm.provider`、`model`、可选 `base_url` 和本机 API key env 后，只会把自然语言和同一对话的有限上下文翻译成 `om-llm-intent-v1` 只读 capability，再走同一条 router。`assistant capabilities` 可查看 LLM 能看到的项目能力清单：写入、确认、升级等能力会出现在清单里，但不会进入 LLM 可执行 enum。`feishu-ws` 可作为长驻 Feishu App long-connection client，通过飞书 SDK 长连接接收消息，并自动回复，不需要公网 HTTPS callback；reaction、reply、queue 行为配置在 assistant config 的 `inbound.feishu_ws` 下。接飞书、微信或 Hermes 前先看
[docs/INBOUND_CONTROL.md](docs/INBOUND_CONTROL.md)。

Research 证据交接：

```bash
./om research collect --config-key us --scope full --output both --no-write-outputs
./om-agent run --tool research --input-json '{"config_key":"us","scope":"full","output":"both","write_outputs":false}'
```

`research` 是给 MacBook 上的 Codex 分析线上质量和策略问题的证据打包入口。线上侧只收集 redacted bundle / handoff，不调用在线 AI；run 列表和日志摘要与 `./om runs` / `./om logs` 同源，调度系统状态需要通过 `scheduler_evidence` 或 CLI 的 `--scheduler-evidence-json` 显式传入。

写工具门禁：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true ./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"edit","confirm":true,...}'
```

规则建议：

- 解释、调查、代码阅读类任务先读文件
- 优先 `./om-agent`，其次 `./om`
- 不要把 `python3 scripts/...` 当成第一入口
- 发送通知、写 Feishu、改生产配置、删除运行产物前必须有明确意图
- 有 `dry-run`、`validate`、`healthcheck`、`runtime_status` 时先走低风险路径

补充说明：

- `./om-agent spec` 才是当前公开工具清单的准确信源

## 定时与长驻任务

README 只记录公开入口和边界。生产 cron id、长驻服务启停和更细的运行手册见 [RUNBOOK.md](RUNBOOK.md)。

| 任务 | 推荐入口 | 运行方式 | 主要副作用 |
|---|---|---|---|
| 期权监控 / 扫描通知 | `./om run tick-cron --market hk --accounts lx sy --timeout 600` / `./om run tick-cron --market us --accounts lx sy --timeout 600` | cron 每 10 分钟唤醒，代码内判断业务窗口 | 写本地报告和运行状态，并按通知策略发送扫描/建议消息 |
| 调度状态检查 | `./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'` | 定时或人工检查 | 只读 |
| 自动交易监听 / 入账 | `python3 -m src.application.trades.auto_intake --config config.us.json --mode apply --yes` | 长驻进程 | 写本地 `option_positions`、intake state/status，并按 receipt 配置发送回执 |
| 过期自动平仓 | `./om option-positions auto-close-expired --config config.hk.json --accounts lx sy --confirm` | 低频定时或人工触发 | 写本地 `option_positions`、运行状态，并按 receipt 配置发送任务级回执 |
| 版本检查 | `./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'` | 低频只读 | 只读 |
| 版本更新预览 | `./om-agent run --tool version_update --input-json '{"bump":"patch"}'` | dry-run | 不写 `VERSION` |

不要把 `version_update apply=true` 放进固定频率任务。它会递增本地 `VERSION`，不等于发布流程。

`tick-cron` 在拿到锁后会先校验 runtime config 的生成指纹；如果
`config.yaml` 更新后没有重新 build，
任务会以 `[CONFIG_ERROR]` 失败并打印 `./om config build ... --output ...`。`--allow-stale-config`
只作为临时应急绕过使用。旧安装的 legacy 分层 user config 仍可兼容检查，但该 authoring
路径已 deprecated。

## 副作用边界

| 命令 / 工具 | 写本地 | 写远端 | 发通知 |
|---|---:|---:|---:|
| `./om-agent run --tool config_validate ...` | 否 | 否 | 否 |
| `./om-agent run --tool healthcheck ...` | 否 | 否 | 否 |
| `./om-agent run --tool runtime_status ...` | 否 | 否 | 否 |
| `./om-agent run --tool scan_opportunities ...` | 是 | 否 | 否 |
| `./om-agent run --tool get_close_advice ...` | 是 | 否 | 否 |
| `./om-agent run --tool query_cash_headroom ...` | 是 | 否 | 否 |
| `./om run tick --config ... --no-send` | 是 | 可能 | 否 |
| `./om run tick --config ...` | 是 | 可能 | 是 |
| `./om run tick-cron --market ...` | 是 | 可能 | 是 |
| `python3 -m src.application.trades.auto_intake --mode apply --yes` | 是 | 否 | 是，默认发送入账回执 |
| `python3 -m src.application.option_intake --config ... --confirm` | 是 | 否 | 否 |
| `./om option-positions auto-close-expired --confirm` | 是 | 否 | 是，默认发送过期自动平仓回执 |
| `./om option-positions auto-close-expired --confirm --no-send` | 是 | 否 | 否 |

## 排障顺序

建议顺序：

1. 先读相关代码、配置文档和测试。
2. 先跑 `config_validate`、`healthcheck`、`runtime_status`。
3. 需要解释候选时先用 `candidate_rank_explain` / `candidate_filter_explain`。
4. 只有在静态信息不足时，才跑真实 tick 或其他会写状态的命令。

常见问题先看哪里：

| 症状 | 先看什么 |
|---|---|
| 配置校验失败 | `CONFIGS.md`、`CONFIGURATION_GUIDE.md`、`./om config explain` |
| 某个 symbol 没进候选 | `candidate_filter_explain`、对应 `candidate_filter_trace.jsonl` |
| 两个账户结果看起来串了 | `scheduler_status`、账户级 source 配置、账户级状态文件 |
| 通知没发出来 | `preview_notification`、`notifications.channel`、secret 文件、OpenClaw route |
| 自动交易监听没有回执 | `runtime_status` 的 `trade_intake.summary`、`auto_trade_intake_status.json`、`trade_intake.receipt.enabled`、通知 route |
| 过期自动平仓没有回执 | `runtime_status` 最新 run 里的 `auto_close_receipt` / `expired_position_maintenance`、`option_positions.auto_close.receipt.enabled`、通知 route；每日维护 cron 重跑时还要看 `receipt_key` 是否已确认发送 |
| 平仓建议异常 | `prepare_close_advice_inputs`、本地 `option_positions`、required data |

## 文档导航

- [CONFIGS.md](CONFIGS.md)：canonical config、分层配置和迁移规则
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)：字段说明、数据来源和配置边界
- [RUNBOOK.md](RUNBOOK.md)：运维巡检、定时任务、应急操作
- [docs/INSTALL.md](docs/INSTALL.md)：安装方式、release 目录布局和 installer 安全契约
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)：普通用户首次运行路径
- [docs/AGENT_GETTING_STARTED.md](docs/AGENT_GETTING_STARTED.md)：Agent 快速开始
- [docs/AGENT_WIKI.md](docs/AGENT_WIKI.md)：Agent 任务手册
- [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)：Agent JSON 合同
- [docs/INBOUND_CONTROL.md](docs/INBOUND_CONTROL.md)：飞书、微信、Hermes 等远程消息入口的安全控制层
- [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)：`om-agent` 工具说明
- [docs/candidate_strategy.md](docs/candidate_strategy.md)：候选生成和策略边界
- [docs/OPTION_POSITIONS_MIGRATION.md](docs/OPTION_POSITIONS_MIGRATION.md)：option positions 迁移
- [docs/OPTION_POSITIONS_REPAIR.md](docs/OPTION_POSITIONS_REPAIR.md)：option positions 修复
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：主要模块边界
- [tests/README.md](tests/README.md)：测试分层和运行方式

## 风险提示

本工具只做监控、筛选、报告和提醒，不构成投资建议。任何下单前都应自行复核价格、流动性、保证金、仓位暴露和事件风险。
