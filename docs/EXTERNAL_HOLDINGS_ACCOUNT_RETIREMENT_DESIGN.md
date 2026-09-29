# `external_holdings` 账户退役设计

状态：Devflow full 已完成 Impl 与 Review；运行环境切换另行授权。事实基线为 `origin/main@975351a4e50781aa5dd3483d3805e4270705e839`。产品输入是另一工作树的 `docs/CLI_REFACTOR_PRD.md` 草案中「Holdings 数据边界」与 CLI-04、CLI-06、CLI-07；当前快照 SHA-256 为 `1020b936b1efc0f740d39ac10f182f1eb7320dbbda7c303b1b44f739e77f3a97`，保存设计时的旧快照仍记录在 `.devflow/scope.md`。PRD 仍是草案，且包含本任务未授权的其它 CLI 改动；本设计只覆盖下面列出的账户退役范围。

## 目标、非目标与成功信号

目标：账户配置和账户级现金、股票持仓只认富途；期权持仓继续以账本为准。飞书 Holdings 仍可供全局持仓风险使用，但不成为账户类型，也不在富途不可用时回填账户数据。

非目标：本轮不实现 `om holdings configure`、Bot 命名重构或标的初始化改动；不迁移、改名或删除既有期权账本记录；设计阶段不修改任何实际 `config.yaml`、生成快照、服务或凭据。源码交付与各运行环境的配置切换分别授权。

成功信号：

1. 新建配置、账户增改 CLI 和 Tool Gateway 只提供富途账户；市场共用 symbols 的现有结构不因退役而改变。（PRD 验收 3）
2. YAML 人工来源与生成配置中的旧账户类型、任意账户级 `holdings_account` 及整项 `portfolio.source_by_account` 均走现有配置错误路径，包括映射值为 `futu` 或 `auto` 的情况；新生成配置不再输出该映射。账户级 `portfolio.source=holdings` 无效，`auto` 仅保留为富途路径的现有别名，不触发回退。不新增专门的拒绝状态。（CLI-07；用户确认的简化取舍）
3. 富途账户只接受同账户富途上下文及匹配缓存；富途失败、缓存来源不匹配和预备上下文来源不匹配均不会转向 Holdings。（PRD 验收 4）
4. 策略需要全局持仓风险时，仍可经原 Holdings 读取器获得全账户上下文，并保留来源、范围、取数及观察时间；直接和预备运行遇到缺失、读取失败或观察证据不足时，账户富途结果仍可用，全局风险按现有 `unavailable_reasons` 标为不可用，不能用账户上下文或零持仓替代。（CLI-06）
5. 旧配置切换可用现有配置操作预览、备份、构建和回读；迁移前后分别核对账户范围、全局风险及账本记录，不自动转移账户归属。（PRD 验收 5）

## 当前事实与 owner 复用

`config.yaml` 是人工来源，市场 JSON 是生成快照，不能手改快照替代构建（[Config Contract](../CONFIGS.md)）。当前独立工作树没有这些被忽略的实际配置文件；任何目标主机的有效配置与账本状态均待单独核对。

| 语义 | 现有 owner 与现状 | 设计归属 |
| --- | --- | --- |
| YAML 账户来源与生成 | `config_yaml.py::_normalize_account_setting` 可由 `holdings_account` 推断旧类型；`layered_config.py::_derive_portfolio` 生成账户来源映射；`config_validator.py::validate_config` 校验类型 | 复用这些 owner，移除旧推断与映射；现有校验报告无效输入，不另建迁移状态机 |
| 首次创建与账户增改 | `config_yaml_init.py::_starter_yaml_payload`、`config_yaml_accounts.py::mutate_yaml_account_config`；`om setup init`、`om config init`、两套账户 CLI facade 和 Tool Gateway 暴露旧参数 | 复用现有入口，删去旧类型/字段选项、默认旧账户及其写入分支 |
| 账户来源决策 | `account_config.py::build_account_portfolio_source_plan` 与 `portfolio_context_service.py::load_account_portfolio_context` 允许 Holdings 作为主来源或 `auto` 回退 | 复用账户决策/加载 owner，只保留富途路径；不能把未知类型默认为富途并继续运行 |
| 预备上下文 | `prepared_portfolio_context.py::_allowed_context_sources` 的 `auto` 可接受 Holdings | 复用原来源绑定验证，仅接受对应富途账户和富途来源 |
| 全局风险 | `pipeline_context.py::load_global_holdings_risk_context`、`portfolio_context_builder.py::load_holdings_records`、`short_vol_risk_context.py::build_portfolio_risk_context` | 保留共享 Holdings 读取器和现有风险 owner；只去掉账户级消费者，并在全局证据缺失时标不可用 |
| 配置写入 | `config_yaml_accounts.py::mutate_yaml_account_config` 复用 `config_authoring_transaction.py::publish_yaml_config_generation` | 使用既有预览、备份、生成和回读；不新增迁移命令 |

检索范围：`src/application`、`src/interfaces`、`domain/domain` 中的 `external_holdings`、`holdings_account`、`source_by_account`、`portfolio_source`、`_global_portfolio_ctx` 声明与调用；`docs/INDEX.md`、根目录 `CONFIGS.md`、`CONFIGURATION_GUIDE.md` 查文档 owner。未发现另一个应成为账户来源或全局风险计算 owner 的实现；`trade_intake.holdings_sync` 是独立的 portfolio-management 迁移别名，本任务不因名字相近而移除。没有新增实体、配置键或依赖。

## 选定行为与失败语义

