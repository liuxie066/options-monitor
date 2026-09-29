# options-monitor 配置契约

本文只定义配置事实源、生成链路和迁移边界。字段与操作示例见 [Configuration Guide](CONFIGURATION_GUIDE.md)。

## 权威模型

```text
src/application/config_defaults.py::DEFAULT_CONFIG
  + config.yaml
  -> om config build / build-assistant
  -> generated runtime snapshots

env-file
  -> runtime secrets, machine settings, and write gates
```

env-file 不是合并进生成快照的配置层；它在进程启动或工具执行时装入有效环境，
生成 JSON 不复制 secrets。

| 层 | 用途 | 是否人工编辑 |
|---|---|---:|
| `DEFAULT_CONFIG` | 系统默认值 | 否 |
| `config.yaml` | accounts、markets、symbols、非 secret override | 是 |
| env-file | secrets、本机设置、写入开关 | 是，且不提交 |
| `config.us.json` / `config.hk.json` | 市场运行快照 | 否，由 build 生成 |
| `resolved/config.assistant.json` | Assistant 运行快照 | 否，由 build-assistant 生成 |
| `portfolio.runtime.json` | 少数 external holdings env 名兼容 | 通常不需要 |

生成后的 runtime JSON 是“本次运行读取的快照”，不是另一套 authoring source。不要一边编辑 `config.yaml`，一边手改 JSON。

## `config.yaml`

稳定约定：

- 顶层 `accounts` 定义账户及类型；
- `markets.us` / `markets.hk` 选择该市场的账户和 symbols；
- symbol 使用规范代码，例如 `NVDA`、`0700.HK`、`9992.HK`；
- per-symbol 策略 override 放在 `markets.<market>.overrides.<symbol>`；
- US 调度 override 放在 `markets.us.schedule`，例如通过 `gates` 设置北京时间截止点；
- YAML 中使用 `covered_call`，生成的内部 runtime / CSV / trace key 仍可能是 `sell_call`；
- `combo_yield` 是当前开仓策略 key；旧 `yield_enhancement` 只在明确兼容边界读取；
- `portfolio_management.enabled` 是全局、默认关闭的 PM 集成开关，同时控制只读工具、
  指派证据和成交后的持仓刷新提示；不要放在 `markets.*` 下；
- `portfolio.holdings.enabled` 是 Portfolio Exposure 的可选 Holdings 来源配置，默认关闭；
  开启前检查 PM 的 Holdings 账户和估值证据，只报告已观测范围，不声明 broker/market 全覆盖。
  本阶段只建立配置和读回，不改变现有指派后情景的持仓来源；
- 旧 `trade_intake.holdings_sync.enabled` 只保留一个版本的迁移读取，旧队列、重试、
  超时和状态目录参数不再生效；
- account label 在 trim + lowercase 后必须唯一；账户隔离、ledger scope 和报告归属都依赖该标识；
- `close_advice` 只保留 `enabled`、`quote_source` 和 `max_items_per_account`
  运行配置。止盈公式与门槛在 `remaining_yield_capture.v1` 中固定，
  不提供可调的策略键；
- 旧 `notify_levels`、`max_spread_ratio`、`strong_remaining_annualized_max`、
  `medium_remaining_annualized_max` 和 `quote_max_age_sec` 不再影响决策，
  验证器只输出迁移警告；
- secrets、token、Feishu credential 和 Agent write gate 不进入 YAML。

当前示例：

- `configs/examples/config.yaml.example`
- `src/application/config_defaults.py`

Close Advice 详细固定规则见
[Close Advice Contract](docs/CLOSE_ADVICE_CONTRACT.md)。

### Portfolio Exposure 的 Holdings 来源配置（研发设计）

目标：为始终可用的 Portfolio Exposure 增加一个**来源纳入意图**，默认不纳入 Holdings；
可预览、开启和关闭，并在开启前核对 PM Holdings 的当前可用证据。本轮仅交付配置行为，
不改变现有“所有未平仓卖出期权均被指派”的只读情景计算及其持仓读取路径。
不恢复已退役的 `external_holdings` 账户，不写 Futu、PM Holdings、OM 期权账本或生产配置，
不新增 Portfolio Exposure 产品总开关，也不承诺覆盖所有 broker 或 market。

