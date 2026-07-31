# 持仓事实核查后续 DeepReview 修正方案

## 1. 文档状态

- **状态**: frozen after remediation PlanReview
- **日期**: 2026-07-31
- **代码基线**: `main@d579683d45f976f38e3ba22b03748fcd1d5a9bff`
- **评审输入**: `docs/reviews/code-review-20260731-170522.md`
- **复审输出**: `docs/reviews/plan-review-20260731-181227.md`
- **当前对象**: 基线之上未合并的 lifecycle bridge、存量 Combo identity adoption 和
  due reconciliation 异常隔离改动
- **父级设计真源**:
  - `docs/plans/option-expiry-close-reason-receipt-redesign-plan-20260730.md`
  - `docs/POSITION_ADVICE_V2_CONTRACT.md`
  - `docs/POSITION_ADVICE_COMPATIBILITY.md`
  - `docs/STRATEGY_ARCHITECTURE.md`
- **文档权威**: 本文仅在下述三个 remediation slice 内是对父级设计的窄化修订；
  父级设计的其余目标、状态机、迁移和生产边界保持不变
- **实施边界**: 本轮只修正方案并复审；不实施代码、不重跑持仓写入、不提交/
  推送、不发布、不迁移、不升级或恢复生产服务

当前 workspace 代码只是需被修正的实现证据，不得倒推为本方案已经验收。

## 2. DeepReview finding 裁决

| Finding | 裁决 | 本方案 owner |
|---|---|---|
| DR-01：migration bridge 未进入冻结 decision snapshot | accepted | Slice R1 |
| DR-02：Combo identity adoption 未验证完整 group membership | accepted | Slice R2 |
| DR-03：due batch 的最终单案 reconciliation 仍可中断整批 | accepted | Slice R3 |

三项不共享新状态表、新业务实体或通用“修复框架”。R1 和 R3 因同时涉及
`close_reason_reconciliation.py` 按顺序实施；R2 与 lifecycle 状态机没有语义依赖。

首轮 remediation PlanReview 的 material findings 也全部 accepted，并由本文收敛：

| PlanReview finding | 本文修正位置 |
|---|---|
| v3 producer/consumer、正式契约和 mixed-version 边界未闭合 | §6.6、§9 |
| R1/R2/v3 的原顺序会产生不可独立通过的半成品 schema | §10 |
| direct anchor 的多 anchor、claim、重放和数量语义未定义 | §6.3 |
| 单 case resolver 未提供账户级 reservation 排他仲裁 | §6.4 |
| Combo 只查当前 projection，无法发现历史 retag/reuse | §7.1–§7.4 |
| Combo membership schema/hash/canonical order 未定义 | §7.4 |
| prepare 后 writer 不校验 generation，陈旧结果可覆盖新状态 | §8.3 |

## 3. 目标

1. 同一 ledger generation 中，live reconciliation、Position Advice 和 positions context
   消费同一份账户级、经验证且完成 reservation 仲裁的 lifecycle anchor/overlay 事实。
2. 有效 migration bridge 可以建立 reservation 和 `cause_pending`，但永远不转移、
   复制或伪造 legacy broker source ownership。
3. 只有“当前成员与 effective event history 成员都恰好是两条精确绑定腿、且从未
   retag/reuse”的存量 group 才能首次建立 immutable Combo identity。
4. Position Advice 在 identity 建立后仍使用冻结、可重算的 membership fact 防御额外腿、
   历史 group id 重用、retag 或跨账户碰撞。
5. 单个 malformed lifecycle case 转成稳定的人工复核结果并继续后续 case；
   SQLite、写权限、CAS、ledger invariant 或未分类基础设施错误仍中断 batch。
6. Provider collect 期间如果 case/anchor/allocation generation 改变，陈旧 decision 零写入
   失败，不能覆盖 terminal 或更新后的 lifecycle state。

## 4. 非目标

- 不自动修改、删除或替换已存在的 lifecycle evidence、source claim、allocation、
  terminal event 或 Combo identity。
- 不执行 lifecycle migration manifest、`adopt-combo-identity --apply` 或任何生产账本写入。
- 不支持一个 Combo group 中多个 Funding cycle、一条 Call 对多条 Put、或 group id 重用。
- 不自动清理或改写历史 `strategy_group_id`；历史 retag/reuse 只 fail closed，人工修正另立
  work unit。
- 不将 bridge 物化为新的 broker evidence，不改变 canonical `trade_events -> position_lots`。
- 不用 broad exception catch 把 provider、repository 或 writer 故障降级成人工复核。
- 不新增第二套 lifecycle/group 真源，不引入跨数据库事务。

## 5. 共同不变量

1. Repository 只负责在一个 SQLite read transaction 中返回 raw facts，不决定
   bridge 或 Combo 业务语义。
2. 所有行动决策只消费被 decision fingerprint 覆盖的冻结事实；决策建立后
   不回查 repository 补齐事实。Apply writer 唯一允许的重读是 transaction 内的
   generation/CAS precondition，不得借此重算或悄悄替换旧 decision。
3. 不可验证的 bridge 或 group membership 只封锁相关 case/group；完整 coherent
   snapshot 仍可为其他无关持仓提供决策。
4. 必需 snapshot row/schema 缺失、事务读取失败或 JSON 无法解析时，整个 snapshot
   `snapshot_unavailable`，不得用空列表伪装成“没有冲突”。
5. Dry-run 与 apply 都从自己的 transaction/snapshot 重新验证；apply 不信任
   dry-run 结果或调用方自述。
6. 新旧 snapshot/fingerprint 版本不混用；旧 artifact 可供审计，不可被 promotion
   或变成 actionable 决策。所有 producer/consumer 通过同一个 strict validator
   验证 schema、必需子契约和可重算 fingerprint，不允许只检查 64 位字符串。
