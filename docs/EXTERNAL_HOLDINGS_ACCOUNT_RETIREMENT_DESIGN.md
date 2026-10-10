# `external_holdings` 账户退役设计

当前全局扫描风险合同见文末「全局 Holdings 扫描分支退役（2026-09-30）」；前文保留当时的设计和迁移依据。其中要求核对全局 Holdings 扫描风险、表引用和凭据的旧操作步骤已被文末取代，不再作为升级前置条件；账户配置、systemd 单元和账本的迁移步骤仍适用。

状态：账户与全局扫描分支已退役；前半部分保留历史迁移依据，当前现金契约见文末。目标环境切换和存量情况分别核验。

## 目标、非目标与成功信号

目标：账户配置和账户级现金、股票持仓只认富途；期权持仓继续以账本为准。飞书 Holdings 仍可供全局持仓风险使用，但不成为账户类型，也不在富途不可用时回填账户数据。

非目标：本轮不实现 `om holdings configure`、Bot 命名重构或标的初始化改动；不迁移、改名或删除既有期权账本记录；设计阶段不修改任何实际 `config.yaml`、生成快照、服务或凭据。源码交付与各运行环境的配置切换分别授权。

成功信号：

1. 新建配置、账户增改 CLI 和 Tool Gateway 只提供富途账户；市场共用 symbols 的现有结构不因退役而改变。（PRD 验收 3）
2. YAML 人工来源与生成配置中的旧账户类型、任意账户级 `holdings_account` 及整项 `portfolio.source_by_account` 均走现有配置错误路径，包括映射值为 `futu` 或 `auto` 的情况；新生成配置不再输出该映射。账户级 `portfolio.source=holdings` 无效，`auto` 仅保留为富途路径的现有别名，不触发回退。不新增专门的拒绝状态。（CLI-07；用户确认的简化取舍）
3. 富途账户只接受同账户富途上下文及匹配缓存；富途失败、缓存来源不匹配和预备上下文来源不匹配均不会转向 Holdings。（PRD 验收 4）
4. 策略需要全局持仓风险时，仍可经原 Holdings 读取器获得全账户上下文，并保留来源、范围、取数时间及可得的源观察时间；源观察时间未知须如实标记，但单凭这一点不关闭全局风险。直接和预备运行遇到来源、范围或数据结构不符，以及读取失败时，账户富途结果仍可用，全局风险按现有 `unavailable_reasons` 标为不可用，不能用账户上下文或零持仓替代。（CLI-06；F2 修订）
5. 旧配置切换可用现有配置操作预览、备份、构建和回读；迁移前后分别核对账户范围、全局风险及账本记录，不自动转移账户归属。（PRD 验收 5）

## 历史设计基线与 owner

`config.yaml` 是人工来源，市场 JSON 是生成快照，不能手改快照替代构建（[Config Contract](../CONFIGS.md)）。历史设计工作树中的配置缺失不证明目标环境状态；任何目标主机的有效配置与账本均须单独核对。

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

## 历史验证范围

旧账户配置、无 Holdings 回退、账户缓存/预备身份、迁移预览及账本不改写，是退役时的验证边界。全局 Holdings 观察语义已被后文扫描分支退役取代，不能重新按旧计划实现。

## 风险与待核实事实

- 原设计阶段未读取目标主机。F2 修订已获准读取 `liuxie-incus` 的既有快照并做上述一次 Feishu 元数据 POC，但未验证表中每行持仓值、所有预期账户覆盖、账本未结情况或新版运行；配置切换的 owner 仍是各目标环境操作者，须在获准切换中另行清点。
- 本轮最小结构检查不能证明每个股票行数值有效；`portfolio_context_builder.py::build_context` 对不支持的资产种类和无效数量仍可能跳过，风险消费端也会跳过畸形股票映射。是否将所有非标准资产计入全局 NAV、哪些行应使风险整体不可用，须由持仓/风险 owner 另行定义并在目标环境只读抽样后实现，不能用本轮 F2 时间语义修复代替。
- 如果 `om accounts remove` 的现有预览不能覆盖某个旧配置的全部引用，先用候选副本与差异展示精确改动；不得为省步骤静默删除或把不完整预览视为批准。

