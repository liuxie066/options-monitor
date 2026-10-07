# Bot / Python runtime / Scene v7

本轮目标是解释 OM 策略、分析已记录的报错、说明标的被过滤的原因，并支持连续对话、个人记忆和只读工具调用。本文描述开发中的源码合同；是否已发布、是否部署以及真实模型的效果需要分别验证。

## 调用链

渠道鉴权与去重 → `channel_facade.py` → `local_harness.py` → `host.py` → `runtime.py`。

`runtime.py` 直接使用现有 Python Chat Completions / Responses 适配器。没有历史上下文的首轮使用原生 `tool_choice=required` 要求一次工具调用；已有上下文的追问由模型判断是否需要重读。Host 校验参数和固定范围，调用原有 OM 工具，返回脱敏结果；模型随后直接给出正文。没有 Pi 子进程、IPC、答案提交工具、claims、证据编号准入或目录激活步骤。

工具由现有 registry 与各领域 `TOOLS` 定义；Scene 直接列出 7 个工具：`project_context`、`project_files`、`candidate_filter_explain`、`runtime_runs`、`runtime_logs`、`runtime_status`、`receipt_read`。渠道场景按身份追加 `bot_memory`。不再按 toolset 展开工具，也不构造目录 catalog、snapshot 或 hash。工具仍拥有账户、配置、历史快照、分页和真实状态判断；Bot 不重新计算历史筛选结果。

## 上下文与记忆

同一渠道、用户、会话和配置范围的最近 10 轮用户/助手正文存入 Host SQLite，和回答及回复 outbox 在同一事务中保存。工具结果用于当前分析；历史回答只提供连续性，当前业务事实必须重读。上下文过长时压缩较旧完整消息组，保留当前问题和最近工具配对；删去的正文只留下标为不完整的短摘录。首次模型请求后优先使用 provider 返回的输入 token 数校准本地估算，再判断是否压缩；provider 不返回 usage 时才使用保守估算提前收束。

个人记忆复用既有 `BotMemoryStore`，按鉴权用户与配置范围隔离。支持明确请求后的保存、查看、搜索、纠正、删除，保留原始用户引用、修订校验、幂等与读取确认。当前不运行后台自动记忆整理。旧 Pi 会话数据库保留，但不作为新版上下文读取来源。

## 完成与失败

默认最多 16 次模型调用、12 次工具调用，总计 180 秒，预留 45 秒回答。工具额度耗尽或连续读取失败后关闭工具，要求模型说明已知结果和缺口。正文达到输出上限最多续写一次；取得工具结果后遇到模型异常，预算允许时尝试一次最终回答。

未知工具、非法参数和跨范围参数不能执行。工具失败保持失败，过大的结果要求缩小范围，不当作有效空结果。项目源码一次最多读取 300 行；读取 cursor 自带下一位置，后续请求无需重复 start_line。模型观察不包含配置或内容摘要 hash，避免把完整性元数据误当业务原因；Host 和工具 owner 仍保留这些校验。取消或超时后的模型/读取结果不能保存答案；已运行的读取线程可能持续到自身超时，但没有提交回答的权限。

运行诊断把 `ran_scan` / `ran_pipeline` 写入标志投影为 `usable_scan_result` / `pipeline_completed_successfully`，避免把失败结果误读成流程从未调用。`account_metrics` 与 `run_audit` 是独立记录；除非单条记录明确给出关系，运行 ID、顺序和时间接近均不构成因果链。

### 成交回执的市场参数纠错（本次实现设计）

问题：渠道会话固定 `config.us.json`，用户给出美股期权成交 `deal_id`，没有指定市场；模型却向 `receipt_read` 传入 `market=HK`。工具正确返回 `SCOPE_DENIED`，模型随后把参数冲突说成“这笔回执属于 HK”，并建议切换配置。`SCOPE_DENIED` 只证明请求参数超出当前授权范围，不能证明回执的实际市场或是否已被监听。通知里的“香港”也可能是时间地点或券商地点，不能据此填写 `market`。模型把完整 `deal_id` 写成省略形式同样会破坏精确查询。

本次只修 Bot 的只读查询路径，保持渠道固定配置、`receipt_read` 自身的账户和市场权限校验、源数据与成交入账流程不变。

