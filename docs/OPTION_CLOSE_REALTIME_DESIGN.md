# 期权实时开平仓与迟到原因设计

状态：设计已确认；Impl A/B 已实现并通过隔离验证，部署状态以运行环境为准。既有合同见
[Futu 成交与期权生命周期](FUTU_TRADE_HOLDINGS_SYNC.md) 和
[期权收益](OPTION_PERFORMANCE_DESIGN.md)。

## 目标与边界

经券商成交证实的期权开仓、平仓，按成交数量和成交时间立即更新 canonical
`trade_events -> position_lots`。平仓原因（正常买卖平仓、到期失效、指派、行权）
可在结算证据到达后补齐。平仓原因未定不使已确认平仓重新显示为 open；收益率
是否可算由实际现金、费用和资本占用证据决定，胜率仍等待原因。

本设计只覆盖今后通过受控 intake 接受的成交。历史已接受但未写终结事件的
case（包括 2026-09-30 约 20:48 CST 只读观察到的 `lx/0700.HK/430P` 四张）
须另作只读盘点、预览和授权修复；
不自动迁移，不写生产账本，不发通知或交易。

## 当前责任归属

| 责任 | owner |
| --- | --- |
| 零价 close 入口与预览 | `src/application/trades/lifecycle.py::_resolve_zero_price_option_close` |
| 公开记账边界 | `src/application/ledger/api.py::record_zero_price_option_close` |
| 单事务即时经济平仓 | `src/application/ledger/writer_lifecycle_evidence.py::record_zero_price_option_close_atomically` |
| 待定原因与精确 replacement | `src/application/trades/lifecycle_reconciliation.py`、`src/application/ledger/writer_lifecycle_allocation.py` |
| 经济事实、资本时间与原因待定收益 | `domain/domain/ledger/economics.py`、`domain/domain/performance/weighted_reducer.py` |

新成交入口原子写入已确认经济平仓；旧 reservation facade 仍有自身兼容职责，不能从它的结果推断新成交未写 close。风险与仓位消费者按同一有效代次读取数量和待交收约束。

## 当前合同

### 1. 成交确认即记经济开平仓

普通非零价开平仓保留现有路径。零价期权 close 只有在 canonical 物理账户、broker
deal key、合约方向、数量、成交时刻和精确 lot 分配均通过现有校验时，由
`ledger/api.py::record_zero_price_option_close`调用 **ledger 内单一事务 owner**：在一条
`BEGIN IMMEDIATE` 写事务中依次完成 case/evidence/source claim 的既有校验与持久化、
每个目标 lot 的 `close` event/allocation、forced-full 投影和同代决策快照发布；
任一步失败则整笔回滚，成功后读回有效投影。事务步骤仅在
传入 `conn` 下运行，由该 owner 调用；不得在事务内调用会再次开启事务的
两个 public writer，也不得先提交 claim 再写 event。direct reservation 旧路径的
public facade 保留既有行为，仅新成交入口走完整原子流程。event 使用券商期权成交
时间，不使用后到的股票交收时间；`price=0` 是券商成交价，
`raw_payload.close_type="cause_pending"` 明确表示原因未定。
allocation ID 复用 `allocation_id_for(case_id, evidence_id, lot_id)`，terminal event ID
复用 `terminal_event_id_for(case_id, evidence_id, lot_id, terminal_type,
contracts_allocated)`；此时 evidence ID 是原 broker close anchor。重复 push/backfill
在同一事务中按物理账户、broker deal key 和原始 payload 校验已有 source claim、
evidence、event/allocation 与投影：完整一致时只读回原成交并返回 `skipped`；
claim 已有但缺有效经济事件的历史半成品进入 `legacy_pending_requires_review`，
不得借重放暗中补账；payload、数量或身份不一致进入 conflict。
部分成交只关闭已证实的张数，后续不同 deal 再关闭剩余量。来源内容变化、数量
不足、lot 漂移或身份冲突直接停在 review，不制造事件或部分写入。

零价成交不等于零费用。保留现有 actual/estimated/missing 费用来源；只有券商明确
给出实际零费用才视为 actual zero。事件及 source claim 保留物理账户、order ID、
source deal ID、费用来源和原始币种/汇率转换证据，供现有按账户和订单同步费用的
路径更新有效事件。开仓成交同样按现有 intake 实时入账；本方案不改变开仓归属判断。

