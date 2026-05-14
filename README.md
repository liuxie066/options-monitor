# options-monitor

`options-monitor` 是一个本地运行的期权监控与提醒工具，主要服务 **Sell Put**、**Covered Call** 和 **Sell Put 收益增强**工作流。

它不是自动交易系统，也不直接替你下单。它负责把行情、账户、持仓、现金、策略阈值和通知串起来，帮助你判断“现在有没有值得进一步人工复核的机会”。

## 它解决什么

核心能力：

- 扫描关注标的的期权链
- 按 Sell Put / Covered Call 规则筛选候选
- 结合账户现金、股票持仓和已开期权仓位计算可用空间
- 为合格 Sell Put 候选寻找可搭配买入的 Call，形成收益增强候选
- 生成报告、通知文本、平仓建议和运行状态
- 给本地 Agent 提供结构化工具入口，便于安全排查和自动化读取

主要入口：

| 入口 | 面向对象 | 用途 |
|---|---|---|
| WebUI | 普通用户 | 首次初始化、编辑配置、查看常用设置 |
| `./om` | 人工 CLI | 手动运行、配置构建、持仓维护、现金查询 |
| `./om-agent` | Agent / 程序 | JSON 工具清单、只读诊断、结构化查询 |

## 快速开始

### 1. 安装

```bash
git clone <repo-url> options-monitor
cd options-monitor
python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt
```

需要可复现安装时使用约束文件：

```bash
./.venv/bin/pip install -r requirements.txt -c constraints.txt
```

`requirements.txt` 已包含 `futu-api`。本项目的行情、期权链、现金和持仓默认依赖本机 OpenD / Futu API。

已有 `config.us.json` 时，可以用一键脚本安装依赖并运行 watchlist pipeline：

```bash
./run_watchlist.sh
```

如果只是首次安装或排障，先完成下面的初始化和验证，不要从真实 pipeline 开始。

### 2. 初始化配置

推荐先用 WebUI：

```bash
./run_webui.sh
```

默认地址：

```text
http://127.0.0.1:8000
```

首次初始化通常会生成：

- `config.us.json` 或 `config.hk.json`
- `secrets/portfolio.sqlite.json`
- 可选通知、Feishu、OpenClaw 相关配置

如果不用 WebUI，可以复制模板：

```bash
cp configs/examples/user.common.example.json configs/user.common.json  # 可选
cp configs/examples/user.example.us.json configs/user.us.json
cp configs/examples/user.example.hk.json configs/user.hk.json
mkdir -p secrets
cp configs/examples/portfolio.sqlite.example.json secrets/portfolio.sqlite.json
./om config build --market us
./om config build --market hk
```

日常通常只编辑：

- `configs/user.us.json`
- `configs/user.hk.json`
- `configs/user.common.json`（可选，共用覆盖）
- `secrets/portfolio.sqlite.json`
- `secrets/notifications.feishu.app.json`（启用飞书通知时）

生成后的 `config.us.json` / `config.hk.json` 是运行时入口，通常不手工编辑。

### 3. 验证配置

纯配置语义校验：

```bash
./om config validate --config-path config.us.json
```

Agent 推荐使用结构化入口：

```bash
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

判断规则：

| 想确认 | 用什么 |
|---|---|
| 配置字段是否合法 | `config_validate` |
| 本机环境、OpenD、SQLite、通知前置条件是否可用 | `healthcheck` |
| 已有运行产物和通知内容长什么样 | `runtime_status` |
| OpenClaw 环境是否 ready | `openclaw_readiness` |

## 常用工作流

### 运行一次监控

正式 tick 入口只有一条：

```bash
./om run tick --config config.us.json --accounts lx
./om run tick --config config.us.json --accounts lx sy
```

传一个账户就是单账户运行，传多个账户就是多账户运行。不传 `--accounts` 时读取 runtime config 顶层 `accounts`。

排障或演练时先禁发通知：

```bash
./om run tick --config config.us.json --accounts lx sy --no-send
```

旧的 `scripts/send_if_needed.py` / `scripts/send_if_needed_multi.py` 入口已移除；定时任务应使用 `./om run tick`。

### 扫描机会

人工 CLI：

```bash
./om scan --config-key us --symbols NVDA,TSLA --top-n 5
```

Agent：

```bash
./om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":5}'
```

该流程会读取行情并写本地报告；它不会发送通知。

解释最近候选为什么这样排序：

```bash
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
```

该工具只读取已有候选 CSV，不重新扫描、不发送通知、不写报告。需要指定文件时传
`candidate_path`，需要看权重变化影响时传 `score_weights` 和 `compare_baseline=true`。

### 查询 Sell Put 现金余量

人工 CLI：

```bash
./om sell-put-cash --market 富途 --account lx
./om sell-put-cash --market 富途 --account sy
```

Agent：

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
```