1. Host 在 `build_tool_payload` 构造 `receipt_read` 参数时，以固定 `config_key` / `config_path` 对应的可信运行配置推导市场。模型显式传入的 `market` 与可信市场不同（大小写不计），则在调用工具前返回 `INPUT_ERROR`：指出冲突来自**本次工具参数**、成交实际市场仍未知；若用户没有明确指定市场，模型应省略 `market` 并按当前固定范围重试。不得自动改为模型要求的市场，也不得把冲突包装成“资源属于 HK”。工具自身的 `SCOPE_DENIED` 保持最后防线。
2. 对 `receipt_read` 的 `deal_id`，Host 拒绝包含 `...` 或 `…` 的省略形式；提示使用用户给出的完整原值。其他合法标识不强加数字长度约束。模型提示词明确区分时区/券商所在地与标的市场，保留用户输入中的完整 ID；精确 ID 查询失败后不得省略 ID 退化为列表查询，再把某一行当成目标成交。若当前问题和可信上下文只有遮盖后的 ID，应请用户提供完整值，不从回执观察中的遮盖值还原。
3. 用户明确要求查询当前固定范围之外的市场时，模型应只报告当前会话无法核实该市场，不能切换授权、跨范围查询，也不能声称目标回执属于那个市场。Host 的硬保证是不会执行**参数显式指定**的跨范围回执查询；目前 `build_tool_payload` 不接收原始用户问题，因此仅靠此层无法禁止模型在明确跨范围请求后省略 `market`、读取当前范围。提示词约束此类重试，脚本测试检查该行为，但不能据此声称 Host 已建立用户意图级的硬拦截。无可信市场或配置不可读时，保持失败，不默认为 US。

验收：用真实 Host 工具循环的隔离 fixture 覆盖“US 固定范围 + 模型误传 HK → `INPUT_ERROR` 且实际读取次数为零 → 省略市场并保留完整 ID 重试 → 返回 US 回执”；覆盖同范围大小写、显式跨范围工具参数和省略 `deal_id` 时的零读取、完整 ID 可读。用脚本模型检查明确的用户跨范围请求不会省略 `market` 去读当前范围、只有遮盖 ID 时请求补全，且错误观察与回答不把参数冲突推断成回执市场。真实模型措辞效果需单独实测，脚本模型测试只证明 Host 合同与提示词输入及指定脚本行为。

复用归属：可信市场推导复用 `runtime_config_freshness.infer_runtime_config_market` 与 `agent_tool_config.load_runtime_config`；参数汇合和报错复用 `bot.tools.build_tool_payload`、`host.py` 的 `INPUT_ERROR` 观察；数据与权限复用 `agent_tools.receipts._receipt_read`；提示词更新 `bot/prompts/tool_rules.md`。不新增市场字段、状态、权限或查询工具。检索范围为上述 owner、`bot/scene.py`、`bot/channel_facade.py` 及相关 Bot/回执测试；关键词为 `receipt_read`、`market`、`deal_id`、`SCOPE_DENIED`、`build_tool_payload`。这些位置已有所需 owner，无新增领域结构。

## 跨市场只读查询设计（2026-09-29）

目标：经渠道 sender allowlist 鉴权的 Bot 用户，在显式配置的只读市场集合内查询 US/HK；例如「3690.HK 的被指派」可选择 HK，并分别查询本地期权交易事件与成交归属记录。保留每次读取的市场、账户和证据来源。此处是源码设计，生产配置和服务变更另行授权。

非目标：不改 Control、交易/账本写入、通知、券商连接，也不把行情或标的代码当成真实指派证明。不让模型决定授权。旧单市场配置默认维持单市场。

现状与依据：渠道入口只接受一个 `config_key` 或 `config_path`，`resolve_trusted_config_scope` 校验身份和新鲜度；Scene 把它作为全部工具的固定输入，Host 遇到冲突拒绝。`bot.default_market_scope=all` 仅是 Control 默认市场语义，不是 Bot 读权限。现有 `receipt_read` 有 market 字段，`trade_attribution_read` 和 `option_positions_read` 有 config_key；后者是纯读工具但尚未放入 Scene。`symbol_market` 已提供标的身份判断。个人记忆当前以 sender 和单配置权限隔离，旧会话不能在授权扩张或撤销后直接继承。

选择的合同：