7. Set-like ids、bindings 和 reason codes 的 canonical order 在各自 contract 中定义，
   不依赖 SQL、dict 或 projection 遍历顺序。

## 6. Slice R1 — Account-coherent lifecycle anchor overlay

### 6.1 Ownership 与文件边界

- `src/application/ledger/repository.py`
  - 只提供一个 SQLite read transaction 内的 account-scoped raw fact bundle；
  - 不决定 direct、bridge、reservation 或 conflict 语义。
- `src/application/ledger/lifecycle_overlay.py`
  - 成为 direct/bridge validation、case-local reservation 请求和 account-level
    reservation arbitration 的唯一纯 resolver owner。
- `src/application/ledger/decision_snapshot.py`
  - 冻结完整 account resolver 输出并纳入 decision fingerprint。
- `src/application/positions/context_builder.py`
  - 只消费 snapshot 中的冻结 resolution，不再单独做 lot-overlap 补丁式裁决。
- `src/application/trades/lifecycle_reconciliation.py`、
  `close_reason_reconciliation.py`、`settlement_observation.py`
  - 通过同一 account resolver facade 取目标 case view；不各自查 claim、重建 bridge
    或重新仲裁 reservation。
- `src/application/ledger/lifecycle_migration.py`
  - 仍只写 immutable bridge、supersession、timing binding 和原 owner claim；
    不派生运行时 reservation。

Domain 层不导入 repository 或 migration helper；application resolver 不写 ledger。

### 6.2 Coherent account fact bundle

共享 reader 必须在一个 `BEGIN` 中读取目标 account 的完整决策闭包：

- 所有未 supersede canonical v2 cases，以及它们引用的 superseded legacy cases；
- canonical/legacy evidence、allocations、source consumption claims 和 timing policies；
- account position lots；
- 用于重放这些 lots 的全局 trade events 与 authoritative effective void ids；
- Combo identity 和 §7 所需的 current/history group membership 输入。

`read_decision_state_rows(account)` 与单 case live reader 复用同一私有 reader。Live path
不得再拼接：

```text
lifecycle_reconciliation_facts()
+ lifecycle_option_close_anchor_facts()
+ get_trade_lifecycle_timing_policy()
```

单 case 调用先在 transaction 内解出 account，再读取并 resolve 整个 account bundle，最后
只返回目标 case view。不得在 transaction 返回后追加 getter。Raw bundle 必须携带
`lifecycle_generation_token.v1` 的重算输入；token 至少覆盖 case/supersession、evidence、
claims、timing、effective allocations、相关 target lots、相关 effective events/voids 和
account arbitration output。Dependency set 包含所有 target manifest 与目标 lots 相交的
未 supersede cases，即使它们当前 missing anchor/零 reservation，避免 collect 窗口中新 claim
把它变成竞争者却不改 token。Token 排除读取时钟与进程 metadata。

### 6.3 Direct 与 bridge anchor contract

纯 resolver 输出 `lifecycle_option_close_anchor_resolution.v1`。每个 case resolution：

```text
case_id
status = missing | direct | bridged | conflict
anchor_facts[]
requested_reservations_by_lot
effective_reservations_by_lot
reason_codes[]
resolver_schema_version
resolution_hash
```

每个 `anchor_fact` 至少冻结：

```text
anchor_fact_id
canonical_case_id
anchor_kind = direct | migration_bridge
bridge_evidence_id | null
source_owner_case_id
source_owner_evidence_id
source_key
source_payload_hash
futu_account_id
execution_time_ms
received_at_ms
quantity
target_contracts_by_lot
anchor_fact_hash
```

这是内存/快照值对象，不是新表、新 evidence 或新 source claim。Hash preimage 排除自身
hash 字段；anchor 按 `anchor_fact_id`、lot manifest 按 `record_id`、reason codes 去重后
字典序排列。

Direct anchor 必须满足：

1. price=0 option-close evidence 直接归属当前 canonical case；
2. 每条 accepted evidence 恰有一个同 case、同 evidence、`option_anchor` role、同 canonical
   source key/payload hash 的有效 owner claim；缺失或多个 claim 均 conflict；
3. Repository insert-once 的同 `evidence_id + source_key + payload_hash + owner` 重放去重；
   同 source key 出现不同 evidence id、payload hash、owner 或经济 payload 时 conflict，
   不把未被 claim 拥有的重复 evidence 静默合并；
4. 每个 manifest 只包含 case target lots、数量为正，且 evidence/claim quantity 精确等于
   manifest 总数；
5. 多个 direct anchors 只在 manifests 两两不相交时合法；任一 lot 重叠即整个 case
   conflict，不做“先到先得”；
6. 合并后每个 lot 数量不超过 case target quantity 和 lot 可分配 quantity；超量、负量
   或无法规范化均 conflict；
7. pairing time 使用所有 accepted direct anchors 中最早的 immutable `received_at_ms`；
   SQL/evidence 顺序不得改变结果。

为兼容已存在的 direct evidence，resolver 接受 canonical `target_contracts_by_lot` 或既有单 lot
`target_lot_id + contracts`，先规范化成同一个 manifest；两种形式同时存在但不一致时 conflict。
V3 anchor fact 只输出 canonical manifest，不继续传播双形态。

Bridge anchor 必须满足：

