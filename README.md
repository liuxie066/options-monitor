# options-monitor

options-monitor（OM）是自托管的期权监控与持仓分析工具。它结合富途 OpenD 行情、账户信息和本地期权账本，帮助用户筛选 Cash-Secured Put（CSP）、Covered Call（CC）与 Combo Yield 候选，复核已有仓位，生成报告和提醒。**OM 提供决策依据，不会自行下单，也不保证收益。**

它适合愿意自行管理账户、核对券商数据，并希望保留扫描与持仓决策证据的用户。支持 US、HK 市场及多账户；不同市场、账户和数据时间应分别核对。

## 能做什么

| 用户的问题 | OM 的能力 |
|---|---|
| 现在有哪些开仓机会？ | 扫描 CSP、CC、Combo Yield，筛选和排序候选，并保留过滤原因与运行证据。 |
| 已开仓位该如何处理？ | 用 Wheel 生命周期与 Close Advice 分析持仓；证据不足时明确标记不可评估。 |
| 实际发生了哪些交易、收益如何？ | 记录交易事件，投影期权持仓，并生成持仓和收益报告。 |
| 什么时候需要关注？ | 生成 Daily Brief 和运行告警；按配置发送通知。 |
| 结果从哪里来？ | 通过报告、候选 trace、诊断命令和本地工具入口追溯数据与决策。 |

策略的适用条件和计算口径见[策略架构](docs/STRATEGY_ARCHITECTURE.md)；产品模块及相互关系见[产品架构](docs/PRODUCT_ARCHITECTURE.md)。

## 五项核心能力

CSP、CC 和 Combo Yield 用于寻找开仓候选；Wheel 跟踪接股后的批次；Close Advice 评估已有卖方期权是否值得提前买回。

