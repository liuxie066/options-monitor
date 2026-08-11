# 成交后 Combo 反向归组 V1 实施方案

> 范围收窄说明（2026-08-11）：本计划中的 staggered-expiry（错期）反向归组范围已不再实施。
> 组合收益策略只保留 same-expiry（同期）结构；错期代码的删除见 work unit
> `combo-yield-remove-staggered-ledger`。以下历史内容仅作记录，不代表现行行为。

## 1. 目标与成功标准

本方案把 Combo 识别从“下单前声明组合意图”改为“成交落账后，根据两条已成交 open lot 反向推理，再由用户确认后写入 canonical Combo 身份”。

V1 成功标准：

- Long Call 或 Sell Put 任一条腿先成交，都先作为独立开仓事实落账，不阻断交易。
- 后续另一条腿成交后，系统从仍未归组的 open lot 中重新匹配；结果不依赖成交到达顺序。
- `candidate_pair_id` 只作为推荐标识；只有它对应的候选在成交前已进入有效 Brief 曝光，且精确命中两条合约时，才构成候选证据。
- 所有 V1 Combo 都必须经过用户对“这两个精确 lot”的确认，系统不得仅凭相似度自动写入 canonical Combo。
- 确认、拒绝、纠错均幂等、可审计；确认写入和纠错撤销均在单个 SQLite 事务内完成。
- V1 仅处理一条完整 open Put lot 与一条完整 open Call lot、数量相等的组合；partial fill、拆分/合并 lot、滚动续接不进入自动推理。
- 推理或确认失败不得回滚、改写或延迟已经成功落账的交易。

## 2. 范围

### 2.1 本期包含

- 新成交 open lot 的成交后 Combo 推理。
- Long Call 先成交、Sell Put 后成交，以及相反顺序。
- same-expiry 与 staggered-expiry 两种现有 Combo Yield 结构。
- 候选 occurrence、Brief exposure、delivery confirmation 三层证据。
- 确定性匹配、歧义输出、用户确认/拒绝/选择另一条腿。
- canonical Combo 身份的原子写入与 append-only 纠错。
- 自动成交入口、历史成交 backfill 完成点和周期 sweep 三个重试入口。
- 现有 CLI 下的只读检查、dry-run、确认和纠错操作面。

### 2.2 本期不包含

- 下单、改单、撤单或任何 broker-facing 行为。
- 根据收益率、价格接近度或时间距离自动决定组合。
- 未经用户确认自动写入 Combo 身份。
- partial fill 的拆 lot、多个 lot 合并为一条腿、跨日旧仓作为新 Combo funding leg。
- Combo rolling、已平仓腿的重新配对、历史 canonical Combo 的批量迁移。
- 新的飞书交互卡片或真实通知发送。
- release、部署、生产配置启用或生产数据回填。

现有显式 `pair_intent_id` 调用保留为兼容路径，但普通成交不再要求用户预先提供该字段；该显式路径视为调用方已经给出的组合授权，不属于 V1 的“成交后推理”状态机。历史已归组记录保持原样，不自动重算。

## 3. 当前问题与目标边界

当前 resolver 会在单笔 open trade 处理中尝试补齐 Combo 标签：Long Call 可能在没有另一条腿时就被标成 Combo，Sell Put 则依赖当时是否能唯一找到 Long Call。这把“交易事实落账”和“组合关系判定”绑在了一起，也让结果受成交顺序影响。

V1 把职责拆成三层：

1. trade intake 只确认并持久化成交事实；
2. post-trade reconciler 从 canonical open lots 生成可审计的配对推理；
3. 用户确认后，ledger command 才原子写入两条 adoption adjustment 和一条 Combo identity。

## 4. Canonical 身份与不变量

现有 `combo_identity.v1` 继续作为 canonical 身份，不新增平行身份体系：

- exactly one `funding_put` member；
- exactly one `participation_call` member；
- 两条成员数量均为正且相等；
- identity 精确保存两条腿的 `record_id` 与 `open_event_id`；
- identity insert-only；后续纠错不覆盖或删除旧 identity。

