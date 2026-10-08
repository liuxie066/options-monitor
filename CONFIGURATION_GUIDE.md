# options-monitor 配置指南

本文面向操作者，说明如何安全维护 `config.yaml`、生成运行快照并检查环境。配置事实链和禁止项见 [CONFIGS.md](CONFIGS.md)。

## 需要维护什么

普通安装只需要三类配置：

| 文件 / 来源 | 内容 |
|---|---|
| `config.yaml` | 账户、市场、symbols、策略与非 secret 行为 override |
| Keychain / systemd credentials | secrets、provider credential；逻辑名和迁移见 `docs/SECRET_STORAGE.md` |
| env-file | 非秘密本机设置和写入开关；`OM_SECRET_BACKEND=env` 仅为显式兼容 |
| 生成快照 | `config.us.json`、`config.hk.json`、`resolved/config.bot.json` |

期权仓位不需要 Bitable：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

开仓扫描的账户现金和股票持仓只读取对应富途账户，期权账本以本地 SQLite 为准；扫描不读取 Feishu Holdings 计算全局风险。独立的 Feishu 持仓上下文导出命令仍可使用该表。

## 初始化

首次安装使用 `om setup init`（源码 checkout 用 `./om setup init`）。引导显示平台默认目录，填写 OpenD 端点、REAL/SIMULATE 环境、一个账户的数字 ID 和所选市场标的。每个标的选择 CSP/CC 策略：CSP 必填最高行权价，CC 必填最低行权价；另一端可选。至少输入一个用户标的，不填示例值。预览确认后创建 YAML、市场与 Bot 快照，并在 `~/.config/options-monitor/runtime-root` 记住目录。通知和 Bot 默认关闭，后续逐项配置；服务安装和启动分别确认。重新进入引导会保留已有 YAML，继续可选步骤；底层创建仍拒绝覆盖已有目标。显式路径与有效 `OM_RUNTIME_ROOT` 优先于用户记录，损坏记录必须由操作者核对。详细流程见[首次运行指南](docs/GETTING_STARTED.md)。

## 通过终端日常管理

运行 `om` 进入日常任务菜单，账户、标的与可选功能使用实际配置表单。所有配置变更展示预览，确认后生成快照；未修改字段保留。直接命令也可使用：

| 任务 | 入口 |
|---|---|
| 账户映射与真实/测试环境 | `om accounts list`、`add`、`edit`、`remove` |
| 同市场共享标的 | `om symbols list`、`add`、`edit`、`rm` |
| 通知开关、通道与接收人 | `om channel configure` |
| Bot 开关、模型与凭证 | `om bot configure`、`om bot model` |
| 全局持仓风险的可选非富途 Holdings | `om holdings configure` |
| 平仓建议开关 | `om close-advice configure` |

非交互配置的完整参数和预览确认凭据见各命令 `--help`。普通设置默认保存到运行目录的 `options-monitor.env`，0600；密钥使用隐藏输入及系统秘密存储。Linux 系统加密凭证写入需单独的 `sudo "$(command -v om)" secrets set <逻辑名> --backend systemd`，前台进程与常驻服务的凭证可读性分别报告。

第二个账户在日常管理中添加；同市场共享已有标的，首次添加另一个市场需同时给出标的及策略。`assistant` 和 `inbound` 保留兼容，推荐名称分别为 `bot` 与 `channel feishu`。配置保存不会重启服务，应用新的服务依赖须另行预览 `om service install`。

### 高级初始化

下列 `config init` 是完整参数入口，不会建立上述用户级目录记录，允许创建尚未填写账户 ID 的占位配置。在源码 checkout 中：

```bash
./om config init \
  --account-label YOUR_ACCOUNT_LABEL \
  --market us \
  --us-symbol AAPL \
  --symbol-strategy AAPL=csp \
  --csp-max-strike AAPL=100 \
  --output config.yaml \
  --runtime-output-dir .
```

把 `YOUR_ACCOUNT_LABEL` 换成自定义账户标签，把 `AAPL` 和 `100` 换成自己的标的与最高行权价。账户标签必填且无默认值；多个标的重复标的、策略与必要边界参数。若选择港股则使用 `--market hk --hk-symbol`；CC 使用 `--symbol-strategy SYMBOL=cc --cc-min-strike SYMBOL=PRICE`。安装后的全局命令可去掉 `./`。

上例生成：

- `config.yaml`
- `config.us.json`
- `config.bot.json`