| 验收 | 可观察行为 |
|---|---|
| H1 默认与校验 | `portfolio.holdings.enabled` 缺省为 `false`，在默认配置和生成的市场快照中可见；只接受全局布尔值，不允许市场覆盖或未知 Holdings 键。 |
| H2 受控切换 | `om config holdings set` 预览无目标写入，展示当前值、目标值、源与全部生成目标；apply 需确认、匹配的源 SHA 和绑定该目标的预览摘要，经现有 authoring/build 事务生成 YAML 中所有已配置的 US/HK 市场与 Assistant 快照、备份并读回；关闭可在 PM 故障时完成。 |
| H3 开启预检 | 开启前检查 PM 集成、Holdings 账户发现及新鲜可信的估值证据。预览可展示预检失败而不写入，apply 必须拒绝。回报仅列出 PM 已观测账户、broker、market 与时间，不把一次预检解释为与 OM 查询账户对应、持续可用或全覆盖证明。 |
| H4 情景隔离 | 原 `portfolio_assignment_scenario` 查询与领域计算没有本轮改动；新键尚不改变查询结果，操作说明和返回值须明确这一阶段边界。 |

事实及复用清单：检索 `src/application/config_defaults.py`、`config_yaml.py`、
`config_validator.py`、`config_authoring_transaction.py`、`portfolio_management.py`、
`portfolio_assignment_scenario.py`、`domain/domain/portfolio_assignment_scenario.py`、
`src/interfaces/cli/config_ops.py` 和相邻测试，关键词为 `portfolio.holdings`、
`portfolio_management.enabled`、`publish_yaml_config_generation`、`read_portfolio_valuation_evidence`、
`project_assignment_scenario`。配置层级及默认值复用 `DEFAULT_CONFIG` 和现有 YAML 转换，
默认值落在 `DEFAULT_CONFIG['defaults']['portfolio']['holdings']['enabled'] = false`；
校验复用 `validate_config`；写入复用 `publish_yaml_config_generation` 的预览、SHA、备份和构建；
PM 连接与估值证据复用 `resolve_portfolio_management_client` 和
`read_portfolio_valuation_evidence`；指派计算复用现有领域 owner，**本轮不接线或复制计算**。
新增的只有 `portfolio.holdings.enabled` 这个独立来源意图和对应 CLI 操作；它不同于控制整个
PM 集成的 `portfolio_management.enabled`。未找到可直接表达这一独立意图的既有键。

选择全局布尔键，而不复用 PM 集成开关或账户类型：后两者已有更广的运行语义。
不调用 PM 当前持仓分布作为 Portfolio Exposure 结果，因为它不包含卖出期权全部被指派的情景。
不在本轮新增查询 facade、状态枚举或后台同步。数据流为 YAML 作者源 → 只读生成预览 →
预检（仅开启） → 现有生成事务 → 市场/Assistant 快照 → 全部目标读回。
预览展示源 SHA、生成后 YAML 的 SHA、当前/目标值、绝对源路径、runtime root、市场集合及目标路径；
PM 故障时仍可得到预览和预检失败原因，但 apply 不放行。确认摘要由源 SHA、目标值、路径、
市场集合和生成后 YAML 的 SHA 确定性计算；apply 重算并拒绝与预览不一致的目标。
市场和 Assistant 快照含生成时间，预览与 apply 的文件 SHA 可以不同，不纳入确认摘要；
源 SHA 另行保护并发编辑。
PM Accounts 契约提供候选账户；估值证据中的 Holdings 行决定已观测账户与 broker/market。
本轮查询尚未接线，
不以 OM/PM 标签交集设门禁，也不宣称这些账户已可由当前情景查询使用。
YAML 与市场快照读回 Holdings 值；
全部目标按事务返回的 SHA 核对文件内容，Assistant 只核对生成身份和摘要，因为它不含 Holdings 值。
事务提交前失败须明确无配置写入；提交后读回失败须报出 `write_applied=true`、受影响目标、
`audit_id`、备份路径和安全复核指引，不把报错解释成未写入。返回值明确“本阶段只是配置，
当前情景查询尚未消费该开关”。预检通过只描述那次观测，后续查询仍须单独判断实时质量。