新归组使用稳定的 `strategy_group_id`：

```text
combo-post-trade:v1:<sha256(account, put_open_event_id, call_open_event_id)>
```

它由精确 open event 对生成，而不是由 symbol/expiry 粗粒度生成，因此相同标的、相同到期日的多组并存不会碰撞。

canonical membership 的有效性继续由当前 membership history 判定：两条 adoption adjustment 必须仍然有效、角色和数量必须精确、不得出现有效 retag。identity 行本身存在但当前 membership 已不精确时，下游必须视为非有效 Combo。

## 5. 成交事实入口修改

### 5.1 resolver 责任收窄

在 `src/application/trades/resolver.py` 中：

- 普通 Long Call / Sell Put open trade 只产生独立开仓 payload；
- position effect 的判断与 Combo 策略标签解耦；
- 不再由 same-expiry 的“当前唯一另一条腿”直接赋予 `strategy_group_id`、`strategy_type` 或 `leg_role`；
- 现有显式 `pair_intent_id` 路径保留兼容，不成为普通用户流程的前置条件。

这一步与 post-trade reconciler 接入必须在同一 work unit 内交付，避免新交易既被旧 resolver 预归组、又进入新推理。

### 5.2 推理触发点

新增 `src/application/trades/combo_reconciliation.py`，在下列三个时点按 account 触发：

- 单笔 open trade 已成功 commit 且 intake 已得到 applied 结果后；
- 历史 backfill 完成一个 batch 后，仅触发一次；backfill 内每条 deal 不单独触发；
- listener 的独立 60 秒周期 sweep。

推理始终发生在交易事务之后的独立事务中。其异常只写入结构化诊断和 listener 状态，不改变已经成功的 trade result、receipt 或 projection。

同进程沿用现有 `process_lock`；跨进程的 proposal upsert、确认和纠错由 SQLite `BEGIN IMMEDIATE` 与事务内重查保证。

## 6. 未分组池的定义

未分组池每次从 ledger 当前事实派生，不维护第二份仓位状态。候选 lot 必须同时满足：

- account、broker/runtime environment 与 canonical symbol 一致；
- 原始 open event 为已确认的开仓成交；
- 当前 quantity 大于 0，且等于该 open lot 的原始 open quantity；
- Put 腿为 Sell Put / short put，Call 腿为 Long Call；
- 当前没有有效 `strategy_group_id`、`strategy_type`、`leg_role`；
- 没有被有效 Combo identity 或已经 `user_confirmed` 的 inference claim 占用；未决 proposal 不预占腿；
- currency、multiplier 相同，数量严格相等；
- Put strike 小于 Call strike；
- same-expiry：Put expiry 等于 Call expiry；
- staggered-expiry：Put expiry 早于 Call expiry；
- 两条 open event 的市场交易日期相同。

市场交易日期必须由 canonical event time 转换得到，并复用 `domain/domain/config_contract.py` 的市场时区权威：US=`America/New_York`、HK=`Asia/Hong_Kong`；不得使用机器本地日期或 intake 日期。market 或 event time 无法确定时 fail closed。

任一 lot 已部分平仓、数量不相等、字段缺失或时间无法规范化时均 fail closed，不生成可确认 proposal。

Long Call 先成交时，第一次 sweep 只把它作为派生的 `waiting_for_counterpart` 展示；Sell Put 到来后，下次 post-commit reconcile 会重新读取整个未分组池并计算配对，不依赖第一次处理留下的内存状态。

## 7. 候选证据链

### 7.1 不把 `candidate_pair_id` 当作成交关系证明

现有 `candidate_pair_id` 主要由 symbol 与 Put/Call 合约代码组成，能表达“推荐的是哪一对合约”，但不能单独证明：

