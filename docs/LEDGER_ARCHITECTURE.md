# Ledger Architecture

本文记录交易与期权持仓账本的运行契约。“多源输入与历史留存”章节包含输入与处理合同、
启用前置及验收要求；代码和文档不能作为生产环境已经完成迁移的证据。

## 权威链路

```text
trade_events -> deterministic projection -> position_lots
```

- `trade_events` 是业务事实。
- `position_lots` 是可重放投影，不是第二套事实源。
- `lot_id` / 当前 `record_id` 是写入目标身份。
- `position_key` 只用于聚合、展示和风险查询，不能代替精确 lot 写入目标。
- Feishu `option_positions`、旧 v2 snapshot 和历史兼容文件不参与稳态读取或写入。

默认 SQLite 位于：

```text
<runtime_root>/output_shared/state/option_positions.sqlite3
```

不要直接修改 SQLite 行。修复必须表达为可审计的语义事件，或通过受控 projection rebuild / verify 恢复派生状态。

订单费用是同一成交事件的延迟证据，不另建费用事实账本。受控 fee enrichment 是唯一
允许更新既有事件费用 provenance 的路径：按完整订单组执行 CAS、审计、读回并在同一
事务重放受影响投影。其他模块不得直接改 `event_json`。下文“Futu 订单身份
补录”定义一条同样受控的 metadata-only 例外。

## 模块所有权

| 边界 | 当前 owner |
|---|---|
| 领域事件与投影规则 | `domain/domain/ledger/` |
| 非 ledger 模块的公共应用入口 | `src/application/ledger/api.py` |
| 命令与维护动作 | `src/application/ledger/commands.py` |
| 查询与读模型 | `src/application/ledger/queries.py`、`read_model.py` |
| 事件写入与投影发布 | `src/application/ledger/writer.py` 公共 facade；实现按职责位于 `writer_*.py` |
| 订单费用迁移与审计 | `src/application/ledger/order_fee_migration.py`；公共入口仍由 `ledger/api.py` 导出 |
| SQLite repository | `src/application/ledger/repository.py` 公共 facade；schema、trade events、position projection、assigned stock、lifecycle 与 strategy identity 分别由 `repository_*.py` 持有 |
| 当前决策投影 | `src/application/ledger/current_decision_projection.py` 公共 facade；事实域、migration 与 runtime 分别由 `current_decision_*.py` 持有 |
| lot 目标解析与 preflight | `src/application/ledger/lot_resolver.py`、`preflight.py` |
| 人工持仓工作流 | `src/application/positions/` |
| broker trade intake | `src/application/trades/` |
| 人工 CLI | `src/interfaces/cli/option_positions.py`、`trade_events.py` |

`positions`、`trades`、Agent tools、CLI 和 pipeline 不应绕过 `ledger.api` 导入内部写入原语。领域层不得反向导入 `src/`。

## 多源输入与历史留存

### 范围与权威

目标是让外部成交、持仓和结算证据经过各自适配器，进入 OM 定义的输入契约；Ledger、
收益及 CSP、CC、Combo、Wheel 消费同一套经济语义。OM 长期保存已接收的成交证据和
已确认的经济事实，历史查询不依赖外部 API 当时仍能返回同一记录。

标准成交与持仓输入分别由 `domain/domain/trade_execution.py`、`position_snapshot.py`
定义。OpenD 接收与明确的逐笔 JSONL 文件共用成交处理入口；CSV 和其他券商适配器
尚未实现。存储复用 SQLite、Inbox、Ledger facade、投影和费用补证路径。
PM 的持仓同步与通知边界仍由
[Futu Trade And Holdings Sync](FUTU_TRADE_HOLDINGS_SYNC.md) 负责。

股票与期权均属于成交证据接收范围，经济处理按资产类型分流。OM 维护期权、既有
交割及指派股票出售、策略覆盖关系；普通股票和 ETF 成交沿现有路径触发 PM 刷新
账户持仓，不在 OM 新建通用股票买卖、成本和收益账本。PM 从券商完整快照更新
绝对数量与平均成本，OM 不向 PM 发送推算增量。普通股票在 OM 没有期权事件，不
表示成交未接收或 PM 分支被跳过；其完成证据来自 Inbox 和刷新意图，而非期权事件表。

权威按问题区分：

| 问题 | 证据与本地责任 |
|---|---|
| 券商账户当前实际持仓 | 账户、环境、范围正确且新鲜完整的券商快照；本地保存其版本与质量，不用历史推算覆盖它 |
| 已发生的经济活动 | 券商成交、交割或经核验的文件证据决定内容；OM 负责持久留存、去重、纠错及恢复 |
| 批次成本、组合归属与覆盖 | 本地经济分配和已确认关系；与券商余额对账，缺失来源保持未知 |
| 扫描与收益 | 读取明确版本的观察和本地事实；当前余额一致不证明历史成交、费用或归属完整 |

快照差异不得生成猜测的平仓、指派或股票出售。可以展示可靠的券商实际余额并同时暴露
本地批次差异；依赖未解决批次、覆盖或费用的判定保持不可用或部分可用。历史事实不会
因后续查询为空而删除。事实更正保留原始证据，并走已有受控更正及下游依赖校验。

### 本地入口与启用条件

同一 Ledger 的所有成交入口共用 `<完整ledger文件名>.trade_intake_inbox.sqlite3`，
例如 `option_positions.sqlite3` 对应 `option_positions.sqlite3.trade_intake_inbox.sqlite3`，
不同扩展名的 Ledger 不共享 Inbox。
来源目录中的旧 Inbox 继续服务已有生命周期控制数据。旧 Inbox 仍有成交行时，新入口
返回 `legacy_inbox_migration_required`，保留原库。该检查覆盖入口配置指向的旧库，
以及此前按去掉 Ledger 扩展名生成的旧库；
启用前必须盘点全部历史来源路径，不能靠更换路径跳过历史证据核验。
启用前需另行核验并迁移旧成交证据与回执状态，不能把启动新代码视为生产迁移完成。
新库中的 writer guard 拒绝不理解新状态的旧连接写入。

`./om run trade-intake --config <config> --execution-file <executions.jsonl>` 默认预览。
文件须为 UTF-8，每行一个 `trade_execution.v1` 对象，最多 10 MiB / 10,000 行，
十进制金额和数量使用无指数字符串，账户必须与配置中的物理账户、环境和标签一致。
文件所有 JSON 语法先验证；逐行语义问题保存为待核验，不凭相似成交补身份。
显式 `--mode apply --confirm` 才持久应用；历史文件不触发 PM 提示或新增通知意图。
当前仅验证标准 JSONL 合成样本，真实券商导出格式需要单独适配和验收。

`./om run trade-intake --config <config> --inbox-id <id>` 默认只读保存的输入、处理结果
和回执状态。显式 `--mode apply --confirm` 从保存的输入恢复，不查询旧成交；
冲突不会被该命令清除，unknown 回执不会自动重发，旧记录不能凭空获得补发资格。
自动处理最多尝试 20 次，达到上限的 pending 在摘要中单独计数；受控恢复可以重新领取。

### 契约一：标准输入

以下名称与字段为输入合同。只将用于隔离、去重、关联及约束的字段提升为明确结构；
原始载荷与适配器诊断保留有版本的 JSON，不要求每种输入都新增独立表。

| 结构 | 字段与约束 |
|---|---|
| `BrokerAccountRef` | `broker_account_id` 为 OM 稳定内部身份；绑定 `broker_id`、`external_account_id`、`environment`；物理账户 ID 按字符串保留，`account_label` 只负责现有 `lx/sy` 路由 |
| `InstrumentRef` | 稳定合约身份、`asset_type`（`stock` 或 `option`）、市场、币种及来源代码；期权包含标的、Put/Call、行权价、到期日、乘数及必要交割规格。股票 ETF 在本范围走股票分支，保留来源证券类型。买卖方向、策略归属不进入合约身份 |
| `ExecutionInput` | `broker_account_id`、`instrument_ref`、`external_id_namespace`、`external_execution_id`、可空订单引用（`external_order_namespace`、`external_order_id`）、`side`、`quantity`、`price`、`currency`、`occurred_at_utc`、`evidence_refs`；开平信息只在有依据时提供，未知不妨碍持久接收证据，但可阻止分配入账 |
| `PositionSnapshotInput` | `snapshot_id`、`broker_account_id`、`source_id`、市场及资产过滤范围、`observed_at_utc`、来源时点（如有）、完整性、质量、明细及证据引用；每行明确合约、持仓方向和数量 |
| `SourceEvidence` | `evidence_id`、`source_id`、账户、数据类型、来源记录身份、载荷版本或内容摘要、接收时间、原始载荷引用、原始时间及其时区、适配器版本；区分逐笔成交、订单汇总、持仓和结算粒度 |

成交的 `ExecutionInput.evidence_refs` 保存稳定的 Inbox 证据集合引用；读取该引用会返回
这笔成交从 push、history 或文件入口留存的全部 `source_evidence.v1` 版本。晚到证据只
追加集合成员，不改写已经入账的经济事件。旧证据明确标记为 `legacy/unversioned`，不猜测
当时的适配器版本。OpenD 持仓快照在 `PositionSnapshotInput` 中同时保存证据引用和对应的
版本化证据 envelope，原始来源行随快照保留。

入账前必需的成交身份、合约、数量、价格或发生时间缺失时，保留证据及具体缺失项，不
构造可入账的完整成交。订单限价、委托数量和累计成交均价不能补成单笔执行数据。
成交输入与结算输入分别处理；普通成交字段不足以证明到期、指派或行权。
资产类型由来源证券类型或可验证的证券资料确定，不能仅凭缺少 `option_type` 判为股票；
未知类型持久接收后待核验，不触发错误入账或 PM 刷新。本次保留既有 Futu 生命周期
证据入口及校验，不扩展任意文件的指派、行权或结算导入；逐笔文件入口只接收已定义的
成交格式。新增结算来源需要其真实格式与独立合同，不预设通用 `SettlementInput`。

金额、价格和数量在输入合同中使用精确十进制，JSON 使用十进制字符串；拒绝非有限值。
期权数量遵循整数张约束，其他资产按明确单位验证。零价格、零实际费用可以合法；缺失
不能补零。适配器负责来源精度、时间和合约解析，保留原始证据，不通过四舍五入修补
来源冲突，不默认乘数为 100。既有浮点事件和投影的存储编码不在本切片批量重写。

费用沿用 `FeeFact` 的实际、估计与缺失语义及 `enrich_order_fees()` 的受控补证合同。
订单引用与费用分组按 `broker_account_id + external_order_namespace + external_order_id`
隔离；订单 ID 命名空间须经验证，不能默认与成交 ID 命名空间相同。
订单总费用须按完整订单范围分摊，不能在各成交中重复记入；晚到实际费用不产生新的成交。
既有实际费用与新观察金额不同仍返回 `actual_fee_conflict`，不自动覆盖；本设计不扩大
实际费用更正权限。处理订单数量、币种或并发版本不一致时保留冲突，不能部分更新投影。

