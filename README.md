# options-monitor

`options-monitor` 是一个本地运行、人工决策优先的期权监控系统。它把行情、现金、正股持仓、期权账本、策略规则、报告和通知串成一条可审计链路，帮助用户完成：

- `Cash-Secured Put (CSP)` 与 `Covered Call (CC)` 候选筛选；
- `Combo Yield` 组合候选评估；
- Wheel 已启用时，符合身份与交割证据的外部 CSP / CC 指派进入对应轮转分支；
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
└─ om config build-bot
   └─ resolved/config.bot.json
```

期权持仓只有一套事实链：

```text
trade_events -> projection -> position_lots
```

- `config.yaml` 是人工编辑源；生成的 JSON 是运行快照，不是日常手工编辑入口。
- 本地 SQLite 是期权交易与持仓事实源；Feishu 不承载 `option_positions` 镜像。
- Futu 账户现金由共享入口读取并判定可信度；扫描、查询、报告、Wheel 和指派情景采用同一套现金标准，有效期统一由 `runtime.portfolio_context_ttl_sec` 控制，缺少证据时明确标为不可用。
- 普通手工 `tick` 不自动发送 scheduled ordinary notification；生产调度使用受保护的 `tick-cron`。
- `./om-agent spec` 是 Tool Gateway 工具名、输入 schema、风险级别和副作用的权威清单。
- 当前汇率统一取最新有效报价；已核实连续休市期间，沿用价也可用于开仓筛选和资金能力计算，保留真实报价时间，交易时段缺价或来源不可验证时明确不可用。
- 缺少行情、费用、历史汇率、事件或身份事实时，系统显式返回 missing、partial 或 not-evaluable，不补造数据。

产品域见 [产品架构](docs/PRODUCT_ARCHITECTURE.md)，技术调用链见 [系统架构](docs/ARCHITECTURE.md)。

## 主要能力与入口

| 能力 | 当前入口 | 权威说明 |
|---|---|---|
| YAML 配置构建与校验 | `om config` | [CONFIGS.md](CONFIGS.md) |
| CSP / CC | `om run tick`、`om scan` | [策略架构](docs/STRATEGY_ARCHITECTURE.md) |
| Combo Yield | 与开仓扫描同链路 | [策略架构](docs/STRATEGY_ARCHITECTURE.md) |
| 轮转策略 | `om run tick`、`om wheel` | [轮转策略 PRD](docs/WHEEL_STRATEGY_PRD.md) |
| Close Advice | 计划 Tick 生产、`./om-agent run --tool close_advice_read` 读取、`om close-advice configure` 启停 | [Close Advice Contract](docs/CLOSE_ADVICE_CONTRACT.md) |
| Daily Decision Brief | `om daily-brief` | [通知体验 PRD](docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md) |
| 决策历史与明确关联结果 | `om-agent run --tool decision_history_read`；旧记录通过 `om decision-history import` 显式迁移 | [决策历史](docs/DECISION_HISTORY.md) |
| 期权账本与生命周期 | `om option-positions`、`om trade-events` | [Ledger Architecture](docs/LEDGER_ARCHITECTURE.md) |
| 期权收益与现金 | `om option-performance` | [Option Performance](docs/OPTION_PERFORMANCE_DESIGN.md) |
| 全部 CSP / CC 指派压力测试 | `om portfolio assignment-scenario` | 本 README 的“指派后资产分布” |
| 本地 Bot（受控 US/HK 只读） | `om bot` | [Inbound Control](docs/INBOUND_CONTROL.md) |
| 结构化 Tool Gateway | `om-agent spec`、`om-agent run --tool <name> --input-json '<json>'` | [Tool Reference](docs/TOOL_REFERENCE.md) |
| Research 取证与归档 | `om research` | [Agent Handbook](docs/AGENT_WIKI.md) |
| 运行诊断、服务与版本升级 | `om status`、`om service`、`om update` | [RUNBOOK.md](RUNBOOK.md) |

本表是主要能力索引，不是 CLI 或 Tool Gateway 的完整命令清单。人工任务按“首次安装”和“日常管理”在 `om help` 中组织；完整顶层命令见 `om help all`。结构化工具名、输入 schema、风险级别和副作用以 `om-agent spec` 为准。

### 轮转策略

轮转策略管理卖出 Put 后买入股票、卖出 Call 后交付股票形成的分支。Wheel 已启用且身份与交割证据完整时，外部 CSP / CC 及对应 Combo 卖腿的指派会自动进入相应分支。成交可对应多个分支或证据不足时，Daily Brief 会持续提示待人工确认，用户可在 Control 预览并确认归属；同一笔多张 Call 成交可按张数分配给多个 Wheel 股票分支，系统不自行选择分支。当前门槛关闭后不再产生新的 Wheel 开仓候选；已有分支与真实成交仍按账本事实保留。

在持股分支，已收到的期权收入会计入总收益，但不用于降低股票的卖出底线；系统合并检查所有可能占用持股的期权合约。在现金分支，新 Put 候选仍须通过资金门槛。该策略只提供监控和候选建议，不自动下单，也不保证收益。

CSP / CC 新开仓只使用 `insurance_underwriting`。历史 artifact 和持仓解释可继续读取
`return_first` / `short_vol`，但这些兼容语义不能重新进入当前开仓配置或正式候选排序。

README 不复制完整规则：[候选策略合同](docs/candidate_strategy.md) 是已经确认的目标口径，
[策略架构](docs/STRATEGY_ARCHITECTURE.md) 定义模块责任；目标是否已经上线必须以当前代码和测试验证，
不能只凭文档判断。

### 策略术语对照

| OM 内部 key | 项目内名称 | 金融专业术语 |
|---|---|---|
| `sell_put` | Cash-Secured Put (CSP) | 现金担保认沽（Cash-Secured Put） |
| `sell_call` | Covered Call (CC) | 备兑看涨（Covered Call） |
| `combo_yield`（结构 `csp_lc`） | Combo Yield CSP+LC | 看涨风险反转（Bullish Risk Reversal，现金担保变体） |
| `combo_yield`（variant `cc_lp`） | Combo Yield CC+LP | 领口策略（Collar，净收权利金即 Credit Collar） |
| `wheel` | 轮转策略 | 轮转策略（Wheel Strategy） |

结构说明、别名与参考来源见 [策略术语对照](docs/STRATEGY_TERMINOLOGY.md)。

## 安装

运行时要求 Python 3.12 或更高版本。

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh | bash
"$HOME/.local/bin/om" setup init
```