- 它属于哪个 account、run 或 Brief revision；
- 推荐发生在成交之前；
- 推荐在两条腿成交时仍有效；
- 用户实际看到了该推荐；
- 同合约多 lot 中具体是哪两个 open event。

因此它保留为 recommendation key，不直接成为 canonical Combo identity，也不能单独触发自动归组。

### 7.2 Candidate occurrence

在 `src/application/combo_yield_steps.py` 发布 account/run-scoped candidate CSV 时，为每个 pair row 附加不可变 occurrence 字段。domain candidate engine 不负责路径、run 或时钟。

`candidate_occurrence_id` 使用 canonical JSON 的 SHA-256，identity payload 至少包含：

```text
schema_version = combo_candidate_occurrence.v1
account
market
run_id
candidate_pair_id
structure_mode
canonical put contract key: underlying, option_type, expiry, strike
canonical call contract key: underlying, option_type, expiry, strike
currency
multiplier
```

`row_content_hash` 作为独立审计字段保存，不参与 occurrence ID：它对发布前的完整 flat candidate row（排除 occurrence 自身三个字段）做 sorted-key JSON、NaN/正负无穷转 null、Decimal/float 转 canonical decimal string 后计算。这样 occurrence 身份不被报价展示字段绑死，但 frozen row 仍可做完整性核验。occurrence payload 同时保存 `generated_at_utc` 和可取得的 `data_as_of_utc`，有效期由 Brief exposure 负责，避免 occurrence 与 Brief digest 循环依赖。

### 7.3 Brief exposure

occurrence 字段保存在 frozen Brief 现有 `candidates.combo_yield` row 中；不把它加入 `candidate_index.representative` 的固定字段，也不改变 coarse action identity。生成 exposure 时，用实际渲染 representative 的 canonical Put/Call contract keys 回连同一 revision 的 candidate rows；只有唯一命中的 occurrence 才能形成 exposure，多命中或缺失均 fail closed 为 structural-only。

成功且 actionable 的 Brief revision 由 immutable Brief facts 确定性派生：

```text
candidate_exposure_id = sha256(
  schema_version,
  candidate_occurrence_id,
  brief_id,
  revision,
  generated_at_utc,
  data_as_of_utc,
  valid_until_utc,
  actionability
)
```

只有实际进入该 revision 渲染内容的 occurrence 才能形成 exposure；仅存在于原始 CSV、未进入 Brief 的候选不算曝光证据。blocked/degraded 且不可行动的 Brief 不形成有效 exposure。旧 revision 没有 occurrence 字段时仍可正常读取，但只能得到 structural-only 证据。

现有 coarse `candidate_identities` 继续用于 symbol/strategy 级投递去重，不改变其语义；新增的 occurrence/exposure 字段只服务于精确证据和审计。

### 7.4 Delivery confirmation

delivery envelope 在现有可扩展的 `render_context` 中保存该消息实际包含的 `candidate_occurrence_ids` 与 `candidate_exposure_ids`，并继续用 `source_digest` 绑定 frozen Brief revision；不修改现有 delivery key 或 coarse `candidate_identities`。只有 provider 流程已有的 confirmed delivery 状态、digest 匹配、render context 与 source revision 可重算一致，才升级为 `exact_delivered_candidate`。

现有 normalizer 会保留 candidate row 的附加字段和 `render_context` mapping，因此这里采用 additive payload，不升级 Daily Brief/delivery schema；旧 envelope 缺少这些键时安全降级为未确认投递证据，不能反向猜测。

同一精确合约 pair 若在多个有效 revision 中曝光，只合并为同一条 lot edge 的有序 evidence list，不因此制造多条配对关系。

### 7.5 时间命中规则

候选曝光精确命中必须满足：

```text
exposure.generated_at <= min(put_trade_time, call_trade_time)
max(put_trade_time, call_trade_time) <= exposure.valid_until
account / market / canonical contract keys / currency / multiplier 全部相等
```

比较使用 canonical event time；缺失、未来曝光、过期曝光或 ingest time 替代 event time 都不得算精确命中。