## 全局 Holdings 扫描分支退役（2026-09-30）

### 目标、范围和成功信号

目标：删除已不可达的扫描全局 Feishu Holdings 风险读取、全局期权上下文和两者的缓存/消费分支；保留候选数据及排序仍使用的账户级风险计算。非目标：不改变账户富途数据、账本期权数据、候选排序公式、Portfolio Exposure/PM 开关、独立的 Feishu 持仓读取命令、真实配置或运行环境；不删除已有运行时缓存文件。

成功信号：① 直接扫描与预备运行不再构造、加载或写入 `portfolio_context.global.json`、`option_positions_context.global.json`，不再读 Feishu Holdings 作为扫描风险来源；② CSP/Combo Put 仍以账户级 `portfolio_ctx` 和 `option_ctx` 计算候选风险字段与排序，CC 保持既有行为；③ 静态导入/调用、测试与操作者文档没有把已退役的全局扫描风险描述成可用能力。仅源码研发交付，不含提交、部署或线上数据清理。

### 历史删除依据与保留边界

当前 `strategy_policy.py::strategy_semantics_for_profile` 对所有可选开仓 profile 都返回 `scan_uses_path_risk=False`；`wants_global_path_risk_context` 只读取该字段。因此 `pipeline_context.py::build_pipeline_context` 中两个全局加载器、预备 worker 中的全局读取以及 `short_vol_risk_context.py` 的 `_global_*` 消费分支都不能由有效配置触发。测试可通过 monkeypatch 人为令门为真，但不能证明产品路径可达。`portfolio_context_builder.py::load_holdings_records` 还供该模块独立 `main()` 使用；`build_shared_context` 也供其 `--shared-out` 使用，不能随着扫描分支一起删除。`positions.context_builder` 的账户账本上下文、`portfolio_context_service.py::load_account_portfolio_context` 的账户富途上下文、`short_vol_risk_context.py::build_portfolio_risk_context` 及候选排序仍在真实调用链上。

复用清单：账户持仓沿用 `portfolio_context_service.py::load_account_portfolio_context`；账户期权沿用 `pipeline_context.py::load_option_positions_context` 与 `prepared_option_positions_context.py`，包括前者使用的 `build_shared_option_positions_context` 和账户共享缓存；CSP 风险计算沿用 `short_vol_risk_context.py::build_portfolio_risk_context`，CC 维持自己的候选逻辑；缓存 I/O 沿用现有账户级路径。新增概念、字段、状态、别名、计算或校验：无。检索范围为 `src/application`、`src/interfaces`、`domain/domain`、`scripts`、测试、服务/部署模板和 `docs/INDEX.md` 指向的文档；关键词为 `wants_global_path_risk_context`、`scan_uses_path_risk`、`_global_portfolio_ctx`、`_global_option_ctx`、两个 `.global.json` 文件名、`load_holdings_records` 和 `build_shared_context`。除上述门控链、独立读取命令与测试外，未发现运行时全局扫描风险消费者；文档历史记载不是运行时消费者。

选定方案：在策略 owner 移除恒假能力字段及门函数，在直接/预备上下文 owner 删除全局读取与两类全局缓存分支，在风险 owner 只消费账户上下文；删去仅为全局风险服务的校验/共享包装与无用导入。保留 Feishu 底层读取和独立命令，不将它改造成账户风险回退。备选的“保留死分支以便未来开启”继续制造无效配置预期和伪覆盖，拒绝；“删除全部 Feishu 读取器”会改变独立命令的现有合同，超出范围，拒绝。