1. target 是未 supersede 的 `lifecycle_case.v2`；
2. bridge 是归属 target 的 `migration_bridge_evidence.v1`、`allocating=false`；
3. legacy case/evidence 精确存在，legacy 已被 target supersede；
4. legacy evidence 是 price=0 option-close，仍归 legacy case/evidence 所有；
5. 恰有一个 `legacy case + legacy evidence + option_anchor` owner claim；
6. claim schema、canonical source key 和 payload hash 可重算；
7. account、Futu account、symbol、option type、side、strike、expiry 与 price 一致；
8. claim canonical payload quantity 等于 target manifest 总数，且不超过 target/lot capacity；
9. 不存在 direct anchor、额外 bridge、额外 owner claim 或 source-key collision。

Bridge v1 只支持一个 bridge 对一个 anchor；多 bridge/multi-anchor bridge 直接 conflict。
经济字段以 claim canonical payload 为准；legacy evidence 只提供被 claim hash 绑定的 ingest
provenance。禁止把 legacy evidence 克隆成 `case_id=canonical_case_id`。Reservation 显式
消费 validated anchor fact，owner 始终保持 legacy case/evidence。

### 6.4 Account-level reservation arbitration

Case-local validation 后，resolver 必须在同一 account bundle 中统一仲裁：

1. 先扣除有效 allocation，得到每个非 conflict case 的正数
   `requested_reservations_by_lot`；已完全分配的 case 不产生 reservation edge；
2. 建立 `case -> target lot` 二部图；同一 lot 被两个或以上 case 请求即为 overlap；
3. 对含 overlap 的连通分量，将其中所有 case 设为同一稳定
   `reservation_target_overlap` conflict，并把这些 case 的**完整 target manifest**
   reservation/action 清零，不允许部分 lot 继续行动；
4. 无 overlap 的 case 才发布 `effective_reservations_by_lot`；
5. cases、lots、edges 和 reasons 都 canonical sort，输出
   `account_lifecycle_reservation_resolution.v1` 及可重算 `arbitration_hash`。

这条仲裁同时覆盖 direct+direct、direct+bridge、bridge+bridge 和 multi-lot partial overlap。
Live 单 case 入口与 snapshot 都必须先运行同一 account graph；不得保留“live 单案先行动、
snapshot 最后才发现 overlap”的双重语义。

### 6.5 Failure semantics

- 无 direct anchor 且无 bridge：`missing/not_started`，不是 conflict。
- Anchor/claim/reference/quantity/direct-overlap/account-overlap 不合法：
  - 所有受影响 case 输出稳定 conflict reason；
  - 其完整 target manifests `actionable=false`；
  - 不生成未验证或部分 reservation；
  - 无关 graph component 继续。
- Coherent reader、必需 schema/JSON、projection 或 resolver infrastructure 失败：
  - decision snapshot 整体 `snapshot_unavailable`、fingerprint `None`；
  - live reconciliation 零写入并向上透传基础设施错误；
  - 不得降级为 `missing` 或 case-local conflict。

### 6.6 Decision snapshot/fingerprint v3

本 remediation 的原子 cutover 冻结：

- `decision_state_snapshot.v3`；
- `decision_state_fingerprint.v3`；
- `lifecycle_option_close_anchor_resolution.v1`；
- `account_lifecycle_reservation_resolution.v1`；
- §7 的 `account_combo_group_membership.v1`。

v3 payload 在现有事实外纳入完整 claims、timing policies、case resolutions、account
arbitration 和 Combo membership。Bridge 引用的 legacy owner claim 必须在 fingerprint
闭包中，不能只筛 `claim.case_id == canonical_case_id`。

`now_ms`、读取时刻和运行进程 provenance 不进入 fingerprint。在相同 ledger facts 下，
pairing/deadline 前后 fingerprint 不变；时间状态只由冻结 timing 和调用方显式
`checked_at` 派生。

`domain/domain/decision_state_fingerprint.py` 是唯一 strict contract validator owner。
Validator 必须检查 exact top-level schema、fingerprint schema、所有必需列表/子契约版本、
每个 nested hash、canonical order/shape，并排除已存 fingerprint 后重算 top-level hash。
它返回稳定 validation status/reason，producer 对失败抛 typed contract error，reader/promotion
对失败 `actionable=false`；任何入口不得再只检查 `trusted + actionable + 64 chars`。

Public reason vocabulary 固定为：`decision_state_schema_unsupported`、
`decision_state_required_fact_missing`、`decision_state_nested_hash_mismatch`、
`decision_state_fingerprint_mismatch`、`decision_state_noncanonical`、
`decision_state_projection_untrusted`。Public payload 不回显 raw path/value；bounded internal
diagnostic 可记录失败 stage/path。

v2 snapshot/fingerprint 和缺少任一 v3 必需字段的 artifact 仅供审计，永远不可进入
Position Advice build、current publication、read action、Daily Brief 或 promotion sample。
完整 producer/consumer inventory、outer-envelope versioning 与 mixed-version 行为见 §9。

## 7. Slice R2 — Exact current and historical Combo membership

### 7.1 Group occurrence 与历史真源

`strategy_group_id` 是大小写敏感的 opaque id，只做 `str(...).strip()`；空值不是 group。
Membership 必须从 authoritative effective `trade_events` history 推导，不能只看当前
`position_lots`，因为 adjust event 可以改变投影字段。

在应用 authoritative voids 后，按稳定 event order 重放所有账户的 open/adjust events，定义：

```text
E              = {funding_put_record_id, participation_call_record_id}
G_current(g)   = 当前 projected strategy_group_id == g 的所有 lots（含 closed）
G_live(g)      = G_current(g) 中 contracts_open > 0 的 lots
H_ever(g)      = effective event history 中曾被赋予非空 g 的所有 record ids
R_touch(g)     = 从一个非空 group 改到不同非空 group、且 before/after 包含 g 的 reassign events
```