## 8. 确定性匹配算法

新增纯 domain matcher，例如 `domain/domain/combo_reconciliation.py`。输入是已规范化 lot facts 与 exposure facts，输出 edge、evidence 和 ambiguity，不读文件、不访问 SQLite、不使用当前时钟。

### 8.1 Evidence grade

- `exact_delivered_candidate`：精确 exposure 且 delivery confirmed；
- `exact_live_candidate`：精确 exposure 有效，但无 confirmed delivery；
- `structural_only`：所有硬约束成立，但没有精确 exposure；
- `insufficient`：任一硬约束失败，不进入匹配图。

V1 中前三类都只生成待用户确认 proposal，任何等级都不自动写 canonical 身份。

### 8.2 全局求解

对同一 account/symbol/market-date 建立 Put-Call 二分图。求解采用离散、可解释的字典序目标：

1. 最大化 `exact_delivered_candidate` edge 数；
2. 再最大化 `exact_live_candidate` edge 数；
3. 再最大化总匹配数。

不使用收益率、成交价差、时间距离或任意连续分数，也不以数据库读取顺序、合约代码排序作为业务 tie-breaker。

只有出现在所有字典序最优 matching 中的 edge 才标记为 `proposal_ready`；其余合法 edge 标记为 `ambiguous` 并列出精确 alternatives。实现可通过“先求最优目标，再逐 edge 禁用重算”判断 forced edge；单一 account/symbol/day 图规模有限，不引入新的图计算依赖。

输入 lot 顺序、成交到达顺序或重复 sweep 不得改变结果。

## 9. 推理状态模型

在现有 ledger SQLite 中新增 `combo_pair_inferences`，它保存推理与人审状态，不替代 trade events、projection 或 combo identity。

核心字段：

```text
inference_id                 # sha256(v1, put_open_event_id, call_open_event_id)
schema_version
algorithm_version
account, symbol, market_date
put_record_id, put_open_event_id
call_record_id, call_open_event_id
evidence_grade
candidate_occurrence_ids_json
candidate_exposure_ids_json
input_snapshot_hash
status
proposal_expires_at_ms
evidence_json
alternatives_json
created_at_ms, updated_at_ms
decision_at_ms, decision_by, decision_reason
strategy_group_id, identity_hash
put_adoption_event_id, call_adoption_event_id
```

允许状态：

- `proposal_ready`
- `ambiguous`
- `user_confirmed`
- `user_rejected`
- `expired_unresolved`
- `superseded`

`waiting_for_counterpart` 只是在 read model 中从未分组池派生，不写 singleton inference row。

状态规则：

- inference ID 绑定精确 open-event pair；重复 sweep 幂等更新仍未决 inference 的证据与 snapshot。
- 已 `user_rejected` 的精确 pair 不因后续 sweep 自动重开；两条腿仍可与其他未被占用 lot 形成新 inference。
- `user_confirmed` 只能通过显式纠错进入 `superseded`。
- facts 漂移、proposal 过期或任一腿被占用时，未决 inference 进入 `expired_unresolved`。
- exposure 的 `valid_until` 只判断两条成交发生时证据是否有效，不作为用户确认截止时间。
- 所有未决 inference 的 `proposal_expires_at_ms` 固定为第二条腿 canonical event time 后 24 小时；超过该窗口转为 `expired_unresolved`，仍需配对时走显式人工流程。

`input_snapshot_hash` 必须覆盖 algorithm/schema version、两条精确 record/open-event ID、account/symbol、option type/side、当前与原始 quantity、currency/multiplier/strike/expiry/event time、当前 strategy membership、evidence grade、排序后的 occurrence/exposure IDs、proposal status/expiry，以及排序后的 alternative inference IDs；排除展示文案、created/updated timestamp 与 actor。这样任何会改变用户决定含义的事实都会使旧确认 fail closed。

