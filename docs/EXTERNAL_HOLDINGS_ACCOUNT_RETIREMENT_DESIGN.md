# `external_holdings` 账户退役设计

当前全局扫描风险合同见文末「全局 Holdings 扫描分支退役（2026-09-30）」；前文保留当时的设计和迁移依据。其中要求核对全局 Holdings 扫描风险、表引用和凭据的旧操作步骤已被文末取代，不再作为升级前置条件；账户配置、systemd 单元和账本的迁移步骤仍适用。

状态：原 Devflow full 已完成 Impl 与 Review；PR #387 复审后的 F2 设计修订及本地修复进行中，运行环境切换另行授权。原设计的事实基线为 `origin/main@975351a4e50781aa5dd3483d3805e4270705e839`；本轮修复工作树以 `ef75a799af4ea1e921a4a1be88cecb7a4a849b77` 为基线。产品输入是另一工作树的 `docs/CLI_REFACTOR_PRD.md` 草案中「Holdings 数据边界」与 CLI-04、CLI-06、CLI-07；原设计快照 SHA-256 为 `1020b936b1efc0f740d39ac10f182f1eb7320dbbda7c303b1b44f739e77f3a97`。PRD 仍是草案，且包含本任务未授权的其它 CLI 改动；本设计只覆盖下面列出的账户退役范围。

## 目标、非目标与成功信号

目标：账户配置和账户级现金、股票持仓只认富途；期权持仓继续以账本为准。飞书 Holdings 仍可供全局持仓风险使用，但不成为账户类型，也不在富途不可用时回填账户数据。

非目标：本轮不实现 `om holdings configure`、Bot 命名重构或标的初始化改动；不迁移、改名或删除既有期权账本记录；设计阶段不修改任何实际 `config.yaml`、生成快照、服务或凭据。源码交付与各运行环境的配置切换分别授权。

成功信号：

1. 新建配置、账户增改 CLI 和 Tool Gateway 只提供富途账户；市场共用 symbols 的现有结构不因退役而改变。（PRD 验收 3）
2. YAML 人工来源与生成配置中的旧账户类型、任意账户级 `holdings_account` 及整项 `portfolio.source_by_account` 均走现有配置错误路径，包括映射值为 `futu` 或 `auto` 的情况；新生成配置不再输出该映射。账户级 `portfolio.source=holdings` 无效，`auto` 仅保留为富途路径的现有别名，不触发回退。不新增专门的拒绝状态。（CLI-07；用户确认的简化取舍）
3. 富途账户只接受同账户富途上下文及匹配缓存；富途失败、缓存来源不匹配和预备上下文来源不匹配均不会转向 Holdings。（PRD 验收 4）
4. 策略需要全局持仓风险时，仍可经原 Holdings 读取器获得全账户上下文，并保留来源、范围、取数时间及可得的源观察时间；源观察时间未知须如实标记，但单凭这一点不关闭全局风险。直接和预备运行遇到来源、范围或数据结构不符，以及读取失败时，账户富途结果仍可用，全局风险按现有 `unavailable_reasons` 标为不可用，不能用账户上下文或零持仓替代。（CLI-06；F2 修订）
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

全局风险链：仅在 `strategy_policy.py::wants_global_path_risk_context` 命中时，`pipeline_context.py` / `prepared_portfolio_context.py` 才通过 `load_holdings_portfolio_shared_context` 取 `all_accounts`。保留 `source_account_identifiers`、`filters`、`retrieved_at_utc`、`source_observed_at`、`source_observation_status` 及 `context_source`。只有显式源观察字段或调用方提供的源时间可填 `source_observed_at` 并标 `trusted`；飞书记录 `last_modified_time` / `updated_at_utc` 是编辑时间，不能充当持仓观察时间。`unknown` 对应空观察时间；旧缓存若标 `feishu_record:*`，拒绝并重新读取，不能保留假 `trusted`。新读取与缓存核对 `portfolio_source_name=holdings_global`、未按账户/券商过滤、非空账户标识、取数时间及现金/股票字典至少有一项可用；这些是范围和最小结构证据，不证明覆盖所有应有账户或每行持仓值有效。观察状态 `unknown` 本身不拒绝，风险结果沿现有 `portfolio_risk_warnings` 标出未知；`as_of_utc` 是取数时间，不冒充源观察时间。TTL 只控制缓存年龄，不充当源观察证据，也不新增任意新鲜度阈值。读取失败、来源或范围不符、结构不完整时，直接和预备运行的账户结果保留，风险消费端给出 `holdings_context_missing` 等现有不可用原因。不得把账户级富途上下文当作全局覆盖，也不得把没有选中记录等同于已证实的零风险。

