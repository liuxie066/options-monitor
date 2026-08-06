# options-monitor

`options-monitor` 是一个本地运行、人工决策优先的期权监控系统。它把行情、现金、正股持仓、期权账本、策略规则、报告和通知串成一条可审计链路，帮助用户完成：

- `Sell Put` 与 `Covered Call` 候选筛选；
- `Combo Yield` 组合候选评估；
- 已开期权 lot 的 `Close Advice`；
- 期权利润、现金活动、持仓与到期生命周期查询；
- Daily Decision Brief、候选变化提醒和离线策略复盘。

它不是自动交易系统，不会替用户下单。候选、平仓建议和研究结论均为 advisory-only；真实交易、配置写入、通知发送、服务变更和生产状态修改必须走各自的显式确认边界。

## 核心边界

系统只有一套主运行链路：

```text
config.yaml
├─ om config build --market us|hk
│  └─ config.us.json / config.hk.json
│     └─ om run tick | om run tick-cron
│        └─ output_runs + output_shared + output_accounts
└─ om config build-assistant
   └─ resolved/config.assistant.json
```

期权持仓只有一套事实链：

```text
trade_events -> projection -> position_lots
```

- `config.yaml` 是人工编辑源；生成的 JSON 是运行快照，不是日常手工编辑入口。
- 本地 SQLite 是期权交易与持仓事实源；Feishu 不承载 `option_positions` 镜像。
- 普通手工 `tick` 不自动发送 scheduled ordinary notification；生产调度使用受保护的 `tick-cron`。
- `./om-agent spec` 是 Tool Gateway 工具名、输入 schema、风险级别和副作用的权威清单。
- 缺少行情、费用、历史汇率、事件或身份事实时，系统显式返回 missing、partial 或 not-evaluable，不补造数据。

产品域见 [产品架构](docs/PRODUCT_ARCHITECTURE.md)，技术调用链见 [系统架构](docs/ARCHITECTURE.md)。

## 主要能力与入口

| 能力 | 当前入口 | 权威说明 |
|---|---|---|
| YAML 配置构建与校验 | `om config` | [CONFIGS.md](CONFIGS.md) |
| Sell Put / Covered Call | `om run tick`、`om scan` | [策略架构](docs/STRATEGY_ARCHITECTURE.md) |
| Combo Yield | 与开仓扫描同链路 | [策略架构](docs/STRATEGY_ARCHITECTURE.md) |
| Close Advice | `om close-advice` | [Close Advice Contract](docs/CLOSE_ADVICE_CONTRACT.md) |
| Daily Decision Brief | `om daily-brief` | [通知体验 PRD](docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md) |
| 期权账本与生命周期 | `om option-positions`、`om trade-events` | [Ledger Architecture](docs/LEDGER_ARCHITECTURE.md) |
| 期权收益与现金 | `om option-performance` | [Option Performance](docs/OPTION_PERFORMANCE_DESIGN.md) |
| 全部 Sell Put / Sell Call 指派压力测试 | `om portfolio assignment-scenario` | 本 README 的“指派后资产分布” |
| 本地 Copilot | `om copilot` | [Agent Integration](docs/AGENT_INTEGRATION.md) |
| 结构化 Tool Gateway | `om-agent spec`、`om-agent run --tool <name> --input-json '<json>'` | [Tool Reference](docs/TOOL_REFERENCE.md) |
| Shadow Replay / Strategy Lab | `om research` | [Shadow Replay Runbook](docs/SHADOW_REPLAY_RUNBOOK.md) |
| 运行诊断、服务与版本升级 | `om status`、`om service`、`om update` | [RUNBOOK.md](RUNBOOK.md) |

本表是主要能力索引，不是 CLI 或 Tool Gateway 的完整命令清单。人工操作入口以 `om --help` 为准；结构化工具名、输入 schema、风险级别和副作用以 `om-agent spec` 为准。

Sell Put / Covered Call 新开仓只使用 `insurance_underwriting`。历史 artifact 和持仓解释可继续读取
`return_first` / `short_vol`，但这些兼容语义不能重新进入当前开仓配置或正式候选排序。

README 不复制完整规则：[候选策略合同](docs/candidate_strategy.md) 是已经确认的目标口径，
[策略架构](docs/STRATEGY_ARCHITECTURE.md) 定义模块责任；目标是否已经上线必须以当前代码和测试验证，
不能只凭文档判断。

## 安装

运行时要求 Python 3.12 或更高版本。

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash
om setup check
```

无参数安装会解析最新 GitHub Release，不跟随浮动 `main`。固定版本或自定义安装目录：

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh \
  -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh \
  --version <release-tag> \
  --prefix "$HOME/apps/options-monitor"
```