第二行直接使用安装器创建的 wrapper，无需先修改 `PATH`。无参数安装会解析最新 GitHub Release，不跟随浮动 `main`；具体参数以安装版的 `om setup init --help` 为准。固定版本和自定义安装目录见 [Install](docs/INSTALL.md)。

安装器会准备 release 目录、Python 环境和 `om` / `om-agent` 用户级 wrapper；不会创建生产配置、写入 secrets、安装服务或启动定时任务。完整平台要求、目录布局和源码安装方式见 [Install](docs/INSTALL.md)。

在源码 checkout 中，以下示例里的 `om` / `om-agent` 可替换为 `./om` / `./om-agent`。

## 五分钟开始

### 1. 初始化配置

第一次安装后运行：

```bash
om setup init
```

引导显示平台默认目录，依次填写市场、OpenD 地址、REAL/SIMULATE 环境、账户标签、富途账户 ID 和监控标的。首次完成一个账户，之后在日常管理中添加账户；同市场账户共享标的。每个标的必须选择 CSP、CC 或两者；CSP 必填最高行权价，CC 必填最低行权价，另一端边界可选。账户标签由用户填写，无默认值；账户标签、账户 ID 和标的不能为空，也不会填入示例标的。预览实际选择，输入 `yes` 后才保存 YAML、运行快照及 `~/.config/options-monitor/runtime-root` 目录记录。

通知通道、Bot LLM 和常驻服务逐项询问，可以跳过；新配置的通知与 Bot 默认关闭。密钥在终端隐藏输入。服务安装、启动分别预览确认；首次手动运行默认不发通知。重新运行 `om setup init` 可以继续可选步骤，已保存的配置保留。然后检查：

```bash
om setup check --format text
```