`PositionSnapshotInput` 的可卖数量和成本为可选值，附对应来源口径；可卖数量不直接等于
可供新 CC 使用的股票，券商平均成本也不等于某个策略 lot 的成本。请求失败、账户错误、
过滤范围不全或无法证明完整性时，空明细不能表示全账户零持仓。快照默认只在自身范围内有效。

适配层负责 SDK、认证、分页、限频、代码、时区和来源字段映射。标准输入验证及 Ledger
写入不查询 OpenD。当前 `normalize_trade_deal()` 中的 Futu 解析和倍率补查留在 Futu
适配路径内；其他来源不能复用它的券商默认值。行情、资金与交易数据按现有独立能力接入，
不强制所有来源实现同一大接口。某来源不支持所需能力时明确返回不支持或缺失，不回退到
另一券商账户的数据。现有策略、配置开关和对外返回值不因适配器更换而自动改变。

### 契约二：成交身份与证据身份

一笔真实成交可以由推送、历史查询和文件共同证明；一笔成交也可能对应多个 lot 分配
及多个既有 trade event。身份关系为：

```text
SourceEvidence 多条 -> execution_id 一条 -> 已应用经济结果及处理结果
  期权：原 trade event 集合 -> lot 分配与投影
  指派股票出售：原 assigned-stock event -> stock lot 与经济引用
  普通股票：Inbox 处理结果与一次性 PM 刷新意图，不生成期权事件
```

- 账户唯一绑定为 `(broker_id, external_account_id, environment)`。来源连接地址、
  文件名、导入批次和逻辑账户标签不参与成交唯一身份。旧账户迁移到新账户时仍保留两个
  物理身份；经确认的展示分组不合并它们的成交键。
- 同一券商账户内，按经过验证的券商成交 ID 命名空间解析成交。若券商 ID 按市场、日期
  或子账户分区，键中必须保留该命名空间，不能假设所有券商 ID 都全局唯一。
- `execution_id` 是 OM 稳定身份。来源渠道自己的记录 ID 通过证据映射到它，渠道差异
  不产生第二份经济量；来源之间只有在明确共享成交身份或经过核验匹配后才合并。
- 同身份、相同经济内容是重放；相同身份、经济内容不同是待核验更正或冲突。保存所有
  不同证据版本，正常处理、重复跳过和重试不得清除冲突。来源字段顺序或非经济诊断变化
  不应制造经济冲突；判定使用有版本的规范化内容，费用补证遵守前述例外。
- 逐笔文件含相同券商成交 ID 时可关联 API 记录。缺成交 ID、只有订单汇总或发生时间
  精度不足时，进入待匹配，不凭价格、时间、数量相似自动去重；订单与其 fills 不能重复入账。
- 并发接收依靠唯一约束和事务内查重；应用层预查只提供预览。响应丢失先查既有 durable
  结果，暂时失败重试，归属不明留待核验，不能通过换一个请求或来源 ID 绕过去重。

成交经济内容比较固定为版本化合同：账户身份、标准合约身份（含资产类型及必要交割
规格）、买卖方向、数量、执行价格、币种和发生时点。先验证来源字段的逐笔含义，再
比较标准字段，不能直接对不同来源的原始字典或生命周期专用 hash 作成交判定。
十进制采用无指数的规范字符串，等值尾零不造成冲突；缺失与零不同，不能从旧 codec
补出的零反推来源明确报告了零。UTC 时点保留来源精度，只有能证明同一时点时才判
等价；不通过截断到秒、日期或四舍五入来合并无法证明一致的输入。

订单引用、来源明确报告的开平信息分别验证：未知补成已知时保留新增证据，并核对
既有分配与绑定；两个已知值相悖保持冲突。推断的开平、策略归属、抓取时间、诊断
字段、费用及证据引用列表不进入不可变成交经济指纹；晚到费用走既有补证合同。
规范化版本和原始证据一起保存；跨版本或旧记录缺必要事实时保持待核验，不依靠
存储浮点再编码来证明新旧来源等价。等价输入不因 API、文件字段名不同产生冲突。

身份迁移保留既有 `event_id`、`lot_id`、组合成员及生命周期引用。当前旧键
`futu:<account>:<futu_account_id>:<deal_id>` 作为来源别名，只有账户、环境、成交证据
足够时才建立映射；不能通过删前缀或忽略账户来匹配。一个新成交身份可解析到多个旧事件，
必须核对已有 `broker_deal_completion` 的拆分数量与完成证据，不能将它们压成单事件。

同一迁移覆盖 `trades/resolver.py -> positions/workflows.py -> ledger.api` 的指派股票
出售路径。旧 `assigned-stock-sale-<source_deal_id>` 只在账户、环境、成交和目标批次
证据一致时绑定为别名；保留原 stock event、`target_stock_lot_id`、费用和换算引用。
既有股票出售不能只按裸成交 ID 跨账户查重，完成读回不能只查询 `trade_events`。
新、旧股票出售入口与期权入口共用身份解析规则，但保持各自现有经济写入 owner。

迁移写入在同一 Ledger 事务内完成身份绑定、已应用事件集合核对和必要的经济写入。
旧键与新键并行可读时，所有写入口先经过同一个解析器；禁止旧、新 writer 各自产生经济量。
迁移期间现有 Futu 新写入仍保持旧事件 ID 生成规则并附加新映射，直至兼容验收完成。
来源不全的旧事件保持原身份和待核验状态，不为了迁移齐全而猜账户或环境。

冲突登记与经济采用共享现有 Ledger 文件的 `<db_path>.writer.lock`，复用
`exclusive_private_file_lock` 的跨进程、同线程可重入语义。由 `ledger.api` 提供薄的
仅加锁上下文，其他模块不导入内部锁实现。任何携带券商成交身份的 push、backfill、
文件和人工重放，都先进入同一 Inbox 接收/解析边界；禁止人工 `--deal-json` 绕过。
无券商身份的人工管理事件仍遵守原管理合同，不冒充 broker execution。

接收方在该锁内保存证据版本及冲突，随后释放。处理方先在锁外完成来源补查和必要
容量观察，再取得同一锁，重读 Inbox 的具体版本、状态和领取令牌，确认仍可采用后
调用既有 Ledger writer；同一身份的新经济应用仍受 Ledger 唯一约束和事务内校验。
来源新补开平或订单关联时，使尚未完成的旧领取失效；新领取归并已保存且无冲突的
已知关联，原始来源载荷保持不变。已入账的成交须核对实际分配，不能只比较原始未知值。
已完成成交的新关联作为新输入版本复核，只补有来源证据的缺失关联并保持投影有效；
不重复消费成交数量，不因补关联新增回执或 PM 提示。原待交付意图与已知送达结果保留。
处理结果以令牌与版本 CAS 写回 Inbox。锁的范围只包括本地最终检查、提交和读回，
不包括 OpenD、费用、PM 或通知调用；当前 Wheel 路径需要的外部容量也必须先备好，
最终事务仍核对本地 lot、intent 和覆盖条件。

顺序固定为 writer 文件锁 → 单库 SQL 事务；进入 Ledger SQL 事务前关闭 Inbox SQL
事务，返回后才开启 Inbox 回写事务。不得用已有 Ledger SQL 事务包住会自行开启
writer 连接的 resolver，也不在持有 Inbox SQL 事务时等待 writer 锁。若保留进程内
`RLock`，顺序仅允许 `RLock → writer 文件锁`，持文件锁的路径不得反向取得 `RLock`。
这不提供跨库原子提交：进程若在 Ledger 提交后退出，重入先读回原经济结果再确认
Inbox；若尚未提交则恢复原领取。令牌对应的一次处理有可核验的进程/运行归属，重新
领取时在同一锁内失效旧令牌，旧持有者返回后不得提交或覆盖新结果。

以这两个受同一锁保护的动作排序：冲突已持久登记在经济采用之前，则旧领取不能
自动入账；冲突登记在已提交经济结果之后，则保留原结果并标为待核验更正，不撤销
或再次应用经济量。到达 SDK 的墙钟时间不代替此持久顺序。复用现有全账本 writer
锁会串行化短接收与提交，验收测量锁等待；没有吞吐证据前不增加逐成交锁平台。

当前 `ContractKey` 使用逻辑账户和 broker，尚不能单独隔离同券商、同逻辑标签下多个物理
账户。新输入中携带物理身份不能自动解决投影隔离：在开仓、平仓目标选择、投影和容量读模型
证明物理范围一致前，不启用此类多账户路由。不得用隐藏改名或将另一券商伪装为 Futu 绕过。

合约身份也有相同接入门槛：当前 `ContractKey` 和 FIFO close selector 未表达所有
乘数与交割规格。只有能无歧义映射到现有 Ledger 合约及 lot 的输入才可入账；其他
形状保存证据并明确不支持或待核验。不得把不同规格合约选成同一平仓目标，也不为
接入尚无真实需求的合约形状重写全部合约模型。

### 契约三：同步覆盖与缺口

复用现有 Inbox 保存待处理证据，按 `source_id + broker_account_id + dataset + 查询过滤范围`
记录同步覆盖。成交、订单费用、结算和持仓快照各有范围；市场或合约过滤的成功结果不代表
整个账户完整。实时推送证明收到该条记录，不证明某个时间区间没有漏单。

目标查询回执至少包含 `request_id`、请求起止时间、来源实际返回/声明的覆盖范围、账户
与环境绑定、分页或截断信息、接收数量、证据引用、观察时间和明确错误。内部时间区间采用
`[start, end)`；适配器将其转换为来源时区和边界语义，边界采用重叠补查及身份去重，
避免截断精度或时区换日造成漏单。回执缺失不能默认完整。

| 进度 | 推进条件 | 不代表什么 |
|---|---|---|
| 查询范围完成 | 身份与过滤范围正确、页/分段齐全且无未解释截断或来源错误 | 不保证请求区间外历史存在，也不表示已持久接收 |
| 持久接收完成 | 该范围返回的每条记录已保存，或已确认存在等价 durable 记录；冲突版本也已保存 | 不表示每条记录已经分配入账或完成费用补证 |
| 业务处理进度 | 通过 Ledger 读回确认结果；未分配、冲突、暂时失败分别计数并能重新取出处理 | 不等于券商持仓已经对齐，不证明历史利润完整 |
| 对账结果 | 明确账本版本、券商快照、时点及范围，保存差异和质量 | 当前余额一致不能补足历史成交和费用缺口 |

补查游标只越过连续、完整且持久接收的区间。失败区间作为缺口保留；后来成功的区间
可以留存回执，但不能用最大返回成交时间跨过缺口。完整空结果可以推进查询/接收游标，
不创建成交；没有完整性证据的空结果保持未知。推送重放、历史补查和文件重导入共用身份规则。

业务处理失败可以与接收游标推进并存，前提是原始输入、冲突版本和处理欠项已可靠持久化，
恢复不依赖下一次外部查询仍能找到它。持久化失败则不能推进。暂时故障不转成永久失败并清除
待处理项；已入账但回执未写成时按既有成交身份恢复回执，不再次应用经济量。