F2 修订依据（2026-09-30）：对 `liuxie-incus:/var/lib/options-monitor` 的同一张 Feishu Holdings 表做单次只读 POC，`records/search` 加 `automatic_fields=true` 后 65/65 条返回 `last_modified_time`，无一条显式源观察字段；最早编辑时间为 2026-03-19。该参数解决“能否取得编辑时间”，不能证明持仓在该时刻被观察。现有风险门只检验时间存在，不检验年龄，既会误称 `trusted`，又会在缺字段时使全局 NAV/集中度永久不可用。因此撤回本轮未落地的 `automatic_fields` 请求改动，以现有 `portfolio_context_builder.py::_record_source_observation` 为观察字段 owner，将 `is_trusted_global_holdings_context` 更名为 `is_valid_global_holdings_context` 并检验来源、范围、最小结构和观察元数据的一致性；直接、预备及风险消费端复用该检查，不增加配置键、状态或风险算法。Feishu 分页若未完整结束必须报读取失败，不得把已取前缀当作全局覆盖。若未来业务需要保证真实持仓新鲜度，须另行约定上游观察批次、覆盖证明和最大允许年龄。

健康检查移除“外部 Holdings 账户就绪”分支；全局风险读取失败仅在风险消费端记录警告并给出 `holdings_context_missing`，健康检查目前不单独探测该来源。`positions/maintenance.py` 的旧账户特例随账户类型退役，但历史账本不被重写；若目标环境仍有该账户待处理 lot，切换前单独确认归属和操作方案，自动到期维护不会枚举已移出配置的账户。

## 旧配置切换

不提供新的拒绝功能、兼容窗口或迁移命令。每个目标环境切换前，由操作者在**旧版仍可运行时**确认实际 runtime root、市场、账户和有效快照，做只读清单：旧账户定义、各市场引用、per-symbol 账户选择、账户来源映射、通知引用及既有账本中该账户的未结记录；另列出将保留的全局 Holdings 表引用及凭据是否配置，只显示脱敏标识，不输出 secret。此清单不把仓库样例配置当成目标环境事实。

对可用现有 `om accounts remove` 处理的账户，在仍支持旧配置的版本上先预览，对比完整候选 YAML 与受影响市场生成配置差异，再经该入口显式应用；跨市场引用逐市场处理。新版校验不能解析旧配置时，不依赖新版删除命令迁移。其余明确的账户级旧字段由操作者在人工来源 `config.yaml` 的候选副本中审阅并移除，不自动改写为富途账户，也不移入全局风险集合。操作前备份来源文件，应用后对受影响市场使用现有 `om config validate` / `om config build`，回读来源和生成快照并核对指纹；不直接编辑 JSON。若待处理 lot 的归属不明，停止环境切换，保留账本与源配置。

已安装的 tick 与 auto-close systemd 单元在 `ExecStart` 中固化了渲染时的 `--accounts`。配置移除旧账户到新 release 完成 service reconcile 之间，必须在授权维护窗口内保持受影响 timer 不触发；受控升级和必要的 `service drift --confirm` 均传 `--preserve-activation-state` 保留暂停状态。升级后先用 `./om service drift --runtime-root <runtime-root>` 核对账户参数，必要时按受控流程 `--confirm` 重渲染，再回读 `systemctl cat options-monitor-tick-<market>.service` 和 `options-monitor-auto-close-<market>.service` 的 `--accounts` 与生成快照的账户集合一致，最后恢复定时任务。不得在旧单元仍引用已删除账户时恢复调度。配置、timer、服务和升级各自需要明确的目标、动作与范围授权。

源代码实现可以先形成独立变更供验证；**新版本在目标环境启用前**，该环境必须完成上述配置切换与回读，并只读核对实际全局 Holdings 快照的来源、未过滤范围、账户标识及 `source_observation_status`；与独立清单比对预期账户覆盖，无法证明的范围如实报告未知。若观察状态为 `unknown`，如实报告观察时间未知，不声称数据新鲜。新代码遇到未迁移旧输入只报普通无效配置，不能静默修正或运行。配置切换失败保留旧版运行条件，按备份恢复并重新核对生成快照；不靠旧版源码继续兼容新语义。

## 实现切片与验证