数据流仍为：有效配置 → 账户富途上下文 + 同账户账本期权上下文（直接或预备）→ CSP/Combo Put 的 `build_portfolio_risk_context` → 候选风险字段/排序。富途读取失败、账户缓存来源错误、预备 manifest 错误、期权或汇率证据缺失，继续沿现有路径处理；本次不改变已有缺失原因或可计算性判定，也不声称缺失期权证据必然阻断集中度。不引入全局回退或新的异常。原 `.global.json` 仅失去读写方，本次不删除真实运行时文件。

### 当前边界

直接和预备扫描只消费账户富途及账本上下文；旧全局缓存失去读写方。物理缓存清理仍归受控运维。独立 Feishu 读取命令保留，不能作为账户失败回退。

## 账户现金读取与可信度统一契约

### 批准目标与边界

同一业务语义使用同一套现金标准，不按消费者另设“实时”与“允许缓存”两类策略。当前源码已通过共享读取与判定 owner 实现；生产是否采用须核对目标环境。

目标：Futu 账户现金的读取、缓存复用、源观察时间、身份与可信度判定只有一个责任入口和一套实现；扫描、查询、报告、Wheel 和指派情景消费同一结论。相同事实、配置和评价时间产生相同现金金额、状态与原因。不同观测时刻允许金额变化；策略抵押、预留、汇率可计算性仍由各自既有 owner 决定。

范围包括所有当前 Futu 账户现金消费者及其数据投影、封存、CLI/Tool facade。非目标：lot 身份、合约乘数、期权抵押与结算政策、股票覆盖规则、PM non-Futu 估值、独立 Feishu Holdings 命令；不新增数据源、缓存服务、场景模式或配置键。生产数据及服务变更按相应受控流程执行。

### 当前责任归属

| 责任 | owner |
| --- | --- |
| 原始资金严格归一化、独立现金时间、分项失败 | `src/application/futu_portfolio_context.py` |
| 配置 TTL | `src/application/config_defaults.py::cash_snapshot_ttl_sec` |
| 身份、缓存/刷新、FX 注入及消费结论 | `src/application/portfolio_context_service.py` |
| 纯现金判定 | `domain/domain/risk_capacity.py::evaluate_cash_snapshot` |
| 封存与公开投影 | `src/application/prepared_portfolio_context.py`、`src/application/runtime_portfolio_snapshot.py`、`src/application/agent_tools/materialization.py` |

扫描、查询、Wheel、归属及指派情景消费共享 `cash_snapshot` 结论；股票、FX、账本结算和策略预留保留各自约束。

### 唯一现金合同