安装器会准备 release 目录、Python 环境和 `om` / `om-agent` 用户级 wrapper；不会创建生产配置、写入 secrets、安装服务或启动定时任务。完整平台要求、目录布局和源码安装方式见 [Install](docs/INSTALL.md)。

在源码 checkout 中，以下示例里的 `om` / `om-agent` 可替换为 `./om` / `./om-agent`。

## 五分钟开始

### 1. 初始化配置

`config init` 会在 `--runtime-output-dir` 下生成 US/HK runtime JSON 和
`config.assistant.json`，同时生成 `config.yaml`。目标已存在时默认拒绝覆盖。

```bash
om config init --output config.yaml --runtime-output-dir .
$EDITOR config.yaml
```

验证人工编辑源并重新生成市场快照：

```bash
om config validate --source yaml --market us --config-yaml config.yaml
om config build --source yaml --market us \
  --config-yaml config.yaml \
  --output config.us.json

om config validate --source yaml --market hk --config-yaml config.yaml
om config build --source yaml --market hk \
  --config-yaml config.yaml \
  --output config.hk.json

om config build-assistant --source yaml \
  --config-yaml config.yaml \
  --output resolved/config.assistant.json
```

再验证生成快照和来源指纹：

```bash
om config validate --config-path config.us.json --market us
om config validate --config-path config.hk.json --market hk
```

配置模型、账户类型、环境变量和迁移方式见 [CONFIGS.md](CONFIGS.md) 与 [配置指南](CONFIGURATION_GUIDE.md)。

### 2. 只读检查

```bash
om-agent run --tool config_validate \
  --input-json '{"config_key":"us"}'
om-agent run --tool healthcheck \
  --input-json '{"config_key":"us"}'
om-agent run --tool runtime_status \
  --input-json '{"config_key":"us"}'
```

生产 release 目录通常没有 repo-local config。检查生产 runtime 时应显式传真实路径：

```bash
om-agent run --tool runtime_status \
  --input-json '{"config_path":"/var/lib/options-monitor/config.us.json"}'
```

还可以用人工 CLI 查看环境、配置来源和运行条件：

```bash
om settings doctor
om doctor --config-key us
om config explain --source yaml --market us \
  --key option_positions.auto_close.enabled
```

### 3. 第一轮扫描

先禁发通知：

```bash
om run tick --config config.us.json --accounts lx sy --no-send
```

`--no-send` 只表示不发通知；扫描仍会读取外部数据并写本地 run、报告、cache 和状态 artifact。它不是 no-write 模式。

检查结果后，可继续手工扫描：

```bash
om run tick --config config.us.json --accounts lx
om run tick --config config.us.json --accounts lx sy
```

计划内扫描和普通通知使用 guarded scheduler：

```bash
om run tick-cron --market us --config config.us.json --accounts lx sy --timeout 600
om run tick-cron --market hk --config config.hk.json --accounts lx sy --timeout 600
```

首次运行的完整顺序见 [Getting Started](docs/GETTING_STARTED.md)。

## 常用工作流

### 查询最新 Daily Brief

查询只读取最近一次可靠成功扫描的快照，不重新扫描、不发送、不修改 delivery state：

```bash
om daily-brief latest
om daily-brief latest --account lx --market US
om daily-brief latest --account lx --market HK --json

om-agent run --tool daily_decision_brief_read \
  --input-json '{"account":"lx","market":"US"}'
```

固定报告点、候选增量提醒、失败重试和渲染规则统一维护在 [通知体验 PRD](docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md)，README 不保留第二份通知规范。

### 解释候选

解释已有候选排序：

```bash
om-agent run --tool candidate_rank_explain \
  --input-json '{"run_id":"<run-id>","account":"lx","mode":"put","top_n":5}'
```

解释某个标的为什么未进入候选：

```bash
om-agent run --tool candidate_filter_explain \
  --input-json '{"run_id":"<run-id>","account":"lx","symbol":"NVDA"}'
```

这两个工具读取已有 candidate / trace artifact，不重跑扫描。

### 查询现金与持仓

```bash
om-agent run --tool query_cash_headroom \
  --input-json '{"config_key":"us","account":"lx"}'

om option-positions list --broker 富途 --account lx --status open
om-agent run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

`query_cash_headroom` 是纯读工具，不持久化查询 cache。`option_positions_read` 也不写账本；某些当前时点查询可能从 OpenD 读取最新报价，并在响应中给出 quote freshness。

新增、平仓、指派、行权和修复必须走语义化账本入口。先 dry-run：

```bash
om option-positions add \
  --request-id manual-open-<stable-id> \
  --account lx \
  --symbol NVDA \
  --option-type put \
  --side short \
  --contracts 1 \
  --currency USD \
  --strike 100 \
  --multiplier 100 \
  --exp <future-expiry> \
  --dry-run