`unset -> g` 可作为 legacy 初次赋组；`h -> g`、`g -> h` 和 `g -> h -> g` 都属于 reuse/
retag conflict。被 authoritative void 的 event 不进入 effective history；void 本身及 resulting
membership change 会改变 history hash 和 decision fingerprint。

首次建立 identity 必须同时满足：

- `record_ids(G_current(g)) == E`；
- `record_ids(G_live(g)) == E`；
- `H_ever(g) == E`；
- `R_touch(g)` 为空，且 selected legs 没有从其它非空 group retag 到 `g`；
- 恰好一个 Funding Put role 和一个 Participation Call role；
- 两腿 `contracts == contracts_open == expected_contracts`；
- 保留现有 exact open-event/record binding、contract key、broker、account、symbol、
  currency、multiplier、strike/expiry 校验。

因此第三条 lot 即使已平仓后被 retag away，或 selected legs 后来被 retag 进一个曾用 id，
仍会被 `H_ever/R_touch` 发现并阻断。方案不禁止通用 adjust 命令，但身份采纳和 advice
对任何涉及非空 group reassignment 的 occurrence fail closed。

### 7.2 Writer transaction 与 replay

`adopt_existing_combo_identity_atomically()` 在同一 `BEGIN IMMEDIATE` 中按以下顺序：

1. 读取全部 effective events，完整重放 projection，与 persisted lots 逐字段比较；
2. 同时构建 `G_current/G_live/H_ever/R_touch` 并验证 §7.1；
3. 验证 selected records 与 immutable open events，构建 canonical candidate identity；
4. 读取 existing identity：
   - 不存在：要求当前/历史精确且两腿 fully open，然后 dry-run 或 insert；
   - 存在且自身 hash/canonical payload 有效、与 candidate 完全相同：仅在当前/历史
     membership 仍合法时返回 `existing`；
   - 存在但 identity 或 membership 无效/不同：返回 conflict；
5. apply insert 后做 identity readback、foreign-key 和 membership generation check。

构建 candidate 时 immutable `contracts` 必须等于 expected。Existing identity replay
允许绑定腿 `contracts_open` 为 `0..expected`，从而支持一腿 residual 或两腿 terminal；
首次 insert 仍只允许 `contracts_open == expected`。

Identity 永久保留；到期或 terminal 只使当期 advice/actionability 失效。Existing replay
仍要求 `G_current(g) == H_ever(g) == E` 且无 retag。新增第三腿、历史第三腿、绑定腿
改组或 group id reuse 均 conflict，不能因 identity 已存在而跳过。

### 7.3 Position Advice 读侧防御

V3 coherent snapshot 复用同一次全局 event replay，为当前账户所有 identity/group 生成
membership fact。`_group_structure_states()` 只有在 strict validator 通过、membership
`status=exact`、当前/历史 global counts 都为 2、current-account ids 精确等于 identity 的
两条 record ids、roles/strategy/account/symbol 全部匹配时，才可调用
`classify_combo_structure()`。

否则相关行统一 `review_required`，输出
`combo_group_membership_unverified`、`combo_group_membership_conflict` 或稳定的 retag/reuse
reason，并保证：

- `model_actionable=false`；
- 零 selected proposal；
- 零 combo leg plan/group operation。

`_apply_selected_proposal()` 必须再次确认 row 是 identity 绑定的 Funding Put record，
且它消费的 membership hash 与 plan/input 绑定值相同；同 group 的其它 Put 永远不能借用
identity 中的 Call。

### 7.4 Membership schema、canonical hash 与账户隔离

内部 resolver 可持有全局 raw members；account snapshot 只发布
`account_combo_group_membership.v1` allowlisted fact：

```text
membership_schema_version
group_id
status = exact | conflict
current_account_member_record_ids
global_current_member_count
global_historical_member_count
external_member_count
external_membership_hash
retag_event_count
retag_history_hash
cross_account_member_present
cross_symbol_member_present
member_bindings_for_current_account
reason_codes
membership_hash
```

不发布其他账户 record ids、event ids、strike、price、premium 或其它经济字段。
Canonical rules：

- group id 使用 §7.1 normalization；
- current-account ids 去重后按 Unicode code point 排序；
- bindings 按 `(record_id, role, open_event_id)` 排序；
- reason codes 去重后排序；
- internal global current/history/reassign tuples 先 canonical sort，再分别生成 redacted hash；
- `membership_hash` preimage 包含 schema version 和所有 allowlisted fields，但排除
  `membership_hash` 自身；
- 缺 schema、wrong hash、重复 binding、count/hash 不一致或非 canonical payload
  一律 conflict/fail closed。

`status=exact` 还要求 `external_member_count=0`、current/historical global counts 都等于
current-account ids 数量且为 2、`retag_event_count=0`。因此外部 redacted hash 只用于
conflict diagnostics/fingerprint drift，永远不是 actionability 的正面证明；consumer 可只靠
本账户 ids/bindings/counts 完整验证 exact path。

Event/lot/SQL 输入排列置换必须生成相同 membership hash 和 decision fingerprint。

## 8. Slice R3 — Typed per-case due isolation

### 8.1 异常分类

新增窄化 application exception `LifecycleCaseDataError`，只表示“该 case 的已持久化或冻结
业务字段无法规范化”：

```text
reason_code = lifecycle_close_reason_data_invalid
stage
field
original exception as __cause__ only
```

它不表示 repository unavailable、SQLite error、写权限缺失、CAS/preflight/invariant
冲突或 provider infrastructure failure。

仅在明确的单字段规范化点把 `TypeError/ValueError/OverflowError` 转换为
`LifecycleCaseDataError`，包括：

- anchor `received_at_ms/event_time_ms`；
- target manifest lot/quantity；
- observation 的结构、quantity 和时间字段；
- `resolve_allocations()` 对该 case 冻结输入的数据校验。

