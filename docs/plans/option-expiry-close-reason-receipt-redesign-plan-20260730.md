# 期权到期平仓原因与回执幂等改造方案

## 1. 文档状态

- **状态**: frozen for fifth plan review
- **日期**: 2026-07-30
- **目标系统**: options-monitor
- **权威账本**: `trade_events -> position_lots`
- **适用账户**: 按运行时配置逐账户处理，账户身份必须同时绑定 OM account 与 Futu account id
- **生产现状**: `options-monitor-trade-intake.service` 已作为止血措施停止；本文不授权恢复服务
- **实施边界**: 本文只定义改造、迁移与验证方案；commit、release、生产升级、迁移 apply 和恢复服务分别需要独立授权

第五次冻结修订收敛四项实施歧义：为可消费 broker source 增加 ledger 级 owner claim；把
receipt-time pairing window 从 immutable market timing policy 中拆出；把业务
`resolution_revision` 与人工补发 `delivery_revision` 分离；禁止 live writer 在历史 replay 时
绕过 migration suppression 补建 pending intent。

## 2. 背景与问题

同一 Futu 成交可能先由 Push 到达，也可能先被历史成交轮询取得。当前链路把
`cause_pending` 当作 Inbox 可重试结果，并在业务处理期间同步发送飞书回执。由此产生两个独立风险：

1. Push、轮询或 Inbox 重试再次处理同一经济事件；
2. 业务状态已写入、飞书已经接收，但本地 receipt 状态未及时确认，导致重复发送。

同时，当前零价到期成交只表达“期权腿已经关闭”，不能单独证明 assignment、exercise 或到期无交割。
本次改造必须将“平仓事实”和“平仓原因”分开，并用可审计证据收敛原因。

## 3. 目标

1. Push 与轮询对同一 Futu deal 只接受一次。
2. Broker evidence 一旦可靠持久化，Inbox 即结束；原因等待不重放原始消息。
3. 平仓事实和原因解析分别可见，但不形成两套可写账本。
4. 所有原因策略集中在独立纯 domain resolver 中。
5. 自动识别：
   - 主动交易平仓；
   - Short Put / Short Call assignment；
   - Long Call / Long Put exercise；
   - 有完整否定证据的到期无交割。
6. 现金结算、证据缺失、匹配歧义、数量冲突一律 fail closed。
7. 通知按业务状态迁移产生，正常案件最多两条，且在进程崩溃、重启、Push/轮询乱序下不重复。
8. 存量案件和存量回执通过 dry-run、明确映射、审计与可恢复迁移进入新链路。

## 4. 非目标

- 不改变期权开仓、普通持仓投影或收益计算规则。
- 不把飞书消息当成交易或生命周期事实。
- 不在 v1 自动处理现金结算期权。
- 不用标的价格、价内/价外状态推测 assignment 或 exercise。
- 不用“查询成功但覆盖范围不明”的空结果证明无交割。
- 不自动纠正已经写错的 terminal event；纠正仍走受控 void/repair。
- 不新增第二个生命周期数据库或跨数据库分布式事务。
- 不在本 work unit 发布、部署、修改生产配置或恢复服务。

## 5. 核心不变量

1. Canonical broker deal identity：

   ```text
   futu:<account>:<futu_account_id>:<deal_id>
   ```

2. `trade_events -> position_lots` 是资金事实唯一权威；通知、Inbox、case status 都不是仓位权威。
3. `trade_lifecycle_cases -> evidence -> allocations -> terminal trade_event`
   是到期生命周期唯一写入主链。
4. 每个可消费 broker source event 必须先按 canonical broker key 在 ledger 内取得唯一 owner claim；
   同一股票成交不得同时解释两个 case。同 case 后续 evidence 只能引用既有 claim，不能重复取得或转移 owner。
5. 原因 resolver 无 I/O、无数据库写入、无通知副作用。
6. Resolver 只提出候选裁决；application writer 必须在一个 ledger SQLite transaction 中重新验证
   case、evidence、allocation 和当前 projection。
7. 任何 source claim、terminal lifecycle event、allocation、case derived status 和通知 intent
   必须原子提交。
8. “不存在交割”只能由完整、冻结、可审计的 broker settlement observation 正向证明。
9. 实际 broker execution time 决定提前/到期后分支；Push receipt time 和 poll discovery time 只用于
   乱序等待及 freshness。
10. `cash_settlement` 是保留原因，不是 v1 可自动落账的 terminal type。
11. Inbox 的 retry 只覆盖 broker evidence 被接受之前的失败；`cause_pending` 不属于 Inbox retry。
12. `accepted`、`confirmed`、`unknown` 或 stale `send_started` 的通知不自动重发；人工补发增加
    delivery revision，不伪造新的业务 resolution revision。
13. 存量已发送的平仓回执必须在启用新 dispatcher 前写入 suppression。
14. 在原因 pending 期间，canonical `position_lots` 保持 terminal event 写入前的剩余数量；
    lifecycle reservation 只改变可操作性，不伪造 projection 已归零。
15. Allocation 永不 UPDATE/DELETE。其 `canonical_terminal_event_id` 被有效 void event 指向后，
    allocation 在所有派生、规划、写入和审计链路中失效。

## 6. 单一事实模型

### 6.1 不新增第二套生命周期状态机

保留现有 lifecycle case、evidence、allocation 和 canonical ledger event。新增的“平仓事实”和
“原因状态”是 `option_lifecycle_read_model.v3` 的派生字段，不是独立可写业务事实。

```text
broker deal / stock settlement / manual evidence
                    |
                    v
trade_lifecycle_case + evidence + allocations
                    |
                    +----> pending lifecycle reservation
                    |          - canonical projection unchanged
                    |          - actionable = false
                    |
                    v
effective canonical terminal trade_event -> position_lots
                    |
                    v
option_lifecycle_read_model.v3
  - closure_fact
  - reason_state
  - close_reason
```

### 6.2 平仓事实派生值

| `closure_fact` | 派生条件 |
|---|---|
| `open` | target lots 仍有数量，且没有已接受的 option-close evidence |
| `partial_close_observed` | option-close/normal-close 只覆盖部分 target quantity |
| `option_leg_closed` | target option quantity 已由正常 close、有效 terminal allocations 或已接受且全量覆盖的零价 option-close evidence 证明关闭；零价 evidence pending 时 canonical projection 仍保留原数量，并由 reservation 阻止再次行动 |
| `closure_conflict` | target 漂移、重复消费、数量超配、互斥 terminal evidence 或 projection 不一致 |

### 6.3 原因状态派生值

| `reason_state` | 派生条件 |
|---|---|
| `not_started` | 尚无 option-close evidence |
| `cause_pending` | 已观察到关闭，但仍处于配对或结算等待期 |
| `partially_resolved` | 部分 target quantity 已有 terminal allocation |
| `resolved` | 全量 target quantity 已唯一分配至一个或多个合法 terminal reason |
| `needs_review` | 自动判断的必需证据不可用、产品不支持或人工处理被要求 |
| `conflict` | 存在互斥事实、超配、重复消费、迟到证据推翻既有 terminal result 等冲突 |

### 6.4 对外原因与现有 ledger event 的映射

| `close_reason` | Canonical ledger event | 适用条件 |
|---|---|---|
| `trade_close` | `close` | 有正常订单/成交证据且价格非零 |
| `assignment` | `assignment` | Short option + 唯一股票交割 |
| `exercise` | `exercise` | Long option + 唯一股票交割 |
| `expiration_no_settlement` | `expire_close` | 到期后完整无交割 observation |
| `cash_settlement` | 无，v1 不落账 | 发现或怀疑现金结算，进入 `needs_review` |

Short option 的 `expire_close` 在展示层写作“到期未被指派”；Long option 写作“到期未行权”。
底层继续使用同一个 `expire_close` event type，避免迁移既有账本事件。

### 6.5 Pending reservation 与 effective allocation

`lifecycle_reserved_contracts_by_lot` 是从 canonical case + 已接受 option-close evidence +
effective allocations 派生的只读 overlay，不新增可独立修改的 reservation 表：