1. 在经验证的独立 `config.bot.json` 的 `bot` 中增加显式 `read_markets: [us, hk]`；缺省为渠道原有单市场。只接受无重复的 `us`/`hk` 非空列表，必须包含渠道主市场；列表形状非法时配置验证失败，遗漏主市场时该渠道请求无法启动。主市场快照提供市场和账户身份，不提供读权限。渠道从同一运行目录解析另一个标准市场文件，不接受模型路径；每次选中后核验文件身份、新鲜度、账户集合及其与实际数据 runtime root 的对应关系。当前 allowlist 用户可配置双市场，生产启用仍遵守配置变更授权。
2. 渠道把市场集合和 Bot 配置代际传入 Host 的可信合同。Host 根据顶层 `config_key`/`market`/`symbol` 和嵌套 `query.symbol` 选择目标市场并拒绝冲突；各工具 owner 规范化筛选并校验 cursor 绑定的查询条件。`symbol_market` 的大写值转为小写比较。省略时单市场沿用原行为，双市场且无可判定标的的查询要求明确市场，不能默选 US。Host 只为选中市场注入可信配置，工具 owner 仍作最终验证；模型永远不能传 `config_path`。
3. Host 对目标市场及顶层/嵌套账户一起校验；同名 `lx` 只有在该市场配置包含 `lx` 时可读。成功构造工具 payload 后产生的工具观察附独立的 Host 路由标签（实际 market/account/tool）；输入被 Host 拒绝时没有这类标签。保留工具自身来源，不能把 Host 标签写成券商证明。回答说明本地 ledger、归属记录还是回执及分页覆盖；空、缺失、失败和未授权各自明确。跨市场一次只读一个目标，不合并两份记录为单一事实。
4. Scene 加入已有 `option_positions_read` 的 `events` 查询，限制可见字段为只读必要筛选；暂不开放 `list`，因为它的共享账本读取尚无市场谓词。现有事件投影补 `event_type`，据此区分本地 assignment 与普通 close，不把它称为券商确认。`trade_attribution_read` 保留独立入口并在现有 owner 增加规范化 `symbol` 筛选，分页前过滤。用户说「被指派」时按 HK 标的定位并分别核验可用证据；缺账户时仅在可信 HK 配置有唯一账户时可默认，否则请用户给账户。未读尽分页只能报告已查页，不能作否定结论。旧 `receipt_read` 的完整 ID 和不降级广泛查询规则继续适用。
5. 会话键和个人记忆 owner 绑定渠道、sender、conversation、主配置身份与 Bot 配置代际；取消分析先按当前会话键定位，再按可信渠道身份匹配并取消旧代际的活跃运行。代际取已校验 assistant 文件的规范路径、完整字节 SHA-256、`_generated.generated_at` 及文件 `mtime_ns`；受控构建的 A→B→A 得到新代际。渠道只从可信 assistant 路径生成并写入合同，`session_key_for_contract` 和 `scope_from_contract` 从同一路径重算并比对，不能信任模型或序列化字段。业务证据记忆在现有 `account_scope` 使用 `us:lx`/`hk:lx`，只从工具的可信实际市场和账户生成。每次重建 Bot 配置（包括版本升级）都会隔离旧会话和个人记忆，即使 `read_markets` 未变；旧数据保留但不自动迁移。每次工具读取前以及回答/outbox 持久化前复核代际，变化即中止本轮，不返回旧授权下的结果。不可读取的配置、冲突、撤权和工具异常均失败关闭，不以空结果返回。

| Scene 工具 | 模型可选市场 | Host 行为 |
| --- | --- | --- |
| `receipt_read` | `market`，双市场 cursor 续页仍必填 | 可信目标配置与回执根目录一致，显式冲突拒绝 |
| `option_positions_read.events`、`trade_attribution_read` | `config_key`；有 `symbol` 时可由标的推导，双市场 cursor 续页仍必填 | 只注入目标市场配置；账户和标的按目标核验 |
| `project_context`、`candidate_filter_explain`、`runtime_status` | 已有 `config_key`；候选工具也可由 `symbol` 推导 | 只注入目标市场配置；无市场且双市场时要求澄清 |
| `runtime_runs`、`runtime_logs` | 在现有 Bot 可见 schema 增加已有 `config_key` 字段 | 只注入目标市场配置；无市场且双市场时要求澄清 |
| `project_files` | `resource=project` 不表示市场；`resource=run` 使用已有 `config_key` | 项目源码不作为市场事实；运行证据按目标市场核验 |