| 切片 | 可验证行为 | 覆盖 | 依赖 |
| --- | --- | --- | --- |
| A. 账户配置只认富途 | 新建、增改、YAML 生成、校验和健康检查不产生旧类型；普通无效配置错误可见 | 1、2 | 无 |
| B. 账户与全局风险分流 | 账户富途失败不回填，缓存/预备来源校验严格；全局 Holdings 保留且缺失明确不可用 | 3、4 | A |
| C. 迁移及公共契约核对 | 用隔离旧配置 fixture 走既有预览/构建路径；文档、CLI/Tool Gateway 帮助、账本不改写的验证与切换说明一致 | 5，复核 1–4 | A、B |
| D. F2 观察证据纠正（本轮） | 未知观察时间不独自挡住全局风险且有 warning；编辑时间及旧缓存不冒充观察时间；错误来源、范围、空结果和分页未完仍不可用 | 4 | B |

最小回归证据：`om setup init` / `om config init`、账户配置与 CLI/Tool Gateway facade 测试；YAML 生成及普通配置校验测试覆盖旧字段/映射的空值、`futu`/`auto` 值；富途失败、错误来源缓存、预备来源和 Holdings 缺失/失败的上下文测试；直接与预备路径验证观察时间未知仍使用全局风险并写出 warning、错误范围与旧编辑时间缓存不可用、空结果与分页未完不可用、编辑时间不冒充源观察时间；旧配置 fixture 在旧版可用入口下的预览、完整差异、备份/生成/回读验证以及账本记录不变的只读断言。共享契约影响的其他消费者按实际 diff 扩展；文档做链接、事实、格式与项目 guardrails 检查。测试 fixture 不接触真实 Feishu、OpenD、运行配置或账本。

## 风险与待核实事实

- `config.yaml` 和 PRD 草案来自不同工作树；设计冻结前核对两者内容 hash 与实际实现 base。PRD 中的“启动拒绝或只读兼容”待定句已被用户的“删除配置、绑定入口”取舍替代，本设计不声称 PRD 草案已更新。
- 原设计阶段未读取目标主机。F2 修订已获准读取 `liuxie-incus` 的既有快照并做上述一次 Feishu 元数据 POC，但未验证表中每行持仓值、所有预期账户覆盖、账本未结情况或新版运行；配置切换的 owner 仍是各目标环境操作者，须在获准切换中另行清点。
- 本轮最小结构检查不能证明每个股票行数值有效；`portfolio_context_builder.py::build_context` 对不支持的资产种类和无效数量仍可能跳过，风险消费端也会跳过畸形股票映射。是否将所有非标准资产计入全局 NAV、哪些行应使风险整体不可用，须由持仓/风险 owner 另行定义并在目标环境只读抽样后实现，不能用本轮 F2 时间语义修复代替。
- 如果 `om accounts remove` 的现有预览不能覆盖某个旧配置的全部引用，先用候选副本与差异展示精确改动；不得为省步骤静默删除或把不完整预览视为批准。

## 全局 Holdings 扫描分支退役（2026-09-30，Devflow simple）

### 目标、范围和成功信号

目标：删除已不可达的扫描全局 Feishu Holdings 风险读取、全局期权上下文和两者的缓存/消费分支；保留候选数据及排序仍使用的账户级风险计算。非目标：不改变账户富途数据、账本期权数据、候选排序公式、Portfolio Exposure/PM 开关、独立的 Feishu 持仓读取命令、真实配置或运行环境；不删除已有运行时缓存文件。

成功信号：① 直接扫描与预备运行不再构造、加载或写入 `portfolio_context.global.json`、`option_positions_context.global.json`，不再读 Feishu Holdings 作为扫描风险来源；② CSP/Combo Put 仍以账户级 `portfolio_ctx` 和 `option_ctx` 计算候选风险字段与排序，CC 保持既有行为；③ 静态导入/调用、测试与操作者文档没有把已退役的全局扫描风险描述成可用能力。仅源码研发交付，不含提交、部署或线上数据清理。

### 当前事实、复用与取舍