返回账户现金、Sell Put 担保占用和剩余可用现金，并支持折算到 CNY。

### 查看平仓建议

人工 CLI：

```bash
./om close-advice --config-key us
```

Agent 推荐用一站式工具：

```bash
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

平仓建议依赖本地 option positions、required data 和行情缓存；如果结果异常，先跑 `healthcheck`，再确认本地持仓数据。

### 管理 watchlist

```bash
./om watchlist list
./om watchlist add TCOM --put
./om watchlist edit TCOM --set sell_put.max_strike=45
./om watchlist rm TCOM
```

Agent 可先只读列出：

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

写入 symbol 配置需要 `dry_run` 或显式写入门禁。不要让 Agent 默认直接改 runtime config。

### 维护 option positions

本地 SQLite 是 option positions 的稳态主存储。

```bash
./om option-positions list --broker 富途 --account lx --status open
./om option-positions add --account lx --symbol 0700.HK --option-type put --side short --contracts 1 --currency HKD --strike 420 --multiplier 100 --exp 2026-04-29 --dry-run
```

写入前先 `--dry-run`。Feishu `option_positions` 只用于可选 bootstrap 和远端镜像，不是稳态主存储。

## 策略模型

### Sell Put

Sell Put 规则通常配置：

- DTE 区间：`sell_put.min_dte` / `sell_put.max_dte`
- strike 区间：`sell_put.min_strike` / `sell_put.max_strike`
- 最低年化净收益：`sell_put.min_annualized_net_return`
- 最低净收入：模板或 symbol 级 `min_net_income`
- 流动性与价差过滤：`min_open_interest`、`min_volume`、`max_spread_ratio`

扫描会结合现金占用、已开 short put、当前价格和期权链报价做过滤。

### Covered Call

Covered Call 规则通常配置：

- `sell_call.enabled=true`
- DTE 区间
- call strike 下限或上下限
- `min_strike_cost_multiplier`

Covered Call 可卖数量来自持仓数据，系统会扣除已被 short call 锁定的股票数量。`avg_cost` / `shares` 不再写在 symbol 配置里，而是从 holdings 读取。
扫描会按 `avg_cost * min_strike_cost_multiplier` 抬高有效 call strike 下限，避免推荐低于成本底线的 Covered Call。

### Sell Put 收益增强

收益增强配置在标的顶层 `yield_enhancement`：

```json
{
  "symbol": "NVDA",
  "sell_put": {
    "enabled": true,
    "min_dte": 20,
    "max_dte": 60,
    "min_strike": 150,
    "max_strike": 160
  },
  "yield_enhancement": {
    "enabled": true
  }
}
```

它依赖 `sell_put.enabled=true`。收益增强不是 Sell Put 高收益候选的二轮筛选，而是在 Sell Put 的 symbol、strike、DTE、现金覆盖和基础风险边界内，寻找“short put 权利金可覆盖 long call 成本”的组合。即使 `sell_call.enabled=false`，启用收益增强后也会补取 Call 期权链。

常用调优项：

- `output_mode`: `separate`、`inline`、`both`
- `objective`: 默认 `premium_funded_long_call`
- `funding_mode`: 默认 `credit_or_even`
- `min_combo_net_credit`: 默认 `0.0`
- `max_call_cost_to_put_credit`: 默认 `1.0`，即 Call 成本不超过 Put 权利金
- `min_upside_lift_to_call_cost` / `min_upside_lift_to_put_credit`: 要求上行潜在收益相对 Call 成本和 Put 权利金足够大
- `call.min_strike` / `call.max_strike`
- `call.min_otm_pct` / `call.max_otm_pct`
- `call.min_delta` / `call.max_delta`
- `max_debit_native`: 配合 `funding_mode=max_debit`
- `min_open_interest` / `min_volume` / `max_spread_ratio` / `max_combo_spread_ratio`
- `scenario_move_factors` / `scenario_weights` / `min_scenario_score`

主要输出：

- `<symbol>_yield_enhancement_candidates.csv`
- `<symbol>_yield_enhancement_alerts.txt`
- `symbols_summary.csv` 中的 `strategy=yield_enhancement`
- 通知里的 `Enhancement` 分区，或 Sell Put 候选下的 `收益增强: 推荐Call=...`

## 配置心智模型

### Canonical runtime config

运行时只认两类 canonical config：

- `config.us.json`
- `config.hk.json`

如果使用分层配置，编辑源是：

- `configs/system.json`
- `configs/user.common.json`（可选）
- `configs/user.us.json`
- `configs/user.hk.json`

然后生成 runtime config：

```bash
./om config build --market us
./om config build --market hk
```

查看某个值来自哪里：

```bash
./om config explain --market us --key option_positions.sync_to_feishu.enabled
./om config explain --market us --key symbol_defaults.fetch.limit_expirations
```

更完整的配置规则见 [CONFIGS.md](CONFIGS.md) 和 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。

### 数据来源

最小配置默认：

- 行情与期权链：OpenD / Futu API
- 持仓与现金：OpenD / Futu API
- option positions：本地 SQLite
- 通知：默认不启用，需要显式配置

Feishu 只在这些场景需要：

- holdings / external_holdings 数据源
- option positions 空库 bootstrap
- option positions 远端镜像
- 飞书通知

当前 symbol required-data 运行时不支持从 OpenD / Futu 自动降级到 Yahoo。`yahoo` / `yfinance` 只用于显式非 Futu source 语义或独立事件风险数据抓取，不是 OpenD fallback。

### 多账户

- 账户标签使用小写，例如 `lx`、`sy`
- 默认账户列表来自 runtime config 顶层 `accounts`
- 单账户和多账户使用同一条 tick 链路
- Feishu-backed 账户应使用 `external_holdings` account type，并通过 `portfolio.source_by_account` 指向 `holdings`

## 通知

支持的正式通知链路：

- 飞书开放平台应用发个人消息
- 微信 Clawbot，通过 OpenClaw `message send` 通道发送

统一 tick 通知语义：

- 同一个通知目标下，每个账户各发送一条消息
- 某个账户发送失败不会阻断其他账户
- 只有发送成功的账户会更新 notified 状态
- 通知内容保持 Markdown-friendly，不做卡片化纯文本

通知凭证默认位置：

- 飞书：`secrets/notifications.feishu.app.json`
- 微信 Clawbot：配置 `notifications.channel=wechat_clawbot` 和 OpenClaw 目标字符串

具体字段见 [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)。命令副作用见 [RUNBOOK.md](RUNBOOK.md)。

## Agent 使用指南

### 安装 Agent 工具

```bash
bash scripts/install_agent_plugin.sh
./om-agent spec
```

常用只读入口：

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
```