```text
reservation exists when:
  accepted option-close evidence covers target lot quantity
  and that quantity has no effective terminal allocation
  and case is cause_pending / partially_resolved / needs_review / conflict
```

Reservation 期间：

- `position_lots.contracts_open` 保持 terminal event 写入前的 canonical 数量；
- lifecycle read model 显示 `closure_fact=option_leg_closed` 或 `partial_close_observed`；
- 对应数量 `actionable=false`，不能再次进入普通自动平仓、Close Advice 或候选行动；
- 同一 case 的 lifecycle terminal writer 可以按冻结 target manifest 消费该数量；
- 普通 broker close 若后来被证明是该 case 的正常成交证据，必须先由 resolver 将原因收敛为
  `trade_close`，再经同一 lifecycle writer 落账，不能绕过 reservation 另建 close；
- reservation 不能改变收益、现金或历史 performance；这些财务视图仍以 canonical projection 为准，
  但必须附带 lifecycle pending/closed overlay，不能把它展示成可行动 open lot。

`effective_void_event_ids` 由 canonical `trade_events` 中合法的 `void` 链实时派生。所有
`resolve_allocations()` 调用都必须传入该集合；被 void 的 terminal event 所属 allocation 保留原 row，
但不再计入 resolved quantity。禁止用 allocation UPDATE/DELETE 或新增 `voided` mutable flag 作为权威。

以下 open-lot 消费者必须在同一 work unit 中统一消费 reservation/effective allocation：

- ledger decision snapshot 与 `src/application/positions/context_builder.py`；
- Position Advice 输入、计划、promotion checks 与 Daily Brief position section；
- Close Advice 及其 reallocation/shadow actionability；
- `list_expiry_close_position_lots` 的到期 discovery（复用现有 case，不重复建案）；
- 普通 trade resolver、auto-close 与人工 close 的 target 选择；
- `list_open_short_assignment_rows` / portfolio assignment scenario；
- agent position query、operator report 与月度/performance 展示；
- lifecycle、ledger、projection 和 promotion quality checks；
- migration inventory、replay 和 correction planning。

这些入口不得各自重新推断 reservation。application 层建立一个 ledger-owned、
account-scoped lifecycle overlay/read facade；行动类消费者排除 reserved quantity，展示类消费者保留
canonical 数量并明确标记 `lifecycle_closed_pending_reason`。找不到 overlay、void chain 不合法或
overlay 与 projection 漂移时统一 fail closed 为非行动且 `needs_review/conflict`。

首次实现必须完成以下 propagation map，不允许保留无 `void_event_ids` 的旧调用：

- `domain/domain/option_lifecycle.py`：公开参数接收 authoritative void ids，并传入两处
  `resolve_allocations()`；
- `src/application/trades/lifecycle_reconciliation.py`：planning、combined resolution 和 readback
  全部使用 transaction/snapshot 同一组 void ids；
- `src/application/ledger/writer.py`：existing、proposed、post-write resolution 都从 transaction 内
  event log 计算 void ids；
- `src/application/positions/context_builder.py`：从 ledger overlay facade 获取 allocations + void ids，
  不直接拼 raw rows；
- `src/application/quality/lifecycle_checks.py`、promotion checks 和 migration replay：调用同一 facade，
  同时审计 raw allocation count 与 effective allocation count。

Facade 返回的是同一 ledger snapshot 的 `allocations + effective_void_event_ids + reservation overlay`
值对象，不缓存为第二份权威状态。

## 7. Intake 身份、来源与 Inbox 收敛

### 7.1 Push 与 Poll 的统一身份

在进入 Inbox 前完成 source binding：

```text
deal_id
+ internal account
+ futu_account_id
= futu:<account>:<futu_account_id>:<deal_id>
```

缺任一字段不得退化为 raw `deal_id`。无法唯一映射账户的 payload 写审计并进入
`identity_needs_review`，不进入自动账本写入。

Push 和 Poll 都必须保留以下 provenance：

- `transport`: `push` 或 `poll`
- `source_id`
- OpenD `host`、`port`
- listener `process_pid` 与进程启动时间
- `futu_account_id`
- `received_at_ms`
- poll 的 query start/end；Push 的 callback sequence（若 SDK 提供）

Provenance 用于回答“哪个进程、哪个端口取得该消息”，但不参与经济事件 identity。

### 7.2 Inbox 状态边界

处理顺序：

1. 解析和绑定 canonical identity；
2. 持久化 canonical broker deal/evidence，依靠唯一键实现幂等；
3. 触发一次原因 reconciliation；
4. 将 Inbox 标记 `handled`；
5. 原因仍 pending 时，由 lifecycle scheduler/新证据继续推进，不重放 Inbox。

只有以下情况保留 Inbox retry：

- 数据库尚未接受 broker evidence；
- 临时数据库错误；
- payload 尚未完成可恢复解析；
- source binding 所需的临时依赖不可用。

以下情况必须 `handled`，不得 retry：

- duplicate broker identity；
- `cause_pending`；
- `partially_resolved`；
- `needs_review`；
- notification pending/accepted/confirmed/unknown；
- notification dispatcher 不可用。

跨 Inbox DB 与 ledger DB 不追求分布式事务。允许“ledger evidence 已成功、Inbox 尚未 handled”
的崩溃窗口，因为重放会命中 ledger evidence 唯一键并安全收敛；不允许先 handled 再写 evidence。

### 7.3 Broker source consumption registry

`trade_lifecycle_evidence` 现有 `(source_type, source_event_id, evidence_type)` 唯一约束不足以证明
source 的全局唯一消费：同一 source 可以换 `evidence_type`，股票组合 evidence 也可能只把 deal key
嵌在 raw payload 中。新增 ledger-local、append-once 的
`trade_lifecycle_source_consumptions`：

```text
source_key PRIMARY KEY                 # canonical broker key，不接受 raw deal id
case_id
owner_evidence_id
source_role                            # option_anchor | stock_settlement
source_payload_hash
created_at_ms
FOREIGN KEY(case_id, owner_evidence_id)
  REFERENCES trade_lifecycle_evidence(case_id, evidence_id)
```

规则：

- 只 claim 会改变经济解释的 broker event：零价 option anchor 与每一笔 stock settlement deal；
  history query、position snapshot、calendar、order/cash-flow observation receipt 作为不可消费证据，
  由 observation hash 保证完整性，不写 source claim；
- 首次 evidence acceptance 必须在同一 ledger transaction 中插入全部 source claims；
- 同 `source_key + case_id + owner_evidence_id + role + hash` 重放为 no-op；
- 同 key 被另一 case、另一 owner evidence、另一 role 或不同 payload hash 使用时为 `conflict`；
- 同一 case 的后续 terminal/correction evidence 可以显式引用既有 claim，但不能复制 claim 或更改 owner；
- void 只使 terminal allocation 失效，不释放 broker source claim；旧 source 不得被另一个 case 重新消费；
- migration 必须先 inventory 现有 source usage，再写 claims；无法唯一归属的历史 source
  进入 `migration_needs_review`。

`source_payload_hash` 使用版本化 allowlist canonicalizer，只包含 broker 经济事实：
canonical source key、account binding、contract/underlying identity、side、quantity、price、
execution time、order id 与 clearing date（源端有值时）。Decimal、timestamp、symbol 先按 ledger
contract 归一化。它明确排除 transport、received-at、process/port、callback sequence、poll query
window 和 JSON 字段顺序；因此同一 deal 的 Push/Poll provenance 不会制造 hash conflict，而同 key
下经济字段漂移会 fail closed。

Writer 在 `BEGIN IMMEDIATE` 内先读取并验证现有 claim owner，随后插入/复用 evidence，再插入全部
source claims，最后写 event、allocation、projection 和 Outbox。任一 claim 唯一冲突使整个
transaction 回滚。该顺序满足 source claim 对 evidence 的外键，不使用 deferred-FK 假设；仅靠
application 层预查或 evidence raw payload 扫描不能作为并发唯一性保证。

## 8. 时间策略

### 8.1 版本化 timing policy binding

不修改既有 `lifecycle_case.v2` immutable payload。新增一张 ledger-local、
append-once 的 `trade_lifecycle_timing_policies`：