**CSP · 现金担保认沽。** 卖出 Put，并预留足以按行权价接股的现金；适合愿意在合适价格买入正股、同时收取权利金的场景。OM 根据账户资金、报价、期限和风险条件筛选候选；候选不代表已经成交，也不保证不会被指派。[了解筛选规则](docs/STRATEGY_ARCHITECTURE.md#csp)

**CC · 备兑看涨。** 持有正股并卖出 Call，以持股覆盖交割义务；适合愿意在合适价格卖出正股、同时增加权利金收入的场景。OM 核对持股覆盖和已有期权占用后推荐候选；股价上涨时，收益可能受行权价限制。[了解筛选规则](docs/STRATEGY_ARCHITECTURE.md#cc)

**Combo Yield · 组合收益。** 当前开仓结构将现金担保的 Short Put 与同标的、同到期的 Long Call 配对，用 Put 的部分净权利金支付 Call 成本，并保留设定比例的净权利金。OM 独立评估两腿及组合，不会因为单腿合格就认定组合合格；真实成交的两腿关系需要明确记录。[了解组合结构](docs/STRATEGY_ARCHITECTURE.md#combo-yield)

**Wheel · 指派后的批次监控。** 当 CSP（包括 Combo Yield 的 Funding Put）被权威事实确认指派，且该账户已启用 Wheel，OM 按接股批次监控正股并推荐符合卖出底线的 CC。关联 Call 平仓或到期后可继续监控；正股全部被叫走或用户手动结束后，当前生命周期终止，不会自动重新卖 Put。[了解生命周期](docs/WHEEL_STRATEGY_PRD.md)

**Close Advice · 已有仓位的提前平仓建议。** 对账本中仍有效的 Short Put/Call，OM 结合开仓权利金、当前买回成本、剩余收益和交易日证据，判断是否值得提前买回，输出 `close`、`hold` 或 `not_evaluable`。它不为新开仓、换仓或自动交易做决定；证据不足时不发平仓建议。[了解判定规则](docs/CLOSE_ADVICE_CONTRACT.md)

## 系统如何工作

```text
config.yaml ──构建──> config.us.json / config.hk.json
                                  │
OpenD / Futu 数据 ───────────────> 扫描与持仓分析 ──> 报告 / 通知
                                  │
交易事件 ──> SQLite 持仓投影 ───────┘
```

- `config.yaml` 是人工维护的配置源；市场运行 JSON 是生成的快照。修改 YAML 后要重新构建受影响的快照。[配置合同](CONFIGS.md)
- 普通机器设置放在 env-file；Mac 凭据由 Keychain 管理，Linux 服务凭据由 systemd credentials 管理。不要把密码或密钥写进聊天、命令参数或配置文件。[秘密存储](docs/SECRET_STORAGE.md)
- OpenD / Futu 提供券商与行情事实；本地缓存和旧报告不能代替当前券商数据。缺失或过期的数据应明确呈现，不能当作零结果。
- 本地 SQLite 的 `trade_events → projection → position_lots` 是 OM 的期权持仓记录链；Feishu 不是期权持仓账本。[账本架构](docs/LEDGER_ARCHITECTURE.md)
- 扫描推荐不会记作真实成交。发送通知、写账本和管理服务各有独立操作入口与副作用；`--no-send` 只禁止通知，扫描仍可能写本地报告和状态。

## 快速开始

需要 Python 3.12 或更新版本、Git、curl，以及可访问 GitHub 的网络。以下以 Mac 默认路径为例；Linux 步骤见[安装指南](docs/INSTALL.md)。安装器默认安装**最新已发布的 GitHub Release**，并创建 `om` 与 `om-agent` 命令；它不会替你配置账户、保存凭据或启动服务。

```bash
curl -fsSL https://raw.githubusercontent.com/liuxie066/options-monitor/main/scripts/install.sh -o /tmp/options-monitor-install.sh
bash /tmp/options-monitor-install.sh
export PATH="$HOME/.local/bin:$PATH"
om setup init
```

`om setup init` 只在终端交互：选择运行目录、US/HK 市场、Futu 账户 ID 和初始标的，确认后生成最小配置并给出带精确路径的 `setup check` / `config edit` 命令。它不连接 OpenD、保存密钥或安装服务。以后运行 `om config edit` 可找到 CSP、CC、Combo Yield、Wheel、Close Advice 等高级配置；密码和密钥通过终端中的 `om secrets set` 输入，不发到聊天框。Linux 可使用相同命令，权限和凭据交付方式见[首次使用指南](docs/GETTING_STARTED.md)。已发布版本可能落后于当前 README；实际操作以安装目录中的文档和 `om --help` 为准。长期运行分别使用 launchd、systemd，见[部署指南](docs/DEPLOY_LINUX_MAC.md)。

人工操作从 `om --help` 查看命令；外部 Agent 或脚本从 `om-agent spec` 查看结构化工具、风险级别和副作用。Tool Gateway 是本地调用入口，不是 OM 的自治交易 Agent。

## 文档导航

| 想了解 | 从这里开始 |
|---|---|
| 安装与首次配置 | [安装指南](docs/INSTALL.md) · [首次使用指南](docs/GETTING_STARTED.md) · [配置指南](CONFIGURATION_GUIDE.md) |
| 策略与持仓 | [产品架构](docs/PRODUCT_ARCHITECTURE.md) · [策略架构](docs/STRATEGY_ARCHITECTURE.md) · [账本架构](docs/LEDGER_ARCHITECTURE.md) |
| 凭据与运行 | [秘密存储](docs/SECRET_STORAGE.md) · [部署指南](docs/DEPLOY_LINUX_MAC.md) · [运维手册](RUNBOOK.md) |
| 工具与开发 | [Tool Gateway](docs/TOOL_REFERENCE.md) · [技术架构](docs/ARCHITECTURE.md) · [完整文档索引](docs/INDEX.md) |

源码开发可从[安装指南的手动安装部分](docs/INSTALL.md#manual-install)开始；仓库提供 `make test` 和 `make lint`。许可条款见 [MIT License](LICENSE)。

本项目提供监控与分析，不构成投资建议。下单前请自行核对价格、流动性、费用、保证金、仓位、事件风险和数据新鲜度。