实施切片：① 默认值与严格校验（H1）；② 开启/关闭、预检、事务与读回（H2、H3，依赖①）；
③ CLI、文档与情景隔离回归（H4，依赖②）。验证使用隔离临时 YAML/runtime root、PM 假客户端，
覆盖成功、预检失败仍可预览且 apply 无写入、关闭恢复、确认目标不匹配、
提交后读回失败回执、全部目标摘要及旧情景回归，再运行项目适用门禁。
风险：当前 PM 估值的 `complete` 要求也受行情质量影响，可能阻断开启；这是本轮保守的
开启门禁，不代表配置后的持续状态。真正按开关切换持仓来源及运行状态显示属于后续独立范围。

## Symbol 并发配置

Symbol 扫描在受支持的 `scan-pipeline` 主线程中串行执行，以保留
`runtime.symbol_timeout_sec` 的可中断 deadline。以下旧键从配置合同中移除，且没有替代键：

- `runtime.pipeline_symbol_max_workers`
- `runtime.watchlist_max_workers`

出现任一键时配置验证会失败；迁移方式是删除该键。account 与 required-data prefetch
并发配置不受此变更影响。详细运行说明见 [Configuration Guide](CONFIGURATION_GUIDE.md)。

## 生成与验证

### 首次初始化与运行目录（研发设计）

目标是让首次 `om setup init` 的结果在新终端仍可找到，让“可继续扫描配置”不被可选 Bot 项混淆，并让确认前的预览及写入失败后的恢复有可核对结果。本设计只覆盖新建本机配置；不迁移现有配置，不修改生产配置、服务或凭证，不连接 OpenD、发通知或自动运行扫描。

| 验收 | 可观察行为 |
|---|---|
| S1 就绪 | 未填写的富途账户 ID 占位符阻断所选市场的配置就绪；缺少所选市场快照同样阻断。Bot 模型或 Session 目录的问题单独显示为可选能力未就绪，不把它算成基本配置失败。`setup check` 不声称已验证实时 OpenD/凭证。 |
| S2 定位 | 成功初始化后，在没有更高优先级覆盖的新终端，`om setup check`、`om doctor --config-key` 和选定的 `om-agent` 配置及运行记录只读入口能定位同一 runtime root；显式路径和有效 `OM_RUNTIME_ROOT` 优先，已有服务显式配置不被用户级记录覆盖。存在但损坏或指向失效配置的记录不静默回退到 repo。 |
| S3 预览 | `--dry-run` 与交互确认前显示目标目录、市场、账户标签、富途 ID 是否待填、主要 starter 选择、全部拟创建文件和目录定位效果；不泄露完整账户 ID 或秘密，不写目标或持久文件。确认时仍拒绝覆盖任何已有目标，包括确认后的并发创建。 |
| S4 恢复 | 所有配置在发布前完成生成和校验；发布中失败时，只恢复本次创建且未被第三方修改的目标。若无法确定或清理，报出具体文件及后续处理方法；重试不覆盖遗留文件。成功后回读目标、目录记录和快照来源新鲜度。 |

现状：`config_yaml_init.py::init_yaml_config` 已产出 starter YAML、做 dry-run 校验并逐个原子写目标；单文件原子写不保证一组文件全部完成。`setup_ops.py::run_setup_init` 只预览路径和占位提示，并打印一次性 `export OM_RUNTIME_ROOT`。`runtime_paths.py::resolve_runtime_root` 及 `agent_tool_config.py::resolve_runtime_config_path` 分别处理根目录，默认回退 repo。`setup/check.py` 把 Bot 检查算入总体错误，所选市场快照缺失却只给 warning。现有 `config_authoring_transaction.py` 是**已有配置**的修改事务，依赖源文件及其 SHA；首次新建没有可恢复的旧源，不把新建硬塞进这条修改事务。