```text
case_id PRIMARY KEY
policy_schema = lifecycle_timing_policy.v1
market
timezone
settlement_style
underlying_security_type
last_trade_cutoff_ms
last_trade_cutoff_source
settlement_deadline_ms
trading_days_json
calendar_source
calendar_observed_at_ms
calendar_hash
created_at_ms
raw_json
```

同一 `case_id` 第二次写入时，payload 必须完全相同，否则报 immutable conflict。
`option_lifecycle_read_model.v3` 优先读取该 binding；没有 binding 的 legacy/v2 case 保留原显示，
但不得自动生成新的 `expiration_no_settlement`。

Timing policy 只冻结市场、合约与结算日历事实，不包含消息到达时间。它可以在 option-close evidence
到达前由 expiry discovery 创建，不依赖 Push/Poll 顺序。

### 8.2 Last-trading cutoff 的权威顺序

只接受以下来源：

1. broker contract metadata 明确提供的 last-trading timestamp；
2. versioned instrument policy registry 中明确支持的 physically settled equity option class。

禁止仅从 `expiration_ymd` 猜测 cutoff。Policy registry 的每一项必须绑定：

- market；
- underlying security type；
- settlement style；
- timezone；
- session close rule；
- policy version 和测试 fixture。

v1 不支持以下自动终态：

- settlement style 未知；
- index、cash-settled、flexible/custom contract；
- underlying security type 未知；
- last-trading cutoff 缺失；
- contract metadata 与 registry 冲突。

这些情况可继续接受 broker evidence，但原因进入 `needs_review`。

### 8.3 两个 broker business day

使用当前 Futu gateway 的 `request_trading_days` 作为 broker-observed market calendar。
从 expiration date 之后严格选取两个 `WHOLE/TRADING` 日期：

```text
first_business_day  = first trading day > expiration date
second_business_day = second trading day > expiration date
settlement_deadline = start of calendar day after second_business_day
                      in market timezone
```

因此“第二营业日结束”被精确定义为第二营业日当地 23:59:59.999 之后。必须在
`observed_at_ms >= settlement_deadline_ms` 时采集完整 settlement observation，不能使用更早的
缓存。缺少两个后续交易日、calendar 查询失败或 calendar hash 不可验证时进入 `needs_review`。

### 8.4 15 分钟窗口的含义

`pairing_until_ms = first_accepted_option_close_evidence.received_at_ms + 15 minutes`。

`received_at_ms` 是 broker evidence 第一次被 ledger 接受时冻结的 ingest provenance；同 canonical
broker key 的 Push/Poll/Inbox replay 不更新该值。`pairing_until_ms` 由 read model 从 immutable
evidence 派生，不写入 timing policy，也不使用后续重复消息更早或更晚的 receipt time 重算。

- 该窗口只用于吸收 Push 乱序并决定是否先发“期权腿已平仓”回执；
- 它不是 assignment/exercise 的最终证据截止；
- 15 分钟内原因收敛，只发一条最终合并消息；
- 15 分钟后仍 pending，发一次 interim receipt，继续等待至 settlement deadline。

实际原因匹配始终使用 broker execution time、clearing date 和 evidence identity，不用 receipt time
判断交易发生在 cutoff 前后。

### 8.5 Due reconciliation owner

没有新 Push/Poll 消息时，case 也必须在 `pairing_until_ms` 和 `settlement_deadline_ms` 到点后推进。
由现有 trade-intake runtime 增加一个 account-scoped、每 60 秒最多执行一次的
`reconcile_due_lifecycle_cases()`：

1. 查询已到 pairing time、尚未产生 interim transition 的 case；
2. 查询已到 settlement deadline、尚无完整 terminal result 的 case；
3. pairing 到点只更新派生状态并原子创建 interim Outbox，不调用 broker；
4. settlement 到点才采集一次新的完整 settlement observation；
5. observation incomplete 时直接转 `needs_review`，不循环高频查询；
6. 新 broker evidence 到达时可以提前重新 reconcile。

每个 runtime source 只能为其明确绑定的 account 执行 due reconciliation。多进程或进程重启时，
transaction 内的 state fingerprint/revision CAS 与 Outbox transition fingerprint 唯一约束保证最终
只有一个 transition；broker query 即使重复也必须生成相同 observation identity。另提供 read-first CLI：

```text
./om option-positions lifecycle reconcile-due --account <account>
./om option-positions lifecycle reconcile-due --account <account> --apply --confirm
```

CLI 默认 dry-run。生产自动调用仍由 trade-intake service ownership 控制，不新增 systemd unit。

现有 `src/application/quality/lifecycle_checks.py` 不得继续独立计算
“next market day + 2 hours” deadline。Quality 只消费 ledger 中冻结的 effective timing policy 和
read model v3；binding 缺失时报告 unavailable，不自行 fallback。

## 9. Broker settlement observation

### 9.1 冻结契约

新增 `broker_settlement_observation.v1`，由 application evidence assembler 构建并作为 lifecycle
evidence 写入 ledger。它至少包含：

```text
schema_version
observation_id
case_id
account
futu_account_id
market
contract_identity
target_contracts_by_lot
frozen_preterminal_remaining_by_lot
anchor_option_deal_key
anchor_execution_time_ms
observed_at_ms
query_window
required_sources
source_results
relevant_rows
source_payload_hashes
source_receipts
calendar_hash
complete
incomplete_reason_codes
```

`observation_id` 是 canonical payload hash。相同 observation 重放必须命中同一 identity。
`frozen_preterminal_remaining_by_lot` 在 observation transaction snapshot 中按
`target_contracts_by_lot - effective allocations` 计算；全量 pending case 等于原 target manifest，
部分已合法收敛的 case 只包含尚待终态的 remaining。它与 canonical projection 不一致时 observation
不能 complete。
每个 `source_receipt` 保存 query input、retcode、完整 row count、coverage/pagination 声明、
observed-at 和 allowlisted canonical rows；也可以引用 ledger-local immutable artifact path，
但必须同时保存文件 hash。只保存 hash 而不保存可重验内容，不能支持 `complete=true`。
账户号码和非判断必需的资金字段按现有审计规则脱敏。

### 9.2 必需来源

对于 `expiration_no_settlement`，以下来源全部必需：

1. **Anchor option close**
   - broker price 精确为零；
   - canonical option contract 与 case 一致；
   - contracts 不超过 target remaining；
   - canonical broker deal identity 完整。
2. **History deals**
   - REAL environment；
   - 精确 Futu account id；
   - query start 为 anchor execution 所在市场日的 00:00；
   - query end 不早于 observation time；
   - API 成功且 adapter 能证明无分页/截断遗漏。
3. **History orders**
   - 覆盖 anchor order id 和同一 query window；
   - 用于识别正常订单与券商自动零价记录；
   - endpoint 缺失或 coverage 不完整时 observation 不完整。
4. **Fresh positions**
   - `refresh_cache=True`；
   - observed at 不早于 settlement deadline；
   - 精确账户；
   - 目标 option contract 已不在 broker open positions；
   - 返回必须是完整账户 snapshot。
5. **Account cash flows**
   - 新增 gateway public method `get_account_cash_flows`；
   - infrastructure adapter 封装实际 Futu SDK 方法，不把 SDK method name 泄漏到 domain；
   - 逐 clearing date 查询 anchor 当地日期至第二营业日的所有日期；
   - 每个日期均记录 retcode、row count、coverage 和 payload hash；
   - endpoint 不可用、任一日期失败或 coverage 不可证明时 observation 不完整。
6. **Trading calendar**
   - 覆盖 expiration 前一日到第二营业日后一日；
   - 完整保存返回日期、类型、observed-at 和 hash。
7. **Contract timing/settlement metadata**
   - 明确 `settlement_style=physical`；
   - 明确 `underlying_security_type=equity`；
   - last-trading cutoff 来源符合第 8.2 节。

普通 `trade_close`、有正向股票交割的 assignment/exercise 不要求完整的否定 observation，但仍要求
其各自的正向证据完整。

### 9.3 `complete=true` 的唯一条件

只有当以下条件全部成立时才为 true：