不得在 writer、repository 或整个 `reconcile_lifecycle_close_reason()` 外围把普通
`TypeError/ValueError/OverflowError` 整体改类，因为现有 writer 使用这些类型表示
transaction authority 和 ledger invariant 故障。

### 8.2 单案阶段边界

将单 case 处理显式分为：

```text
coherent fact read
  -> prepare/normalize case, anchor, timing and observation envelopes
  -> decide whether observation is due
  -> collect typed observation when required
  -> pure close-reason decision
  -> atomic ledger writer (apply only)
```

`_prepare_close_reason_reconciliation(...)` 是只读/无写入阶段。在任何 polled stock
settlement apply、case state advance、issue evidence、terminal allocation 或 Outbox 写入前，必须
完成 case/anchor/target/timing 和 observation candidate envelope 规范化。因此 malformed case 在
dry-run/apply 下都是零写入。Prepare 输出 immutable decision input 和
`expected_lifecycle_generation_token`；token 包含目标 case facts 以及所有会改变该 case
reservation arbitration 的 target-lot dependency hash，不用一个无关账户字段就使所有 case
虚假冲突。

Expected provider unavailable、coverage incomplete 或 unsupported endpoint 按父设计返回 typed
incomplete observation，不通过 exception 表示。Observation collector 的未分类 exception 向上
透传；移除 due loop 中的 `except Exception`。Collector response 只能补充 prepare 已声明的
target case/lot candidate；它不得改变 `case_id`、manifest 或 source ownership。Candidate 与
prepared binding 不一致时零 poll/write，返回 typed incomplete observation 和稳定
`settlement_observation_binding_mismatch`，不能让通用 settlement matcher 改写另一 case。

`reconcile_due_lifecycle_cases()` 只捕获 `LifecycleCaseDataError`，不捕获普通
`TypeError`、`ValueError`、`OverflowError`、`RuntimeError`、`sqlite3.Error`、`OSError`
或 `PermissionError`。

### 8.3 Prepare-to-writer generation/CAS

Provider collection 可以跨越较长时间，因此 prepare 的 token 必须由每个最终 writer
作为 required precondition 接收。覆盖范围包括：

- case state advance；
- issue evidence/source claim；
- polled settlement evidence apply；
- terminal event + allocation + projection update；
- 与上述 mutation 同 transaction 的 Outbox。

Apply writer 在同一 `BEGIN IMMEDIATE` 中、任何 insert/update/outbox 前：

1. 重读并规范化目标 case、evidence/claims/timing/allocations、target lots/effective events；
2. 重算目标 case resolution hash 和 target-lot reservation dependency/arbitration hash；
3. 重算 `lifecycle_generation_token.v1`，与 prepare token 做 exact compare；
4. 不同则抛 `LifecycleGenerationConflict`，零写入；相同才执行旧 decision。

Polled settlement writer 还必须在 transaction 内证明 candidate 精确绑定 prepared case/targets；
不能只凭重新搜索到的“唯一匹配 case”绕过 expected token。

`LifecycleGenerationConflict` 是 typed concurrency/CAS failure，不是
`LifecycleCaseDataError`，due loop 不捕获。Writer transaction 内不得在 mismatch 后重算
decision 并继续。Dry-run 在 collector 返回后也用 read-only transaction 做相同 compare；
若已 stale，同样 fail closed，不能输出看似仍可 apply 的旧结果。

Token 至少覆盖：case resolution revision/state、anchor resolution hash、effective allocations、
timing binding、target lot/event/void facts，以及所有 target manifest 与同一 target lots 相交的
未 supersede cases 及其 edge/anchor inputs/hash（包括当前 missing/零 reservation case）。
新 terminal allocation、新/void claim、bridge、case state、target-lot event 或竞争 reservation
均必须改变 token；完全无关 lot/case 的变化不应改变它。

### 8.4 Result contract 和 apply 语义

因 failure semantics 改变，聚合结果升级为 `due_lifecycle_reconciliation.v2`。
Case-local malformed 结果至少包含：

```text
case_id
status = needs_review
reason_codes = [lifecycle_close_reason_data_invalid]
failure_class = case_data
failure_stage
failure_field
write_status = not_attempted
apply_changes
```

公开结果不回显 raw payload；原异常只作 bounded audit diagnostic。Dry-run 和 apply 对
malformed case 的业务字段完全相同，只有 `apply_changes` 不同，并且均为零
event/allocation/state/Outbox 写入。

后续合法 case 继续执行。Writer、SQLite、权限、generation/CAS/preflight 或未分类 provider
异常向上透传并中断 batch，不进入 `results`。

Apply batch 仍是逐 case transaction，不是整批原子：若后面 case 发生基础设施失败，
前面已 commit 的合法 case 保留；重跑必须依靠现有 source claim、CAS、fingerprint 和
Outbox 幂等收敛。

## 9. Atomic v3 contract cutover

### 9.1 Closed producer/consumer inventory

V3 是一个不可拆分的 contract cutover。实施前用 `rg` 生成调用点清单并在 code review
逐项勾销，至少覆盖：