当前 `strategy_policy.py::strategy_semantics_for_profile` 对所有可选开仓 profile 都返回 `scan_uses_path_risk=False`；`wants_global_path_risk_context` 只读取该字段。因此 `pipeline_context.py::build_pipeline_context` 中两个全局加载器、预备 worker 中的全局读取以及 `short_vol_risk_context.py` 的 `_global_*` 消费分支都不能由有效配置触发。测试可通过 monkeypatch 人为令门为真，但不能证明产品路径可达。`portfolio_context_builder.py::load_holdings_records` 还供该模块独立 `main()` 使用；`build_shared_context` 也供其 `--shared-out` 使用，不能随着扫描分支一起删除。`positions.context_builder` 的账户账本上下文、`portfolio_context_service.py::load_account_portfolio_context` 的账户富途上下文、`short_vol_risk_context.py::build_portfolio_risk_context` 及候选排序仍在真实调用链上。

复用清单：账户持仓沿用 `portfolio_context_service.py::load_account_portfolio_context`；账户期权沿用 `pipeline_context.py::load_option_positions_context` 与 `prepared_option_positions_context.py`，包括前者使用的 `build_shared_option_positions_context` 和账户共享缓存；CSP 风险计算沿用 `short_vol_risk_context.py::build_portfolio_risk_context`，CC 维持自己的候选逻辑；缓存 I/O 沿用现有账户级路径。新增概念、字段、状态、别名、计算或校验：无。检索范围为 `src/application`、`src/interfaces`、`domain/domain`、`scripts`、测试、服务/部署模板和 `docs/INDEX.md` 指向的文档；关键词为 `wants_global_path_risk_context`、`scan_uses_path_risk`、`_global_portfolio_ctx`、`_global_option_ctx`、两个 `.global.json` 文件名、`load_holdings_records` 和 `build_shared_context`。除上述门控链、独立读取命令与测试外，未发现运行时全局扫描风险消费者；文档历史记载不是运行时消费者。

选定方案：在策略 owner 移除恒假能力字段及门函数，在直接/预备上下文 owner 删除全局读取与两类全局缓存分支，在风险 owner 只消费账户上下文；删去仅为全局风险服务的校验/共享包装与无用导入。保留 Feishu 底层读取和独立命令，不将它改造成账户风险回退。备选的“保留死分支以便未来开启”继续制造无效配置预期和伪覆盖，拒绝；“删除全部 Feishu 读取器”会改变独立命令的现有合同，超出范围，拒绝。

数据流仍为：有效配置 → 账户富途上下文 + 同账户账本期权上下文（直接或预备）→ CSP/Combo Put 的 `build_portfolio_risk_context` → 候选风险字段/排序。富途读取失败、账户缓存来源错误、预备 manifest 错误、期权或汇率证据缺失，继续沿现有路径处理；本次不改变已有缺失原因或可计算性判定，也不声称缺失期权证据必然阻断集中度。不引入全局回退或新的异常。原 `.global.json` 仅失去读写方，本次不删除真实运行时文件。

### 实施与验证

单片 A：删除扫描全局链和只为它服务的测试/文档声明，同时保持账户级风险计算与候选排序。覆盖成功信号 ①②③，无前置切片。实现 owner：`strategy_policy.py`、`pipeline_context.py`、`prepared_portfolio_context.py`、`short_vol_risk_context.py`、`portfolio_context_builder.py`、`portfolio_context_service.py`；相关测试及当前文档 owner 随行为同步更新。用直接与预备入口的回归测试证明无全局读取/缓存写入；保留账户期权共享缓存刷新测试，并把现有 `_global_*` 跨标的排序 fixture 改成账户现金、股票和 `option_ctx`，断言 CSP 具体风险值、缺失原因与排序。CC 只核对既有行为。保留独立 Feishu 命令测试，删除人为打开恒假门的测试。按实际消失的顶层函数和导入，向追加式 `docs/public_surface_retirements.json` 登记；旧条目不改。逐处修正 `README.md`、`CONFIGURATION_GUIDE.md`、`docs/AGENT_GETTING_STARTED.md`、`docs/INDEX.md`、`docs/STRATEGY_ARCHITECTURE.md` 的当前能力说明，保留独立 Feishu 命令与 Portfolio Exposure 配置的事实。运行受影响测试、完整项目必需检查、静态全局符号/缓存引用检查及文档/公共面 guardrails。

风险与边界：运行环境可能留有旧全局缓存，但本次源码不再读取，物理清理须另行授权和目标绑定。现有“期权上下文缺失时集中度可能仍可计算”的静态线索由 CSP 风险 owner 后续单独核实，不借退役改动扩大失败语义。先前设计中关于 Feishu 观察时间的结论仍是历史事实，退役后不再是扫描门禁；若将来重新引入跨账户风险，需另行定义业务需求、上游覆盖及观察时间合同，不能复活旧死分支。