```

写入前确认目标 runtime root、SQLite、account、lot 和 event 语义。`add`、`assign`、
`exercise` 在 dry-run、确认写入和重试时必须复用同一个 `--request-id`，以便在响应丢失后
安全返回原结果。修账流程见 [Option Positions Repair](docs/OPTION_POSITIONS_REPAIR.md)。

### 指派后资产分布

把所选账户中所有 open short Sell Put 和 Sell Call 同时按 strike 实物指派，并按当前现货价格与当前显式汇率证据计算 CNY 资产分布、现金覆盖、费用、到期梯度和潜在负债：

```bash
om portfolio assignment-scenario --accounts lx sy
om portfolio assignment-scenario --accounts lx sy --format json

om-agent run --tool portfolio_assignment_scenario \
  --input-json '{"accounts":["lx","sy"]}'
```

该功能是纯读压力测试，不写 assignment event、不修改 `position_lots`、不修改 portfolio-management 持仓，也不发送通知。固定口径：

- 只处理 open short put/call；Long Option 完全不读取、不估值、不保留；
- portfolio-management 提供全部非期权资产、当前报价、显式 FX 和补充标的报价，OM SQLite 提供 short option lot；
- MMF 并入现金，资金覆盖统一用 CNY；账户、券商和币种拆分仍保留作操作约束；
- 股票按当前 spot 估值，指派现金按 strike 结算；历史已收权利金不重复计入；
- 费用复用统一股票费用计算器；缺少券商、币种或指派费用规则时返回 `partial` 和 `null`，不按 0 处理；
- 现金不足形成 funding liability，Sell Call 覆盖不足形成 short-stock liability，不会被改写成执行错误。

Copilot 通过同一个 `portfolio_assignment_scenario` 纯读工具调用，不维护第二套触发词或计算逻辑。使用 Copilot 时需在 assistant 配置中显式启用可选的 `portfolio` toolset，并保持 portfolio-management API 仅在同机 loopback 提供服务。

### Close Advice

生成新报告会读取行情并物化本地报告：

```bash
om close-advice --config-key us
om-agent run --tool get_close_advice \
  --input-json '{"config_key":"us"}'
```

只读取已有报告：

```bash
om-agent run --tool close_advice_read \
  --input-json '{"config_key":"us","query":{"option_type":"call","side":"long"}}'
```

Close Advice 不自动平仓，不修改 lot，也不按当前默认策略重写历史开仓 thesis。

### Option Performance

```bash
om option-performance report \
  --config-key us \
  --account lx \
  --period mtd

om option-performance report \
  --config-key us \
  --account lx \
  --period month \
  --month 2026-06

om-agent run --tool option_performance_report \
  --input-json '{"config_key":"us","account":"lx","period":"ytd","as_of_date":"2026-07-17"}'
```

利润、现金与活动是不同口径：

- `pnl` 回答利润；
- `cash` 回答现金变化；
- `activity` 回答权利金和合约活动；
- `portfolio_pnl_bridge` 与 `portfolio_cash_bridge` 分别对接 PnL 和现金恒等式。

交易现金的 CNY 金额在事件写入时按成交附近汇率证据冻结。旧事件没有合格快照时，原币金额保留，CNY 保持 `null/partial`，不会用当前汇率反推。历史 `monthly_income_report` 与 `option-positions report monthly-income` 已移除。

### Research / Shadow Replay

只收集并输出到终端、不写 evidence bundle：

```bash
om research collect \
  --config-key us \
  --scope full \
  --output both \
  --no-write-outputs
```

`om research` 各子命令的写入参数并不统一：`collect` 使用 `--write-outputs --confirm`，Shadow Replay / Strategy Lab 的部分动作使用 `--write`，dataset build 和 archive verify 也有自己的 artifact 语义。执行前先看子命令 `--help` 与 [Shadow Replay Runbook](docs/SHADOW_REPLAY_RUNBOOK.md)；不要把 Research 整体理解成“永远只读”。

### Tool Gateway

```bash
om-agent spec
om-agent run --tool healthcheck \
  --input-json '{"config_key":"us"}'