复用清单和检索范围：检索 `src/application/config_yaml_init.py`、`config_yaml.py`、`config_authoring_transaction.py`、`runtime_paths.py`、`agent_tool_config.py`、`setup/check.py`、`src/interfaces/cli/setup_ops.py`、`agent_tools/project.py`、`project_runs.py`、`runtime.py` 及相邻测试，关键词为 `init_yaml_config`、`publish_yaml_config_generation`、`resolve_runtime_root`、`OM_RUNTIME_ROOT`、`REPLACE_WITH_FUTU_ACCOUNT_ID`、`rollback_hint`、`runtime_root_unavailable`。starter 字段与 YAML 生成复用 `init_yaml_config`；快照预备与最终来源元数据复用 `config_authoring_transaction.py` 的现有准备逻辑；检查复用 `evaluate_runtime_config_readiness`；目录解析归并到 `resolve_runtime_root`；Bot 模型和 Session 检查复用 `run_setup_check`。`atomic_write_text` 只可用于暂存文件，不能发布新目标：它会 `replace` 已存在的目标。新增一个窄的原子无覆盖发布步骤及每用户单值的 runtime root 记录；后者必要是因为进程环境和 repo-local env-file 不能在独立终端及版本切换后稳定指向自定义目录。无新的账户、市场、策略或密钥字段。

选择：用户级记录为 `~/.config/options-monitor/runtime-root` 中的一行绝对目录，创建时权限为仅当前用户可写，读取时要求非符号链接的普通文件且属当前用户。解析顺序为显式参数、有效 `OM_RUNTIME_ROOT`、该记录、repo 兼容回退。**没有记录**表示沿用旧版 repo fallback；无法区分从未初始化和用户删除了记录，诊断须显示实际来源。**记录存在但无效**、不安全或指向没有 `config.yaml` 的目录时报错，不读取 repo 的另一份配置。`setup init` 在记录不存在时创建；已有安全记录且内容等于本次目标目录时原样保留并允许创建尚不存在的配置目标，失败补偿不删除它；已有不同或不安全记录属于冲突，显示路径并要求操作者先处理，不自动改写。配置目标有任何一个已存在则仍拒绝覆盖，给出冲突清单。服务生成的 `OM_RUNTIME_ROOT` 是显式值，仍优先于记录。Tool Gateway 的运行记录只读入口把通过上述校验的 `user_record` 来源视为可信根；repo fallback 继续不可信，不放宽账户和 run scope。解析器提供可注入用户目录，显式提供隔离环境时不能偷偷读取真实操作者 HOME，供临时目录测试与服务隔离使用。舍弃自动编辑 shell profile（只影响 shell，且可能重复）；舍弃将用户记录写进 release/repo-local `.env`（版本切换不稳定）；舍弃无条件采用平台默认目录（不能支持交互选择的自定义目录）。

`setup init` 的 dry-run 先生成 starter，再检查全部目标及记录冲突；仅可使用可清理的临时校验文件，不写目标或持久状态。预览以摘要而非完整 YAML 呈现，包含默认 US/HK symbols、Assistant/Bot 默认开关、待填账户 ID 和每个文件的路径。若 repo 已有配置且无用户记录，预览明确说明新记录会改变无显式参数命令的默认实例。还要读取当前有效 `OM_RUNTIME_ROOT` 来源；若它与目标不同，预览和成功提示都明确指出该高优先级覆盖及需要清除/更正的来源，不声称本次记录会胜过它。交互 `yes` 或显式 `--apply` 才允许继续。无需另加迁移开关。apply 再查目标是否仍不存在；发布每个目标时仍使用同目录暂存文件加原子无覆盖链接，任何并发目标冲突即停。所有市场及 Assistant 快照在发布前由 starter 字节生成，但元数据中的来源、重建命令和原始 SHA 必须绑定**最终** `config.yaml` 路径及内容。发布顺序为快照、YAML、最后目录记录；成功后逐项回读并用公共 freshness 检查确认快照可用。若普通 I/O 失败，按本次创建清单反向恢复，须同时核对文件身份（inode/device）和内容指纹才删除；不同内容、身份变化或清理失败均保留并报出，不删除已有目录、其他文件或用户后续编辑。进程被强杀时可能留下文件；下次运行拒绝覆盖并列出具体目标供操作者核对，不能凭路径推断可删除。为这个边界不新增自动恢复命令或后台状态机。

`setup check` 的 `summary.ok` 表示所选市场的**离线配置与安装先决条件**通过；它不证明连接券商或通知可达。所选快照缺失、所选市场关联的任一 Futu 账户使用 starter 占位 ID、无效或过期均为 error。Bot 模型/Session 检查保留在 `checks`，不满足时为 warn，并在摘要中单独给出 `bot_ready`；Bot 只有两项均为 ok 才算就绪。非 repo 默认根的 Bot 快照从该根的 `resolved/config.assistant.json` 读取，不从 repo 偷换来源。文本输出同时写明“基本配置”和“可选 Bot”，不能把前者描述为可直接实盘运行。完整凭证状态仍由 `om secrets status` 负责，未在此处复制秘密判断。