已有目标文件时默认拒绝覆盖；先检查差异，不要直接使用 `--force` 覆盖生产文件。首次运行检查用 `om setup check --market us --format text`；占位富途账户 ID 和缺少市场快照阻断离线配置就绪，Bot 就绪单独显示。

当前 starter 见 [config.yaml.example](configs/examples/config.yaml.example)。

## 最小 YAML

```yaml
accounts:
  lx:
    type: futu
    futu_account_id: "REPLACE_WITH_FUTU_ACCOUNT_ID"
markets:
  us:
    accounts: [lx]
    symbols:
      - NVDA
      - GOOGL
    overrides:
      NVDA:
        sell_put:
          dte: [20, 45]
          strike: [80, 120]

  hk:
    accounts: [lx]
    symbols:
      - "0700.HK"
      - "9992.HK"

# 仅在同机已安装并启用 portfolio-management 时打开
portfolio_management:
  enabled: false
```

约定：

- 账户标签小写；
- 港股使用规范 `.HK` 代码并建议加引号；
- `markets.<market>.accounts` 只引用顶层已定义账户；
- `symbols` 保持字符串列表；
- 个性化策略配置放在 `overrides.<symbol>`；
- `portfolio_management.enabled` 是全局开关，不按市场配置；默认关闭；
- `portfolio.holdings.enabled` 控制全部指派后分布是否补充 PM Holdings 的非富途资产，默认关闭；
- YAML 使用空格缩进，tab 会被拒绝。

系统默认值在 `src/application/config_defaults.py::DEFAULT_CONFIG`。不需要把所有默认字段复制进 `config.yaml`。

## 维护监控标的

人工入口是 `om symbols`：`list` 查看 YAML 中该市场的清单，`add`、`rm`、`edit` 默认只预览；确认后追加 `--apply`，会校验并发布 YAML 与运行快照。未指定 `--config-yaml` 时使用当前运行目录中的 `config.yaml`；操作另一实例须显式指定文件。生成的 `config.us.json` / `config.hk.json` 不是此命令的输入或写入目标。

```bash
om symbols list --market us
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE
om symbols add YOUR_SYMBOL --strategy csp --csp-max-strike YOUR_MAX_STRIKE --apply
om symbols edit YOUR_SYMBOL --set sell_put.enabled=false
om symbols rm YOUR_SYMBOL
```

将 `YOUR_SYMBOL` 和 `YOUR_MAX_STRIKE` 换成自己的标的与 CSP 行权价上限。新增时必须选 `--strategy csp|cc|both`：CSP 必须给 `--csp-max-strike`，CC 必须给 `--cc-min-strike`；可选 `--csp-min-strike`、`--cc-max-strike`。`NVDA` 会识别为 US，`0700.HK` 会识别为 HK；`--market` 可省略，但显式指定时必须与标的一致。只配置一个市场时，`list` 可省略 `--market`；多个市场时需指定。新增标的只覆盖明确选择的策略开关与行权价边界，其余设置继承现有默认配置。`edit --set` 修改 `markets.<market>.overrides.<symbol>` 下的相对路径；列表值使用 JSON 写法，例如 `--set 'accounts=["lx"]'`。高级单项设置也可用 `om config symbol set --help`。删除市场最后一个标的会被配置验证拒绝。Agent 的结构化入口是 `om-agent run --tool manage_symbols`，其写入门禁另见 [Tool Reference](docs/TOOL_REFERENCE.md)。

## Portfolio Exposure 的 Holdings 来源配置

Portfolio Exposure 沿用“所有未平仓卖出期权均被指派”的情景口径。情景以富途 OpenD 的
股票和现金（含 MMF）为底，OM `position_lots` 提供未平仓卖出期权。开启
`portfolio.holdings.enabled` 后，仅补充 PM Holdings 中券商明确为非富途的资产；PM 中
富途股票、现金和 MMF 副本全部排除。来源不明的 PM 行不纳入并标记结果为 partial。
富途股票及期权标的价格取自 OpenD 市场快照；汇率使用同次情景的 OM 市场汇率观测。
休市价或报价缺失会标记 partial，不以 PM 报价回退。Holdings 关闭时查询不依赖 PM；
缺少富途底仓时结果为 unavailable。
开启预检以已配置 OM 账户为范围，要求 PM `non_futu` 估值证据新鲜可信，并展示完整原始
broker 清单、分类和行数。成功读取但没有合格非富途资产可显示 `ready_empty`。预览将非富途
broker 原文集合按账户写入待发布配置；apply 重读 PM，集合变化会在写入前拒绝。
查询遇旧配置缺集合或新 broker 值会暂停整份 PM 补充并标 partial。不把 Holdings 写入
Futu 账户资金、持仓或 OM 期权账本。`portfolio_management.enabled` 控制全局 PM 集成，
`portfolio.holdings.enabled` 只控制此情景补充。PM 尚未开启时，用 `--enable-pm` 将两个设置
纳入同一次预览、确认和配置发布；预检失败或取消不会先留下已开启的 PM。关闭 Holdings 保留 PM 集成。
PM 不可用时仍可预览开启目标，但 apply 会拒绝；关闭无需 PM 预检。