经济平仓写入成功后，intake/Inbox 的成交结果为 `applied`；原因另以
`cause_pending` 表示。相同 push/backfill 读回为 `skipped`，不得再次写 event、
receipt 或 outbox；若原因已更正，按原 claim 读回更正后的有效状态，不能把已 void
的原 pending event 当缺失。若提交结果不明，先按 broker identity 读回，不能盲重试。

### 2. 原因迟到时更正同一平仓事实

生命周期 case 和源成交唯一 claim 继续持有平仓事实。领域生命周期读模型须从
**有效**终结事件识别 `close_type=cause_pending`：这些张数已关闭，
`reason_state=cause_pending`，不能仅因 allocation 数量满额标为 `resolved`，
也不能在到期观察窗之前退回 `open`。直接读取与可信决策快照都须携带同一代次
的有效 pending event/lot/张数映射；从有效 event 与 allocation 派生并校验，
不靠已失真的 compact 数量猜原因。最小持久合同是在 lifecycle case decision fact
的 `resolution` 增加 `pending_close_contracts_by_lot`（可为空）；发布时从同事务
有效事件求和，校验每 lot 不超过已分配 `close` 张数，并纳入既有 fact hash、代次
与严格 schema 校验。由于当前 fact 校验要求精确 key 集，须提升该 fact schema，
旧版可信快照先从 canonical 事件重建，不能把缺字段解读为零。直接读模型与快照
读模型均用这张映射使 pending 张数优先于到期观察窗的 `open` 分支；映射缺失、
数量不符或代次不同则 `needs_review/conflict`，不得报告正常买卖平仓。
快照中的 reserved 数量只表示尚未进入 canonical close 的旧记录；新事件对应张数
的有效预留必须为零。读者同时遇到旧预留和新分配时按 lot/source 核对，不重复释放。

结算证据证明到期失效、指派或行权后，仅当**一份证据**能与**一个 broker close
anchor** 的全部有效 pending events/allocations 在物理账户、逐 lot 清单和总张数上
完整相等，才自动更正。沿现有 lifecycle allocation writer 在同一事务内校验 case 代次、
source claim、原 pending events/allocations、结算证据及必要股票证据。不能按当前
`remaining_contracts_by_lot` 分配：这些张数已经关闭，剩余量为零；应从待 void 的
有效 pending allocations 取得原逐 lot 张数。用现有 `correction_void_events` 能力，
void 该 anchor 的全部原 events，按相同逐 lot 张数写一份结算 evidence 对应的
确定类型 replacement events/allocations，forced-full 重放投影并读回。
replacement ID 由后到的结算 evidence ID 派生；原 broker source claim 仍只归
原 anchor，不再 claim 一次。
更正后有效平仓张数与更正前逐 lot 相等，事务内任何校验失败全部回滚。

仅有部分结算证据（例如原 4 张 pending 中证明 2 张指派），或一份证据跨多个
anchor、逐 lot 映射不清时，只记录既有结算观察/审查证据，标记 `needs_review`，
**不 void、不拆分、不改期权张数/现金/胜率**；原 4 张仍保持已平仓且原因待定。
后续若取得能完整对应单一 anchor 全部 lot 的权威证据，可重走上述自动更正。
多份部分证据自动聚合、2+2 原因拆分及费用分摊不在本轮承诺内，需独立设计和验收；不能把
部分证据当作整批原因，也不能因缺原因将仓位恢复为 open。