实施切片：① 就绪语义（S1）；② 持久目录定位（S2）；③ 预览、发布与失败恢复（S3、S4，依赖②的目录记录契约）。每片都有其对应的 `setup` facade/应用层测试；第三片加故障注入，证明未覆盖已有目标、失败后只清本次同内容文件、冲突保持可恢复。最后验证 CLI 与 Tool Gateway 新进程、Mac/Linux 路径语义及相关文档，且运行仓库必要门禁。README 的快速开始、Getting Started 和配置指南按最终公开行为同步，不把设计描述冒充已发布版本行为。

四路独立设计建议裁决：采纳原子无覆盖发布、最终 YAML 来源身份、Tool Gateway 来源信任适配、Bot 快照与选定根绑定、占位范围、无记录兼容语义及 dry-run 持久写入措辞。将“repo 已有配置时一律阻断”改为明确预览并要求现有确认，因为操作者可能正从源码目录迁往新目录；此处不能自动迁移旧文件。未采纳额外持久标记来检测用户删除记录，单文件无法识别该情形，增加另一份状态也不在本轮目标内。Planreview 指出的高优先级 env 覆盖和同值记录恢复路径已按上述合同补齐。

风险：同一用户多个独立 OM 安装共享一个记录，显式 `OM_RUNTIME_ROOT` 或显式 config path 可选择其他实例；首次初始化会预览默认实例改变。普通进程失败可补偿，强杀后仍需人工核对遗留文件；不自动猜测文件归属。用户自定义目录若被移动，记录会报错并指向修复位置，不回退 repo。记录被用户删除时会恢复旧版 repo fallback，诊断显示实际来源，但无法自动证明这是有意删除。

---

```bash
./om config validate --source yaml \
  --market us \
  --config-yaml config.yaml

./om config build --source yaml \
  --market us \
  --config-yaml config.yaml \
  --output config.us.json

./om config build-assistant --source yaml \
  --config-yaml config.yaml \
  --output resolved/config.assistant.json

./om config validate \
  --config-path config.us.json \
  --market us
```

`config build` 会写 `_generated` 来源与指纹。新生成的 YAML 市场快照按下面的市场输入指纹判定新鲜度；旧快照仍按完整文件 SHA 检查，需显式重建才能采用新语义。`tick` / `tick-cron` 拒绝过期或无效快照。

### 市场配置新鲜度：按实际依赖判定

#### 目标、边界与验收

目标是让助手独立设置变化不再阻断 US/HK 监控，同时保留真实监控配置变化后的过期保护。
原始文件 SHA 继续用于来源审计、预览确认和并发修改保护；它与市场配置是否可继续使用是两个判断。

| 验收 | 必须成立的行为 |
|---|---|
| S1 助手独立修改 | 只改 `assistant` 的模型、上下文或开关，当前市场输入不变时 US/HK 快照仍 fresh |
| S2 监控变更保护 | 当前市场 symbols、schedule、选用账户或共享监控参数改变时，相应快照仍 stale |
| S3 异常与兼容 | 来源缺失、无法读取、YAML 解析或市场转换失败、来源身份错误继续阻断；旧快照按旧规则检查，经显式重建迁移 |
| S4 只读与一致性 | 所有读取入口使用共同判定；检查不写配置、状态或快照，不自动重建；原始 SHA 并发保护保留 |

范围是 YAML 市场快照的生成元数据和公共新鲜度检查，以及既有生成事务、调用入口的必要回归验证。
不重构助手 CLI 写入流程、不拆分 YAML 文件、不自动重建、不调整调度、策略、权限或通知行为。
Assistant runtime JSON 的有效性与激活流程保持现有合同；非 YAML 来源和系统默认配置继续使用现有严格指纹规则。
本设计不授权 commit、push、merge、release、deploy 或生产写入。

#### 当前事实与 owner