1. **金额**：继续使用 Futu builder 的 cash_by_currency 与 cash_components_by_currency；net_cash_power 只保留现有独立展示语义，不能代替现金。现金表必须非空、币种有效、值为有限数值且不能是 bool；必需证据缺项、资金响应部分失败、现金可靠性未明确为 true 均不可用；可选 SDK 字段未返回的含义按下表处理。合法零值和负值保留。正常化币种别名沿用现有 owner；不能把畸形值丢弃后当作完整余额，也不能为缺失币种凭空补零。
2. **身份**：从当前有效配置复用账户映射和环境解析。Futu 来源、逻辑账户、唯一物理账户、交易环境、运行市场必须匹配；filters、capacity_authority 和 source_account_identifiers 互相矛盾或缺少必要绑定时不可用。不得借用另一市场/账户/运行根目录的缓存。现金池仍按原物理账户语义计算，不将 US/HK 余额重复相加。
3. **观察时间**：现金只使用独立的 cash_source_observed_at；不能回退到现有 source_observed_at（其在 FX/持仓查询后才赋值）。缺失或无效均不可用，builder 不得默认当前时间补现金时间。禁止用文件 mtime、as_of_utc、读取缓存的时间替换源时间。时区缺失、未来时间、无法解析均不可用。直接取数的现金时间在资金响应成功返回后立即捕获，早于任何 FX/持仓查询；后续持仓/报价慢不能把早先现金重新标新，持仓仍保留自己的观测时间。
4. **有效期**：唯一配置为 runtime.portfolio_context_ttl_sec；缺省使用 config_defaults 的 900 秒，只在共享应用 owner 解析一次。可信条件包含 `0 <= evaluated_at - observed_at <= ttl`。显式非整数、bool、零和负值作为无效配置报不可用，并由配置校验提示；不再允许调用方通过 0 偷换为强制实时模式。目标环境若仍用 0，切换前须经现有配置流程处理。
5. **可靠性**：cash_balance_reliable 必须为 true，cash_balance_unavailable_by_row 不得有错误；上游显式 stale/untrusted/unknown 不可提升为可信。正常 Futu builder 未给 observation_status 时，可由完整身份、时间和可靠性证据判为可信；不会仅凭 context_source=futu_direct 判可信。account_cache 和 futu_direct 是读取来源标签，不影响同一事实的可信结论。
6. **结果**：portfolio context 使用 `cash_snapshot` 对象，字段为 status（fresh/stale/unknown）、reason_codes、source_observed_at、evaluated_at、max_age_sec。金额继续保存在原字段，不另存一份。fresh 表示可供后续业务计算，stale 表示超龄或上游明确过期，unknown 表示证据/身份/数值/配置不可用。多个原因去重并稳定排序；unknown 优先于 stale。统一原因至少覆盖 CASH_SOURCE_INVALID、CASH_IDENTITY_MISMATCH、CASH_OBSERVATION_MISSING、CASH_OBSERVATION_IN_FUTURE、CASH_OBSERVATION_STALE、CASH_BALANCE_UNRELIABLE、CASH_AMOUNT_INVALID、CASH_TTL_INVALID、CASH_PROVIDER_UNAVAILABLE。用户文案由原有展示层翻译。
7. **失败输出**：只允许 fresh 现金进入容量计算；无效/部分原始值可留作诊断，但必须携带不可用状态，不能出现在“可用现金”结果中。已有 cash_balance_reliable 表达上游完整性；整体可信度只由 cash_snapshot 表达，不能再根据前者单独批准。FX 缺失不把原币现金变成不可信；涉及换算的派生值依既有 FX owner 返回不可用及原因。

共享纯函数显式接收原始 context、从配置解析的预期 authority、评价时间和 TTL，返回上述结果；数值校验复用现有工具并补足 finite 判定。共享应用 owner 提供一个消费结论的小函数：对象缺失/格式无效直接 unavailable，不能通过猜测旧字段恢复 fresh。消费者只读该结果，不复制时间、账户或 reliable 判断。对当前操作需要重新评价时，仍调用同一个纯函数并传入当前配置和时间。

### Futu 资金字段与局部失败（P1、P2、P5）

资金行在 source owner 严格解析；身份过滤前先拒绝非映射坏行，不能静默丢弃后声称响应完整。每个选中账户资金行至少有一个明确、有限的受支持组成；存在空组成行、非法组成或 provider 明确部分失败，整个现金结果不可用。业务现金范围固定为 CNY/HKD/USD，不要求八个币种字段全部返回。

| 字段/输入 | 本地合同 |
| --- | --- |
| hk_cash/us_cash/cn_cash/jp_cash/sg_cash/au_cash/ca_cash/my_cash | 保留全部字段解析及原始证据；HKD/USD/CNY 的有限值（含数字字符串、显式 0、负值）按原币计入。JPY/SGD/AUD/CAD/MYR 明确为 0 时排除业务现金组成，不查 FX、不警告；非零（正或负）保留原币诊断并以 unsupported_cash_currency_nonzero 标记整笔现金不可用 |
| fund_assets/mmf_assets/money_fund_assets | 同一基金组成的别名，沿用现有行币种解析；只计一次。多个明确返回的别名必须数值一致，否则视为冲突，不重复相加；应用同一币种范围规则，逐组成检查非零，禁止以异号相加掩盖异常 |
| 字段不存在、None、空串、-、N/A | SDK 未返回该可选组成；跳过该组成且保留原始缺值证据，不推断为零或该币种不适用。至少一个明确组成且无其他错误才满足现有资金行合同 |
| bool、NaN、Inf、非数值字符串 | 非法值，不能作为未返回跳过；该行及现金结果不可用 |
| cash、net_cash_power | 不作为上述组成的回退；net_cash_power 保持独立展示 |
| 相同物理账户/环境/行币种的重复行 | 归一化后的受支持组成、缺值集合和身份一致才去重一次；任何余额或缺值冲突均不可用，禁止 first-wins |