更正后的期权事件仍使用原券商期权平仓时间、零成交价、物理账户、订单及 deal
身份；股票交收时间只用于股票事件和证据。保留原已确认现金及汇率转换，并以
更正时**当前有效**费用事实填入 replacement，不能从原 event 复制已过时的
estimated/missing 费用；原期权事件已有的 cash conversion 逐字段带到 replacement
并校验，不按结算时新 FX 重算。尚为 pending 的换汇不是已冻结汇率，replacement
仍保持待换汇状态。费用同步与更正均在 ledger writer 事务中按物理
账户/order ID 核对有效事件；更正时重读最新 fee fact。后到 actual 费用先归属
当前有效 event；若费用在原因之后才到，`order_fee_sync` 也必须选择这个 broker
成交锚定的 assignment/exercise/expire event，不能被现有“未执行期权成交费用为零”
或“事件含股票交收”分支跳过。期权成交费用按原 option order 身份归属，股票交收
费用仍按 stock settlement 身份归属；两者身份不能安全区分时保留 missing/review，
不能合并或双收。未执行期权的零费规则仅适用于没有 broker close anchor 的真实
未执行终结。
缺 actual 时继续
报告 missing，不把原因分类产生的零费用当成券商实际零费。已有确定原因的重复
证据幂等，证据互相冲突进入 `needs_review/conflict`，不得重开仓、重复平仓或创建
股票批次。指派股票批次仍须
独立的结算证据。没有安全 replacement 时整个事务回滚，原待定平仓继续有效。

相同合约在旧 case 全平仓后再次开仓时，后续成交须按**当前有效 lot 集合**选择
或创建 case；不能因合约/方向相同复用冻结了旧 lot 的 case。新旧 case 按物理账户、
lot 集合及成交来源隔离，旧代次不可被新一轮成交修改。

### 3. 消费者的可观察语义

`weighted_reducer.py` 明确识别 `cause_pending`：该 allocation 为 `terminated`，
资本在券商期权成交时间结束，现金计入对应币种的 terminated/total；卖出期权胜率
不增加分母或分子，并报告 `close_reason_pending`。若开平仓现金和实际费用均完整，
HKD 月收益率与年化率可计算，即使胜率和总报告仍为 partial；缺费用时只有相关
收益率继续 partial/null，明确显示 `fee_missing`。原因补齐后，胜率按最终原因重算；
收益率的成交时间、现金与资本天数不因结算时间改变。

`context_builder.py` 以 canonical `contracts_open` 为仓位数量，不把已分配事件再从
数量中减一次；仍未入账的旧 reservation 只在可信同代快照下按现有规则影响风险。
待交收导致的现金/股份不可用量从有效 lifecycle case/evidence 派生，**即使 lot
已全平仓、`contracts_open=0` 也不能因 open-position 循环跳过**；该量与持仓数量
分别计算，不再从已关闭张数重复释放或扣减。Put/Call 的 case 阻断只能由与该
case、物理账户、币种/标的及交收时点匹配的可信结算证据解除；账户总现金或总持股
变化不能证明这笔 case 已释放。若消费者改用更新且可信的券商可用额/可用股数
直接作容量上限，必须作为独立的 authoritative 模式，不再叠加该 case 的推测释放，
并验证同账户其他交易不会导致双计。缺足够证据时保持不可用。仅补齐平仓原因不
证明资金或股份可用。下游 auto-close、Wheel、Daily Brief、Control 均以同一
有效投影看剩余张数；原因未定以现有生命周期状态暴露，不伪装成已确认指派或到期失效。

仅有完整券商持仓快照中某仓位消失、但没有精确平仓成交时，只能确认快照时点
已不在持仓，不能推造平仓时间、分配和收益率；保留 review/partial。

## 验证入口

- `tests/test_trades_resolver_close.py`、`tests/test_trades_lifecycle_runtime.py`：真实 resolver/生命周期 facade 的即时数量变化、原因 pending、重复及失败回滚。
- `tests/test_lifecycle_settlement_semantics.py`、`tests/test_trade_receipt_inbox_lifecycle.py`：结算、Inbox 与幂等恢复。
- `tests/test_positions_context_builder_partial_close.py` 及绩效消费者测试：canonical 剩余量、待交收约束和原因/费用缺口分别保留。

隔离验证覆盖账户/市场/物理身份、部分成交、重复/乱序、实际零费与缺费、同合约再开平仓、完整单 anchor 更正和部分证据拒绝。历史已经接受但缺经济事件的半成品不能借重放补账；须重新盘点、预览并走授权修复。

券商 push 不保证网络或进程故障期间零延迟，backfill 仍使用原成交时间。源码及本地测试不证明历史账本修复、生产配置或运行服务状态。