- 所有 required sources 均 `status=complete`；
- account、environment、market、contract identity 全部一致；
- 所有查询输入和时间覆盖被记录；
- 没有 truncated、partial、stale、fallback-cache 或 unknown pagination；
- observed time 达到 settlement deadline；
- payload hashes 与冻结 rows 一致；
- 无股票交割、无现金结算、无正常订单、无互斥 evidence；
- broker position snapshot 显示目标 option quantity 为零；
- canonical projection 的每个 target lot remaining 与 observation 冻结的
  `frozen_preterminal_remaining_by_lot` 完全相等；
- target lots 没有 competing effective close/assignment/exercise/expire_close event，也未被其他
  lifecycle case 或普通 close 消费；
- 对应 target quantity 仍由本 case 的 accepted option-close evidence reservation 独占。

“retcode success”但 coverage 不明确仍为 `complete=false`。

这里故意不要求 terminal 写入前的 canonical projection 为零。零价 evidence 只建立关闭事实和
reservation；projection 必须等到 `expire_close` terminal event 写入后才归零。Writer 在同一事务中
重新检查上述 precondition，写入 `expire_close` + allocation，重放全部 effective trade events，
并在 commit 前验证每个 target lot 的 projected remaining 为零；否则整个事务回滚。

### 9.4 迟到证据

自动写入 `expire_close` 后到达 assignment/exercise evidence 时：

1. 不自动覆盖或追加第二个 terminal allocation；
2. case 转为 `conflict`，reason code 为 `late_settlement_conflicts_with_expire_close`；
3. 原 Outbox 记录不删除；
4. 生成一次 operator alert；
5. 只能通过受控 lifecycle correction command 修正；
6. correction 使用 append-only void chain，不 UPDATE/DELETE 原 event、evidence 或 allocation；
7. 修正完成后以更高 `resolution_revision` 发送“结果更正”，保留原消息引用。

Correction transaction 必须：

1. 锁定 case revision，并验证旧 terminal event、allocation 与原 revision 一致；
2. 验证已存在合法 void event，或在本事务内创建唯一 void event 指向旧 terminal event；
3. 从 canonical trade event log 计算 `effective_void_event_ids`，并把包含本次 void 的集合传给
   allocation resolver；
4. 确认旧 allocation 因其 terminal event 被 void 而失效，重新得到 frozen target remaining；
5. 验证新 evidence 唯一、未消费、数量/方向完整；
6. 原子写入新 terminal event、新 allocation、全量 projection、case revision 和
   `resolution_corrected` Outbox；
7. commit 前 readback：旧 allocation ineffective、新 allocation effective、projection 与 event replay
   完全一致。

默认路径把 void 与 corrected terminal event 放在同一事务，避免中间暴露可行动 open lot。若 operator
已经通过既有受控命令单独 void，case 必须保持 conflict reservation、所有行动入口 fail closed；
correction 事务只接受能从 canonical void chain 验证的旧 event，不接受 CLI 参数自述“已 void”。

## 10. 独立原因判断模块

### 10.1 Ownership

- **Domain**: `domain/domain/option_close_reason.py`
  - 定义输入值对象、reason enum、判断顺序和纯函数；
  - 不导入 `src/`、Futu、SQLite、CLI、飞书。
- **Evidence assembler**: `src/application/trades/close_reason_evidence.py`
  - 调用 gateway；
  - 构建、冻结和验证 observation；
  - 不写 terminal ledger event。
- **Reconciliation orchestrator**:
  `src/application/trades/close_reason_reconciliation.py`
  - 调用 resolver；
  - 在 writer transaction 中重新验证 case/evidence/allocation；
  - 落 canonical event、projection、derived status 和 notification intent。
- **Ledger write owner**: 扩展现有 `src/application/ledger/writer.py` 与
  `src/application/ledger/api.py`，不从 trades 模块导入 ledger internals。

### 10.2 Resolver 输入和输出

```python
resolve_close_reason(
    target: CloseReasonTarget,
    evidence: CloseReasonEvidenceBundle,
    timing: EffectiveLifecycleTiming | None,
    now_ms: int,
) -> CloseReasonDecision
```

`EffectiveLifecycleTiming` 是 application/read facade 组装后传入 domain 的只读值对象：
`last_trade_cutoff_ms` 与 `settlement_deadline_ms` 来自 immutable timing policy，
`pairing_until_ms` 来自首次接受的 option-close evidence。它不是一张可写状态表，也不能把
receipt-time 字段回写 timing policy。

`CloseReasonDecision` 只包含：

- `status`: not_started / cause_pending / partially_resolved / resolved / needs_review / conflict
- `close_reason`: trade_close / assignment / exercise /
  expiration_no_settlement / cash_settlement / None
- `contracts_resolved`
- `proposed_allocations`
- `evidence_ids`
- `reason_codes`
- `public_transition`

它不返回 SQL、通知文本或 Futu payload。

### 10.3 判断顺序

```text
if target identity or quantities invalid:
    return needs_review

if evidence contains mutually exclusive terminal facts,
   duplicate source consumption, over-allocation or projection drift:
    return conflict

if no option-close or canonical normal-close evidence:
    return not_started

if exact normal order + exact close deal + price > 0:
    if execution local date < expiration date:
        return resolved(trade_close)
    if timing is missing:
        return needs_review(last_trade_cutoff_unavailable)
    if execution_time <= last_trade_cutoff:
        return resolved(trade_close)
    else:
        return conflict(nonzero_close_after_last_trade_cutoff)

if option-close price < 0 or price missing:
    return needs_review(option_close_price_invalid)

if option-close price > 0 and no exact normal order:
    return needs_review(nonzero_close_order_evidence_missing)

# 以下只处理 price == 0
if cash settlement evidence exists or settlement_style == cash:
    return needs_review(cash_settlement_unsupported_v1)

stock_matches = find_unique_unconsumed_stock_settlement(...)

if stock_matches are over-allocated, direction-conflicting,
   multiply-matchable across cases, or inconsistent with strike/multiplier:
    return conflict

if stock_matches uniquely cover only part of target quantity:
    return partially_resolved(assignment or exercise)

if stock_matches uniquely cover all target quantity:
    if position_side == short:
        return resolved(assignment)
    if position_side == long:
        return resolved(exercise)
    return needs_review(position_side_missing)

if timing is missing:
    return needs_review(lifecycle_timing_policy_unavailable)

if now_ms < pairing_until_ms:
    return cause_pending(awaiting_out_of_order_pair)

if now_ms < settlement_deadline_ms:
    return cause_pending(awaiting_settlement_evidence)

if observation.complete
   and settlement_style == physical
   and broker option position absent
   and canonical target remaining equals observation.frozen_preterminal_remaining_by_lot
   and target has no competing effective terminal event or consumption
   and this case exclusively reserves the target quantity
   and no stock settlement
   and no cash settlement
   and no normal order
   and no conflict:
    return resolved(expiration_no_settlement)

return needs_review(settlement_observation_incomplete)
```

### 10.4 股票交割矩阵

| Option position | Option type | 股票方向 | 原因 |
|---|---|---|---|
| Short | Put | Buy | Assignment |
| Short | Call | Sell | Assignment |
| Long | Call | Buy | Exercise |
| Long | Put | Sell | Exercise |

### 10.5 股票交割匹配

自动匹配必须同时满足：

- 相同 internal account 与 Futu account id；
- 相同 canonical underlying；
- 股票方向符合矩阵；
- broker execution price 以 Decimal 精确等于 option strike；
- shares 精确等于 `contracts * multiplier`，允许多笔股票 deal 合计；
- 每个股票 broker deal identity 尚未被其他 lifecycle evidence 消费；
- clearing/execution time 落在 anchor 当地日期到 settlement deadline 的 observation window；
- 该候选集合在全局 pending cases 中唯一。

v1 不拆分一笔股票 deal 到多个 lifecycle case。若一笔聚合股票成交可匹配多个 strike/case，
进入 `needs_review`，由人工提供 broker reference 和数量 allocation。

Resolver 可以提出 allocation，但 ledger writer 必须在 `BEGIN IMMEDIATE` transaction 中重新检查：