| Boundary | 必须修改/验证的 owner |
|---|---|
| Schema、canonicalizer、strict validator | `domain/domain/decision_state_fingerprint.py` |
| Coherent producer | `src/application/ledger/repository.py`、`lifecycle_overlay.py`、`decision_snapshot.py`、`api.py` |
| Ledger source receipt/payload | `position_advice_source_producers.py`、`position_advice_source_receipts.py` |
| Account source capture/binding | `position_advice_account_sources.py`、`position_advice_authority_binding.py`、`account_run.py`、`pipeline_context.py` |
| Immutable input/build/current | `position_advice_input_builder.py`、`position_advice_runner.py`、`position_advice_plan_builder.py`、`position_advice_current_repository.py` |
| Live context/read | `positions/context_builder.py`、`position_advice_reader.py`、`agent_tools/position_advice.py` |
| Promotion archive/replay/gate | `position_advice_promotion.py`、`position_advice_promotion_checks.py` |
| Daily Brief/notification boundary | `position_advice_notification_authority.py` 及只消费 validated v3 read output 的 Daily Brief、authority token 和 notification path |
| Contract/docs/fixtures | `docs/POSITION_ADVICE_V2_CONTRACT.md`、`docs/POSITION_ADVICE_COMPATIBILITY.md`、`docs/STRATEGY_ARCHITECTURE.md` 和所有 v2 snapshot fixtures |

为让 previous binary 凭 outer schema/filename 就能拒绝新 artifact，而不是依赖它尚不存在的
nested validator，原子 cutover 精确升级以下 public envelopes：

```text
position_advice_ledger_source.v2
position_advice_account_sources.v3
position_advice_input.v3
position_advice.output.v3
account_decision_current.v3
position_advice_read.output.v3
position_advice_promotion_checks.v2
```

Current pointer 使用新的 `.v3.json` path；new code 只把 v2 pointer/artifacts 作为历史审计，
old code 不识别 v3 pointer/output/input。`position_advice_source_receipt.v1`、source manifest v2、
authority policy v1 和 promotion evidence/gate outer schema 可保持不变，因为它们已经显式绑定
producer/checks schema/hash；但 validators 必须拒绝其中的旧 nested versions。Authority mode
仍为现有 `v1 | v2_shadow | v2`，artifact schema v3 不新增另一套 advice authority。

保留 `docs/POSITION_ADVICE_V2_CONTRACT.md` 文件名和 authority mode 命名，避免把 artifact
revision 误解为新 advice authority；更新其 public contract table，明确 Position Advice v2
family 的 current immutable artifact revision 已切到 v3。Compatibility 文档同步写明 v2
artifact 是历史审计、不是 fallback。

Source manifest/receipt 必须绑定新的 producer schema/policy hash。旧 v2 snapshot sample、旧
promotion checks、aggregate counters 和 ready gate 只能审计，不能与 v3 sample 混算。新
evidence 必须从内容寻址 archive 中的完整 v3 plan/input pairs 重放生成，不信任旧 aggregate
自述。

### 9.2 One validator, no partial acceptance

§6.6 strict validator 必须被所有表中 producer/consumer 调用。验证顺序固定：

1. exact snapshot/fingerprint schema；
2. required top-level account/scope/trust fields；
3. required lifecycle resolution/arbitration 和 membership 子契约；
4. nested hash、count、canonical order 和 identity binding；
5. 排除 stored fingerprint 后重算 top-level fingerprint；
6. 与 input/plan/current/live expected fingerprint 做 exact compare。

任一步失败，producer 不发布，reader/promotion 返回稳定 non-actionable reason；不得把缺失
lists 当空集，不得仅凭 `trusted/actionable/len==64` 通过。

### 9.3 Mixed-version 与 rollback matrix

| Runtime / artifact | Required result |
|---|---|
| new code + v2 snapshot/current | 历史可读；build/read/promotion/Daily Brief action 全部 fail closed，等待下一次完整 Account Run |
| new code + malformed/partial v3 | producer 零发布；reader/promotion 零 action，明确 contract reason |
| new code + valid v3、live fingerprint 相同 | 才可按现有 authority mode继续 |
| new code + valid v3、live fingerprint 不同 | stale；零 action，不做字段级 fallback |
| old code + new v3 artifact | old code 不识别 v3 pointer/input/output；代码降级前仍必须先 expected-hash CAS 到 `v1`，使它可能看见的 stale v2 pointer 因 generation/policy mismatch 零 action，再停旧进程并切换 release |
| mixed old/new processes | 禁止；controlled service swap 必须证明旧进程退出后才启动新进程，CLI/operator 不得跨 release 复用 apply intent |

Previous-release compatibility harness 至少验证旧 reader 不发现 v3 current path/schema，且在
authority 已 CAS 到 v1 后，即使 legacy v2 pointer 仍存在也零 v2 action。无法运行 previous
binary 时，release gate 必须把该项列为人工阻断，而不是假设兼容。本方案本身不授权 CAS、
release 或 deployment。

Cutover 不重写、不删除 v2 immutable artifacts/current pointer。新 code 只在一次完整成功的
Account Run 后原子发布 v3 current；在此之前 Position Advice v2 authority 可暂时 unavailable，
但不得 fallback 到 v2 artifact。Retention/cleanup 必须同时识别仍受保护的 v2 historical refs
和 v3 current refs。

### 9.4 Freeze gate

只有 R1 resolver、R2 membership producer、strict validator、所有 consumers、promotion replay、
docs 和 fixtures 同时就绪后，才冻结 v3 bytes。Freeze 前字段可在同一未发布 work unit 内
调整；freeze 后任何 semantic/required-field/hash-preimage 变化都必须新 schema version。

## 10. Implementation sequencing

### Slice A — Additive R1 readers/resolvers

- 新增 coherent raw reader、direct/bridge resolver、account arbitration 和 generation token；
- 保留现有生产 call sites 与 legacy rebasing，避免尚无 v3 consumer 时提前切换；
- 用纯单元/fixture 测试证明新 resolver contract，不改变 public artifact。

### Slice B — Additive R2 history/membership producer and writer guard