此处“完整”仅指现有受支持资金响应合同；不声称 SDK 已返回所有可能币种或账户资产。原始行/既有错误字段保存诊断，不增设币种政策配置。[Futu 资金接口](https://openapi.futunn.com/futu-api-doc/trade/get-funds.html) 及本机 SDK 10.10.7008 的 cashInfoList 解析支持“未返回不等于零”的区分。测试覆盖单个合法组成、全未返回、合法行混坏行、零余额与冲突重复。

资金查询失败时现金不可用；资金查询成功后先固定现金时间，FX、持仓或 include_options 的条款补全失败不得抹掉该现金事实。FX 使用原缺失状态；持仓使用现有 position_snapshot_input 的 completeness=partial/unknown、quality=unavailable 和 error，不得以空 rows 伪装 complete。仅已完成的阶段能标完整。异常在阶段边界处理，取消/进程超时继续向外终止，不能捕获成成功或在截止后补请求。

现金独立成功不代表完整 portfolio 成功：共享 reader 返回分项证据；归属、CC、依赖股票的 NAV 和综合指派情景须保留各自持仓门禁。尤其 short_vol_risk_context 不得把缺 stocks_by_symbol 当作零股票加现金计算总 NAV。runtime snapshot 的 broker_positions completeness 从真实持仓快照产生，不能从 prepared manifest 的载入成功推导 complete；source binding 校验相应核对原 payload 的分项证据。broker_cash 可保留可信现金，综合状态保留部分失败。A1 比较现金结论，不能要求所有综合业务状态相同。

### 读取、缓存与封存

共享读取顺序：有效配置和期望身份 → 读取指定目录现有 portfolio_context.json → 共享评价 → fresh 且包含本次所需数据则复用 → 否则最多一次 Futu 刷新 → 对刷新结果运行同一评价 → 返回完整 context 和现金结论。共享现金结论只接受归一化后的 CNY/HKD/USD 键；其他币种键返回 CASH_CURRENCY_UNSUPPORTED。含其他币种键的旧当前缓存（包括零余额）视为未命中，沿既有流程刷新一次，成功后使用 source owner 的三币种结果；不改写旧封存历史。缓存 JSON 损坏等视为未命中；刷新失败不退回过期余额，错误统一映射为不可用，保留原本的日志与故障通道。取消/超时不再补发请求。

state_dir、runtime root、run_id、account 继续由既有入口绑定；共享 loader 不搜索其他根目录或其他 run 的缓存。不同目录使用相同读取规则，本轮不建立全局现金缓存仓库，也不承诺不同观测时刻读取同一快照。只读入口继续 write_cache=False；prepared 继续通过现有封存写入路径产出，不修改已封存 payload。允许写缓存的入口使用既有 atomic_write_json，避免并发半文件；缓存读取失败可以刷新，但写入失败不得悄悄伪报已缓存。

FX 注入在共享 reader 完成缓存/刷新选择后作用于返回副本：调用方明确传入的当前 FX observation 覆盖缓存 FX，明确传入 None/空值时清除旧 FX 并返回缺失状态；未提供该参数才走原 FX owner。区分“未传”和“明确缺失”只需现有参数或一个内部哨兵，不加策略模式。prepared 原有后置覆盖并入此处；不修改原缓存对象、封存文件、现金金额或现金时间。缓存命中且有显式 FX 时 broker 调用为零。

write_cache=False 必须贯穿嵌套 FX owner（包括 lock 文件），不是仅禁止 portfolio_context.json 写入。Wheel worker 显式接受由入口绑定 runtime root 的 FX observation 或 cache path，并向 current_exchange_rate_snapshot 传递 write_cache=False；禁止按源码目录推导运行缓存。隔离测试递归检查 portfolio、FX、lock 文件均无创建/改写。

现金读取共用共享 reader：现金查询和指派情景传递既有 FX observation；Wheel worker 保留 include_options、进程隔离、10 秒截止和取消。需要完整股票/期权快照时，loader 复用现有 position_snapshot_scope_errors 检查覆盖；对归属的 60 秒持仓门槛沿用原规则，缓存不满足时仅在共享 loader 内刷新一次。现金始终使用同一 TTL；持仓失败阻断归属，但必须表达为持仓证据失败。该附加检查只用于请求持仓的既有用途，不增加现金场景模式。

Live 查询在共享 loader 完成时评价；一次扫描在输入准备时固定评价结果并在同次运行复用。Wheel 预览保存该现金事实，确认属于新的决策时刻：在既有确认边界重核源时间、当前有效 TTL 与身份，已过期或配置改变则要求重新预览；不能用新现金悄悄确认旧预览，也不将网络 I/O 放进账本写锁。现金结论及原始现金证据纳入现有 capacity identity/hash；运行评价时钟不作为唯一变化理由强制每次预览失效，比较原始事实与当次策略参数。

Wheel 确认的落点：复用 candidate snapshot 已有 kind=portfolio 依赖及 hash，加载其绑定的 prepared manifest/payload 或原 portfolio_context 文件，并校验 run/account/root/hash；不新增现金快照文件或全局仓库。CLI 和 Tool 当前确认前调用实时 _cash_capacity 的路径改为读此绑定事实；依赖文件已变化/缺失即要求重新预览。将同一现金原始证据、当前有效 TTL/预期 authority 与确认时间传给 create_wheel_intent → Put 确认；_put_portfolio_context 必须保留这些证据。确认只调用共享纯评价，随后沿原事务重核最新 ledger/reservation；不在锁中联网。现金事实指纹覆盖金额、来源/身份、独立现金时间、可靠性与 TTL，排除 evaluated_at 及由它产生的 status/reason_codes；完整封存 payload hash 仍包含整个 cash_snapshot。TTL/身份变化或过期拒绝新确认；已持久化相同 request 的幂等读回保持在新鲜度检查之前，不能因事后过期破坏回执读取。测试同时覆盖 CLI、Tool、API 与幂等重试。

Prepared manifest/payload hash、run FX hash、run config hash 继续覆盖同一事实。新运行必须封存 cash_snapshot 和 cash_source_observed_at；runtime snapshot 的 broker_cash facts 和真实 tool 投影必须带出它们、cash_balance_reliable、cash_balance_unavailable_by_row 及原始来源状态。原 snapshot section 的 not_applicable freshness 不承担业务现金可信度，校验器不能据其放行现金，相关消费者读取现金字段结论。

历史兼容限定在已有 artifact 读取入口：有封存结论的报告原样重放，不以当前时钟判过期，不发 broker 请求。旧封存缺少 cash_snapshot 时，仅当原绑定的配置、源现金事实、独立可信现金观测时间、可信的当次评价时刻都齐全，才在内存中调用同一评价函数重建；否则显示现金证据不足。不得以当前配置/文件 mtime 补旧证据，也不重写原内容、hash 或已持久化报告。结构化 runtime snapshot 的 broker_cash section 新写入使用 v2，原 v1 按其固定字段集合与原 hash 校验；顶层和其他 section 版本保持原合同。仅该 section 按版本分派固定字段集合，拒绝混合形状及未知字段，不引入通用 schema 迁移器。旧结构解码成功不代表现金可用于当前决策。旧 payload fixture 应覆盖这些行为。

### 消费者共用规则

| 消费链 | 共同规则 | 独立约束 |
| --- | --- | --- |
| 直接扫描、prepared → CSP/Combo risk | 共用 reader；缺现金证据时现金容量和依赖现金的 NAV 不可用；真实零/负值保留 | 候选公式、抵押结算、CC 股票门槛 |
| cash headroom → CLI、Tool、footer | 移除 ttl=0 和私有现金 freshness；输出共享状态/时间/原因，CNY 换算不足独立呈现 | 原有字段和 facade，金额换算 owner |
| prepared → Daily Brief / runtime snapshot | 传递封存结论、原始可靠性，renderer 不补查现金 | run/config/FX/hash 绑定和历史稳定性 |
| Wheel preview → confirm | 经 reader，按当前有效配置复核同一现金标准；缺字段预览不可批准 | 预览证明、账本指纹、预留及确认事务 |
| 成交归属 observation → capacity check | 同入口、同现金结论，指纹包含现金判断依据 | 60 秒持仓对账、10 秒进程预算、取消、只读观测 |
| portfolio assignment → portfolio evidence | Futu cash 标准与其他入口一致；固定 300 秒不得再用于现金判定；综合状态如实保留持仓/报价缺口 | PM non-Futu、估值与行情自身合同 |

消费者不按 reliable 标志、固定 300 秒或默认 TTL 单独批准现金。builder 保留证据生成、domain 保留数值防御，显示层只投影共享状态；股票新鲜度仍由股票 owner 判定。

### 验收与验证入口

| 验收 | 可观察结果 | 验证入口 |
| --- | --- | --- |
| A1 同一标准 | 同样事实、有效配置、评价时间，各消费者金额和现金 status/reason_codes 一致 | 共享参数化 fixture 贯穿直接扫描、prepared、查询、Brief、Wheel、指派应用入口 |
| A2 缓存与时间 | 旧在线缓存缺独立现金时间只刷新一次；FX/持仓延迟不能续期现金；新文件旧源时间被刷新；旧文件新源时间可用；TTL 边界等号可用、超出不可用；未来/缺时区/缺时间不可用；显式无效 TTL 可见 | 共享 loader + 配置校验 + 真实 query/prepared facade 的 fetch 计数 |
| A3 金额与身份 | 零、负值保留；可选缺值不伪造零；坏行、冲突别名/重复行、NaN/Inf/bool、空/部分结果不可用；账户、物理 ID、环境、市场不匹配无法复用 | Futu builder/loader 与 domain 判断，跨账户缓存回归 |
| A4 故障与副作用 | 后续 FX/持仓失败保留可信现金并阻断依赖失败项的综合计算；取消/超时不补请求；只读模式含嵌套 FX/lock 无写入，无真实 provider/ledger/通知调用 | stub gateway、临时目录、worker cancellation 和写入 spy |
| A5 封存与传递 | cash_snapshot 经 prepared、runtime projection、Tool 白名单传递；晚一天重渲染相同报告不变；旧证据缺失有明确原因 | prepared、runtime snapshot、Daily Brief、Tool 输出与旧 artifact fixture |
| A6 既有资金安全 | 可信现金不能清除未决结算/预留；确认原事实超龄/TTL身份变化被拒绝，持久幂等读回不受事后超龄影响；缓存命中新 FX 生效、明确缺失不回用旧 FX；持仓门槛及 FX hash 保留 | Wheel preview/confirm、成交归属、CSP/Combo、汇率回归 |
| A7 唯一实现 | 所有 Futu 账户现金读取经共享 owner，无场景 ttl=0、独立 300 秒现金规则、reliable-only 批准或动态入口遗漏 | 真实消费者清单、静态调用/导出复核及 A1 集成用例 |

当前验证入口：`tests/test_cash_snapshot_contract.py`、`tests/test_futu_portfolio_context.py`、`tests/test_prepared_portfolio_context.py`、`tests/test_query_sell_put_cash_futu.py`、`tests/test_runtime_portfolio_snapshot.py`、`tests/test_portfolio_assignment_application.py` 及 Wheel 确认测试。

旧缓存缺独立现金时间须刷新；旧封存缺必要证据明确不可用。持仓完整性/60 秒门槛不因现金缓存复用而放宽，历史 schema 的 hash/source binding 不因新 cash 字段放宽。目标环境现金、配置及服务未由本契约核验。