账户链：CLI/Tool Gateway 或 YAML → 配置生成与校验 → 账户来源规划 → 富途账户上下文/同账户富途缓存 → 扫描或查询。账户类型只剩 `futu`。旧类型、账户级 `holdings_account`、整项 `source_by_account`、`portfolio.source=holdings` 由既有验证入口报普通配置错误，不按字段值、账户类型或空值放行；`portfolio.source=auto` 只选富途。内部读取器也不得把不认识的类型或来源静默降为富途。富途读取失败应沿现有错误边界暴露，不读取 Holdings 的账户切片、旧缓存或表数据。现金、股票取富途，期权 lot 权威仍在 SQLite 账本。

全局风险链：仅在 `strategy_policy.py::wants_global_path_risk_context` 命中时，`pipeline_context.py` / `prepared_portfolio_context.py` 才通过 `load_holdings_portfolio_shared_context` 取 `all_accounts`。保留 `source_account_identifiers`、`filters`、`retrieved_at_utc`、`source_observed_at`、`source_observation_status` 及 `context_source`。新读取与缓存均核对 `portfolio_source_name=holdings_global`、`filters.account/broker` 为空及可信观察状态和时间；TTL 只控制缓存年龄，不充当源观察证据，不新增任意新鲜度阈值。读取失败、缺失或证据不可信时，直接和预备运行的账户结果保留，风险消费端给出 `holdings_context_missing` 等现有不可用原因，相关风险判断按既有 `unavailable_reasons` 路径收口。不得把账户级富途上下文当作全局覆盖，也不得把没有选中记录等同于已证实的零风险。

健康检查移除“外部 Holdings 账户就绪”分支，保留全局风险来源的独立可用性表达。`positions/maintenance.py` 的旧账户特例随账户类型退役，但历史账本不被重写；若目标环境仍有该账户待处理 lot，切换前单独确认归属和操作方案。

## 旧配置切换

不提供新的拒绝功能、兼容窗口或迁移命令。每个目标环境切换前，由操作者在**旧版仍可运行时**确认实际 runtime root、市场、账户和有效快照，做只读清单：旧账户定义、各市场引用、per-symbol 账户选择、账户来源映射、通知引用及既有账本中该账户的未结记录；另列出将保留的全局 Holdings 表引用及凭据是否配置，只显示脱敏标识，不输出 secret。此清单不把仓库样例配置当成目标环境事实。

对可用现有 `om accounts remove` 处理的账户，在仍支持旧配置的版本上先预览，对比完整候选 YAML 与受影响市场生成配置差异，再经该入口显式应用；跨市场引用逐市场处理。新版校验不能解析旧配置时，不依赖新版删除命令迁移。其余明确的账户级旧字段由操作者在人工来源 `config.yaml` 的候选副本中审阅并移除，不自动改写为富途账户，也不移入全局风险集合。操作前备份来源文件，应用后对受影响市场使用现有 `om config validate` / `om config build`，回读来源和生成快照并核对指纹；不直接编辑 JSON。若待处理 lot 的归属不明，停止环境切换，保留账本与源配置。配置和服务变更均另需目标、动作与范围明确授权。

源代码实现可以先形成独立变更供验证；**新版本在目标环境启用前**，该环境必须完成上述配置切换与回读。新代码遇到未迁移旧输入只报普通无效配置，不能静默修正或运行。配置切换失败保留旧版运行条件，按备份恢复并重新核对生成快照；不靠旧版源码继续兼容新语义。

## 实现切片与验证

| 切片 | 可验证行为 | 覆盖 | 依赖 |
| --- | --- | --- | --- |
| A. 账户配置只认富途 | 新建、增改、YAML 生成、校验和健康检查不产生旧类型；普通无效配置错误可见 | 1、2 | 无 |
| B. 账户与全局风险分流 | 账户富途失败不回填，缓存/预备来源校验严格；全局 Holdings 保留且缺失明确不可用 | 3、4 | A |
| C. 迁移及公共契约核对 | 用隔离旧配置 fixture 走既有预览/构建路径；文档、CLI/Tool Gateway 帮助、账本不改写的验证与切换说明一致 | 5，复核 1–4 | A、B |

最小回归证据：`om setup init` / `om config init`、账户配置与 CLI/Tool Gateway facade 测试；YAML 生成及普通配置校验测试覆盖旧字段/映射的空值、`futu`/`auto` 值；富途失败、错误来源缓存、预备来源和 Holdings 缺失/失败的上下文测试；直接与预备路径的全局风险成功、错误范围缓存、观察证据不足及不可用策略测试；旧配置 fixture 在旧版可用入口下的预览、完整差异、备份/生成/回读验证以及账本记录不变的只读断言。共享契约影响的其他消费者按实际 diff 扩展；文档做链接、事实、格式与项目 guardrails 检查。测试 fixture 不接触真实 Feishu、OpenD、运行配置或账本。

## 风险与待核实事实

- `config.yaml` 和 PRD 草案来自不同工作树；设计冻结前核对两者内容 hash 与实际实现 base。PRD 中的“启动拒绝或只读兼容”待定句已被用户的“删除配置、绑定入口”取舍替代，本设计不声称 PRD 草案已更新。
- 目标主机的有效配置、全局 Holdings 覆盖和账本未结情况未在此阶段读取。配置切换的 owner 是各目标环境操作者；必须在获准的环境切换中清点并处理，不能据仓库文件推断生产状态。
- 如果 `om accounts remove` 的现有预览不能覆盖某个旧配置的全部引用，先用候选副本与差异展示精确改动；不得为省步骤静默删除或把不完整预览视为批准。