通过 YAML authoring/build 事务预览和写入：

```bash
./om config holdings set --enabled true --enable-pm
./om config holdings set --enabled true --enable-pm --apply --confirm \
  --expected-source-sha256 <预览中的 before_sha256> \
  --expected-preview-sha256 <预览中的 preview_sha256>
./om config holdings set --enabled false
./om config holdings set --enabled false --apply --confirm \
  --expected-source-sha256 <关闭预览中的 before_sha256> \
  --expected-preview-sha256 <关闭预览中的 preview_sha256>
```

预览摘要绑定目标值、配置路径和 runtime root；apply 改动这些目标时需重新预览。
写入后会核对 `config.yaml`、所有已配置市场 runtime JSON 和 Bot 快照的摘要；
如果写入后读回失败，错误会给出已写入状态、审计 ID 与备份路径，须先核对目标再重试。
如需回滚，恢复 YAML 备份后，还须用 `om config build` 和 `om config build-bot`
重建返回结果中列出的市场与 Bot 目标，并核对读回；只恢复 YAML 不会撤销生成快照。

## Symbol 扫描并发

Symbol pipeline 不提供 worker 数配置。它按输入顺序串行处理 Symbol，使受支持的
Linux/macOS `scan-pipeline` 主线程中的 `runtime.symbol_timeout_sec` 能真正中断超时处理。
整次 Tick 的 wall-clock 最终边界仍由 `tick-cron --timeout` 负责。

不要设置 `runtime.pipeline_symbol_max_workers` 或 `runtime.watchlist_max_workers`；
这两个键已退役，配置验证会要求删除。数据预取并发与账户并发是独立合同，不受影响。

同市场多账户 Tick 默认按账户数全并行。可在 `config.yaml` 的 `runtime.multi_account_max_workers`
设置正整数上限；实际 worker 数不会超过本轮账户数。旧键 `runtime.account_max_workers`
仍兼容，两者同时出现时新键优先。`om config validate` 和 `om config build` 会拒绝
这两个键的无效值及明显拼写错误；省略时保持默认全并行。

## 账户

账户类型为 `futu`：

### `futu`

```yaml
accounts:
  lx:
    type: futu
    futu_account_id: "REPLACE_WITH_FUTU_ACCOUNT_ID"
```

`futu` 账户的现金、股票持仓和可用 trade-intake 能力从账户设置派生。多 OpenD endpoint、host、port 和服务配置应通过当前示例、`config explain` 和 service preflight 核对，不要从历史 redesign plan 复制。