未决的 `proposal_ready` 与 `ambiguous` 都不占用腿，因此用户可以选择 alternative。为避免并发重复认领，确认事务中对 Put 和 Call record/event 做事务内重查；数据库分别增加只覆盖 `user_confirmed` 的 Put/Call partial unique index。两个并发确认由 `BEGIN IMMEDIATE` 串行化，后进入者在写入前看到已有 confirmed claim 并 fail closed。

## 10. 用户确认接口

V1 复用 `./om option-positions` facade，不新增通知通道：

```text
./om option-positions combo-reconcile --account <account> --dry-run
./om option-positions combo-inferences --account <account> --status <status>
./om option-positions confirm-combo --inference-id <id> --expected-input-hash <hash> --dry-run
./om option-positions confirm-combo --inference-id <id> --expected-input-hash <hash> --apply
./om option-positions reject-combo --inference-id <id> --expected-input-hash <hash> --reason <text>
./om option-positions supersede-combo --inference-id <id> --reason <text> --dry-run|--apply
```

交互语义：

- “是”：确认当前精确 inference；
- “不是”：拒绝当前精确 inference；
- “选择另一条腿”：从 `ambiguous` alternatives 中选择另一个 inference ID 后确认；
- “稍后”：不写决定，等待后续读取；超过 expiry 后自动失效。

所有 write 命令先支持 dry-run；`--apply` 必须带 `expected-input-hash`，防止用户确认期间 facts 已变化。CLI 只做参数适配，业务命令落在 ledger/application facade。

## 11. 原子确认写入

新增 `adopt_post_trade_combo_pair()` command，复用现有 repository transaction、adjustment event、projection 和 identity 能力，但不能通过两个独立 public write 调用拼接。

单个 `BEGIN IMMEDIATE` 事务中必须按顺序完成：

1. 读取 inference，校验状态、expiry 与 `expected-input-hash`；
2. 重查两条 record/open event 的 account、symbol、quantity、open 状态和未归组状态；
3. 重查没有其他 active/confirmed claim；
4. 计算稳定 `strategy_group_id` 与两条确定性 adoption event ID；
5. 写 Put adjustment：`strategy_type=combo_yield`、`leg_role=funding_put`；
6. 写 Call adjustment：`strategy_type=combo_yield`、`leg_role=participation_call`；
7. replay projection 并验证 exact membership；
8. insert-only 写入 `combo_identity.v1`；
9. 把 inference 更新为 `user_confirmed` 并保存 event IDs、identity hash、actor 与时间；
10. commit。

任一步失败全部 rollback。重复相同确认返回已有成功结果；同 inference 不得创建第二组 adjustment event 或 identity。确认成功后，其他共享任一腿的未决 inference 由后续 sweep 标记为 `expired_unresolved`。

## 12. 拒绝与纠错

### 12.1 拒绝

拒绝只更新精确 inference 为 `user_rejected`，不改 trade events、position lots 或其他 pair。原因与 actor 必须审计保存。

### 12.2 已确认后的纠错

新增 `supersede_post_trade_combo_pair()`，在一个事务中：

1. 精确读取已确认 inference 与 identity；
2. 验证两条 adoption adjustment 仍是该 inference 的有效成员；
3. 为两条 adoption adjustment 分别写确定性 append-only void event；
4. replay projection；
5. 验证旧 group 已不再具有 exact effective membership；
6. 将 inference 更新为 `superseded`，保存原因、actor 与 void event IDs；
7. commit。

旧 identity 不删除、不覆盖；下游通过 effective membership 校验忽略它。纠错后两条仍 open 且符合条件的 lot 可参与新的推理。两个 void 不能通过现有单事件 public intervention 分两次提交，以免形成半撤销状态。

## 13. 运行模式与安全启用

为避免发布即改变 canonical 状态或把一个账户的决定扩散到另一个账户，增加按 account 显式配置的最小开关：

```text
trade_intake.combo_reconciliation.default_mode = off
trade_intake.combo_reconciliation.accounts.<lowercase-account> = off | observe | confirm
```