- 实现 effective event history membership、canonical hash 和 adoption transaction guard；
- snapshot 暂不发布未冻结字段；Position Advice 暂不消费半成品 membership；
- writer enforcement 可独立通过 focused tests，但不得单独 release 本 remediation work unit。

### Slice C — Atomic v3 integration/cutover

- 同一 slice 更新 snapshot producer、source producer、全部 build/read/promotion consumers、
  fixtures 和 §9.1 docs；
- 在这一 slice 才将 live reconciliation、settlement observation、positions context 全部切到
  shared account resolver，并冻结 R1/R2 facts；
- 同一 slice 内禁止先提交“缺 membership 的 v3”或让任何 consumer 接受 partial v3。

### Slice D — Retire old paths

- `rg` 证明所有 call sites 已切换后，删除多 getter 拼接、legacy evidence rebasing 和
  context-builder overlap second resolver；
- 删除仅服务旧路径的 tests/fixtures 前，先把其行为断言迁到新 facade；
- 这是 cutover 后的机械清理，不改变冻结 contract。

### Slice E — R3 due isolation and generation CAS

- 在 coherent resolver/generation API 稳定后修改 `close_reason_reconciliation.py`；
- 一次完成 prepare typing、writer token precondition 和 due-loop catch narrowing；
- 不改 v3 snapshot 或 Combo membership contract。

### Aggregate validation

- 每个 slice 的 focused tests 与静态检查通过；A/B 中间态不是可发布版本；
- C 完成前不得对外产生 v3 artifact，C 后不得走旧 resolver；
- 不允许为让聚合测试通过而放宽任一 fail-closed assertion；
- 实现完成后运行新的 aggregate DeepReview，不复用本 PlanReview 作为代码验收。

## 11. Test matrix

### 11.1 R1 lifecycle anchor/snapshot

1. Valid bridge 的 live model 与 frozen snapshot model 逐字段一致：
   `closure_fact`、`reason_state`、`lifecycle_state`、reservation/remaining、
   deadline、anchor ids、reason codes 和 actionability。
2. 同 fixture 在 pairing/deadline 前后均等价；仅 `checked_at` 改变不改 decision fingerprint。
3. Direct 多 anchor manifests disjoint 时按最早 immutable receive time 配对；相同 evidence id
   的 insert-once replay 去重，live/snapshot 等价。
4. Direct missing/multiple claim、same source key/different evidence/hash、overlapping manifests、超量、
   非 target lot 或 direct+bridge：整个 case conflict，零 reservation。
5. Bridge owner/role/hash/account/Futu account/quantity/reference/supersession 不符：两入口
   得到同一 conflict，零未验证 reservation。
6. Direct+direct、direct+bridge、bridge+bridge 和 multi-lot partial overlap：account graph
   将所有 involved cases 同步 conflict，完整 manifests 零 reservation/action；无关 component 继续。
7. 无 anchor/无 bridge 保持 `missing/not_started`。
8. Rows/cases/edges 顺序变化 resolution/arbitration/fingerprint 不变；claim/bridge/overlap
   generation 变化 fingerprint 必变。
9. 并发 claim/bridge 写入期间，reader 只能看到完整 before 或 after generation。
10. Valid bridge 的 Position Advice 不再出现 `evidence_without_allocation`，且零 action proposal。

### 11.2 R2 Combo identity

1. Extra open Put、extra open Call、duplicate role、extra closed lot、跨账户、跨 symbol：
   首次 adoption 全部失败且零 identity 写入。
2. 第三条 closed lot 随后 retag away、selected leg 从其它 group retag into g、g 被
   `g -> h -> g` 重用：current projection 即使恰为 E，仍因 history conflict 零写入。
3. Selected leg partial/closed 时首次 adoption 失败。
4. Dry-run 成功后新增第三条 lot 或 retag event，apply 重验并失败。
5. Identity 建立后一腿 closed 或两腿 closed、无 history drift 时相同参数 replay 为 `existing`。
6. Existing identity 无效、参数不同、新增/历史第三腿或 identity 后 reassignment：冲突。
7. Valid identity + extra current/historical member：Position Advice 所有相关行 review，零 proposal/leg plan。
8. Exact active pair 保留现有 group plan；exact bound residual 保留 residual 分类；
   exact closed pair 无 open advice。
9. Membership fact 缺失、wrong schema/hash/count/binding 或 non-canonical order 时 fail closed。
10. Event/lot 输入全排列保持 membership/fingerprint 不变。
11. 跨账户碰撞改变 count/hash/fingerprint，但快照没有其他账户 ids、event ids 或经济字段。

### 11.3 R3 due isolation and generation CAS

1. 两个 due case：第一个 malformed `event_time_ms`，第二个合法；
   dry-run/apply 都返回第一个稳定 review result 并继续第二个。
2. Malformed case 在 dry-run/apply 中零 poll/state/evidence/event/allocation/Outbox 写入。
3. Writer 抛 transaction-authority `TypeError`、普通 `ValueError`、`LedgerPreflightError`：全部透传。
4. Repository `sqlite3.OperationalError`、`PermissionError` 或未分类 collector `RuntimeError`：
   全部透传，不处理后续 case。
5. Typed incomplete observation 保持现有 `needs_review`，不通过 exception 流转。
6. Collector 返回另一 case/lot binding：稳定 incomplete reason，通用 settlement matcher
   零调用、两边 case 均零写。
7. Barrier 双 worker：A prepare/collect 时 B 提交 terminal、new allocation、new/void claim、
   bridge 或竞争 reservation；A 的 dry-run/apply generation compare 均失败且零写。
8. 完全无关 lot/case 的 commit 不改变 A token；A 仍可按原 decision 安全提交。
9. 每个 state/issue/settlement/terminal-allocation/Outbox writer 缺 token、错 token均在首写前失败。
10. 前一合法 case 已 commit、后一 case 基础设施失败：前者保留，重跑零重复事件/
   allocation/Outbox。