旧 `external_holdings` 账户、账户级 `holdings_account`、`portfolio.source_by_account` 和 `portfolio.source: holdings` 需在升级前从人工配置中迁出；这些输入在新版配置校验中报普通配置错误。开仓扫描不再使用全局 Holdings 风险快照。已安装 systemd 单元的 `--accounts` 不会随配置自动更新；配置切换到受控升级重渲染单元之间须保持受影响 timer 暂停，核对新单元账户集合后再恢复。迁移顺序与账本核对见 [退役设计](docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md#旧配置切换)。

账户增删改应直接修改 `config.yaml`，然后 validate 并重建受影响的
runtime snapshot：

```bash
./om config validate --source yaml --market us --config-yaml config.yaml
./om config build --source yaml --market us \
  --config-yaml config.yaml \
  --output config.us.json
```

`./om-agent add-account` / `edit-account` / `remove-account` 是受控账户入口；
先 `--dry-run` 检查候选改动，再通过 `OM_AGENT_ENABLE_WRITE_TOOLS=true`
与 `--confirm` 写入精确目标。

## 市场与 symbol override

`markets.us` / `markets.hk` 分别定义：

- 本市场启用的 accounts；
- 本市场扫描的 symbols；
- 每个 symbol 的策略 override。

示例：

```yaml
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
    overrides:
      NVDA:
        sell_put:
          enabled: true
          dte: [20, 45]
          strike: [80, 120]
        covered_call:
          enabled: true
          dte: [20, 60]
          strike: [125, 160]
        combo_yield: true
```

YAML authoring 使用 `covered_call`；生成的 runtime、CSV 或 trace 可能使用内部 key `sell_call`。`combo_yield` 是当前开仓策略名，旧 `yield_enhancement` 只属于明确的兼容读取。

不要在静态文档里猜某个字段是否仍有效。检查来源和值：

```bash
./om config explain --source yaml \
  --market us \
  --key markets.us.overrides.NVDA.sell_put
```

## Bot 与入站

`bot` / `inbound` 仍写在 `config.yaml`，但运行时由独立快照消费：

```bash
./om config build-bot --source yaml \
  --config-yaml config.yaml \
  --output resolved/config.bot.json
```

模型 API key 只 provision 到固定逻辑凭据；YAML 选择 provider/model 即可。旧 `api_key_env`
仅在显式 `OM_SECRET_BACKEND=env` 的迁移模式下作为兼容名称使用。

Feishu long-connection、WeChat ClawBot 和本地 Bot 共享 Control/Bot 安全边界，但渠道 credential、sender allowlist 和 provider readiness 分别验证。详见：

- [Inbound Control](docs/INBOUND_CONTROL.md)
- [Bot v2](docs/BOT_DESIGN.md)
- [Linux / Mac Deployment](docs/DEPLOY_LINUX_MAC.md)

## 通知

当前普通通知支持的 provider / channel 以配置验证器为准，主要包括：

- `wechat_clawbot`
- `feishu_app`

两者配置方式不同：

- WeChat 使用已有 binding / target；
- Feishu App recipient 来自受控环境变量，不应复用 WeChat `notifications.target`；
- webhook、App、入站 long-connection 和 external holdings 是不同角色。

不要复制旧 OpenClaw、Feishu option-position mirror 或 `notifications.daily_brief.enabled` 作为路由开关。scheduled ordinary notification 的 renderer authority 是 Daily Brief；兼容 preview 不具有 scheduled sender authority。

发送前先检查：

```bash
./om settings doctor
./om channel status \
  --runtime-root /var/lib/options-monitor \
  --profile-path /var/lib/options-monitor/service.profile.json \
  --env-file /etc/options-monitor/options-monitor.env
```

真实发送需要明确授权；不要用真实通知命令当连通性探针。

## env-file

Linux 推荐：

```text
/etc/options-monitor/options-monitor.env
```

macOS 推荐：

```text
$HOME/Library/Application Support/options-monitor/options-monitor.env
```

常见内容包括：

- Feishu App credential 与 recipient env；
- external holdings table env；
- LLM provider API key；
- Tool Gateway 写工具开关；
- runtime/service 机器级设置。

只读检查：

```bash
./om settings inspect
./om settings doctor
```

`settings inspect` 应只输出脱敏来源；若发现 secret 明文进入日志或配置输出，应停止后续操作。

## 生成运行快照

```bash
./om config validate --source yaml \
  --market us \
  --config-yaml config.yaml

./om config build --source yaml \
  --market us \
  --config-yaml config.yaml \
  --output config.us.json

./om config validate \
  --config-path config.us.json \
  --market us
```

HK 同理。生产建议把 YAML 和生成快照放在 release 外：

```text
/var/lib/options-monitor/config.yaml
/var/lib/options-monitor/config.us.json
/var/lib/options-monitor/config.hk.json
/var/lib/options-monitor/resolved/config.bot.json
```

service profile 应记录这些显式路径。升级时缺少 YAML authoring source 会 fail closed；legacy JSON 不是升级恢复通道。

## 各检查入口的职责

| 入口 | 检查什么 |
|---|---|
| `om config validate --source yaml` | YAML 与 defaults 合并后的结构、removed 字段和语义 |
| `om config validate --config-path` | 生成 runtime JSON、市场契约和生成指纹 |
| `config_validate` Tool | runtime JSON 的基础结构 |
| `healthcheck` Tool | OpenD、SQLite、credential 与运行前置条件 |
| `runtime_status` Tool | 现有 run/service/state artifact，不替代 config validator |
| `om settings doctor` | env-file 和 provider setting readiness |
| `om service preflight` | 部署前 profile、路径和服务前置条件 |

推荐顺序：

```bash
./om config validate --source yaml --market us
./om config validate --config-path config.us.json --market us
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

## 独立 Feishu Holdings 上下文导出

使用独立的 Feishu Holdings 上下文导出命令时，通过 env-file 提供 App credential 与 holdings table 引用。`portfolio.runtime.json` 只在必须替换默认 env 名等兼容场景使用。开仓扫描不使用此数据源。

它不能配置：

- Feishu `option_positions` bootstrap；
- Feishu `option_positions` mirror；
- 第二套期权持仓事实源。

需要向协作者提供诊断信息时，可以分享：

- 脱敏后的 `config.yaml`；
- `config explain` 输出；
- `settings doctor` 脱敏输出；
- holdings 字段名；
- `app_token/table_id` 的非 secret 部分。

不要分享 app secret、user token、webhook secret 或 LLM API key。

## 已退役旧配置

旧 `configs/user.*.json` authoring 和迁移命令已删除。直接维护 `config.yaml`，再重新生成所有 runtime snapshot。

## 变更检查清单

每次配置变更至少确认：

1. 修改的是正确的 `config.yaml`；
2. 没有把 secret 或 write gate 写入 YAML；
3. US/HK 目标市场正确；
4. account 与 symbol 没有跨市场串用；
5. YAML validate 通过；
6. 对应 runtime JSON 已 rebuild；
7. runtime fingerprint 新鲜；
8. Bot 配置变化时已 rebuild Bot JSON；
9. `healthcheck` 没有新增阻断项；
10. 首次真实运行先 `--no-send`，并理解它仍会写本地 artifact。

## 配置开关迁移

新配置的 `notifications.enabled` 默认 `false`，只有显式开启才允许主动通知。
旧配置升级前先运行 `om config migrate-switches --config-yaml <path> --runtime-root <root>`。
预览会删除无效的 `bot.toolsets`、`bot.tool_loading_mode`、
`notifications.daily_brief.enabled`、`features.wheel.enabled` 和
`trade_intake.combo_reconciliation.default_mode`，保留账户级组合归因模式和 Wheel 激活历史。
旧通知总开关缺省时，预览会明确补为 `true` 以保留原意；市场级显式关闭仍然关闭。

核对 `changes`、目标路径及两个哈希后，使用同一命令加上 `--apply --confirm`、
`--expected-source-sha256 <before_sha256>` 和 `--expected-preview-sha256 <preview_sha256>`。
发布使用现有配置事务，备份 YAML、重建所有已配置市场及 Bot，并读回校验。
发生冲突时不会猜测 `holdings_account` 的账户映射，也不会代替 `om bot migrate` 迁移旧存储。
重复使用旧预览会因源内容变化被拒绝；需重新预览。新建配置无需运行迁移。

Wheel 使用 `om wheel activation` 管理账户窗口；配置字段删除不会开启或关闭账本窗口。
组合归因未列出的账户固定为 `off`，仅配置实际需要的账户模式。

### 配置意图与实际就绪

`bot.enabled` 是 Bot 唯一启用开关，默认关闭，统一控制新入站命令处理和模型对话。
已受理分析的可信取消通道保留，关闭 Bot 后仍可停止在途分析；替代分析仍需开启 Bot。
`om bot configure` 保存该开关；关闭后仍可使用本地配置、状态与诊断命令。保存配置不启动服务。
本地 `bot run --model-config-json` 只覆盖模型选择，也必须存在已开启的 Bot 配置；
无外部调用的显式 eval fixture 不受运行配置依赖影响。

旧 `assistant` 配置通过 `om config migrate-switches` 显式迁移为顶层 `bot`；
旧总开关（缺省 true）与旧 `assistant.bot.enabled`（缺省 false）都开启才迁为开启。
模型配置、市场读权限一并保留；新旧根同时存在时拒绝迁移。
先查看预览，再使用预览哈希确认发布；生成 `resolved/config.bot.json`，旧快照不再被加载，
也不会自动删除。服务路径更新应在另行授权的部署流程中执行。

`config explain` 和 runtime status 的配置摘要只证明发布的配置意图。Bot diagnostics
区分 `configured_enabled` 与配置就绪；`live_requested` 仅记录请求，当前诊断不执行模型探测，`live_checked=false`；
不能据此证明服务或模型实际运行。
`om settings inspect/explain/doctor` 包含 `OM_INBOUND_MODEL_WRITE_ENABLED`，与执行端共用权限解释；
模型、交易等分项写权限仍受 `OM_INBOUND_OPERATIONS_ENABLED` 总闸约束。

`om holdings configure --interactive` 先预检候选 PM+Holdings 配置，再确认发布。显式输入的
PM 地址仍通过普通连接设置独立保存；若之后配置发布失败，`completed_steps` 会报告已经
保存的连接设置，PM 与 Holdings 不会部分启用。高级回执、重试、历史回填开关仍保留在
高级配置，不加入普通业务菜单。