OpenClaw 环境：

```bash
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
```

如果生产目录、账户或 cron id 与默认路径不同，可以复制：

```bash
cp configs/examples/openclaw.profile.example.json openclaw.profile.json
```

然后传入：

```bash
./om-agent run --tool openclaw_readiness --input-json '{"profile_path":"openclaw.profile.json"}'
```

### Agent 默认规则

Agent 处理这个仓库时应遵守：

- 解释、调查、代码阅读类任务先读文件，不要先跑脚本
- 优先使用 `./om-agent`，其次 `./om`，再考虑 `python3 -m ...`
- 不要把 `python3 scripts/...` 当作第一选择
- 发送通知、写持仓、写 Feishu、修改生产配置、删除运行产物前必须获得用户明确要求
- 有 dry-run、validate、healthcheck 或 test 时，先用低风险入口
- 写工具需要 `OM_AGENT_ENABLE_WRITE_TOOLS=true` 和 payload/CLI 里的显式确认

可直接给 Agent 的最短提示：

> This repo is operations-sensitive. For explanation, investigation, or code-reading requests, inspect files and summarize first. Prefer `./om-agent`, then `./om`, then `python3 -m ...`. Use `python3 scripts/...` only when explicitly requested or when no higher-level entry point covers the task. Never send notifications, mutate runtime state, write Feishu, or edit production config unless the user explicitly asks for it.