- 未列出的 account 继承 `default_mode=off`；生产配置中不允许把 default 改成非 off。
- `off`：该 account 不自动运行；保持兼容。
- `observe`：自动计算并持久化 inference，但不允许 apply canonical adoption。
- `confirm`：允许用户通过显式确认命令 apply；仍不存在自动确认模式。

confirm 命令先从 inference 读取 account，再校验该 account 的 effective mode。该 mode 只控制自动 reconciler 与新的 confirm adoption：read-only 查询、dry-run 和对既有错误确认执行 append-only supersede 不得被 `off` 阻断，以保证随时可审计和纠错。配置中的 account 必须是当前 runtime 已映射的 lowercase account，未知项 fail closed。

周期为代码固定的 60 秒，不增加额外调参项。该配置进入现有 config validation 与生成快照；任何实际生产启用均属于独立授权，不在本方案执行范围内。

## 14. 实施切片

### Slice 1 — 契约、纯 matcher 与 persistence

- 定义 occurrence/exposure/inference schema version 与 canonical hashing。
- 实现纯 domain hard gates、evidence grading、字典序 matching 和 forced-edge 判断。
- 增加 `combo_pair_inferences` migration、repository 查询/upsert 和 claim 约束。
- 提供 read-only/dry-run reconcile facade，不接自动 intake。

退出条件：乱序输入、Long Call-first、Put-first、多解、过期和缺字段测试全部确定性通过；无 canonical 写入。

### Slice 2 — Candidate occurrence 与 Brief evidence

- 在 candidate publication 边界附加 occurrence。
- occurrence 只进入现有 frozen candidate row；renderer/persistence 通过 canonical contract keys 唯一回连，并把已渲染 occurrence/exposure 写入现有 delivery `render_context`。
- 保持现有 coarse candidate identity/dedupe 契约不变。
- reconciler 只通过 Daily Brief repository 读取 frozen revision/exposure，不自行遍历或猜测 CSV 历史。

退出条件：成交前/后、有效/过期、已投递/未投递证据等级均可由 frozen artifacts 重放。

### Slice 3 — 用户决定与原子 ledger command

- 实现 confirm、reject、supersede application command。
- 扩展 ledger API 和现有 option-positions CLI facade。
- 完成事务 fault-injection、重复调用、并发 claim 和 membership 验证。

退出条件：每个故障点 rollback 后 trade/projection/identity/inference 都没有半状态；幂等重试返回同一结果。

### Slice 4 — 成交入口切换与自动重试

- 普通 resolver 停止提前赋予 Combo 标签，显式 `pair_intent_id` 兼容路径保留。
- 在 post-commit、backfill batch-end 和独立周期 timer 接入 reconciler。
- 增加 account-scoped `off|observe|confirm` validation；未列账户默认 `off`。
- 先以 observe 做离线/本地 replay，确认歧义率和误配 proposal 为零后，另行授权启用 confirm。

退出条件：交易落账不再依赖组合预声明；推理失败不影响交易 receipt；历史已归组 Combo 行为不变。

## 15. 验证矩阵

### 15.1 Domain

- Long Call first / Sell Put first 输出相同 inference ID、edge 与 evidence。
- 输入随机排序、重复输入、重复 sweep 结果不变。
- 同合约多 lot、一个 Put 对多个 Call、多个最大 matching 均输出 ambiguity，不靠排序选中。
- delivered/live/structural evidence 的字典序匹配符合定义。
- future、expired、wrong account、wrong contract key、missing event time 全部不能精确命中。
- partial close、数量不等、currency/multiplier 不等、strike/expiry 非法全部 fail closed。

### 15.2 Repository 与事务

- inference upsert 幂等，terminal rejection 不重开。
- 两个并发确认争夺同一腿时最多一个 commit。
- confirm 在每一步注入失败均完全 rollback。
- 相同 confirm 重试不重复写 adjustment/identity。
- supersede 同时 void 两条 adoption；任一失败全 rollback。
- supersede 后 effective identity 不再被下游选中，旧 identity 审计仍可见。

