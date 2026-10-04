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

## 现状代码依据

- `src/application/trades/resolver.py::resolve_trade_deal` 是成交入口。普通开平仓
  进入现有 `record_normalized_trade_event` 路径；零价期权平仓优先转到
  `src/application/trades/lifecycle.py::_resolve_zero_price_option_close`。
- `src/application/ledger/writer_lifecycle_evidence.py::accept_option_close_evidence_atomically`
  已在一个 SQLite 事务中校验物理账户、broker source、价格、数量和 lot，冻结
  `target_contracts_by_lot`。但目前只写 case/evidence/source claim，不写 close
  event；`lifecycle.py` 返回 `reserve_option_close` 且 `projection_changed=False`。
- `domain/domain/option_lifecycle.py` 将“预留但未分配”视为 `cause_pending`；
  `src/application/positions/context_builder.py` 暂从 open 数量减预留量计算风险。
  一旦 canonical lot 已平仓，此覆盖必须停止再次扣减，但待交收资金/股份提示仍按
  证据保留。
- `domain/domain/ledger/economics.py` 从 close event 生成数量、现金、费用和终结时间；
  `domain/domain/performance/weighted_reducer.py` 将普通 `close` 当作买/卖平仓判胜，
  到期仍 open 则以 `terminal_evidence_missing` 拒算资本天数。因此零价待定原因不能
  仅写普通 `close`，也不能把未知费用填零。
- `src/application/ledger/writer_lifecycle_allocation.py::apply_lifecycle_allocation_atomically`
  已支持 correction void 和新终结事件在同一事务内预检、投影、持久化；
  `domain/domain/ledger/projection.py` 对 void 进行全量重放。
- 上述 evidence 与 allocation 两个 public writer 各自调用
  `with_sqlite_repo_transaction`；顺序调用会产生两个提交，不能实现成交、claim 与
  终结事件的原子性。新路径须由 ledger 内一个事务 owner 执行。

## 拟议合同

### 1. 成交确认即记经济开平仓

普通非零价开平仓保留现有路径。零价期权 close 只有在 canonical 物理账户、broker
deal key、合约方向、数量、成交时刻和精确 lot 分配均通过现有校验时，由
`ledger/api.py` 的一个新零价平仓入口调用 **ledger 内单一事务 owner**：在一条
`BEGIN IMMEDIATE` 写事务中依次完成 case/evidence/source claim 的既有校验与持久化、
每个目标 lot 的 `close` event/allocation、forced-full 投影和同代决策快照发布；
任一步失败则整笔回滚，成功后读回有效投影。实现时从现有两个 writer 提取仅在
传入 `conn` 下运行的步骤，由该 owner 调用；不得在事务内调用会再次开启事务的
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

## 实现切片与验收

| 切片 | owner 与行为 | 必须从真实入口验证的结果 |
|---|---|---|
| A：即时经济事实 | `trades/lifecycle.py` 经 `ledger/api.py` 到 ledger 单一事务 owner 原子接受零价 close；投影、生命周期直接读模型、可信决策快照、performance、Inbox 和风险消费者同步采用待定原因语义 | 隔离 SQLite 中，`lx/0700.HK/430P` 四张经一笔已确认零价 deal 立即从 open=4 到 0，成交时间为资本终点；原因 pending、胜率 partial；费用 actual 时 HKD 收益率可算，缺费用时仍 partial。claim 后、event 后、projection 前注入失败均无持久半成品；全平仓后资金/股份阻断仍在；相同 deal 重放 `skipped` 且无第二 receipt。普通开/平仓及部分成交不回归。 |
| B：原因补齐 | `trades/lifecycle_reconciliation.py` 与 ledger allocation writer 复用单 evidence 的 correction void + replacement；最终原因、股票事件与费用证据各由原 owner 控制 | 完整匹配单 anchor 全部 lot 清单的指派/行权/到期证据只保留一次有效平仓数量；4→2 的部分结算保持原 4 张 pending close、进入 review，不能重开或把 4 张全标指派；晚到、重复、冲突、重启、事务故障后读回事件/allocations/lot/stock，数量与现金守恒；原期权成交时刻不变，完整更正后胜率更新。依赖 A。 |

验证用现有 `resolve_trade_deal`、push/backfill Inbox 恢复入口、
`option_performance_report` facade 和风险上下文读取；覆盖 `lx/sy`、US/HK、
不同物理账户、同合约多 lot、部分/重复/乱序成交、相同 deal ID 不同 payload、
实际零费与缺费用、actual 费用先于/后于原因、更正期间的费用同步、同合约再开平仓、
两连接并发。零价普通买卖平仓若无独立原因证据，维持 pending/review，不从零价
推断为到期或指派。A 的旧行为测试
`tests/test_trades_resolver_close.py::test_resolve_trade_close_apply_keeps_zero_price_option_leg_pending_without_stock_settlement`
须改为先失败再通过的核心断言，并加入性能/风险消费者测试；B 使用隔离 SQLite
验证原子替换和失败回滚；另测 4→2 部分证据、单证据跨多个 anchor 均只记观察而
不改经济事件。A/B 可分别开发和测试，但须一起交付：A 单独运行时
旧结算路径会把已关闭数量当作无余量，无法安全补齐原因。测试不得接真实 OpenD、
飞书或生产账本。

## 风险与待核事项

- 现有 lifecycle allocation 校验要求 `terminal_type == event_type`，本方案用
  `event_type=close`、`close_type=cause_pending` 避免新增事件类型；Impl 需核实所有
  决策快照、通知与 Control 消费者是否正确区分“数量已关闭”和“原因待定”。
- 原有已接受 evidence 仍可能保留预留而未写 event；部署后必须先只读盘点并给出
  单独的预览/修复方案，不能让新 writer 把历史 broker source 当作新成交重放。
- 券商 push 是成交被接收后的实时更新，不保证网络、OpenD 或进程故障期间零延迟；
  backfill 恢复仍按原券商成交时间入账。

设计稿经两轮 planreview 后获用户确认进入 Impl。Impl 仅改隔离工作树的源码与
测试；历史账本修复、生产配置和运行服务仍须单独处理。