- option/stock source claim 仍由本 case/evidence 拥有，新增 source 能在 registry 中原子 claim；
- target lot canonical remaining 与 observation 冻结的 pre-terminal remaining 一致；
- target quantity 仍由当前 case reservation 独占；
- `effective_void_event_ids` 已从 transaction 内的 canonical trade event log 重新计算；
- allocation 未超配；
- projection 与 expected remaining 一致；
- terminal event 写入并重投影后，allocated target remaining 按预期下降；全量 terminal 时为零。

任一检查失败，事务回滚并返回 conflict；不得使用 resolver 的陈旧候选直接写账。

## 11. Notification transition 与 transactional Outbox

### 11.1 消息数量

正常案件：

1. 15 分钟内原因已收敛：只发一条最终合并消息；
2. 15 分钟后仍 pending：发一次 `option_leg_closed`；
3. 后续收敛：再发一次 `resolution_confirmed`。

因此正常案件最多两条。

`needs_review` 或 `conflict` 只发一次 operator-visible 状态消息。人工修复后的
`resolution_corrected` 是异常更正消息，不计入正常两条上限，但必须引用原 case 和 revision。

### 11.2 通知 case id

- 到期生命周期：使用 canonical lifecycle `case_id`；
- 普通主动平仓：使用 `close:<canonical broker deal key>`，即
  `close:futu:<account>:<futu_account_id>:<deal_id>`，仅作为 notification subject/case id，
  不创建新的 lifecycle case。

一个 broker deal 可能按 target lot 拆成多个 canonical close events。通知身份必须位于 broker deal
聚合层，不能使用任一 split event id。普通平仓 Outbox payload 冻结：

- canonical broker deal key；
- 按 `(target_lot_id, event_id)` 排序的全部 canonical close event ids；
- target lot ids 与各 lot contracts；
- total contracts；
- broker execution time、contract identity 和 account binding。

只有在同一 transaction 内所有 split events 写入、全量 projection 重放及 readback 成功后，才创建
一条 `resolution_confirmed` Outbox，`resolution_revision=1`。重复 broker deal 重放必须命中相同
Outbox 唯一键并返回 existing。

Live writer 只有在本 transaction 首次创建该 broker deal 的完整 split set 时才可创建
`pending` intent。若全部 split events 在 transaction 前已存在且没有 Outbox/suppression，这属于
历史 replay 或 cutover 缺口：返回 `notification_history_unseeded` 审计，不得补建 pending intent。
历史 suppression 或 operator 选择的历史 final intent 只能由 migration manifest 创建。这样未列入
manifest 的历史记录即使被 replay 也保持零通知。

缺 canonical broker deal key 时不允许退化为 raw `deal_id`、
单个 event id 或随机 id；账本处理按既有 identity fail-closed 规则进入 review，通知写
`notification_identity_missing` 审计/抑制，不自动发送。

### 11.3 Outbox schema

在 option ledger SQLite 中新增 `trade_lifecycle_notification_outbox`：

```text
outbox_id PRIMARY KEY
case_id
transition_type
resolution_revision
delivery_revision
transition_key
state_fingerprint
status
payload_json
payload_hash
provider_message_id
claim_id
claimed_at_ms
send_started_at_ms
attempt_count
next_attempt_at_ms
last_error
provider_receipt_json
created_at_ms
updated_at_ms
confirmed_at_ms
UNIQUE(transition_key, delivery_revision)
UNIQUE(case_id, transition_type, resolution_revision, delivery_revision)
UNIQUE(case_id, transition_type, state_fingerprint, delivery_revision)
```

Outbox payload 在状态迁移时冻结，renderer/dispatcher 不得重新读取可变 case 生成不同文本。

- `resolution_revision` 只在 canonical lifecycle derived state fingerprint 发生变化时递增；
  相同 evidence/allocation/projection 状态重放必须保持原 revision。
- `state_fingerprint` 由 transition 前后 canonical case/evidence/effective allocation/projection
  摘要生成；普通 close 使用完整 broker-deal split-set hash。相同状态重放即使来自另一 worker，
  也必须命中同一 transition。
- Lifecycle `state_fingerprint.v1` 的 canonical JSON 只包含影响业务裁决的字段：case id/schema 与
  target manifest、resolver 引用的 evidence id/hash、对应 source claims、effective allocations
  （含 terminal event id/quantity/terminal type）、effective void event ids、target-lot projected
  remaining、reason state/close reason/sorted reason codes、timing policy hash 与被采用的 observation
  hash。列表按稳定 key 排序，Decimal/timestamp/symbol 使用 ledger canonicalizer。它排除
  `now_ms`、updated/received timestamps（pairing deadline 本身除外）、进程/端口 provenance、
  Outbox delivery 状态和 provider receipt，避免无关运行时变化推进业务 revision。
- `transition_key` 是业务消息槽位，不由 worker 随机生成：
  - lifecycle interim: `lifecycle:<case_id>:option_leg_closed`；
  - initial final: `lifecycle:<case_id>:resolution_confirmed`；
  - first operator state: `lifecycle:<case_id>:needs_review|conflict`；
  - correction: `lifecycle:<case_id>:resolution_corrected:<resolution_revision>`；
  - ordinary close: `close:<canonical broker key>:resolution_confirmed`。
  同一固定槽位出现不同 state fingerprint/payload hash 时不覆盖旧 row、不新发消息，转
  `notification_transition_conflict` 审计。这样新增 observation 或重复 due tick 不会产生第二条
  `needs_review/conflict/interim/initial-final`。
- `delivery_revision=0` 是该业务 transition 的原始 intent。人工 compensating resend 复用原
  `resolution_revision/state_fingerprint`，只递增 `delivery_revision`。
- `outbox_id` 从 `transition_key + resolution_revision + delivery_revision +
  state_fingerprint + payload_hash` 确定性生成。

### 11.4 原子写入

以下内容必须在同一个 ledger `BEGIN IMMEDIATE` transaction：

- lifecycle evidence；
- terminal trade event（如果有）；
- position projection；
- lifecycle allocation；
- case derived status、state fingerprint 与 `resolution_revision`；
- 对应 notification outbox intent。

普通 `close` 的 canonical event、projection 与单条最终 outbox 同样原子提交。
这里的“单条”指一个 canonical broker deal 一条：writer 先完成该 deal 的所有 per-lot split events，
再以 broker deal key 聚合创建 Outbox，不能在 per-event loop 中创建 intent。

如果状态迁移不需要发消息，不创建 Outbox。Writer 必须比较 transaction 内 before/after state
fingerprint；相同 fingerprint 不递增 revision、不创建新 intent。仅依赖“revision 自增 +
`UNIQUE(case_id, transition_type, revision)`”不能证明幂等。

### 11.5 Delivery 状态机

```text
pending
  -> claimed
  -> send_started
       -> confirmed
       -> accepted
       -> explicit_failed
       -> unknown

pending / explicit_failed -> claimed       # 到期后有界重试
claimed(stale, no send_started) -> pending # 安全回收
send_started(stale) -> unknown             # 冻结，不自动重发
accepted -> confirmed | unknown            # 仅 provider reconcile/人工操作
unknown -> confirmed | explicit resend     # 仅人工确认
suppressed                                # migration terminal state
```

规则：

- `claim_id` 每次 claim 唯一；complete 必须 compare-and-set 当前 claim。
- claim lease 固定 5 分钟；只有没有 `send_started_at_ms` 的 stale claim 可以回收。
- 发送网络请求前先持久化 `send_started`。
- 只有可证明请求未被 provider 接受的失败才是 `explicit_failed`。
- HTTP 超时、连接中断发生在 send started 之后或 worker 崩溃，一律 `unknown`。
- stale `send_started` 不自动 reclaim 成 pending。
- 若 provider 支持 message id/idempotency key，保存并查询；不能依赖其存在。
- 可重试的明确 pre-acceptance failure 最多 3 次总尝试，backoff 为 30 秒、5 分钟；
  不可重试 4xx 或第 3 次失败保持 terminal `explicit_failed`，等待人工处理。