Inbox 与 Ledger 当前有各自存储事务，不假设跨库原子提交。顺序是先 durable 接收，再
经 Ledger facade 应用并读回，最后确认处理结果；每一处崩溃窗口通过同一身份重入恢复。
同步回执与接收游标须由持有 Inbox 的边界维护，只有 durable 证据存在后才能推进；游标
写入失败允许重复查询，不允许先推进后保存数据。

自动重试达到现有尝试上限时，原始输入与最后原因仍持久保留。状态查询需区分可自动
重试、次数耗尽而暂停、证据冲突和已完成，不能只展示一个无法解释的 pending 总数。
复用现有 Inbox、监听状态与人工重试入口：本地恢复读取保存的版本、重新经过领取与
提交校验，次数恢复须有明确操作者动作和审计；不更换成交身份，不重新依赖 provider
历史查询，不清除冲突。暂停可由既有计数与原因推导，不要求另建任务表或无限自动重试。

来源已知的可查询起点、区间限制和能力证据由适配器记录；未知保持未知，不在核心硬编码
Futu 的天数。查询完成只说明来源合同内的请求完成；超过可验证历史范围时保留覆盖缺口，
不能用空结果证明从未成交。断线补查仍服务于常驻接入；较早历史的初次导入或恢复是显式
有界任务，不自动扩大 listener 为全历史扫描，也不因历史导入触发实时成交通知或 PM 刷新。
后续费用及生命周期补证沿各自已有恢复路径，不能由成交接收游标宣布完成。
晚到成交使用来源支持的更新时间游标或明确的回看范围；超出回看范围且无额外核验时
保留历史覆盖限制，不承诺重叠一个窗口即可发现任意久以前的新记录。费用补查应能从
Ledger 的缺费用候选恢复，复用 `sync_order_fees()` 的选择及应用逻辑；不能只依赖
内存 fee queue 或原成交仍处于 recent history 窗口。