本次修复基于 `4ee3b408edeb020483a8906512d438cb131ad0c1`。
`config_yaml.py::yaml_to_market_user_config` 已负责选取当前市场、相关账户和共享配置，且不将 `assistant` 放入市场输入。
原实现的 `_build_yaml_generated_metadata` 只记录完整文件 SHA，导致公共 freshness 检查把助手独立变化也判为 `source_changed`。
现在 `resolve_yaml_runtime_config` 从一次 YAML 读取构建市场输入并记录原始与 effective 两种指纹；公共 checker 按下述协议比较。

`config_authoring_transaction.py::publish_yaml_config_generation` 已支持多市场生成、来源 SHA 冲突检测、备份与失败恢复。
聊天模型切换经 `assistant/model_operations.py` 调用该事务；CLI 模型 add/use 经 `write_model_config_update` 只写 YAML。
补齐 CLI 联动不能覆盖手动编辑 YAML，且仍要求助手操作重建无关市场，因此不作为本次方向。

修复落在现有 config YAML 转换/生成与 freshness owner。tick、tick-cron、runtime readiness/status、升级验证
继续调用同一个检查函数，不在各入口增加助手特判，也不建立另一份配置字段白名单。

#### 指纹与数据流

生成时对已经得到的 `yaml_to_market_user_config(raw_cfg, market)` 结果计算确定性 SHA-256。
载荷包含算法版本、规范市场和市场 user config；编码固定为 UTF-8 JSON，key 排序、固定分隔符，
使用 `allow_nan=False` 拒绝非有限数字，生成和检查复用同一个指纹 helper。
真实配置数组保持顺序；只有 `_normalize_combo_yield` 派生的 `_explicit_fields` 与 `_explicit_call_fields`
具有集合语义，须在现有转换 owner 中按字段名稳定排序。这样重排 YAML mapping 键不会改变指纹，
并通过相同 explicit override 结果证明策略行为未变；不引入通用数组排序。
不把生成时间、源码路径、`_generated`、`_resolved` 或助手配置混进这一载荷。
这里的“有效”指现有转换器输出的市场输入，不重新实现默认值合并或配置转换。
系统 defaults 的变化仍由独立 system 来源指纹阻断；不额外承诺默认值等价化。

在 `_generated.sources` 的 YAML `market_user` 来源项增加可选字段：

```json
"effective": {
  "kind": "yaml-market-user-v1",
  "market": "us",
  "sha256": "<canonical market input SHA-256>"
}
```

保留原 `path`、`sha256` 以及 `_resolved.config_yaml_sha256` 的原始文件含义，不用语义 SHA 替换它们。
在现有 YAML loader 内只执行一次 `read_bytes`，用该字节缓冲计算原始 SHA，并从同一缓冲按 UTF-8
解码和现有规则解析；市场输入和 effective SHA 都从这份解析结果产生。保留 tab、根对象、未知键等已有验证。
元数据构建函数接收这次读取所得 raw SHA，不再打开 YAML 重新计算 `_generated`、`_resolved` 或返回 meta 中的 SHA。
不能用重新序列化的 YAML 计算人工作者文件的 raw SHA；注释、换行和键顺序属于原始字节证据。
普通 build 若读取后源文件发生新修改，允许生成内部一致的旧快照，下一次检查必须发现相关市场变化；
不承诺为整个 build 持有源文件锁。

在现有 `config_yaml.py` 中提供窄的公共转换/指纹 helper，freshness 复用它。
`GENERATED_KEY`、`GENERATED_SCHEMA_VERSION` 两个共享常量位于现有 `config_primitives.py`；
`config_yaml` 从该中立 owner 导入，freshness 保留已有导出兼容。freshness 单向依赖 config_yaml，
不会形成反向边。函数内导入也会被仓库静态依赖图计算，不能把延迟导入当作零循环检查的替代。
不新建模块或配置子系统；更新生成的依赖图并要求 production module cycles 为 0。

```text
同一份 YAML 内容 -> 现有市场转换 -> 市场 user config -> runtime config
       |                         |
       +-> 原始 SHA              +-> effective SHA

读取快照 -> 校验身份及来源 -> 重算当前市场 effective SHA -> fresh / stale / invalid
```