脚本化初始化使用 `om setup init --help` 中的完整参数；账户标签须通过 `--account-label` 提供，写入还需要明确 `--futu-acc-id`、`--trd-env`、市场、用户标的和策略边界，先用 `--dry-run` 预览，再以相同参数改用 `--apply`。已有目标拒绝覆盖；异常中断留下的文件需先核对。高级占位配置仍可用 `om config init` 创建，同样须提供账户标签，未完成时不算就绪。`om setup check` 检查离线配置与安装条件，Bot 单独报告，不验证券商登录或通知可达。显式配置路径和有效 `OM_RUNTIME_ROOT` 优先于用户目录记录。完整说明见[首次运行指南](docs/GETTING_STARTED.md)和[配置指南](CONFIGURATION_GUIDE.md)。

之后增删监控标的用人工 CLI；不直接编辑生成的 JSON。新增和删除默认只预览，核对后追加 `--apply`，命令会同时发布 `config.yaml`、已配置市场和 Bot 的运行快照：

```bash
om symbols list --market us
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE --apply
```

将 `YOUR_SYMBOL` 和 `YOUR_MAX_STRIKE` 换成自己的标的与 CSP 行权价上限。新增时必须选 `--strategy csp|cc|both`；选 CC 时须给 `--cc-min-strike`，选 both 时两项都要给。CSP 下限和 CC 上限可选，详见 `om symbols add --help`。`NVDA` 会识别为美股，`0700.HK` 会识别为港股；`--market` 可省略，若指定则必须与标的一致。只配置一个市场时，`om symbols list` 也可省略 `--market`；配置多个市场时需指定。

在终端运行 `om`，日常菜单提供结果与状态、账户与标的、通知与 Bot、策略与全局持仓风险、运行维护五组任务。也可以直接调用：

| 任务 | 命令 |
|---|---|
| 查看账户或标的 | `om accounts list`、`om symbols list` |
| 配置通知与 Bot | `om channel configure`、`om bot configure` |
| 配置可选 Holdings 来源 | `om holdings configure`；依赖同机 PM，只补充全局持仓风险中的非富途资产 |
| 开关平仓建议 | `om close-advice configure` |
| 管理常驻服务 | `om service install`、`om service start`、`om service stop`；默认预览 |