2026-09-07 核对的 [Futu 历史成交文档](https://openapi.futunn.com/futu-api-doc/trade/get-history-order-fill-list.html)
规定缺省起止时间按 90 天补齐、同账户每 30 秒最多查询 10 次；该页面没有承诺最早可查
日期或无限保留。90 天不应写成总历史保留期限。另据
[Futu 账户说明](https://openapi.futunn.com/futu-api-doc/qa/trade.html)，部分历史仍属于被综合
账户替代的旧账户 ID，历史导入需要明确旧账户范围。实际账户历史覆盖尚未由本设计验证。

### 交易监听整体重构

交易链覆盖成交接收、入账、费用、汇率、策略关系、生命周期及
通知编排；成功标准是职责和恢复路径清楚、已知缺陷有行为验收，不以拆文件或增加
抽象数量为目标。复用现有存储及公共入口，不引入通用任务平台或统一策略状态机。

本次范围从 `./om run trade-intake` 的监听入口及历史补查、人工重试进入，沿现有
`process_supervisor -> auto_intake -> Inbox -> intake/resolver -> ledger.api`
核对到持久结果与交付。标准输入、历史留存、逐笔文件入口和持仓对账遵守本节前三项
契约。CSP、CC、Combo、Wheel 共用经济事实；不修改候选筛选、排序、投资策略参数、
PM 的持仓同步合同，不建设下单系统，也不自动变更生产账本或部署服务。

成功信号分别为：

- S1：同一成交跨渠道及新旧身份重放仅有一套经济效果，冲突证据保留，无法安全分配时可恢复。
- S2：接收回调只做可靠接收；查询、入账和交付的完成状态可以区分，崩溃后从持久结果继续。
- S3：费用和人民币换算独立补证，订单费用守恒，汇率有效时间可追溯，晚到证据不依赖内存。
- S4：策略关系在提交时验证，普通 CSP/CC、Combo 买卖腿和 Wheel 的收益维度保持既定合同。
- S5：逐笔文件验证来源边界；完整券商快照与本地批次差异可解释，未知范围不提供虚假容量。

选择沿既有 owner 重构并逐步替换内部路径。仅提取 `auto_intake.py` 的函数不能解决
跨入口冲突、欠项恢复和事务内采用问题；另建通用消息平台、provider 基类或第二份账本
则会新增一致性边界。本设计均不采用，不改变现有进程部署形态。

| 职责 | 目标边界与既有 owner | 完成依据 |
|---|---|---|
| 来源接收 | adapter 与 `trades/inbox.py` 负责来源绑定、原始证据、重复和冲突隔离；SDK 回调持久接收后返回 | 处理领取绑定已保存的具体版本；冲突不能被普通处理、重复跳过或重试覆盖 |
| 成交与批次分配 | `trades/intake.py`、`resolver.py` 及指派股票出售的 `positions/workflows.py` 经 `ledger/api.py` 应用规范化事实；必要身份、合约与数量先验证 | 一个身份只应用一次经济量；普通股票保留 PM 刷新分支；无法安全分配时保留证据与欠项，不伪造持仓或成功关系 |
| 订单费用 | `trades/order_fee_sync.py` 查询并校验整单证据，`ledger/order_fee_migration.py` 持有费用更新事务 | actual／estimate／missing 分别保留；整单数量、币种与金额守恒，重启后可从本地欠项恢复 |
| 汇率及人民币换算 | 来源适配保留币对、来源有效时间、抓取时间及证据；`cash_conversion.py` 与 `ledger/cash_conversion_migration.py` 按统一口径冻结或补证 | 原币事实与人民币换算分别判定完整性；缺汇率保持 pending，补费用不等于补汇率完成 |
| 策略关系及生命周期 | Combo reconciliation、Wheel intent/companion 和 lifecycle 各自拥有判断；正式采用由 Ledger 写入边界最终验证 | 关系具有明确证据及数量，采用时仍满足对应条件；普通 CSP/CC 不依赖组合匹配成功 |
| 通知与其他交付 | 既有普通平仓和生命周期 outbox 仍由 Ledger 与其 dispatcher 持有；其余普通回执由 trades 的持久结果和发送路径负责；PM 刷新保留一次性提示合同 | 成交提交、费用补证、策略采用与通知确认分别可查询；发送失败不重做成交；历史导入不生成可投递的新意图 |

`auto_intake.py` 保留启动、来源协调和已有周期调度。SDK 回调不执行整单费用查询、
策略扫描、生命周期 provider 初始化或通知网络调用。外部查询先取得有版本的证据，
写入前由领域与 Ledger owner 验证；不能为了缩小共享锁而取消必要的事务内校验。
关闭生命周期 provider 观察时，不在普通成交接收路径提前创建相应 gateway；已有
启动 checkpoint seal 等持久恢复前置仍须保留。
接收后唤醒现有处理循环，重启时从 Inbox 恢复；新成交不被迫等待 60 秒失败重试
定时器。周期扫描用于兜底，唤醒信号丢失不能丢失已持久接收的成交。验证接收耗时、
入账延迟、写锁等待及欠项恢复，不以拆文件或未经测量的提速比例作为性能验收。

每个成交分别展示接收、分配入账、费用、人民币换算、归属和交付进度。复用各 owner
的现有结果与欠项，不用一个 `processed` 状态代表全部完成，也不强制把这些进度
集中成一张新表。某项补证迟到时，只重做它及受影响的派生结果，不再次消费成交数量。
新写入依赖的校验条件失败时保留原始证据；不得为让流程继续而跳过覆盖或关联约束。

依赖顺序是可靠接收、身份与数量验证、经济入账，再处理依赖该事实的补证与交付。
这不是六项任务无条件并行：整单费用依赖完整成交集合，换算依赖币种、时点和金额，
策略关系依赖可用的 lot 与容量，通知依赖明确版本的持久结果。外部查询不持有写事务；
正式采用时必须重新验证可能变化的前置条件。暂时不可得、永久不支持、证据冲突和
已完成分别保留原因，不能都落成成功或通过无限重试掩盖冲突。

费用补查从已持久保存的订单欠项恢复，内存队列只用于及时触发；失败、未终态及费用
尚未可得均保留下一次可恢复依据。费用补查依赖的外部证据仍不可得时保持欠项，不能
承诺 provider 永久可查；本地可恢复表示下一次能继续尝试并解释缺口。

普通平仓已有 `broker_close_notification.v1` 的事务内 outbox，不迁走其发送 owner、
稳定身份、pending/unknown 及 `notification_history_unseeded` 语义。其余目前直接
发送的普通回执，复用 trades 的 Inbox 持久结果保存冻结发送身份、路由、内容和状态；
`trades/receipt.py` 与已有补偿入口负责读回及恢复，不伪装成生命周期案件。
Ledger 提交后、普通回执记录前崩溃时，先读回既有成交结果，再创建同一发送身份；
执行 provider 调用前，须在冻结尝试的同一事务核对当前领取与版本；失效领取不得转成
业务失败回执或覆盖当前处理状态。发送后结果未落地或 provider 结果不明
时保持 unknown；只有已验证可查询或支持该重试窗口内幂等的能力才允许自动恢复发送，
否则走已有受控核验/补偿入口。已确认发送、确定的未发送失败和 unknown 分别处理。
已有无法证明是否送达的历史普通回执不能仅因状态缺失而补发。首次创建 Inbox 时若
经济事实已经存在，也不能授予新的回执资格；恢复资格须在同一 writer 锁下核对并持久保存。
所有路径只保留一个发送 owner，不引入通用通知平台。

实时 push、断线补查与显式历史文件导入的交付资格在持久接收时固定，随处理结果
传到实际创建通知意图的 owner。历史导入首次平仓也不得创建可投递 outbox，不能只
关闭上层 callback；抑制决定须可读回。用途不参与经济唯一身份，重导入不得改写既有
实时成交的 pending/unknown outbox，也不得将历史抑制升级为实时发送。

PM 刷新沿既有 `portfolio_management` 开关、apply、首次 Futu push/backfill 股票
资格生成，只发送账户与稳定 request ID。处理结果与刷新意图在同一 Inbox 事务保存，
先于 processed 状态更新；接管处理不能丢掉尚未认领的意图。监听恢复同时读取已处理
成交中尚未认领的提示，按原来源和物理账户核对资格。Inbox 认领后，在锁外尝试一次；普通
期权成交不触发 PM，历史文件导入不触发 PM。原请求超时、结果未知或进程在认领后
崩溃时不自动重发，仍由 PM 的既有全量同步兜底。不能把 PM 提示当成普通成交通知
增加重试；返回 accepted 只证明 PM 收到提示，不证明持仓已同步。

汇率按经济事件发生时点转换后的 `Asia/Shanghai` 自然日归属，同一日期、同一币对
使用同一汇率，与美股交易日或接收日期无关。晚接收成交和晚到实际费用仍使用原经济
日期；一笔订单若跨北京时间日期成交，各成交及其分摊费用分别使用自身经济日期。
来源有效时间、抓取时间和汇率适用日期分别保留，重新抓取不能刷新旧报价的有效日期。

复用 `FXRateFact`、既有 performance evidence SQLite 与现金换算 owner，日级规则只
服务现金换算，实时写入和历史补证共用它；其他估值的时点 selector 不改成按日选择。
每日固定结果须持久保存币对、适用日期、选用事实、口径版本及更正引用，并由存储唯一
约束和事务内选取保证并发写入不会选出两个日价。没有该日期的可信证据时人民币金额
保持 pending，原币成交和实际费用照常完成；补齐日价后沿既有受控路径完成换算。

每天采用首次成功取得且可核验来源报价日期属于该北京时间日期的可信报价，固定后
当日复用；沿用腾讯优先、新浪兜底，不将该市场报价称为官方日价。来源报价日期不可
核验或仅本机抓取时间新鲜时不得采用。当前 observation 缓存不是日价账本，固定的
日价写入持久证据后才可用于换算；当日可信报价尚不可得时先 pending。无报价日期沿用现有明确官方
carry 日期、最长 7 日的受限政策，否则 pending，不新增隐含节假日延续或自动前日价。
固定日价不随重放或缓存更新漂移；更正须有明确证据、受控预览及影响范围，不能在查询
报表时改写。股票出售预览只读证据，实际日价固定与现金换算在已有出售事务内完成；
预览、校验失败或事务回滚不固定新日价。已有冻结换算保持其原口径并可辨认；本次不自动重算既有 observed 金额，
未统一的历史范围不得声称已采用新日价。其迁移仍走已有受控更正，不靠重新导入成交。

策略采用保留基础收益分类和实际关系两个维度。Combo 自动采用须在提交时验证唯一性；
Wheel intent 按每笔实际成交数量消费，余量、绑定订单的命名空间及账户范围均须核对。相同 intent
创建请求的身份由稳定请求参数决定，首次接受时的容量证据另行保留；动态容量变化不
破坏已成功请求的读回。部分成交、补关联及关系更正不得改变账户经济总量。

结构迁移与行为修复分别验收：迁移保持正确的公共合同、旧身份和恢复语义，缺陷修复
明确触发条件及修复前后结果。切片仅可回退到经过验证、能理解或安全拒绝新持久状态
的 writer 版本。每次激活前在迁移回执中固定最低兼容版本、允许回退的数据条件和
writer 退出/重启顺序；按已有数据库 writer guard 模式保护受影响状态，并验证旧
writer 不能忽略这些状态继续写入，不能假定现有 guard 已覆盖新增字段或表。
若无法证明安全拒绝或兼容，则仅允许前向修复，不能用陈旧备份覆盖之后收到的事实。
只保留一个经济写入口；替代路径通过验收后移除被替代的内部逻辑，历史身份兼容保留
在单一边界，避免长期双轨。升级后产生新冲突、领取和通知状态的临时库必须验证允许
版本的读写或明确拒绝；保留旧 event ID 不能代替此验收。

### 实现切片与验证边界

| 顺序 | 切片与已有 owner | 完成条件 |
|---|---|---|
| 1 | 固定标准输入、经济内容比较、身份解析与 Inbox 领取/提交协调，复用 `trades/normalizer.py`、`ledger/external_event_key.py`、`trades/deal_identity.py`、`trades/inbox.py`、`positions/workflows.py` 与 repository | 正确的 Futu 外部行为兼容；新旧期权/指派股票别名返回原结果；账户环境与可表达合约隔离；并发冲突不能越过最终检查；普通股票保留接收结果 |
| 2 | 分离监听编排、Futu 适配与公共入账，覆盖 `auto_intake.py`、Ledger commands/writer、lifecycle 来源校验、普通通知及 PM 提示 | 接收回调不执行重业务；核心不查询 OpenD；preview、写入与读模型身份一致；现有平仓 outbox 和 PM 一次性合同保留；通知中断不重做成交或盲目重发 |
| 3 | 补齐查询覆盖、费用及汇率恢复，复用 backfill、Inbox、order fee sync、history adapter、cash conversion 及其 migration；查询与原币费用恢复、FX 口径激活分别验收 | 接收覆盖与本地欠项可恢复，费用整单守恒；原币费用恢复不等待 FX 决策，人民币可保持 pending；FX 只在已确认日期/来源规则与证据充分时激活 |
| 4 | 修正策略关系采用与生命周期边界，复用 Combo adoption、Wheel intent/companion 及现有事务 | 自动 Combo 在提交时仍无歧义；Wheel 部分成交、订单绑定和相同请求重试正确；基础收益分类及经济总量稳定 |
| 5 | 以有明确格式的券商逐笔导出文件验证第二入口，继续经 Ledger facade 接收 | API/文件共用接收、补证及关系流程；无 ID、订单汇总和冲突保持待核验；重导入无新增经济量及实时通知 |
| 6 | 将现有持仓适配与质量对账接入标准快照，复用 `quality/opend_position_adapter.py`、`quality/position_checks.py`、`futu_portfolio_context.py` | 快照与本地批次职责明确；过滤、不完整、过期、不同账户及不同环境不互相补值；容量不跨物理账户合并 |

上述六个切片分别映射 S1、S2、S3、S4、S1/S5、S5。合并为“接收与恢复、归属、外部
对账”三个切片，会把身份兼容、进程恢复及费用/FX 口径切换绑定为一次验收与回退，且
让前两项等待 FX 定义；文件映射与持仓容量也有不同的证据门槛。因此保留六个可分别
验证的行为增量。1 是后续身份路径的前提；2 为 3、4 的独立恢复提供编排，5 复用
1—4 的入口，6 完成快照到容量的范围闭环。前五项不得提前启用尚未证明隔离的多物理
账户路由。每次替换都须验证原公共入口以及重启后的 durable 结果，再删除旧内部路径。
切片 3 内的 FX 口径激活是独立条件，不阻止已经通过验收的查询覆盖和原币费用恢复；
六个主要切片不代表所有子结果必须一次同时上线。任何生产激活仍走独立发布/升级授权。

当前生产适配仍支持 Futu，规范化类型保留 `futu_account_id`；部分公共 open/close
command 也固定 broker，不能只换 gateway 或 writer。`futu.deal` 保留旧 `futu:` 事件键，
其他明确成交命名空间使用完整成交身份派生事件键。`source="opend_push"` 和 lifecycle
的 Futu 来源约束仍须保留，不能抹去来源后削弱现有结算证据验证。

backfill checkpoint 在明确的完整查询回执与 durable 接收成立时推进，并不等待所有
业务处理完成。缺少 `account_results`、分页终结或账户覆盖证明时不能默认查询完整。
游标按来源端点、物理账户、REAL 环境、成交 dataset 和过滤范围分区；账户改绑不能
覆盖原范围的游标。旧无范围游标保留为未验证证据，不能直接用于新范围。
Inbox 留存各来源版本与冲突输入；费用恢复从 Ledger 的订单欠项重新构建工作队列，
内存队列仅负责及时触发。来源已经无法提供的历史费用仍明确缺失，不承诺自动补齐。

每个切片独立验证且只保留一个经济写入入口。切片 5 的文件列映射与支持范围由实际样本
确定；未取得样本前完成合同及合成 fixture 验证，不声称已支持任意 CSV。现有旧事件缺少
物理账户或环境证据的比例、实际历史可查范围以及其他券商的 ID 命名空间，均在对应迁移
或接入前做只读盘点，不能依据当前设计推测。

尚待固定的条件及责任：现金换算 owner 核验每日首个可信报价的来源日期证据；
文件适配 owner 核验实际导出列和身份粒度；账户身份 owner 盘点旧记录可迁移证据；
监听 owner 在实施前核对目标代码基线、已合入的相关修复与当前设计差异。以上分别
阻止相应 FX 口径切换、声称真实文件支持、无证据别名迁移及在过时基线上重复修复，
不授权猜测缺失数据或扩大来源支持范围。

### 验收矩阵

以下定义验收要求，具体覆盖与剩余启用条件须由验证记录证明。经公共 Ledger facade 和持久化读回证明
经济量与身份；适配器、同步和恢复场景使用离线 fixture，不调用生产 provider。

| 场景 | 必须证明的结果 |
|---|---|
| 同一成交依次 push、history、逐笔文件导入 | 一个 execution 身份及一套经济效果，多份来源证据；费用不重复 |
| 不同券商、物理账户或环境使用相同成交 ID | 身份隔离，open preview、close target、写入与读模型均不能跨账户匹配 |
| 一笔旧成交已拆成两个平仓事件 | 新旧键返回原事件集合和原 lot ID；数量守恒，完成证据不丢失 |
| 旧记录缺物理身份，或文件仅提供订单汇总 | 保留待核验，不猜映射、不生成 fills、不以订单价格补执行价格 |
| 同身份不同经济内容；稍后正常重放 | 两份证据及冲突持续可见，重试不能清除冲突或产生第二笔经济量 |
| 估计费用后补实际费用，重复收到补证 | 沿现有 CAS、审计和读回合同更新费用；不新增成交，投影及报表一致 |
| 查询漏页、截断、单账户失败或缺完整性回执 | 失败范围保留缺口，相应游标不越过；其他范围的成功可持久保存 |
| 完整空区间与失败空区间 | 前者可推进接收覆盖，后者保持未知；二者均不改变已存成交或虚构零持仓 |
| durable 入队后、Ledger 提交后分别崩溃 | 前者可恢复处理；后者恢复回执；两种重试均不产生第二次经济效果 |
| 接收完成但入账暂时失败或需要人工核验 | 接收游标语义准确，欠项保留并可从本地重试，不依赖 provider 再次返回 |
| 跨时区换日、请求边界及晚到数据 | 重叠查询与稳定身份去重不漏不重；新接收事实按实际发生时间参与历史计算 |
| 成交离开 recent history 窗口且进程重启后才出现实际费用 | 从本地缺费用候选恢复补查，重复执行无额外经济效果，不依赖内存队列 |
| 券商持仓 100 股，本地批次合计 200 股 | 展示券商余额与差异，禁止用 200 股承诺新容量，不猜卖股原因或 lot |
| 同一事实的策略归属更正 | 普通 CSP/CC、Combo 卖腿、独立买腿及 Wheel Call 仍按既定维度统计；账户总计与基础腿不变 |
| 首版成交已接收未入账，后到冲突版本；另测已入账后的漂移 | push、补查及重试均保持冲突；不未经核验选择任一版本入账，已保存版本与实际处理版本一致 |
| 旧日期汇率被重新抓取，或事件先于报价发生 | 抓取时间不能冒充有效时间；是否可用由同一明确规则判定，实时写入和历史补证不会隐式采用不同口径 |
| 原币费用补齐但人民币换算缺证据 | actual fee 与 FX 欠项分别可见；补 FX 不重复记费用，不用当前汇率填历史缺口 |
| Combo 唯一方案产生后、采用前新增可配对腿 | 自动采用重新验证后停止；明确的人工选择仍遵守其独立确认合同 |
| 2 张 Wheel intent 分别成交 1 张，或收到另一订单的同合约成交 | 合法成交逐笔消费且剩余量正确；已绑定订单必须匹配，不错误归属或要求首笔即消费全部 |
| Wheel intent 首次提交刷新容量后收到相同请求重试 | 返回既有接受结果，原请求与接受时证据可追溯，不再预留或误报请求变化 |
| 普通回执发送成功但本地结果未保存，或 provider 结果未知 | 成交不重复；稳定发送身份可恢复，未知交付不盲目重发 |
| 生命周期 provider 观察关闭，普通成交到达 | 不创建该观察 gateway；必要 checkpoint 仍执行，成交接收及既有短窗口补查不受无关 provider 初始化阻塞 |
| 两个账户有相同成交 ID 的指派股票出售，再分别通过 API/文件重放 | 各自落到正确股票批次；保留旧 stock event、费用和换算引用，同一出售无第二份经济量 |
| 相同成交采用不同字段名、等值十进制，再改变执行价或币种 | 等价支持格式重放；真实经济变化进入冲突；缺失与零、来源字段与订单汇总分别判断 |
| V1 已领取，V2 冲突先登记；另测提交后才登记 V2 | 前者拒绝自动采用 V1，后者保留原经济结果与冲突；多进程和人工重放不能绕过共享边界 |
| 领取者退出或旧令牌失效后返回 | 新领取可从持久结果恢复，旧令牌不能提交或回写；Ledger 已提交时不重复应用 |
| 合约基础字段相同，仅乘数或交割规格不同 | 公共平仓目标选择与写入不消费另一规格的 lot；不支持的形状保留证据 |
| 普通股票首次 push、重复 push、批量 backfill，或 PM 认领后超时/崩溃 | 沿现有条件触发一次提示，批量每账户至多一次；不重试 PM、不生成期权事件；accepted 不冒充同步完成 |
| 历史文件首次导入普通平仓，随后重导入并重启 dispatcher | 无新的可投递意图，离线 sender 零调用；既有实时 pending/unknown 平仓的唯一 owner 与状态保留 |
| 普通非 outbox 回执在 Ledger 提交后、发送前、发送后分别崩溃 | 原经济量不变；可证明未调用时安全恢复，结果不明时保持 unknown；旧回执缺证据不能补发 |
| 自动重试达到上限后重启，再执行受控本地恢复 | 暂停原因可见，可从保存的版本重新处理；不依赖 provider 返回旧成交，不清冲突、不换成交键 |
| 新持久状态激活后，以允许回退的 writer 版本读写 | 正确识别状态或明确拒绝；不能恢复旧冲突漏洞、遗漏新欠项或重复发送，不用陈旧备份抹去新事实 |
| FX 规则尚不可激活，原币费用已具备补证条件 | 原币 actual 独立完成且数量/金额守恒，人民币继续 pending，之后补汇率不再记一遍费用 |
| 同一北京时间日期内多次成交、不同市场日期、进程重启及并发首次取价 | 同一币对引用同一持久日价；北京时间午夜前后分别归属各自日期，不按接收日期分组 |
| 日价迟到、费用次日才补齐，或订单成交跨北京时间日期 | 原币事实持续有效；补证使用各成交原经济日期及对应日价，不换用补证当天价 |
| 当日无可信报价、来源只有旧报价或历史 observed 已冻结 | 新欠项明确 pending；已有换算保留可辨认的旧口径，不伪造日期或静默重算历史 |

每个切片先运行并扩展下列已有离线测试；新文件入口仅在现有 owner 无合适测试入口时
新增一份边界测试，覆盖合成逐笔文件与重复导入，不复制整套 Ledger 测试。

| 切片 | 最小关联验证集合 |
|---|---|
| 1 | `test_trades_normalizer.py`、`test_trades_inbox.py`、`test_trade_contract_identity.py`、`test_assigned_stock_sale_intake.py`、`test_trades_resolver_open.py`、`test_trades_resolver_close.py`；临时库和真实子进程证明冲突/提交顺序及恢复 |
| 2 | `test_trades_auto_intake_backfill.py`、`test_trades_auto_intake_receipt_routing.py`、`test_trades_receipt_compensation.py`、`test_trades_portfolio_refresh.py`、`test_ledger_sqlite_workflows.py`；模拟 sender/provider，验证持久结果及零次或唯一允许的交付调用 |
| 3 | `test_trades_history_backfill.py`、`test_order_fee_sync.py`、`test_cash_conversion_at_write.py`、`test_cash_conversion_backfill.py`、`test_exchange_rates_fetch.py`；费用与 FX 分开验收，跨日、迟到及并发结果读回 |
| 4 | `test_combo_reconciliation_application.py`、`test_combo_reconciliation_repository.py`、`test_wheel_strategy.py`、`test_wheel_workflows.py`；实际采用 facade 验证竞争、部分数量及同请求恢复，补相关生命周期入口回归 |
| 5 | 逐笔文件公共入口测试，联动切片 1 的 API/文件身份及切片 2 的历史交付抑制案例；真实样本支持单独记录证据 |
| 6 | `quality/test_opend_position_adapter.py`、相关持仓检查及容量读取测试；范围、账户、时点与不完整快照在公共读模型中保持边界 |

表内路径均相对 `tests/`，运行方式为
`PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider <对应测试路径>`。
每个切片另运行相关静态/边界检查，最终按完整改动范围执行项目 analyze 与必要回归。
Ledger 公共入口覆盖开仓、平仓、费用、拆分与重试读回；不把纯字段转换单测当作迁移
成功，也不以全文档检查代替业务验收。
测试按业务不变量组织，优先覆盖真实公共入口、持久化与失败恢复；重复或只绑定内部
实现结构的测试，在保留有效覆盖后合并或替换。代码测试验证入账和关系规则，不证明
策略具有投资收益；测试数量或全部通过也不代表缺失的并发、晚到和崩溃场景已被验证。

## 成交接收与日汇率查询成本

Inbox 按成交身份读取候选；日汇率固定和只读预览只读取 FX 事实。两条路径保留既有
Ledger、Inbox、汇率表和业务判定，不新增领域实体、通用查询框架、缓存或后台迁移服务。
代码具备以下行为；旧生产库是否完成索引维护，需要独立核验。

### Inbox 候选与身份约束

`trades/inbox.py` 的存在性核验和 `applied_execution_association_conflicts` 经
`ledger/api.py` 使用共享候选读取。SQLite 查询由 `repository_trade_events.py`、
`repository_assigned_stock.py` 和 `repository_trade_schema.py` 负责。原 Python
规则仍拥有完整身份、经济内容及关联冲突判定，SQL 不重做账户归一化或身份哈希。

接收流程为：取得既有 Ledger 写锁 → 读取已应用候选和旧身份候选 → 原身份/关联判定 →
持久保存 Inbox 证据、冲突或领取状态 → 释放锁并沿现有方式唤醒处理循环。
重复成交也核验关联冲突，不把判定移到锁外。

| 事实表 | 身份 JSON 路径 | 非唯一表达式索引 |
|---|---|---|
| `trade_events` | `$.raw_payload.execution_id` | `idx_trade_events_execution_identity_v1` |
| `assigned_stock_events` | `$.execution_id` | `idx_assigned_stock_execution_identity_v1` |

设 K 为对应路径，查询条件为 `K = 当前身份 OR K IS NULL OR K = ''`。
同一索引服务精确身份和两类旧候选，不增加 legacy 部分索引。候选经过现有事件 codec，
保留一笔成交分配多个 lot 的全部事件和 void 历史。完整身份包括券商、物理账户、环境、
命名空间、外部成交 ID；逻辑账户标签不能代替物理隔离证据。

分区成立依赖以下存储不变量：除缺失、JSON null 和空字符串外，任何已声明的 K 必须是
非空字符串，且等于领域函数从 `execution_input` 推导的完整身份。共享 schema owner
校验维护时的历史数据、两类 upsert 及订单关联/时间修正的 JSON 替换入口。非空 K 配
缺失或不完整输入、非字符串 K、身份矛盾均拒绝；不猜测补写或删除身份。upsert 继续拒绝
输入可推导身份但没有 K 的新写入。费用、现金换算等合法改写保留这对身份元数据。

旧记录可能只有别名、缺少物理账户，或已有完整输入但未存 K；这些记录继续进入 Python
核验。存在性结果保留 True/False/None：已应用、完整读取证明未应用、证据不足。
失败或不完整读取不能当成空集合并授予回执恢复资格。非 SQLite 或协议不完整的仓库保留
原兼容读取；没有增加通用 Repository 接口。

默认关联判定仅采信实际推导身份等于当前成交的候选，包括输入完整但没有 K 的旧记录。
其它旧候选仅用于存在性核验；不能作为显式 `applied_events` 传入并扩大关联采信范围。

### 索引维护与运行模式

新空表在初始化时建立上述索引。有数据的旧表不在普通启动时全量扫描建索引；索引不存在
或定义不匹配时继续安全全读。索引缺失不会使成交被拒收或丢弃。

共享 `build_position_projection_indexes` 在同一 writer 锁和事务中解码、核验所有适用
表，全部通过后才创建索引。无传入连接时使用 `BEGIN IMMEDIATE` 并负责提交/回滚；
传入连接必须已有事务，由调用方提交/回滚。新空表可用空表证明，不必做历史解码。
历史声明身份矛盾使维护失败并回滚，继续使用原全读；修复历史身份是独立操作。

旧库复用 `position_projection_migration.py` 的完整受控维护。清单、事务内重查和验证
绑定指派股票表的存在性和事件内容指纹；清单后股票事实变化会使证据失效。股票表存在时
才要求和构建其索引，缺少可选表不会阻止旧库维护。

维护会强制重建投影、生成 checkpoint，并将 checkpoint mode 设为 `disabled`。
重建前在同一事务中使旧 checkpoint 失效，避免旧 schema 的 checkpoint 继续被当作
可信证据。索引、投影或提交前任一步失败均整体回滚。维护前记录原模式，成功后读回索引、
投影及模式；原来为 `enabled` 的库也须重新完成 shadow/acceptance 验收及独立 activate
门槛。索引安装成功与原运行模式恢复是两个结果，不能自动激活或省略验收。

维护保留原入口的确认边界，不新增 CLI。部署、生产维护及业务数据写入仍需独立授权。
总维护成本与其中索引阶段成本分别记录，不能把完整维护误解为只加两个索引。

### FX 独立读取

`PerformanceEvidenceSQLiteRepository.read_fx_rates` 复用 `EvidenceReadBundle` 和
原 schema gate；私有 `_read_fx_rates_conn` 复用 `_FX_SELECT`、`_rate_from_row` 和
FX 校验。`freeze_cash_fx_daily_rates` 的两次读取、`load_cash_fx_payload(persist=False)`
的只读预览使用此路径，不读取或解析估值记录。

读取保留全部 FX 事实及修订祖先，不缩成当天最新一行。北京时间同日固定价、晚到费用的
原经济日、并发首次固定、读回和证据校验保持原规则。固定使用调用方已有事务；无外部
连接时仍由原 `BEGIN IMMEDIATE` 路径提交或回滚。预览不迁移 schema、不固定日价、不写缓存。

公共只读方法与 `read_all` 一样，将数据库读取或值校验异常表达为不可用 bundle；私有
事务读取不吞存储异常。`load_cash_fx_payload` 仍仅将 ValueError 转为空 FX／人民币
pending，保留原币事实；SQLite 缺列、锁或读写错误继续传播，由原事务 owner 回滚。
传入连接时不另开连接，不单独提交或回滚外层事务。

坏估值不阻塞上述 FX 路径。需要两类证据的 `read_all`、import 和估值消费者继续完整校验。
历史现金换算补证/更正中的 `cash_conversion_migration.py` 及其它消费者的独立
`read_all` 预读取保留原行为，不能声称所有现金换算入口都已移除估值依赖。

### 验证边界与剩余成本

Inbox 的公共接收、重放、账户/环境/命名空间隔离、多 lot、void、关联冲突和回执恢复，
由既有 Inbox、identity、recovery、stock、publication 和 migration 测试覆盖。
旧候选与原全读逐项对照，直接 builder 和完整维护覆盖身份失败、索引后失败、事务回滚、
原 enabled 库维护及独立恢复激活。FX 测试覆盖同日并发、跨日、晚到费用、修订链、只读
无写、坏估值隔离、pending 与存储失败的区别，以及费用和股票消费者的外层事务。

离线性能验收使用 100、1,000、5,000 条成交历史，以及固定两条 FX 配
0、1,000、5,000 条估值。预热一次、计时五次；计时与追踪分开。Inbox 用 25 层、FX
用 1 层 tracemalloc，仅比较参数相同的样本，并核对业务结果、查询计划和候选解码数。
准备和维护不计入稳态耗时；补充 legacy 混合及股票历史样本。

完整身份且索引就绪时，Inbox 成本主要取决于当前成交和旧候选数量；旧身份记录多时仍有
O(legacy) 成本。FX 路径不再随估值数量增长，但仍读取全部 FX 历史。实际生产分布和锁
竞争不由合成样本证明，分别由 Ledger 和 FX owner 按运行证据评估。

费用候选筛选、完整持仓投影、Combo/Wheel 扫描、CSP/CC 收益分类、PM 同步合同及来源
适配不在这两项优化范围内。不要据此推断完整入账已经变为常数成本。

## 单次投影刷新与 SQLite 写连接生命周期锁

### 当前合同

- 每个非 dry-run 的到期维护账户只重建一次 `trade_events -> position_lots` 投影，并在这次刷新后确定本轮 account/broker/market lot 成员集。
- 同一 ledger SQLite 文件的 repository writer 和公开 migration writer，从打开、WAL 配置、事务到关闭和 SQLite/WAL/SHM 安全属性加固，全部位于同一 db-path writer lock 内。
- 保留 auto-close 的当前 fail-closed 语义、fresh-lot 校验、对外结果字段和回执行为。
- 纯读查询不纳入 writer lock；不用 retry、sleep 或扩大 `busy_timeout` 代替锁边界。

### 运行合同

Auto-close 数据流：

```text
positions maintenance
  -> 复用同次 repository open 的 startup recovery 回执，否则重建一次 trade_events -> position_lots
  -> 读取并过滤本轮 account/broker/market open lots
  -> 刷新行情/交割证据
  -> ledger.auto_close_expired_positions(projection_refresh=<typed result>)
       -> 不再重复刷新
       -> 重读本轮已选 record IDs 的 fresh lots 并合并上游证据
       -> decision -> preflight -> optional expire-close write -> readback
  -> 将同一次 projection refresh 结果暴露为既有 projection_refresh 字段
```

本轮成员集在唯一刷新后冻结。刷新恢复的 lot 会参与本轮；成员集冻结后新增的 lot 留给下一次定时维护，不扩大本轮 account/broker/market 范围。已选 lot 在写前仍重读当前字段并执行原有 preflight。fresh-read 必须用 close-candidate identity 复核 account、broker、symbol、option type、side、strike、expiration 和 currency；身份改变时不得合并先前行情，并以 `position_lot_identity_changed` fail closed。

`load_option_positions_repo()` 在 startup recovery 时保存同库的 typed `ProjectionRefreshResult`；positions orchestrator 单次消费该回执，避免恢复后立即重复 full rebuild。没有 recovery 回执时，positions orchestrator 自行刷新并传入同库本轮结果。`auto_close_expired_positions()` 收到该输入时不再刷新；其他调用方未传入时继续默认自行刷新，保留公共 ledger 安全语义。typed 结果只作为 ledger 输入；positions 层继续通过既有顶层 `projection_refresh` 字段输出。`ExpiredCloseRunResult` 仍只携带 decisions、applied 和 errors，不增加字段、不改变 `to_payload()` key set。无 trade event 或 dry-run 时不伪造刷新结果；刷新失败时保留现有 `projection refresh failed before auto-close` 错误和整次零写入行为。

SQLite writer 状态转换：

```text
acquire <db>.writer.lock
  -> connect + connection invariants + busy_timeout
  -> PRAGMA journal_mode=WAL + synchronous=NORMAL
  -> optional BEGIN IMMEDIATE
  -> write body
  -> commit | rollback
  -> conn.close()
  -> validate and harden SQLite/WAL/SHM artifact permissions
release <db>.writer.lock
```

`_writer_connection()` 在调用现有 `_connect()` 前取得外层 writer lock。`connect_private_sqlite()` 继续作为共享连接工厂；如果它在 `sqlite3.connect()` 成功后执行的 artifact 加固失败，工厂在返回前关闭已创建连接并重新抛出该错误。repository 和 migration writer 都在外层锁内调用这个工厂；其他已有调用方只获得相同的失败清理，不新增 writer lock。

工厂成功返回后，`_connect()` 保持重入取锁和 PRAGMA 顺序，并校验 `journal_mode=WAL` 的实际返回值为 `wal`。如果 connection invariant、PRAGMA、WAL 返回值校验或后续初始化 artifact 加固任一步失败，`_connect()` 在外层锁内尝试关闭连接、完成可行的 artifact 加固，并重新抛出初始化错误。这个外层锁一直持有到正常路径或失败路径的 `close()` 和 `secure_sqlite_artifacts()` 完成；连接不能因任一 helper 尚未成功返回而逃出清理边界。

position-projection migration 的 `_write_connection()` 持有同一 `<db>.writer.lock`。它只负责 db-path lock、连接打开与初始化、WAL 实际返回值校验、连接关闭和 artifact 加固；apply/activate/deactivate 调用方继续负责 `BEGIN`、commit、rollback 和 `write_applied` 时序，不复用 repository 的事务 owner。current-decision migration 复用该 context manager 并遵守同一边界。读只诊断连接不受这个 writer lock 合同限制。`secure_sqlite_artifacts()` 只验证普通文件并收紧权限，不 checkpoint、不删除 WAL/SHM；活跃 reader 存在时 sidecar 可以继续存在。

失败语义：

- shared factory 在连接创建后的首次 artifact 加固失败：工厂在返回前尝试 close，并重新抛出该加固错误；writer 调用时整个分支仍位于 db-path lock 内。
- factory 返回后的 invariant、PRAGMA、WAL 校验或后续初始化 artifact 加固失败：不开始业务事务；`_connect()` 仍在 writer lock 内尝试 close 和可行的 artifact 加固，重新抛出初始化错误后释放锁。
- 写入体或 commit 失败：在同一锁内 rollback、close 并加固 artifact 权限，向上抛出错误。
- close 或 artifact 安全属性加固失败：锁仍由 context manager 释放，错误不得被改写为成功。
- 不新增多异常优先级状态机；保持当前 Python context-manager 的异常传播。任何 auto-close 失败都不自动 retry；操作员通过现有定时重试和读回回执区分“未写入”与“已应用”。
- auto-close 投影刷新失败时整次零写入；进入逐 lot 写入循环后，后续 lot 失败不回滚先前已成功的 durable close，响应同时保留 `applied` 和 `errors`。

## 写入语义

账本动作按业务事实区分，不能互相替代：

- open
- buy-close
- expire-close
- assignment
- exercise
- assigned-stock sale
- adjustment
- void / repair

每次写入都必须满足：

1. account、broker、symbol、option type、side 和 contract identity 明确；
2. close / assignment / exercise 解析到确定 lot；
3. 数量不会超过当前可用 lot；
4. 幂等身份足以防止 broker deal 或人工请求重复落账；
5. 写前 preview 与写后 projection 使用同一事实；
6. 身份冲突、projection drift 或关键证据缺失时 fail closed。

手工入口默认先 dry-run。例如：

```bash
./om option-positions add \
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

`add`、`assign`、`exercise` 的 preview、apply 和响应丢失后的重试必须复用同一个
`--request-id`。相同 request ID 与相同 intent 返回原结果；同一 ID 绑定不同 intent
会 fail closed。确认前检查响应中的目标 SQLite、account、lot/event identity、数量和写入合同。

## 批量 adjustment 投影成本

### 目标、边界与成功信号

`record_manual_position_adjustments()` 在一次原子批量写入前复用同一份 current projection，
并把全部候选 adjustment 作为一个集合只预览一次，不按 adjustment 数量重复读取和投影完整
`trade_events` 历史。

成功信号：

- 两项及更多 adjustment 的预检固定为一次 current preview 和一次 combined candidate preview；
  全历史读取次数不随 batch cardinality 线性增长；
- trusted checkpoint 下完整入口至多两次 full-prefix read：一次 combined candidate fallback 和一次
  事务内最终 projection；checkpoint disabled 或 untrusted 时至多三次：current preview、combined candidate
  preview 和事务内最终 projection 各一次；两种模式都不随 batch cardinality 增长；
- preflight 返回字段、patch、event time 和写入结果保持兼容，forced-full 与优化路径的最终 lot
  fingerprint 完全一致；
- 任一 target、current fields、contract identity、patch 或 combined projection 无效时，整批零写入；
  事务内仍重读当前 lot，并要求重读字段与 preflight advisory 字段逐字典相等，再完成最终 projection、
  current-decision finalize 和 commit；
- 完整 projection 缺少可恢复 state 时继续 fail closed，不发布 read model、不提交事务，也不新建
  checkpoint；warning-only/no-state 行为只增加 characterization regression，不修改 runtime 源码；
- Phase 3A 的 special Combo fixture 使用晚于既有历史且早于 expiration 的开仓时间，完整场景不再因
  fixture 自身的 `economic_adjust_invalid` warning 中断。

该实现没有让 adjustment 支持 tail projection，也没有达到 Phase 3A 冻结的 `500 ms`、`64 MiB` 和零
full-prefix-read 门槛。本地 `1` warmup / `3` repetitions non-acceptance smoke 在 10,000 events / 100
open lots 的两项 batch 上，fast 路径 wall/CPU P95 为 `0.880 s` / `0.863 s`、两次 full-prefix read，
forced-full 路径为 `1.158 s` / `1.137 s`、三次 full-prefix read；最终 lot fingerprint 完全一致，fast
路径 Python peak allocation 为 `79,110,743` bytes。该 smoke 只证明当前主机上的结构性收益，不是
跨主机 acceptance 结论。

当前仓库中该 batch facade 的唯一调用者是 Phase 3A benchmark；本分片只改善 admission/benchmark
路径，不宣称生产 tick、通知或交易入口会因此加速。若未来接入生产 caller，必须先解决下述响应丢失
后的批量重试风险。

不修改 schema、公开 facade、CLI、config、正常 read path 或单笔 close path，不发布、部署或写入生产
账本和运行环境。不处理通用事件 JSON 解码/复制成本，也不增加 cache、后台任务或新依赖。

### 当前事实与选定方案

原 batch command 对每个输入分别调用 `_preflight_lot_adjust()`。该函数先读取 trusted current
projection，再对单个 `adjust` 候选调用 `preview_position_projection_append()`；领域 projector 把
`adjust` 视为 control event，candidate tail 因此回退 full replay。事务内
`persist_manual_adjust_events()` 又必须对最终事件集合重算一次投影。批量项越多，写前 full replay
越多；单笔 close 和 current projection read 不经过这条重复链路，保持不动。

当前实现保留 singular preflight 和事务 writer 的职责，只抽出一段共享的
“基于已读取 current preview 构造一个 adjustment preflight result 与候选 event”逻辑：

1. singular `_preflight_lot_adjust()` 读取一次 current preview，构造一个候选并预览一次；外部行为不变；
2. 新的 batch preflight 读取一次 current preview，按既有顺序验证每个唯一 `record_id`、current
   fields、open 状态和 contract identity，并构造各自 patch、event time 与候选 event；
3. batch preflight 将全部候选 events 交给一次 `_preview_append_projection()`，沿用既有 error/ineligible
   阻断规则；warning 保留在 preview 结果中，但不放宽事务内最终完整 projection 的发布条件；
4. command 把每项 preflight 的 advisory current fields 和 event time 交给现有
   `persist_manual_adjust_events()`；后者在同一 SQLite transaction 内重读目标，并在构造任何正式 event
   前要求 `current_fields == fields`；任一字段漂移都 rollback，随后才检查 group collision、构造正式
   events、发布最终 projection 和 current-decision，并一次 commit 或 rollback；
5. 不把 advisory preview 对象或投影状态带入写事务，不降低 transaction-time revalidation。

`ensure_projection_publishable()` 的 error/ineligible 规则只负责 preview 资格；事务内 `_run_full_path()`
仍要求完整 resumable state。warning 导致 state 缺失时继续抛错并由 transaction rollback，不发布
position lots，也不推进 trusted head。Phase 3A 当前中断来自 synthetic special Combo 开仓时间晚于
expiration；只把该 fixture 的时间改为 `1_850_000_000_500` ms，使其晚于基线历史且早于
`2028-12-15`，并用 regression 证明 warning-only/no-state 的 fail-closed 行为没有漂移。

拒绝的替代方案：

- 允许所有 `adjust` 直接 tail-resume：会扩大 domain control-event 契约，经济字段、合约身份和策略
  metadata 的安全边界需要另行设计；没有当前收益证据时不做；
- 修改 generic projector 为 streaming 或重写事件 codec：改动面远大于已定位的 batch 重复调用；
- 为 warning 新增 allowlist、持久化 head diagnostics 或改变全局发布语义：当前 fixture 修正后没有必要；
- 新增 repository latest-event query 以省掉 disabled/untrusted 下的 current full read：只少一次固定读取，
  却扩大 persistence API，当前 benchmark-only 收益不成立；
- 新增 batch projector、cache 或 checkpoint 类型：现有 preview 与 transaction runtime 足够表达本分片。

### Owner、数据流与失败语义

受影响 owner：

- `src/application/ledger/commands.py`：batch 输入归一化、调用 batch preflight、组装兼容结果；
- `src/application/ledger/preflight.py`：共享 adjustment 构造校验和一次 combined preview；
- `src/application/ledger/manual_trades.py`：事务内 advisory/current exact-equality fence；
- `scripts/benchmark_data_storage_projection.py`：只修正 synthetic special Combo event time；
- `tests/test_position_projection_runtime.py`、`tests/test_research_performance_baseline.py`：行为、计数、
  原子性、runtime characterization 和 fixture 回归。

```text
record_manual_position_adjustments(adjustments)
  -> normalize batch + reject missing/duplicate record_id
  -> current preview once
  -> per target: current fields + identity + patch + candidate event
  -> combined candidate preview once
  -> persist_manual_adjust_events(existing transaction owner)
       -> BEGIN IMMEDIATE + reread all targets
       -> exact advisory/current fields fence
       -> group collision checks + rebuild official events
       -> final projection + current-decision finalize
       -> commit | rollback
```

preflight 仍是 advisory；正式事务内的 `current_fields == fields` 是 batch 专属并发 fence，不新增 hash、
版本列或通用比较 helper。combined preview 比逐项 preview 多检查 batch 内相互作用，只可能把失败提前到
写事务前，不允许原先被拒绝的写入。重复 target、缺失 target、closed lot、identity mismatch、invalid
patch、projection error、任一字段漂移和最终 projection/finalize 失败都保持整批零写入。

批量写入在提交成功但响应丢失后重试，当前可能因相同 event ID 携带不同 `event_time` 而冲突；本分片不
扩张为幂等协议改造。owner 为 `src/application/ledger/manual_trades.py`，影响是该 facade 暂不适合新增生产
caller；在任何生产接入前，必须复用单笔 adjust 的 existing-event 语义并补响应丢失回归。

### 实现与验证

1. 在 `preflight.py` 复用现有校验构造共享 helper，增加 batch preflight；在 `commands.py` 仅替换
   per-item preflight loop；在 `manual_trades.py` 增加 exact-equality fence。参数化验证一、二、三项 batch
   在 trusted 模式分别保持至多两次 full-prefix read，在 disabled/untrusted 模式分别保持至多三次；
   同时覆盖相同 `as_of_ms` 下的结果顺序，以及 trusted/disabled 模式的最终 fingerprint parity。
2. 增加 duplicate target、单个 invalid item、transaction-time 任一字段漂移和 final projection/finalize
   late failure 的 public batch facade 回归，全部断言 event、lot 和 current-decision 零写入；保留一条
   warning-only/no-state forced-full 继续 fail closed 的 runtime characterization。只修正 special Combo
   fixture 时间，并验证完整 fixture 零 diagnostics、可完成。
3. 运行 focused tests、Ruff、dependency-graph check、guardrails、`git diff --check` 和完整 pytest；
   再运行 Phase 3A `1` warmup / `3` repetitions smoke，比较 wall/CPU、allocation、full-prefix reads、
   parity 和原子性。该 smoke 必须标记为 non-acceptance，不替代指定 reference host 上的正式 `5/30`。

验收首先看结构性计数和正确性：full-prefix reads 对 batch cardinality 有界、fingerprint parity、任一
失败零写入、warning/no-state 继续 fail closed。优化后绝对耗时和 peak allocation 如仍超过冻结门槛，
只记录实测差距，不把结构性计数达标表述成 Phase 3A acceptance；只有出现真实生产调用压力或正式
admission 需求，才重新评估 metadata-only tail support。

开放项：无。批量响应丢失重试作为明确 deferred risk，在生产接入前解决。commit、push、merge、
release、deploy 和生产写入继续是独立授权边界。

## Futu 订单身份补录

### 目标、边界与成功信号

历史手工 Futu 期权 open 事件可能已保留完整成交经济事实，但缺少 OpenD 费用查询所需的
`raw_payload.futu_account_id` 和 `raw_payload.order_id`。本变更让操作员在人工核实 OpenD
历史订单后，用现有 `trade-events repair` 原地绑定这两个身份，然后仍由现有
`fees-sync` 查询并持久化 actual fee。

成功必须同时满足：

- dry-run 展示唯一目标、绑定前后身份和 `expected_before_sha256`，不写 SQLite；
- apply 后仍是同一 `event_id` 和 `ingest_seq`，事件数量、合约、金额、数量、时间、
  cash-conversion fact IDs、lot identity 和下游 close/adjust lineage 不变；
- 原地更新造成的全局 position source generation 变化在同事务中发布，不留下新的
  position 或 current-decision dirty state；
- 带有有效下游 close/adjust 的 open 事件可补身份；经济或 lot-target override 仍走原有
  void/replacement 路径及 downstream dependency 阻断；
- 同一绑定重试为零 DML no-op；部分身份、冲突身份、目标已 void 或 CAS 冲突时零写入并失败；
- 绑定后的同范围 `fees-sync` dry-run 选中该订单，actual fee 写入后再次 dry-run 收敛为 no-op。

本变更不支持 close、expire-close、assignment、exercise 或 assigned-stock sale；不自动匹配 OpenD
历史订单，不进行批量映射、部分身份补齐、经济事实更改、下游链重写、
schema/config 变更或生产回填。账本与 OpenD 时间不一致时必须走下述独立时间修正路径；
近似时间、合约、数量或价格不会被代码提升成持久订单身份。

### 当前事实与选定方案

当前 `repair` 已接受 `--futu-account-id` 和 `--order-id`，但总是 void 原事件并追加
replacement。这会被有效下游 close/adjust 正确拒绝，而强行 replacement 又会改变 event
identity 并破坏指向原 event ID 的证据。另一个已验证的约束是：任何 `event_json` 更新都会推进
全局 position source generation，所以只重建 position lots 不足以保持 current-decision 可信。

选定的最小方案保留现有 CLI 和 application facade，在 ledger command 入口优先识别
identity-only override，不进入 append-repair preflight：

1. 对 `futu_account_id` 和 `order_id` 按原值校验且不静默 trim；前者必须是无前导零的正整数字符串，
   后者必须非空且不含空白或控制字符。两字段必须同时显式提供。
2. 有效 override key set 精确等于这两个身份字段时才走 identity-only；与任何经济、合约、
   时间或 lot-target override 混用时继续原 void/replacement 语义。
3. 目标必须是 active canonical Futu `open` 期权事件；首次绑定时 fee basis 必须尚非 actual。
   相同 `(futu_account_id, order_id)` 被其他 active event 使用时首版直接拒绝，不实现多事件订单分配。
4. 目标两个身份都缺失时允许绑定；两者都与输入相同时即使 fee 后来已为 actual 也返回 no-op；部分存在或
   任一值冲突时 fail closed。目标已有相同身份但没有本功能 provenance 时仍是 no-op，不为补审计数据改写历史。
5. identity-only 要求显式、非默认 `reason`引用人工 OpenD 核对证据。绑定自身不连接 provider；
   REAL 账户范围、终态、币种、成交数量和 actual fee 仍由后续 `fees-sync` 验证。

不新增 `bind-order-identity` 命令，因为它会重复现有参数、写入门禁和 facade。不让
`fees-sync` 自动匹配缺身份事件，也不增加通用 event mutator 或新审计表。

### 数据流、状态与失败语义

```text
om trade-events repair --futu-account-id ... --order-id ... --reason ...
  -> trades.review / ledger.api（现有签名不变）
  -> ledger.commands 优先选择 identity-only 或原 append-repair
  -> dry-run: 构造 advisory preview，零写入
  -> apply: db-path writer lock -> BEGIN IMMEDIATE
            -> 重读原始 JSON 及有效 void 集，重做全部验证
            -> capture global current-decision fence
            -> old-JSON CAS + exact readback
            -> forced-full position projection，要求 lot diff 为零
            -> finalize current-decision projection -> commit
  -> 现有 fees-sync 独立 dry-run/apply
```

apply 不使用 dry-run 结果作为事实；preview 明确标记为 advisory。事务内验证从原始存储 JSON 做
copy-on-write，只允许以下路径变化：

```text
raw_payload.futu_account_id
raw_payload.order_id
raw_payload.order_identity_provenance
```

`order_identity_provenance` 不得覆盖已有同名数据，它记录 schema version、以
`event_id + normalized futu_account_id + order_id` 确定性生成的 binding ID、来源
`manual_trade_event_repair`、reason、绑定前两个空身份、`expected_before_sha256` 和首次
`bound_at_ms`。apply 回执另返回实际 `after_sha256`；避免把时间字段包含在 dry-run 的预期 after hash 中。

原始 SQL 由 trade-event repository 内一个窄 CAS 方法持有：
`WHERE event_id = ? AND event_json = ?` 必须更新且只更新一行。intervention 持有策略、原始 JSON
深度 diff、事务编排和读回校验；canonical codec 只用于验证结果，不用来重序列化无关的历史字段。
CAS、读回、lot 零差异、current-decision finalize、foreign-key 检查或 commit 任一失败都回滚；不 retry。

响应状态为 `dry_run`、`applied` 或 `no_op`。`no_op` 退出码为 0、`write_applied=false`、不推进
source generation；验证失败或冲突退出码为 2。普通 void/replacement repair 的现有 JSON 响应保持不变，
identity-only 的文本回执、help 和 rollback hint 必须明确不会生成 void/replacement event。

### Owner 与实现分片

- `src/interfaces/cli/trade_events.py`：保留现有参数和高风险写入门禁，修正 help、文本回执和真实
  `write_applied` / rollback hint。
- `src/application/trades/review.py` 和 `src/application/ledger/api.py`：保留现有公开签名与分层，不新增 facade。
- `src/application/ledger/commands.py`：优先分流 identity-only 与原 append-repair，组装稳定响应。
- `src/application/ledger/interventions.py`：持有目标状态机、allowlist diff、current-decision fence 和事务编排。
- `src/application/ledger/repository_trade_events.py`：只新增目标化的 raw event JSON CAS，不提供通用 mutator。
- `src/application/trades/order_fee_sync.py` 继续持有 provider 验证；
  `src/application/ledger/order_fee_migration.py` 以原始 JSON 做 CAS 与 fee-only copy-on-write，并维护全局
  trade-event current-decision fence。

代码、CLI 回执、[Option Positions Repair](OPTION_POSITIONS_REPAIR.md) 和测试作为一个最小纵向分片交付，
避免 ledger 已分流但 CLI 仍报 `void=None repair=None` 的中间状态。

### 验证计划、风险与开放项

最小回归覆盖：

- 有 downstream close 的 open 事件：dry-run 零写入；apply 后只有三条 allowlist JSON 路径变化，
  event count/ID/ingest sequence、完整 lot fingerprint、cash-conversion IDs 和 downstream lineage 不变；
- 两个已初始化账户的 current-decision 在绑定后仍 clean，业务 payload 不变；强制 projection/finalize
  失败时原 JSON、source generation 和读模型整体回滚；
- 同一绑定再执行为零 DML no-op；单字段、空白、部分或冲突身份、重复 active identity、
  已 void 目标、非 open、已有 actual fee 和 CAS 冲突都失败且零写入；
- 身份参数与 strike 等任一经济 override 混用时，原有 void/replacement 行为和 downstream 阻断不变；
- 原始 JSON 含 legacy top-level `fee_provenance` 或未知 raw keys 时不会被 codec 顺带规范化；
- fake OpenD provider 验证绑定后 `fees-sync` 选中订单并写 actual fee，保留无关历史 JSON，且同范围
  第二次 dry-run 与相同身份重试均为 no-op。

实现验证运行 focused pytest、受影响文件 Ruff、`git diff --check`、文档/敏感产物 guardrails 和
`./.venv/bin/python scripts/generate_dependency_graph.py --check`。

残余风险：

- identity-only 不加载 runtime config，所以不在绑定阶段证明 Futu account ID 属于账本 account；
  owner 仍是 `order_fee_sync`，影响是错误但格式合法的身份可以被持久化。本分片用显式人工证据、
  高风险确认、重复 identity 阻断和后续 `fees-sync` 缩小风险；如要由系统证明合约/方向/价格，
  应作为 provider admission 的独立 hardening，不塞入本 repair。
- 首版不允许覆盖或解绑，错误绑定不能用同一命令修正。owner 是后续 ledger repair 设计，影响是
  提交后只能使用写前备份或另行授权的前向修复；本命令不会自动创建备份。生产使用必须逐笔 preview、
  另行创建并验证备份、写后读回和
  `fees-sync` dry-run，任一不一致立即停止。
- 首版拒绝多事件共用订单身份；如未来出现经证明的合法 split-fill/order 组，owner 是
  `order_fee_sync` 和 ledger identity contract，需单独设计分配与纠错，不放宽本分片。

开放项：无。当前能力不需要新 schema、public command 或 provider capability。发布/升级和
生产回填继续是独立授权边界。

## OpenD 历史开仓时间修正

`trade-events repair --trade-time-ms` 的 time-only 请求是第二个受控原位例外。它只接受未 void 的
canonical Futu open，要求事件已保存 `opend_order_evidence.v1`，订单数量合计等于事件 contracts，
且目标时间精确等于证据中最早订单的成交时间。命令不连接 provider，也不接受近似匹配或与其他
override 混用。

apply 在同一个 `BEGIN IMMEDIATE` 事务内以旧 JSON 和旧 SQL 时间做 CAS，保留 `event_id`、
`ingest_seq`、lot identity 与 downstream lineage，只同步修改 SQL/JSON 时间并写入
`opend_trade_time_correction.v1` provenance。SQLite immutable trigger 只在上述 provenance、已存
OpenD 证据和最早成交时间同时吻合时放行；其他交易时间更新仍被拒绝。随后强制全量投影，并要求
只有目标 lot 的 `opened_at` 改变，否则整体回滚。

旧事件时间形成的 `cash_conversions` 会在同一 CAS 中移除并记录失效的 fact kind，避免错误时点的
CNY 金额继续通过读取校验。批次时间修正后必须独立 dry-run/apply 现有
`option-performance cash-conversion backfill`；旧 backfill audit 保留，新换算写入独立时间修正 audit，
再验证月度报告。备份、生产写入和运行环境升级仍是独立授权边界。

## 读取语义

运行时风险、Close Advice、Performance 和 Agent tools 从 canonical read model 读取：

- 单 lot 查询保留真实开仓、费用、策略快照和生命周期字段；
- 聚合持仓只用于展示或风险计算；
- 历史 `as_of` 查询不能用当前报价回填历史缺口；
- 当前时点报价刷新失败时返回明确 quote status；
- 缺少费用、汇率、行情或 lifecycle evidence 时保留 partial / missing，不把未知值写成零。

常用只读入口：

```bash
./om option-positions list --account lx --status open
./om option-positions inspect --record-id <lot-id>
./om trade-events list --account lx
./om trade-events fees-sync \
  --config-key us --account lx \
  --start-date 2026-08-01 --end-date 2026-08-23

./om-agent run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
```

具体子命令以 `./om option-positions --help`、`./om trade-events --help` 和 `./om-agent spec` 为准。

### 稳定事件分页

Agent 通过 `option_positions_read action=events` 分页读取 canonical `trade_events`。SQLite 为每个
事件分配单调且不复用的 `ingest_seq`；首次查询记录最大序号，后续页使用
`trade_time_ms DESC, event_id DESC` 的 keyset cursor，并始终限制在该序号边界内。因此新增事件
不会插入正在进行的结果流，分页条数可以在 1–20 之间变化，也不会导致已返回成员重复。

这个 snapshot 冻结的是成员集合、筛选字段和排序字段，不是整行 JSON 的历史版本。事件成员
不可删除，`ingest_seq`、事件身份、账户、市场、position effect 和合约筛选字段不可修改；交易
时间只允许上述 OpenD 证据约束的原位修正，其他时间更新仍不可变。价格等不参与查询的补充字段
仍可按现有账本语义更新。完整 TradeEvent 的编码与验证继续
由 Python canonical codec 负责，SQLite 不实现第二套领域 JSON 校验器。

旧库只声明新增列，不在普通启动时扫描回填。必须通过受控 position-projection migration 分批
填充分页投影并发布索引与约束；完成前 `action=events` 明确返回 pagination unavailable。

## 到期与交割生命周期

到期短仓不能仅凭“过了 expiry”自动写成 worthless：

- 价外自动关闭需要符合市场时区和报价证据；
- 价内、平值或缺少 spot 时进入 review；
- option leg 与 stock settlement leg 可以异步到达；
- assignment / exercise 必须有匹配的交割事实；
- `external_holdings` 账户缺少 broker lifecycle evidence 时默认要求人工复核。

到期维护由独立 `auto-close-expired` 服务/定时入口负责，不是普通 `account_run` 或扫描 pipeline 的隐式步骤。

## Projection 验证与恢复

`verify-projection` 默认是纯只读诊断：它可以读取已有 checkpoint 加速比较，但不会创建目录、
覆盖 latest report 或发布新 checkpoint。只有明确需要留下运维证据时才使用
`--publish-evidence`：

```bash
./om option-positions verify-projection --mode auto
./om option-positions verify-projection --mode auto --publish-evidence
```

生产定时验证显式使用 `--publish-evidence`；临时排查保持默认只读。

发现 read model、report 或 lot 状态异常时，按顺序处理：

1. 用只读 inspect/history/verify 确认 active runtime root 和 SQLite；
2. 检查 trade event 是否完整、重复或存在目标歧义；
3. dry-run projection rebuild；
4. 只有在差异可解释且目标准确时才 apply；
5. 用相同 runtime root 复查 lot、event history、Close Advice 和 Performance。

不得用以下方式“修好显示”：

- 直接更新 `position_lots`；
- 重新接回 Feishu / v2 兼容状态；
- 用聚合 `position_key` 猜测 close lot；
- 为缺失历史事实填入当前价格、当前汇率或零费用。

完整修复步骤见 [Option Positions Repair](OPTION_POSITIONS_REPAIR.md)。

## 下游合同

- [Close Advice Contract](CLOSE_ADVICE_CONTRACT.md)：如何消费 lot、行情和策略快照。
- [Option Performance Design](OPTION_PERFORMANCE_DESIGN.md)：利润、现金、activity 和组合桥接。
- [Assigned Stock Return Design](ASSIGNED_STOCK_RETURN_DESIGN.md)：assignment 后的正股成本与收益。
- [Architecture](ARCHITECTURE.md)：ledger 与 interfaces/application/domain/infrastructure 的整体边界。