新协议的来源校验必须早于任何现有 `loaded`、`inline`、path 缺失等短路分支。
对带 `effective` 字段的 YAML `market_user`，先确认必需来源 role 唯一且齐全、市场匹配、
`loaded is True`、非 inline 文件来源、非空合法 path，以及 raw SHA 是 64 位十六进制字符串；
再检查 effective 对象、已知 kind、相同市场和 64 位十六进制 digest。`effective` 字段存在但为 null、
空对象、错误类型或未知版本都必须失败，不得当作旧快照缺失字段。重复/丢失来源及不合法 flags 同样非 ok。
只收紧新 effective 协议的文件来源合同；无 effective 的旧快照保留原规则，不借机重构旧协议。

通过上述检查后：从已记录路径读取并解析 YAML，用相同转换和编码计算当前指纹；
相同则该来源 fresh，即便原始文件 SHA 不同；不同则保持来源变更阻断，并提示显式重建。
每次语义检查仍实际读取来源，不通过旧 raw SHA 相等短路元数据和解析验证；无需缓存或后台服务。
其它来源逐项检查保持不变，不能因为市场输入相同而忽略 system/default 的漂移。
读取/UTF-8 解码/YAML 解析/转换/编码异常在公共 checker 内转换为结构化非 ok，保留来源 role/path、
安全的错误分类和重建提示；不把完整 YAML 内容、可能含值的解析片段写入错误结果。
`ensure_runtime_config_freshness` 继续抛出统一 RuntimeConfigFreshnessError，消费者不增加逐入口捕获或 fallback。

#### 兼容、失败与副作用

| 输入或事件 | 判定/效果 |
|---|---|
| effective 存在、版本/市场/digest 合法且重算一致 | 该 YAML 市场来源 fresh |
| 有效指纹不同 | stale，沿用 `source_changed` 与现有重建提示，补充可区分的指纹比较依据 |
| effective 缺失的旧快照 | 继续按原始 SHA 严格比较，不在读取时补字段 |
| effective 已存在但格式错误、空值、未知 kind 或市场不符 | invalid/非 ok，不能退回旧规则放行 |
| 文件不存在、权限不足、解析/转换/编码失败 | 非 ok，诊断返回结构化错误；执行入口在扫描前阻断 |
| 非 YAML、非 market_user、system 来源 | 维持原有检查语义 |
| 生成过程中 YAML 变化 | 不能发布内容与指纹不同代的快照；保留现有事务 stale-preview 保护 |

旧消费者仍读取保留的 raw SHA，遇到源文件变化会保守拒绝；升级后的消费者对旧快照也继续保守拒绝，
操作者通过已有 build/受控升级流程生成新元数据。此处没有自动迁移或恢复写入。

`_retarget_runtime_metadata` 仅把事务暂存路径改回真实路径并维护原始 SHA；必须原样保留 effective 指纹，
因为指纹不依赖暂存路径。旧事务备份、提交失败补偿、回滚和 source SHA 预览冲突检查均保持。
事务的 `expected_source_sha256` 保护修改前版本；`config_doc` 是有意修改后的文档，两者无需也不应要求相同 SHA。
事务生成中的原始 SHA 与 effective SHA 来自修改后暂存文件的同一份字节，不能另读真实旧 YAML 来计算有效指纹。
运行时 source 文件每次读取形成一次检查快照；不承诺跨整个 tick 的文件锁或可变配置热加载。

#### 实现切片与验证计划

本任务按一个完整、可独立验证的行为切片交付：**助手配置独立修改时监控继续，真实变更与无效来源仍阻断（S1–S4）**。
同一切片包含生成与检查、必要的稳定化/常量依赖调整，以及旧快照、普通 build、现有事务和实际入口回归。
这是一个增量协议，不能单独交付写者或读者；事务与入口测试直接证明该行为，不再单列“仅测试”切片。

源码 owner 为 `src/application/config_yaml.py`、`src/application/runtime_config_freshness.py`、
`src/application/config_primitives.py`。`src/application/config_authoring_transaction.py` 仅在 retarget 合同确有必要时改动，
本次只补事务回归，未修改事务源码。
回归优先扩展 `tests/test_config_yaml.py`、`tests/test_runtime_config_identity.py`、
`tests/test_config_authoring_transaction.py`、`tests/test_tick_cron.py` 和既有诊断测试。
使用合成配置和临时目录，不读取生产 secret、不访问 broker、不发送通知。