- `accepted/confirmed/unknown/suppressed` 永不自动重发。
- 人工 `explicit resend` 不把原 unknown row 改回 pending，也不增加业务
  `resolution_revision`；它创建同 state fingerprint、下一 `delivery_revision` 的 compensating intent。

### 11.6 Dispatcher ownership

不新增第二个通知系统。先在现有 trade-intake 进程中加入有界 outbox dispatch loop，并提供
`./om option-positions lifecycle receipts dispatch --once` 作为受控恢复入口。

即使 dispatcher 不运行，业务写入仍成功且 Outbox 保留 pending。业务处理不再直接调用当前同步 receipt sender。
将来如果拆成独立 timer，只替换 dispatcher runtime owner，不改变 Outbox contract。

## 12. 人工复核

扩展现有 `./om option-positions lifecycle`，不增加新的顶层 CLI：

```text
./om option-positions lifecycle inspect --case-id <id>

./om option-positions lifecycle resolve \
  --case-id <id> \
  --reason assignment|exercise|expiration-no-settlement|trade-close \
  --broker-ref <ref> \
  --note <text>

./om option-positions lifecycle resolve ... --apply --confirm

./om option-positions lifecycle correct \
  --case-id <id> \
  --expected-revision <revision> \
  --void-terminal-event-id <event-id> \
  --reason assignment|exercise|expiration-no-settlement|trade-close \
  --broker-ref <ref> \
  --note <text>

./om option-positions lifecycle correct ... --apply --confirm
```

默认 dry-run。Apply 必须要求：

- exact case id；
- 当前 case revision；
- 明确原因；
- broker reference 或人工说明；
- 显示将创建的 evidence、allocation、terminal event、projection diff 和 Outbox；
- `--apply --confirm` 双开关。

人工操作写 append-only `manual_lifecycle_resolution.v1` evidence，仍通过相同 canonical writer；
不能直接 UPDATE position lot 或删除旧 evidence。

`correct` 是迟到 evidence/错误 terminal result 的唯一 lifecycle 修正入口。Dry-run 必须展示旧 event、
对应 allocation、将创建或复用的 void event、void-aware remaining、新 event/allocation、projection diff、
新 revision 与 correction Outbox。Apply 复用第 9.4 节的单事务规则；重复相同 correction 返回 no-op，
不同 correction 对同一 expected revision 必须 CAS 失败。普通 `trade-events void` 不能直接宣布
lifecycle 已修正，只会让 case 保持 conflict reservation，直至 `correct` 收敛。

通知 unknown 的人工收敛使用：

```text
./om option-positions lifecycle receipts inspect --outbox-id <id>
./om option-positions lifecycle receipts reconcile \
  --outbox-id <id> \
  --mark confirmed|resend \
  --broker-ref <ref> \
  --note <text>
./om option-positions lifecycle receipts reconcile ... --apply --confirm
```

`resend` 会保持原业务 `resolution_revision/state_fingerprint`，创建更高
`delivery_revision` 的 compensating intent，不把原 unknown row 改回 pending。

## 13. 存量迁移

### 13.1 迁移范围

不扫描并通知全部历史。迁移工具先生成 explicit manifest，operator 选择需要进入新原因流程的 case。
未列入 manifest 的历史记录保持只读，不产生新通知。

Inventory 至少列出：

- legacy lifecycle cases；
- v2 cases；
- target lots 与 remaining quantity；
- lifecycle evidence 和 allocations；
- canonical terminal events；
- authoritative void chains、effective void event ids 与 effective allocations；
- broker deal keys；
- broker source usage 与 planned immutable owner claims；
- current receipt states；
- candidate exact mapping；
- ambiguous/unmappable reason。

### 13.2 映射规则

自动迁移必须一对一证明：

- 相同 account、Futu account id、canonical contract、side、strike、expiration、multiplier；
- target lots 唯一且数量相符；
- broker deal identity 唯一；
- 无现有互斥 terminal event；
- 无重复 source evidence consumption；
- 每个可消费 broker source 能唯一映射到一个 canonical owner case/evidence/role/hash。

不能唯一证明时：

- 不创建自动 terminal event；
- 标为 `migration_needs_review`；
- 生成 review-only manifest row；
- 禁止最终结果 Outbox。

### 13.3 Legacy 与 v2 的处理

- 已有 v2 case：保留 case id、evidence 和 allocations；增加 immutable timing policy binding。
- 所有 v2/legacy resolution 都从 canonical trade events 重算有效 void set；被 void event 指向的旧
  terminal allocation 仅在 inventory 展示为 ineffective，不修改或删除原 row。
- 自动迁移仅对能唯一归属的 option anchor/stock settlement source 写
  `trade_lifecycle_source_consumptions`；voided terminal 的 source claim 仍归原 case，不释放。
- 只有 legacy case且可唯一映射：由 discovery 建立 canonical v2 case；通过
  非 allocating 的 `migration_bridge_evidence.v1` 引用 legacy evidence，不篡改原 row。
  Bridge 只保存 `referenced_legacy_evidence_id`，不能作为 terminal allocation source，也不能复制
  legacy row 的 broker source identity；新的自动裁决必须使用独立冻结的 settlement observation。
- legacy 已有 terminal event：保持冻结，只补 receipt suppression，不重写 terminal event。
- legacy 与 v2 同时存在：v2 为 canonical owner，legacy 标记 `superseded` 并记录
  `superseded_by_case_id`；原记录保留。

### 13.4 回执继承

对于 manifest 中的目标案件：

- 所有 cutover 前的 close receipt 视为已投递；
- 在 Outbox 中插入 `suppressed` 的 `option_leg_closed` migration row；
- 普通主动平仓按 canonical broker deal key 聚合 suppression；同一 deal 的多个 split event 只能得到
  一个 `close:<broker key>` suppression row；
- 缺 canonical broker deal key 的历史普通 close 只进入 review manifest，不生成 event-id fallback
  suppression 或新通知；
- 不把旧 raw deal receipt 直接伪装成 provider-confirmed 新消息；
- 最终原因尚未收敛：保持 pending，收敛后允许一个 `resolution_confirmed`；
- cutover 时已收敛且 operator 明确选择需要结果确认：只创建一个冻结的最终 intent；
- 其它历史 terminal cases 默认不补发。

Migration 是历史 Outbox seed 的唯一 owner。Live writer 遇到“canonical events 已存在、Outbox 与
suppression 都不存在”的历史 replay 时只记录 `notification_history_unseeded`，不得自行补建
pending intent。Manifest apply 为 suppression/final intent 冻结 state fingerprint；
repeat apply 必须命中相同 fingerprint 和 `delivery_revision=0`。

Dispatcher 必须在 suppression 和 manifest apply 全部完成、audit 通过之后才允许启动。

### 13.5 安全流程

```text
1. keep trade-intake stopped
2. backup ledger + WAL/SHM-safe snapshot
3. migration inventory --dry-run
4. review exact counts and mapping manifest
5. migration apply --manifest ... --confirm
6. repeat dry-run; expected apply count = 0
7. full projection replay
8. authoritative void-chain/effective-allocation audit
9. lifecycle duplicate/source-claim ownership/reservation audit
10. outbox broker-deal aggregation/suppression/final-intent audit
11. start new release only after separate production authorization
12. verify Push + Poll duplicate canary without manual resend
```

迁移逐 case transaction，manifest 有内容 hash 和 applied receipt。中途失败可从未完成 case 继续；
已经完成的 case 重跑为 no-op。

Rollback 不删除新表或新 evidence：

- 服务保持停止；
- 恢复升级前 ledger snapshot，或保留新账本并修复 forward；
- 不允许旧 release 在新 ledger 上重新启动同步 receipt 路径；
- 若二进制回滚，trade-intake 继续停止，直到兼容性评估完成。

## 14. Implementation slices

### Slice A — Domain contract 与 read model

文件范围：

- `domain/domain/option_close_reason.py`
- `domain/domain/option_lifecycle.py`
- `domain/domain/lifecycle_allocation.py`
- domain tests

工作：

- 增加纯 resolver、reason enum、输入输出值对象；
- 新增 `option_lifecycle_read_model.v3`、pending reservation 与 void-aware effective allocation
  派生字段；