`om help` 按场景说明任务；`om help all` 保留所有高级命令。模型与控制命令统一推荐 `om bot`，Feishu 传输用 `om channel feishu`；旧 `assistant` / `inbound` 入口保留兼容。普通设置和静态凭证检查不证明 OpenD 登录、消息送达或模型可用。Linux 密钥录入和服务安装涉及系统权限，按终端提示完成；当前普通前台进程不能直接读取 systemd 加密凭证，详见[首次运行指南](docs/GETTING_STARTED.md#3-完成外部接入)。结构化集成使用 `om-agent spec` 和 `om-agent run`，边界见 [Tool Reference](docs/TOOL_REFERENCE.md)。

### 2. 只读检查

```bash
om config validate --config-key us
om doctor --config-key us
om status --config-key us
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
OM_CONFIG_DIR="$(cat "$HOME/.config/options-monitor/runtime-root")"
om run tick --config "$OM_CONFIG_DIR/config.us.json" --accounts lx --no-send --force
```

`--force` 让这次手动扫描跳过计划时段限制。`--no-send` 只表示不发通知；扫描仍会读取外部数据并写本地 run、报告、cache 和状态 artifact。它不是 no-write 模式。

示例中的 `lx` 换成初始化时选择的账户标签。检查结果后，可继续手工扫描：

```bash
om run tick --config "$OM_CONFIG_DIR/config.us.json" --accounts lx --force
```

计划内扫描和普通通知使用 guarded scheduler：

```bash
om run tick-cron --market us --config "$OM_CONFIG_DIR/config.us.json" --accounts lx --timeout 600
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

把所选账户中所有 open short CSP 和 CC 同时按 strike 实物指派，并按当前现货价格与当前显式汇率证据计算 CNY 资产分布、现金覆盖、费用、到期梯度和潜在负债：

```bash
om portfolio assignment-scenario --accounts lx sy
om portfolio assignment-scenario --accounts lx sy --format json

om-agent run --tool portfolio_assignment_scenario \
  --input-json '{"accounts":["lx","sy"]}'
```

该功能是纯读压力测试，不写 assignment event、不修改 `position_lots`、不修改 portfolio-management 持仓，也不发送通知。固定口径：

- 只处理 open short put/call；Long Option 完全不读取、不估值、不保留；
- 富途账户股票、现金与 MMF 从 OpenD 读取，富途股票现价也以 OpenD 为准；OM SQLite 提供 short option lot；
- `portfolio.holdings.enabled` 默认关闭；开启后仅补充 PM Holdings 中经预检确认的非富途资产，不重复计入 PM 的富途股票、现金和 MMF 副本；
- 非富途资产估值使用 PM 报价和显式 FX；PM 证据不可用时保留富途基线并标记 `partial`；
- MMF 并入现金，资金覆盖统一用 CNY；账户、券商和币种拆分仍保留作操作约束；
- 股票按当前 spot 估值，指派现金按 strike 结算；历史已收权利金不重复计入；
- 费用复用统一股票费用计算器；缺少券商、币种或指派费用规则时返回 `partial` 和 `null`，不按 0 处理；
- 现金不足形成 funding liability，CC 覆盖不足形成 short-stock liability，不会被改写成执行错误。

Bot 通过同一个 `portfolio_assignment_scenario` 纯读工具调用，不维护第二套触发词或计算逻辑。Bot 按场景加载该工具；需要 PM 补充时显式开启 PM 集成和 Holdings，并保持 portfolio-management API 仅在同机 loopback 提供服务。
渠道 Bot 默认只读本渠道市场；只有显式配置 `bot.read_markets: [us, hk]` 后，已鉴权用户才可按标的和目标市场读取另一市场的配置账户。`assignment` 本地事件与成交归属分别取证，不代表券商确认；详见 [Bot 边界](docs/INBOUND_CONTROL.md#bot-boundary)。

### Close Advice

新报告只由计划 Tick 的 sealed required-data 路径生成。它把 run ID、snapshot manifest 和 Close Advice plan 绑定到同一份不可变输入；缺少或不一致时失败关闭。`om close-advice` 仅提供启停配置：

```bash
om close-advice configure
```

读取已有报告不会刷新持仓或行情，也不会生成新建议：

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
  --period ytd

om-agent run --tool option_performance_report \
  --input-json '{"config_key":"us","account":"lx","period":"ytd","as_of_date":"2026-07-17"}'
```

报告只提供期权净现金流、卖出/买入期权胜率和期权收益率，支持 MTD/YTD，金额保持
原币。正股交易、指派/行权交割现金、PnL、CNY 换算和行情刷新均不在该报告内。
券商确认的零价期权平仓按成交时间更新未平仓数量；原因待证时胜率暂不可算，
现金和费用证据完整时仍可计算收益率。历史未入账成交不会自动补账。

### Research

只收集并输出到终端、不写 evidence bundle：

```bash
om research collect \
  --config-key us \
  --scope full \
  --output both \
  --no-write-outputs
```

`om research collect` 默认不写 evidence bundle；写入需要同时使用
`--write-outputs --confirm`。`archive pull` 默认只生成同步计划，只有 `--write`
才拉取本地归档；`archive inventory` 与 `archive verify` 用于检查本地归档。执行前先看具体
子命令的 `--help`，不要把 Research 整体理解成“永远只读”。Research 不自动修改生产配置、
交易状态或通知。

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
| Bot 运行快照 | `om setup init` 默认生成 `<runtime_root>/resolved/config.bot.json`；高级命令可显式指定输出位置 |
| 普通设置 / 写入开关 | `options-monitor.env` 或显式选择的 env-file |
| Secrets | macOS Keychain / Linux systemd 加密凭证，使用 `om secrets` 管理，见 [密钥存储](docs/SECRET_STORAGE.md) |
| 期权事实 | `<runtime_root>/output_shared/state/option_positions.sqlite3` |
| 单次运行 | `<runtime_root>/output_runs/<run_id>/` |
| 共享状态与报告 | `<runtime_root>/output_shared/` |
| 账户级输出 | `<runtime_root>/output_accounts/<account>/` |

账户标签使用小写，例如 `lx`、`sy`。账户类型为 `futu`；账户现金与股票持仓来自对应富途账户，trade-intake 能力从账户设置派生。富途失败时不使用 Holdings 回填账户数据。

Feishu 在本项目中的角色：

- 独立的 Holdings 持仓上下文导出命令（开仓扫描不读取它计算风险）；
- `feishu_app` 出站通知；
- Feishu long-connection 入站消息。

这些角色不使 Feishu 成为期权账本事实源。

## 副作用与确认

仓库没有一个适用于所有命令的统一 `--write` 语法。按实际能力分级：

| 类型 | 例子 | 默认边界 |
|---|---|---|
| 纯读取 | `config validate`、`runtime_status`、`daily-brief latest`、`query_cash_headroom` | 不写产品状态 |
| 本地物化 | `run tick --no-send`、`scan_opportunities` | 可写 run/report/cache，不发送 |
| 受控本地写入 | config/symbol/account 编辑、Research artifact | 通常 dry-run 或显式 apply/write；以子命令为准 |
| 高风险写入 | trade event、lot、服务、Feishu、真实发送 | 需要明确目标和显式确认 |

以下操作在执行前必须确认精确目标：

- 发送真实通知；
- 修改生产 `config.yaml`、runtime JSON、secrets 或 env-file；
- 写 trade events、position lots、Feishu 或 broker-facing state；
- 安装、启停或修改 systemd / launchd 服务；
- 删除 runtime outputs、state、cache、SQLite 或历史证据。

## 部署与运维

运行 `om` →「日常管理」→「运行与维护」→「service」，可以预览并确认安装、启动或停止服务。安装定义与启动分开确认；也可直接使用 `om service install/start/stop`，默认只预览。平台要求和确认参数见 [安装指南](docs/GETTING_STARTED.md#6-可选长期运行服务)。

代码目录与运行目录必须分离。典型 Linux 布局：

```text
<deploy-home>/apps/options-monitor/current   # code/release
/var/lib/options-monitor                    # runtime state
/etc/options-monitor/options-monitor.env    # ordinary process settings
/etc/credstore.encrypted                    # encrypted systemd credentials
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

Linux 主机预置加密凭据后，推荐在 render 时显式加上 `--include-secret-credentials`，按 unit 生成最小凭据注入；默认使用 `LoadCredentialEncrypted`，受限 Incus/LXC 可显式选择 `--secret-credential-delivery runtime-files`，两者都不使用 secret env。渲染不会创建或修改凭据。旧 `--include-feishu-agent-credential` 只用于存量共享 env materializer；用 `om service credentials-migrate` 做默认 dry-run 的受控迁移。完整契约见 [Secret Storage](docs/SECRET_STORAGE.md)。

平台部署、升级、回滚和服务检查见 [DEPLOY.md](DEPLOY.md)、[Linux / Mac Deployment](docs/DEPLOY_LINUX_MAC.md) 与 [RUNBOOK.md](RUNBOOK.md)。

## 文档

从 [Docs Index](docs/INDEX.md) 开始。主要权威文档：

- [Getting Started](docs/GETTING_STARTED.md)：首次安全运行。
- [CONFIGS.md](CONFIGS.md)：配置事实源、生成链路与迁移契约。
- [配置指南](CONFIGURATION_GUIDE.md)：账户、市场、环境变量和验证方法。
- [产品架构](docs/PRODUCT_ARCHITECTURE.md)：产品域与模块关系。
- [系统架构](docs/ARCHITECTURE.md)：技术分层与真实调用链。
- [策略架构](docs/STRATEGY_ARCHITECTURE.md)：CSP、CC、Combo Yield。
- [Ledger Architecture](docs/LEDGER_ARCHITECTURE.md)：交易与持仓事实边界。
- [Tool Reference](docs/TOOL_REFERENCE.md)：当前 Tool Gateway 分类和 manifest 使用。
- [RUNBOOK.md](RUNBOOK.md)：巡检、故障诊断和应急操作。

`docs/gateflow/`、`docs/reviews/` 和 `docs/plans/` 是阶段性工作流证据，不是当前产品或运行契约。遇到冲突时，以当前源码、配置验证器、测试、runtime evidence 和上述 living docs 为准。

## 风险提示

本项目只做监控、筛选、报告、提醒和人工复盘，不构成投资建议。任何下单前都应自行复核价格、流动性、费用、保证金、仓位暴露、事件风险和数据新鲜度。

旧配置升级前请阅读[开关迁移说明](CONFIGURATION_GUIDE.md#配置开关迁移)，运行
`om config migrate-switches` 预览，核对通知意图及已退休字段后再显式应用。