验证矩阵包括：助手设置的不同改法、注释/键顺序变化、symbols/schedule/选用账户/共享参数变化；
US/HK 显式身份；来源及元数据缺失/损坏；非 YAML 兼容；默认配置漂移；事务 retarget 与并发改写失败；
检查前后文件内容和 mtime 不变；普通 build 和至少一个真实执行入口的前置门禁。
明确反例包括：新来源 loaded=false/缺失、inline=true、缺失 raw SHA、重复必需 role、effective=null/未知版本；
Combo Yield 顶层及 call 的键顺序互换而 explicit override 不变；readiness 结构化错误和 tick-cron 的
`[CONFIG_ERROR]`/重建提示而非 traceback；普通 build 在读取后并发修改源文件，以及含注释/CRLF 时 raw SHA
与读取字节完全一致；事务修改前 SHA 冲突仍拒绝、修改后有效指纹通过 retarget 验证。
其它市场独立变化按同一个投影规则自然判定，不另建按字段追踪依赖的能力。

针对性命令（在配置好 Python >= 3.12 的工作区运行）：

```bash
.venv/bin/python -m pytest tests/test_config_yaml.py tests/test_runtime_config_identity.py tests/test_config_authoring_transaction.py tests/test_tick_cron.py
.venv/bin/python -m ruff check <本次 Python 改动>
.venv/bin/python scripts/generate_dependency_graph.py --check
git diff --check
```

最终运行仓库的 `make test` 和 `make lint`（使用该工作区可用的 Python 环境）；本次会改变 imports，
因此用既有脚本重新生成依赖图再执行 `--check`，包含零循环检查。
源码、测试或配置内容变化才重跑相关检查；具体验证结果记录在本次 review artifact。

#### 风险与未决项

- 配置 owner：转换器未来新增市场依赖时，必须继续统一复用同一转换器并补回归，避免指纹遗漏。
- 配置 owner：effective 格式必须区分旧快照“缺失”与新元数据“损坏”，兼容不得削弱 fail-closed。
- 运维 owner：新语义只对升级且重建后的快照生效；发布/升级另行授权。
- 配置诊断 owner：复用转换器可能重复输出已有 legacy 警告；无状态写入，日志静默化及更丰富的指纹展示延后处理。
- 延后项（助手 CLI owner）：模型 CLI 的事务联动与提示一致性不属于当前批准范围。


不确定某个最终值来自哪里时：

```bash
./om config explain --source yaml \
  --market us \
  --key <dot.path>
```

## Runtime lookup

公开入口通常按以下优先级解析运行快照：

1. 显式 `config_path` / `--config`；
2. 显式 `config_key=us|hk` 对应的 runtime config；
3. 入口定义的 repo-local fallback。

生产 release 目录不应依赖第 3 项。服务和诊断命令应传 `/var/lib/options-monitor/config.us.json` 等真实持久路径。

## Secrets 与本机设置

env-file 保存：

- Feishu App / webhook 等凭证；
- LLM provider API key；
- external holdings 表引用；
- Tool Gateway 写入开关；
- 其他不应进入 Git 的机器级设置。

Linux 推荐：

```text
/etc/options-monitor/options-monitor.env
```

macOS 推荐：

```text
$HOME/Library/Application Support/options-monitor/options-monitor.env
```

只读检查：

```bash
./om settings doctor
./om settings inspect
```

不要提交 env-file，不要在 issue、日志或聊天中粘贴 secret。

## 已退役旧配置

旧 layered JSON authoring 和 `config migrate-yaml` 已删除。现有安装必须直接维护
`config.yaml`，分别 validate 并重新 build US/HK runtime JSON 与 assistant JSON；当前版本不提供旧字段转换器。

## 数据配置边界

期权账本不需要 Feishu table config：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

`portfolio.runtime.json` 只在 external holdings 需要替代 env 名等兼容场景使用。它不能重新引入 Feishu `option_positions` bootstrap 或镜像。

## 禁止项

- 禁止把 `config.json`、`config.scheduled.json`、`config.market_*.json` 当作正式运行入口。
- 禁止提交 `config.yaml` 的真实生产副本、生成的用户 runtime JSON、env-file 或备份。
- 禁止用手改生成 JSON 代替 `config build`。
- 禁止在生产 release 目录内保存唯一配置副本。
- 禁止通过 legacy JSON、Feishu 表或自动 fallback 绕过当前验证器。