所有显式字段须在一次调用内一致。双市场 cursor 不单独作为授权或市场选择器，续页重复原 `market`/`config_key`；原工具的 cursor 签名和筛选校验仍有效。Host 的路由标签与工具自身来源分列，未注入目标配置的工具不得声明目标市场事实。

复用归属：可信配置身份/新鲜度由 `bot.config_scope.resolve_trusted_config_scope` 统一提供给渠道、工具和记忆，授权代际由 `bot.model_config` 提供；市场解析复用 `symbol_identity.symbol_market`；Tool 参数校验复用 `bot.tools.build_tool_payload`；账户集合复用 `account_config.accounts_from_config`；只读数据复用 `agent_tools.positions`、`agent_tools.receipts`、`trades.attribution`；会话/记忆分别复用 `bot.session` / `bot.memory`。唯一新增配置名为 `bot.read_markets`，因为现有 `default_market_scope` 没有授权含义；事件 `event_type` 与归属 `symbol` 是既有 owner 的读投影/过滤扩展。检索范围：上述 owner、`config_validator.py`、`config_yaml.py`、Scene、`agent_tools.project`、Bot/channel 测试；关键词 `config_key`, `config_path`, `market`, `account`, `authority_scope`, `read_markets`。未找到可复用的 Bot 多市场权限集合；未引入第二套账本、路由器或持久状态。

拒绝方案：把固定 `us` 改为默认 `all`（扩大所有部署读取面）；让模型直接改 `config_path`（越过可信边界）；按用户问题中的 `.HK` 直接授予权限（标的身份不是授权）；在 bot.default_market_scope 上叠加 Bot ACL（混淆 Control 与只读权限）。

实现切片与验收：

1. `trusted-market-routing` 对应 S1/S2：Bot 配置解析、会话可信集合、Host 选择、数据根/账户校验、结果来源。先写失败测试：US 单市场拒绝 HK；双市场 3690.HK 选 HK；冲突 market/config_key/嵌套筛选、缺失/过期 HK 配置、未配置 HK 账户均零读取；省略市场的单/双市场行为不同；取消键一致，非默认 runtime root 不串读。
2. `records-and-isolation` 对应 S3/S4，依赖前片：Scene 事件入口、`event_type`/归属 `symbol` 读扩展、提示词、会话/记忆代际。测试 assignment 与 trade attribution 的不同来源、未证实回答、分页未尽、A→B→A 会话/记忆隔离和运行中撤权；端到端 Host fixture 验证无真实券商和通知调用。

风险：标的能提示市场，不能证明交易类型；本地事件可能滞后于券商，回答必须写证据时点。`config_path` 的兄弟运行文件只有在同一受控 runtime root 且通过身份/新鲜度校验时可用。每次受控重建都会产生新代际并隔离旧会话及个人记忆；若恢复旧文件及完全相同的元数据（包括回滚恢复），代际可能重用，需在恢复流程中检查。真实模型措辞仍需模型级验收；脚本模型测试只能证明 Host 合同与指定脚本行为。

## 保留边界

交易、账本、配置、通知与服务操作仍由原有业务模块和 deterministic Control 管理。模型没有修改这些状态的工具。既有渠道权限、去重、取消、Host 租约、outbox 和真实数据保持独立。

普通安装与新版升级仅需要 Python。回滚到旧发布仍要核验旧 Pi 存储和旧运行时；历史迁移说明见 [Pi legacy storage](PI_AGENT_CORE_INTEGRATION.md)。

## 验证

`tests/test_bot_python_runtime.py` 覆盖模型工具循环、两个 HTTP 协议、当前轮压缩、上下文事务、个人记忆隔离、取消、真实源码读取、真实过滤快照和报错写入记录。渠道 HTTP、取消、业务工具与安装升级测试分别验证相邻边界。脚本化模型结果不等同于真实模型语义验收；真实模型必须回答同一组问题后再评估效果。

同一 `deepseek-flash` 下进行两轮成对复验：完整证据直答与 Bot 工具路径都覆盖策略主要边界；报错回答都保留两条独立记录的证据边界，没有再把它们拼成因果链。结果保存在 `/private/tmp/om-python-paired-yz2ru4ib`。这是本轮目标题的回归证据，不代表生产状态或长期可靠性统计。