- 不引入 I/O；
- 保留现有 ledger event types。

验收：

- if/else 矩阵全覆盖；
- Long/Short、Put/Call 四种股票方向通过；
- 缺数据、现金结算、互斥 evidence fail closed；
- pending reservation 不改变 canonical remaining，但 `actionable=false`；
- 同一 allocations 在传入/不传入有效 void event id 时得到预期的 effective remaining；
- domain import boundary 测试通过。

### Slice B — Timing 与 broker observation

文件范围：

- `src/infrastructure/futu_gateway.py`
- `src/application/trades/close_reason_evidence.py`
- 新增 timing policy adapter
- `src/application/quality/lifecycle_checks.py`
- `src/application/quality/service.py`
- focused gateway/evidence tests

工作：

- 增加 history order、account cash flow 的 gateway public methods；
- 构建 timing policy binding；
- 构建、冻结并验证 `broker_settlement_observation.v1`；
- 缺 endpoint/coverage/metadata 时返回 typed incomplete，不抛成全局 intake retry。
- 让 quality 消费 lifecycle effective deadline，删除独立 deadline fallback。

验收：

- Friday/weekend、连续假日、DST、缺 calendar、缺 cutoff；
- 每个 cash-flow clearing date query；
- success-empty 与 partial/unknown coverage 分离；
- snapshot hash 和 account binding 防串户。
- quality 与 lifecycle read model 对同一 case 给出相同 deadline。

### Slice C1 — Ledger 幂等原语与原子 writer

文件范围：

- `src/application/ledger/repository.py`
- `src/application/ledger/notification_outbox.py`
- `src/application/ledger/api.py`
- `src/application/ledger/commands.py`
- `src/application/ledger/writer.py`
- ledger repository/writer tests

工作：

- 新增 immutable timing policy、broker source consumption 与 Outbox schema；
- 为 case derived state 增加 transaction-local before/after fingerprint、单调
  `resolution_revision` 与 CAS；
- 对同 state replay 保持 revision，不新增 transition；
- 原子 claim option/stock source，允许同 case 后续 evidence 引用 claim，禁止跨 case 复用；
- 原子写 terminal event、projection、allocation、case derived state 和 Outbox intent；
- 普通 close 只在首次创建完整 split set 的 transaction 中创建 live intent；历史 replay 无
  suppression 时只写审计。

验收：

- 并发 source claim 只有一个 owner，loser 返回 conflict；
- 同 source 改 evidence type、role、payload hash 或 case 均不能绕过唯一约束；
- 同 state 并发/replay 的 resolution revision 和 Outbox 数量不增长；
- normal close 的历史 replay 不产生 pending intent；
- transaction 任一 write/readback 失败时 source claim、event、projection、allocation、state 和
  Outbox 全部回滚；
- repository schema upgrade 与旧 binary compatibility 在临时副本验证，不触碰生产 ledger。

### Slice C2 — Canonical lifecycle adoption 与 overlay propagation

文件范围：

- `src/application/trades/lifecycle.py`
- `src/application/trades/lifecycle_reconciliation.py`
- `src/application/trades/close_reason_reconciliation.py`
- `src/application/trades/deal_identity.py`
- `src/application/positions/context_builder.py`
- `src/application/ledger/decision_snapshot.py`
- Position Advice、Close Advice、report/agent query、assignment scenario 的 lifecycle overlay consumers
- lifecycle/ledger/promotion quality checks

工作：

- zero-price option deal 采用唯一 v2 case；
- 停止创建新的 legacy-shaped case；
- 在 evidence accepted、terminal reason pending 时派生 reservation，projection 保持 pre-terminal 数量；
- 所有 open-lot 行动入口消费统一 overlay 并排除 reserved quantity，展示入口保留数量并标记 pending；
- 从 canonical trade events 计算 effective void event ids，并传给全部 allocation resolver call sites；
- 通过 C1 writer transaction 重新验证 evidence/source claims/effective allocations/reservation；
- `expiration_no_settlement` 写入前验证 projection 等于 frozen pre-terminal remaining，写入后验证为零；
- lifecycle correction 原子写/复用 void、新 event/allocation/projection/revision/correction Outbox；
- 所有行动与展示入口只消费同一个 ledger-owned overlay facade。

验收：

- Push-first、poll-first、stock-first、option-first；
- partial allocation；
- 同股票 evidence 多 case 歧义及 source claim 冲突；
- concurrent reconcile 只生成一个确定的 event/allocation split set 和一个对应 transition Outbox；
- zero evidence -> pending non-actionable -> complete observation -> expire_close -> projection zero；
- pending 期间 Position Advice、Close Advice、auto close、assignment scenario 均不产生行动；
- expire_close -> void -> assignment、assignment -> void -> expire_close、partial 与 repeat correction；
- 所有 read/planning/writer/quality call site 对同一 void chain 得到相同 effective allocation；
- projection replay 无 mismatch。

### Slice D — Inbox 收敛

文件范围：

- `src/application/trades/inbox.py`
- `src/application/trades/intake.py`
- `src/application/trades/auto_intake.py`
- current audit/status tests

工作：

- 统一 canonical deal key；
- broker evidence accepted 后将 pending/needs_review 视为 Inbox handled；
- source process/port provenance 落审计。
- 在现有 runtime 中增加 account-scoped due reconciliation tick。

验收：

- 同 deal Push/Poll 任意顺序只处理一次；
- ledger-write-before-Inbox-crash 重放为 no-op；
- `cause_pending` 不进入 retry；
- intake status 可区分 Inbox pending 与 lifecycle pending。
- 无新消息时 pairing/deadline 到点仍只产生一次对应 transition。

### Slice E — Transactional Outbox 与 renderer

文件范围：

- 新增 trade lifecycle outbox service
- 复用当前 trade receipt renderer 中的展示逻辑
- `src/application/trades/auto_intake.py`

工作：

- 在 C1 Outbox schema 上实现 claim/CAS/lease；
- 冻结 transition payload；
- dispatcher loop；
- direct synchronous receipt path 退出业务处理链；
- unknown 人工冻结。

验收：

- 在 transaction commit、claim、send_started、HTTP write、provider response、
  confirm persistence 每个边界做 fault injection；
- stale claimed 可回收，stale send_started 变 unknown；
- accepted/confirmed/unknown 不自动重发；
- 一个 broker deal 关闭 2/3 个 lots 仍只有一条最终 intent，payload 含有序 event/lot/quantity；
- 上述普通 close 的 2/3 lots 场景分别保留 2/3 个确定的 per-lot events，split 总 contracts
  精确等于 source deal，不创建 lifecycle allocation；
- 相同 raw deal id 在不同 account/Futu account id 下生成不同 intent；
- 缺 canonical broker key 不生成自动通知，broker deal replay 为 no-op；
- 正常案件一条或两条，不出现第三条普通消息。

### Slice F — 人工 CLI

文件范围：

- `src/interfaces/cli/option_positions.py`
- application manual resolution service
- CLI tests/docs

工作：

- inspect、resolve、receipt reconcile；
- lifecycle correct 的 void-aware dry-run 与单事务 apply；
- dry-run 默认、apply+confirm、revision compare-and-set；
- append-only manual evidence 和 compensating notification。

验收：

- stale revision 拒绝；
- 缺 broker ref/note 拒绝；
- dry-run 零写入；
- apply 后 projection、audit、Outbox 一致。
- correction 后旧 allocation ineffective、新 allocation effective；重复 correction no-op。

### Slice G — Migration

文件范围：

- versioned migration service/CLI
- migration tests and operator docs

工作：

- inventory、manifest、exact mapping、suppression、idempotent apply；
- 从 authoritative void chain 重算 effective allocations，并按 broker deal key 聚合普通 close suppression；
- ambiguous rows 隔离；
- backup/readback/replay/audit 输出。

验收：

- 一合约多 lot；
- legacy+v2 重叠；
- 已确认/unknown receipt；
- 中途失败后重跑；
- repeat dry-run apply count 为 0；
- 未列入 manifest 的历史 case 零变化、零通知。
- voided terminal allocation 不被错误继承；per-event 历史 close 不产生重复 suppression。