11. CLI aggregate write flag 准确区分“只有 malformed”与“malformed + valid apply”。

### 11.4 V3 contract and compatibility

1. Input builder、ledger source publisher、account source capture、runner/plan builder、reader、
   positions context、promotion archive/replay 各入口分别测试：v2、partial v3、wrong nested hash、
   wrong top-level fingerprint 均 fail closed。
2. Valid v3 从 producer bytes 到 input/plan/current/read replay 只使用一个 strict validator，
   live fingerprint mismatch 零 action。
3. Source producer schema/policy hash 未升级或 manifest 混用旧 ledger source 时拒绝 adoption。
4. 旧 promotion samples/counters/gate 不计入 v3 ready；从完整 v3 archive replay 后才生成
   新 evaluator evidence。
5. Previous-release compatibility fixture：v3 artifact 在 authority expected-hash CAS 到 v1 后，
   旧 reader/Daily Brief 零 v2 action；未先 CAS 的 downgrade 被 release checklist 阻断。
6. Contract docs 中 schema 表、mixed-version matrix、rollout/rollback 顺序与 executable
   constants/fixtures 一致。

### 11.5 Aggregate gates

- Focused suites：
  - `tests/test_lifecycle_redesign_contracts.py`
  - `tests/test_settlement_observation.py`
  - `tests/test_position_advice_v2_ledger.py`
  - `tests/test_position_advice_v2_input_builder.py`
  - `tests/test_position_advice_source_producers.py`
  - `tests/test_position_advice_account_sources.py`
  - `tests/test_position_advice_runner.py`
  - `tests/test_position_advice_promotion.py`
  - `tests/test_position_advice_plan_builder.py`
  - `tests/test_option_positions_cli.py`
  - decision snapshot/fingerprint 相关 tests
- 受影响 Python 文件 Ruff 通过。
- 若 import graph 变化，先用 `python3.12 scripts/generate_dependency_graph.py` 重生，
  再运行 `--check`。
- `git diff --check` 通过。
- `python3.12 -m pytest -q -p no:cacheprovider` 全量通过。
- Full projection replay fixture 零 mismatch。

## 12. Success criteria

1. Valid migration bridge 在 live/snapshot 中产生相同 closure/reservation/reason facts，且 legacy
   source owner 没有被改变。
2. Bridge/source claim 的存在、缺失或冲突被 decision fingerprint v3 覆盖；旧 v2
   artifact 不可 promotion。
3. Valid bridge 不再被 Position Advice 报为 `evidence_without_allocation`；无效 bridge 只封锁
   相关 case 且零未验证 reservation。
4. 同一 target lot 的跨 case reservation 在 live/snapshot 中由同一 account graph 仲裁；
   overlap cases 全部零 reservation/action，入口不再分叉。
5. 首次 Combo adoption 的当前与历史 group 成员都恰好是两条 selected fully-open legs；任何
   额外 open/closed/retagged/跨账户成员都零写入失败。
6. 已存在 identity 可在合法 residual/closed lifecycle 后幂等 replay，但 membership 漂移
   必须 conflict。
7. Position Advice 只对 exact membership 的 identity 产生 combo action，且只有绑定
   Funding Put 可发起 group operation。
8. Malformed case 在 dry-run/apply 均零写入、输出稳定 review result、不阻断后续
   合法 case。
9. Provider collect 窗口内 relevant generation 改变时所有 writer 零写失败；无关 case
   不制造虚假 CAS。
10. Infrastructure/writer 故障不被伪装成 case review；逐 case commit 后重跑仍幂等。
11. 所有 producer/consumer 只接受 strict v3；旧 promotion aggregate 不复用，mixed-version
   rollback 按 §9.3 fail closed。
12. 全量测试、Ruff、dependency graph、projection replay 和 whitespace gate 全部通过。

## 13. Residual risks 与追踪边界

- **存量错误 identity**: 读侧 guard 会 fail closed，但本方案不自动删除/修正已写
  identity。发现后单独建立 operator correction work unit。
- **Legacy multi-anchor**: bridge v1 不支持多 anchor，保持 conflict/review-only；如有实际需求，
  以新 contract 单独设计。
- **Malformed case 重复扫描**: 本 slice 不为数据错误新增可写 issue 状态；定时器可能
  在后续 tick 再次报告同一 case，但零经济/通知写入且必须保持稳定 reason code。
  若需要持久化 suppress/ack，单独设计，不在本修复中暗加 mutable flag。
- **逐 case apply 非整批原子**: 前面 case 可能已 commit 而后面基础设施失败；依靠现有
  幂等契约重跑，不为此引入跨 case 大事务。
- **版本偏差**: v3 producer/consumer 必须在同一 release 中切换；旧 artifact 只读。
- **旧 binary downgrade**: v3 不是为 authority 仍处于 `v2` 时的 in-place code downgrade
  设计；必须先走现有 authority CAS 回滚并验证零 action。自动兼容旧 binary 不在本 work unit。
- **生产事实**: 本方案不证明生产中当前 bridge、identity 或 due case 的数量和状态。
  任何生产 inventory、迁移、修正或服务恢复均需独立读先和授权。

## 14. 交付边界

PlanReview 通过只证明本文可交给 implementation agent，不证明当前 workspace
代码已修复。后续边界依次独立：

```text
plan accepted
  -> implementation
  -> focused/full validation
  -> aggregate DeepReview
  -> commit/push (separate authority)
  -> release (separate authority)
  -> production upgrade/migration/data correction/service restore (separate authority)
```