```

`om-agent spec` 用于发现当前环境公开的工具及其调用合同；`om-agent run` 用于按工具名执行一次结构化调用，必须根据 manifest 传入符合 schema 的参数。

`om-agent` 是给外部 agent、脚本和操作者使用的结构化 Tool Gateway，不是 OM 自己的自治 Agent。每个工具的 manifest 会声明：

- `read_only`
- `risk_level`
- `side_effects`
- `requires_confirm`
- `requires_env`
- `safe_default_input`

`read_only=true` 表示不修改产品事实或配置，不一定表示不会物化本地报告/cache。调用前按 manifest 判断，不从工具名猜副作用。JSON envelope 与集成合同见 [Agent Integration](docs/AGENT_INTEGRATION.md)。

## 配置与数据

| 数据 | 权威位置 |
|---|---|
| 人工配置 | `config.yaml` |
| US/HK 运行快照 | `config.us.json` / `config.hk.json` |
| Assistant 运行快照 | 本地 init 为 `config.assistant.json`；服务 profile 通常使用 `resolved/config.assistant.json` |
| Secrets / 写入开关 | env-file |
| 期权事实 | `<runtime_root>/output_shared/state/option_positions.sqlite3` |
| 单次运行 | `<runtime_root>/output_runs/<run_id>/` |
| 共享状态与报告 | `<runtime_root>/output_shared/` |
| 账户级输出 | `<runtime_root>/output_accounts/<account>/` |

账户标签使用小写，例如 `lx`、`sy`。账户类型为 `futu` 或 `external_holdings`；数据源和 trade-intake 能力从账户设置派生，不能把一个账户的现金、持仓或状态 fallback 到另一个账户。

Feishu 有三种彼此独立的角色：

- 可选的 `external_holdings` 数据源；
- `feishu_app` 出站通知；
- Feishu long-connection 入站消息。

这些角色不使 Feishu 成为期权账本事实源。

## 副作用与确认

仓库没有一个适用于所有命令的统一 `--write` 语法。按实际能力分级：

| 类型 | 例子 | 默认边界 |
|---|---|---|
| 纯读取 | `config validate`、`runtime_status`、`daily-brief latest`、`query_cash_headroom` | 不写产品状态 |
| 本地物化 | `run tick --no-send`、`scan_opportunities`、`get_close_advice` | 可写 run/report/cache，不发送 |
| 受控本地写入 | config/symbol/account 编辑、Research artifact | 通常 dry-run 或显式 apply/write；以子命令为准 |
| 高风险写入 | trade event、lot、服务、Feishu、真实发送 | 需要明确目标和显式确认 |

以下操作在执行前必须确认精确目标：

- 发送真实通知；
- 修改生产 `config.yaml`、runtime JSON、secrets 或 env-file；
- 写 trade events、position lots、Feishu 或 broker-facing state；
- 安装、启停或修改 systemd / launchd 服务；
- 删除 runtime outputs、state、cache、SQLite 或历史证据。

## 部署与运维

代码目录与运行目录必须分离。典型 Linux 布局：

```text
<deploy-home>/apps/options-monitor/current   # code/release
/var/lib/options-monitor                    # runtime state
/etc/options-monitor/options-monitor.env    # secrets/settings
```

服务文件先生成到临时目录供人工检查；`service render` 会写输出文件，但不会自动安装或启动服务：

```bash
om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --markets us hk \
  --accounts lx sy \
  --output-dir /tmp/options-monitor-service
```

平台部署、升级、回滚和服务检查见 [DEPLOY.md](DEPLOY.md)、[Linux / Mac Deployment](docs/DEPLOY_LINUX_MAC.md) 与 [RUNBOOK.md](RUNBOOK.md)。

## 文档

从 [Docs Index](docs/INDEX.md) 开始。主要权威文档：

- [Getting Started](docs/GETTING_STARTED.md)：首次安全运行。
- [CONFIGS.md](CONFIGS.md)：配置事实源、生成链路与迁移契约。
- [配置指南](CONFIGURATION_GUIDE.md)：账户、市场、环境变量和验证方法。
- [产品架构](docs/PRODUCT_ARCHITECTURE.md)：产品域与模块关系。
- [系统架构](docs/ARCHITECTURE.md)：技术分层与真实调用链。
- [策略架构](docs/STRATEGY_ARCHITECTURE.md)：Sell Put、Covered Call、Combo Yield。
- [Ledger Architecture](docs/LEDGER_ARCHITECTURE.md)：交易与持仓事实边界。
- [Tool Reference](docs/TOOL_REFERENCE.md)：当前 Tool Gateway 分类和 manifest 使用。
- [RUNBOOK.md](RUNBOOK.md)：巡检、故障诊断和应急操作。

`docs/gateflow/`、`docs/reviews/` 和 `docs/plans/` 是阶段性工作流证据，不是当前产品或运行契约。遇到冲突时，以当前源码、配置验证器、测试、runtime evidence 和上述 living docs 为准。

## 风险提示

本项目只做监控、筛选、报告、提醒和人工复盘，不构成投资建议。任何下单前都应自行复核价格、流动性、费用、保证金、仓位暴露、事件风险和数据新鲜度。