### Slice H — Integrated verification 与 rollout handoff

工作：

- focused suites；
- full lifecycle/trade intake/ledger/notification regressions；
- dependency graph regeneration/check；
- full projection replay fixture；
- 生成 production dry-run handoff。

不在本 Slice：

- commit/push；
- VERSION/tag/Release；
- 生产升级；
- migration apply；
- 启动 trade-intake；
- 发送真实飞书。

## 15. Test matrix

### 15.1 原因判断

| 场景 | 预期 |
|---|---|
| cutoff 前，正常订单，price > 0 | `trade_close` |
| cutoff 后实际成交，price > 0 | `conflict` |
| Short Put，price = 0，Buy stock | `assignment` |
| Short Call，price = 0，Sell stock | `assignment` |
| Long Call，price = 0，Buy stock | `exercise` |
| Long Put，price = 0，Sell stock | `exercise` |
| price = 0，只有部分股票数量 | `partially_resolved` |
| price = 0，现金结算 evidence | `needs_review` |
| price = 0，完整无交割 observation | `expiration_no_settlement` |
| price = 0，任一 required source incomplete | `needs_review` |
| 同股票 deal 可匹配两个 case | `needs_review` 或 `conflict`，零自动 allocation |
| expire_close 后迟到 assignment | `conflict`，不自动覆盖 |
| expire_close -> valid void -> assignment correction | 旧 allocation ineffective，新 allocation effective，projection 正确 |
| assignment -> valid void -> expire_close correction | 同上，revision 单调增加 |
| partial terminal -> void -> corrected partial/full | effective remaining 与 replay 一致 |

### 15.2 Intake 与并发

- Push 先、Poll 后；
- Poll 先、Push 后；
- 两个 worker 并发接受同 broker key；
- evidence 已提交、Inbox 未 handled 时崩溃；
- stock 先到、option 后到；
- option 先到、stock 15 分钟内到；
- option 先到、stock 15 分钟后但 deadline 前到。
- case/timing policy 先建立、option evidence 后到；pairing time 仍从首次接受 evidence 派生；
- 同 broker key 的 Push/Poll replay 不改变 frozen `received_at_ms/pairing_until_ms`；
- 同 stock source 改 evidence type、source role 或 case 竞争，只有原 owner claim 有效；
- zero option evidence accepted 后，projection 仍为 frozen remaining、reservation non-actionable；
- pending reservation 期间普通 close/auto-close/Close Advice/Position Advice 不产生第二次行动；
- terminal writer commit 后 reservation 消失，projection 由 effective terminal event 归零。

### 15.3 时间与 observation

- Friday expiry；
- first/second business day 有假日；
- DST 切换；
- calendar partial；
- cutoff metadata missing/conflict；
- positions cache 未 refresh；
- history query coverage truncated；
- cash-flow 某一 clearing date 失败；
- observation 早于 deadline；
- payload hash 被篡改。

### 15.4 Outbox

- 状态迁移前崩溃；
- ledger transaction commit 后 dispatcher 未运行；
- claimed 后、send_started 前崩溃；
- send_started 后、HTTP 前崩溃；
- provider accepted 后、本地 confirmed 前崩溃；
- explicit pre-acceptance failure；
- stale send_started；
- manual mark confirmed；
- manual compensating resend；
- compensating resend 只增加 `delivery_revision`，业务 revision/state fingerprint 不变；
- migration suppression 与 live transition 竞争。
- 一个 broker deal 跨 2 个/3 个 target lots，只创建一条 aggregate close intent；
- 同一普通 close 场景分别创建 2/3 个 per-lot events、零 lifecycle allocation；每个
  `(broker deal key, target lot id)` 只有一个 effective close event，split 数量完整且总 contracts
  等于 source deal；
- 到期 lifecycle evidence 跨 2/3 个 target lots 时，分别创建 2/3 组一一对应的
  terminal event/allocation，但仍只有一个 case transition intent；
- 相同 raw deal id、不同 account/Futu account id，产生两个不同 notification subjects；
- broker key 缺失时零自动 intent；
- 历史 broker deal replay 在无 suppression 时仍为零 pending intent；
- split close transaction/replay 重跑 aggregate intent 为 no-op；
- correction revision 只在 corrected ledger/projection readback 成功后创建一个 correction intent。

### 15.5 Migration

- legacy case 无 target manifest；
- legacy/v2 同时存在；
- 多 lot 同合约；
- existing terminal event；
- existing confirmed/unknown receipt；
- ambiguous mapping；
- apply 中断与重跑；
- projection replay；
- voided terminal allocation 的 effective replay；
- ordinary close per-event legacy receipt 聚合为 broker-deal suppression；
- dispatcher 未启用前无真实发送。

## 16. 可观测性

Intake status 必须分别显示：

- Inbox pending/retry/handled；
- lifecycle `cause_pending`/needs_review/conflict；
- 最老 pending case 与 deadline；
- observation incomplete reason；
- Outbox pending/claimed/send_started/accepted/confirmed/unknown；
- oldest unknown；
- duplicate broker key count；
- source process/port/account provenance；
- migration manifest/apply receipt。

禁止继续用 `inbox pending=0` 推断生命周期已收敛，也禁止用 service active 推断回执已送达。

## 17. Success criteria

1. 同一 Futu deal 经 Push、Poll、Inbox retry 后：
   - canonical broker evidence identity 只接受一次；
   - 可消费 option/stock broker source 只有一个 immutable owner claim；
   - 生成一个完整、确定且不重复的 terminal split set；
   - 每个 `(canonical broker deal key, target lot id)` 至多一个 effective terminal event；
   - 普通 close 不创建 lifecycle allocation；到期 lifecycle terminal event 与 allocation
     按 target lot 一一对应；
   - split event 数量与 matched target lots 一致，总 contracts 精确等于 source deal；
   - 普通主动平仓只有一个 broker-key aggregate final intent；
   - 到期 lifecycle case 至多一个 interim 和一个 final intent；correction 使用更高 revision，
     不重复原 transition。
2. `cause_pending` 不再增加 Inbox attempt。
3. 原因 pending 时 canonical projection 保持 frozen pre-terminal remaining，但所有行动面都把
   reserved quantity 判为 non-actionable；terminal commit 后 projection 才按 event 归零。
4. 无完整 settlement observation 时绝不自动写 `expire_close`；完整 observation 的 precondition 不
   依赖尚未存在的 terminal projection zero。
5. 一个普通 broker close deal 无论拆成多少 lot events 都只有一个 broker-key aggregate intent；
   缺 canonical broker key 时零自动回执。
6. Long/Short 四种 assignment/exercise 映射与数量 allocation 正确。
7. Void 后所有 read/planning/writer/quality/migration 链路忽略旧 terminal allocation；
   correction 后旧/新 allocation effectiveness 与 canonical projection replay 一致。
8. 任何 terminal 写入后 full projection replay 无 missing/extra/field mismatch。
9. Outbox 所有崩溃点不产生自动重复发送；不确定投递进入 unknown。同一业务 state replay 不增加
   `resolution_revision`；人工补发只增加 `delivery_revision`。
10. Migration dry-run 数量明确，apply 后 repeat dry-run 为零，未选历史记录无变化；历史 event
    replay 不得绕过 manifest/suppression 创建 pending intent。
11. 生产服务恢复前能独立证明：
   - 新 release 已安装；
   - schema/manifest/migration audit 通过；
   - dispatcher suppression 已就绪；
   - Push 与 Poll canary 在 dry-run/隔离通知目标下幂等。

## 18. Rollback 与生产边界

- 本地实现通过不代表允许 release。
- Release 成功不代表允许生产升级。
- 生产升级成功不代表允许 migration apply。
- Migration 成功不代表允许启动 trade-intake 或发送飞书。
- 任何需要真实飞书、生产账本写入、service start/stop 的步骤都必须单独确认。
- 若上线后出现 unknown 激增、重复 intent、projection mismatch 或 observation completeness 漂移：
  立即停止 trade-intake/dispatcher，保留 ledger、WAL、Outbox 和 audit，不手工重发。