更多工具合同见 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) 和 [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)。

## 定时与长驻任务

README 只记录公开入口和安全边界；生产 OpenClaw cron 的具体 id、启停和排障步骤见 [RUNBOOK.md](RUNBOOK.md)。

| 任务 | 推荐入口 | 运行方式 | 主要副作用 |
|---|---|---|---|
| 期权监控 / 扫描通知 | `./om run tick --config config.us.json --accounts lx sy` | 交易时段定时执行 | 读取行情和持仓，生成报告，可能发送通知并写运行状态 |
| 调度状态检查 | `./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'` | 定时或排障只读检查 | 无业务写入 |
| 交易监听 / 自动入账 | `python3 -m src.application.auto_trade_intake --config config.us.json --mode apply` | 长驻进程 | 监听 OpenD 成交推送，写本地 option positions 和 intake state |
| 持仓镜像同步 | `./om option-positions sync-feishu --config config.us.json --apply` | 低频定时或人工触发 | 写 Feishu 镜像表，并更新本地 sync metadata |
| 版本检查 | `./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'` | 低频只读检查 | 无业务写入 |
| 版本更新预览 | `./om-agent run --tool version_update --input-json '{"bump":"patch"}'` | dry-run | 不写 `VERSION` |

不要把 `{"bump":"patch","apply":true,"confirm":true}` 放进固定频率任务；它每次都会递增本地 `VERSION`。真正发布时应由发布流程传入明确目标版本。

## 排障顺序

优先顺序：

1. 读相关代码、配置文档和测试
2. 跑 `healthcheck`
3. 跑 `config_validate`、测试或 dry-run
4. 必要时再运行真实 tick 或其他会写状态的命令

常见问题：

| 症状 | 先看什么 |
|---|---|
| 配置校验失败 | `notifications.*`、`trade_intake.account_mapping.futu`、`account_settings.*`、`symbols[]` |
| 只看到一个 OpenD endpoint | 账户映射、OpenD 在线状态、账户级持仓连接 |
| 两个账户持仓一样 | Futu account id 映射、`portfolio.source_by_account`、账户级 data source |
| 通知保存或发送失败 | `notifications.channel`、`notifications.target`、secrets 文件、OpenClaw 目标 |
| option positions 异常 | SQLite 路径、lot/event 状态、rebuild / inspect / reconcile 输出 |

副作用边界：

| 命令 / 工具 | 写本地 | 写远端 | 发通知 |
|---|---:|---:|---:|
| `./om-agent run --tool config_validate ...` | 否 | 否 | 否 |
| `./om-agent run --tool healthcheck ...` | 否 | 否 | 否 |
| `./om-agent run --tool runtime_status ...` | 否 | 否 | 否 |
| `./om run tick --config ... --no-send` | 是 | 可能 | 否 |
| `./om run tick --config ...` | 是 | 可能 | 是 |
| `python3 -m src.application.auto_trade_intake --mode apply` | 是 | 否 | 否 |
| `./om option-positions sync-feishu --apply` | 是 | 是 | 否 |

## 文档导航

- [CONFIGS.md](CONFIGS.md)：canonical config、分层配置和迁移规则
- [CONFIGURATION_GUIDE.md](CONFIGURATION_GUIDE.md)：字段说明、数据来源和配置边界
- [RUNBOOK.md](RUNBOOK.md)：运维巡检、定时任务和应急操作
- [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)：Agent 接入快速开始
- [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)：Agent JSON 合同
- [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)：`om-agent` 工具说明
- [docs/OPTION_POSITIONS_MIGRATION.md](docs/OPTION_POSITIONS_MIGRATION.md)：option positions 迁移
- [docs/OPTION_POSITIONS_REPAIR.md](docs/OPTION_POSITIONS_REPAIR.md)：option positions 修复
- [tests/README.md](tests/README.md)：测试分层和运行方式

## 风险提示

本工具只做监控、筛选、报告和提醒，不构成投资建议。期权交易风险较高，任何下单都需要自行复核标的、价格、仓位、保证金、流动性和事件风险。