### 15.3 Candidate / Brief / delivery

- occurrence hash canonicalization 对列顺序、NaN 表达稳定。
- 只有实际渲染的 occurrence 进入 exposure。
- blocked/degraded non-actionable Brief 不形成有效 exposure。
- delivery confirmed 必须同时满足 source digest 和 occurrence membership。
- coarse candidate dedupe identity 的既有测试保持不变。
- 新代码读取没有 occurrence/render-context evidence 的旧 artifact 时降级为 structural-only；旧代码重新读取 additive candidate row/render context 时不改变既有 Brief digest 或 delivery content normalization。

### 15.4 Intake 集成

- resolver 对普通单腿 open 不再写 Combo 标签。
- 显式 `pair_intent_id` 兼容测试保留。
- post-commit reconciler 抛错时 trade 仍为 applied、receipt 不变。
- backfill 仅在 batch-end reconcile 一次。
- 周期 sweep 与 post-commit 重入不重复 proposal。
- 历史已归组/已有 identity 的 lot 不进入未分组池。
- 测试过程不发送真实通知、不写生产 runtime data。

### 15.5 质量门

- 先运行 matcher、ledger、trade intake、Daily Brief/delivery 的 focused tests。
- 导入边界变化后重新生成并检查 dependency graph。
- 再运行完整 pytest；使用项目约定的 Python 3.12 环境。
- config example 的 US/HK validate 与 dry-run build 都通过。

## 16. Rollout 与回滚

1. 合入时所有 account 默认 `off`，仅验证 schema migration 和既有行为。
2. 在隔离 runtime/历史快照上运行 dry-run replay，比对 proposed、ambiguous、insufficient 及被排除原因。
3. 经独立授权只对一个明确 account 切到 `observe`，只写 inference，不改 canonical membership；不得用该账户结果推断其他账户。
4. 抽样核对精确 lot pair、候选时间证据、歧义和 partial-lot 排除。
5. 经再次独立授权只对已核验 account 切到 `confirm`；canonical 写入仍逐笔需要用户 apply。

运行开关回滚先把模式改回 `off`；二进制回滚前用兼容性测试确认旧 normalizer 可读取 additive candidate row/render context，且 digest 不漂移。已确认 Combo 不因关闭 reconciler 被删除。错误确认使用 append-only supersede 命令纠正，不直接修改 SQLite 行。

## 17. 已锁定决策

- 普通用户不需要提前声明 Combo 意图。
- 所有 V1 canonical Combo 均需用户确认，候选证据不触发自动确认。
- `candidate_pair_id` 不升级为 canonical identity；使用 occurrence + exposure + delivery 构成可验证证据。
- V1 只接受精确两条、完整 open、等数量 lot。
- 匹配只用硬约束与离散 evidence priority，不使用连续 score。
- structural-only 只看同市场交易日；跨日旧仓 pairing 留给显式人工流程或后续版本。
- 历史 identity insert-only；纠错使用 append-only void 并由 effective membership 判定当前有效性。
- 通知交互、自动确认、partial/multi-lot 与滚动续接均不在本期扩展。

## 18. 预计改动归属

- Domain：`domain/domain/combo_reconciliation.py`、既有 combo identity/membership contract 测试。
- Candidate publication：`src/application/combo_yield_steps.py` 与 candidate artifact tests。
- Brief/delivery evidence：Daily Brief service/repository/renderer 的唯一回连与 `render_context` 适配及测试；不扩大 coarse identity/schema。
- Trade orchestration：`src/application/trades/resolver.py`、`combo_reconciliation.py`、`auto_intake.py` 及测试。
- Ledger：现有 repository/writer/commands/API 边界、schema migration、membership/transaction tests。
- CLI/config：现有 `option-positions` facade、layered config validation/examples。

不新增平行仓位表、消息通道、外部服务或第三方依赖。
