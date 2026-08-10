# Gateflow Implementation Plan — HK Combo Capture / Failure Notification

- Work unit: `hk-combo-capture-failure-notification`
- Gate: `plan`
- Date: 2026-08-10
- Status: accepted after adversarial re-review
- Goal confirmation: `docs/gateflow/hk-combo-capture-failure-notification/goal-confirmation.md`
- Initial review: `docs/reviews/plan-review-20260810-111309.md`
- Accepted re-review: `docs/reviews/plan-review-20260810-111928.md`
- Branch: `fix/hk-combo-capture-failure-notification`
- Base: `main@0d635e11`

## 1. Goal and completion signal

在不放宽任何身份或通知安全门禁的前提下，修复 HK tick 中的两个编排断点：

- capture status 必须按 snapshot owner 分区后再做严格 scope 校验，不能让
  `combo_yield` 进入 put/call opening validator；
- current-run portfolio identity receipt 必须在进入可预期的 operational
  failure boundary 前发布，且完整 source graph 必须复用这份 write-once receipt。

完成信号是：默认 pipeline 端到端回归可封存 opening + SP+LC Combo 或
opening + CC+LP；prefetch barrier 与 pipeline nonzero 能在当前-run identity
可验证时生成受限 `fixed_failure`；unknown/duplicate/config/identity/receipt conflict
全部继续 fail closed。

### 1.1 Non-goals and scope boundary

- 不改 scheduler processed-target/retry 语义、OpenD timeout/retry/freshness 策略或服务退出码。
- 不改 notification wording/action/policy、snapshot/receipt schema、public CLI 或 runtime config。
- 不重放、不发真实通知、不写生产 runtime/broker/Feishu 数据。
- 不修改 `VERSION`，不 merge、release、deploy 或升级远端。
- 不触碰、暂存或提交当前 worktree 的无关脏改动。

### 1.2 Goal alignment

| Plan item | Binding goal / success signal |
|---|---|
| S1 owner-aware status/pair routing | Goal 1; success signals 1–4: opening 不消费 Combo，默认 pipeline 双快照封存，unknown/duplicate fail closed，SP+LC/CC+LP 状态独立。 |
| S1 frozen-data and CC+LP producer mapping | Goal 1; success signal 4: 早期数据失败、扫描失败、not-applicable 不被误降级成 no-candidate。 |
| S2 early portfolio receipt + owner-local reuse | Goal 2; success signals 5–6: prefetch 前当前-run identity 可验证，full graph 复用同一 receipt，operational failure 可准备 fixed-failure。 |
| S2 strict conflict/recovery tests | Success signal 6: identity/config/authority/receipt conflict 仍 no-send。 |
| Aggregate validation and reviews | Success signal 7: 聚焦+回归+compileall+diff check 通过。 |

### 1.3 First-principles judgment and direct code evidence

- `pipeline_watchlist.run_watchlist_pipeline_default()` 在封存 opening 前用 put/call
  `expected_scopes` 验证全部 capture statuses，而 Combo/CC+LP 过滤发生在 opening seal 之后；
  因此 owner partition 必须发生在 validator 之前。
- `symbol_monitoring` 的 normal Combo 路径发布 `strategy_mode=combo_yield`，但 frozen
  required-data 分支缺少对应 capture，且 CC+LP result 的 `status=not_applicable` 目前被硬编码
  `completed` 覆盖。
- SP+LC pair sink 原样转发 dataframe rows，无 `variant`；CC+LP rows 显式带
  `variant=cc_lp`，因此 pair router 需要窄的 legacy compatibility 而不是全部强制必填。
- `account_run.run_one_account()` 在 pipeline nonzero 时早于完整 source publication 返回；
  `tick_account_execution` 的 prefetch barrier 也不进入 account runner，但两者之前已有冻结并
  验证的 prepared portfolio context。
- Portfolio source path 由 run key + payload hash 确定，receipt 含 `completed_at` 且 write-once；
  所以正确语义是在 source owner 内 publish-or-reuse exact bytes，而不是在早期/后期各发布一次。

## 2. Authority and state-flow decision

### 2.1 Capture status ownership

`candidate_capture_status_sink_fn` 仍为单一内存收集面，但封存前先解析成带 owner
的 canonical status：

```text
(symbol, strategy_mode=put|call)                 -> opening snapshot
(symbol, strategy_mode=combo_yield, variant=sp_lc) -> combo snapshot
(symbol, strategy_mode=combo_yield, variant=cc_lp) -> cc_lp snapshot
anything else                                   -> fail closed
```

- `variant` 对 `combo_yield` 为必填；产生端对默认变体显式发布 `sp_lc`。
- expected opening scopes 继续由解析后的 `sell_put.enabled` /
  `sell_call.enabled` 决定。
- expected combo scopes 复用现有
  `derive_yield_enhancement_policy(resolve_yield_enhancement_cfg(resolved_item))`，由其
  `enabled` 和 canonical `variant` 决定；不建立第二套配置解析。
- whitelist 和 runtime profile resolution 与执行路径保持一致。
- 每个 owner 独立校验 unexpected、duplicate 和 missing scope；一个 owner 的状态不参与
  另一个 snapshot 的归约。

### 2.2 Combo / CC+LP aggregate status contract

对每个已启用 owner，将其 expected scopes 补齐 missing status 后按同一个 private reducer
归约：

1. 全部 `completed`：不显式传 `opening_status`，由该 owner 自己的 pairs 推导
   `candidates_found` 或 `no_candidate`；
2. 全部 `not_applicable`：`not_applicable`；
3. 所有非 `not_applicable` scope 都是 `market_closed`：`market_closed`；
4. 有 `completed` 且同时有 `failed` / `incomplete` / `unavailable` /
   `not_applicable`：`partial_data`；
5. 没有 `completed`，且只有失败/缺失/不可用：`data_unavailable`；
6. 非法 status value 不降级成可用状态，直接 fail closed。

Pairs 也按 variant 分区，但保留既有 SP+LC producer 契约：

- pair 缺少/空 `variant` 规范为 legacy canonical `sp_lc`；
- 显式 `variant=cc_lp` 路由到 CC+LP snapshot；
- 其他显式 variant 直接 fail closed，不默认归入 SP+LC。

SP+LC Combo snapshot 只接收 canonical `sp_lc` pair，CC+LP snapshot 只接收
`cc_lp` pair。候选存在不得覆盖失败/部分数据状态；只有 owner 全部
completed 时才由 pairs 推导 found/empty。

Combo capture 产生端必须保留 CC+LP summary 的 terminal semantics：

- `candidates_found` / `no_candidate` -> capture `completed`；
- `not_applicable` -> capture `not_applicable` 并保留 reason；
- 未知 summary status -> fail closed；
- scan exception 和 frozen required-data unavailable -> capture `failed`。

此映射只对显式 CC+LP summary status 生效；现有 SP+LC summary 不宣称该字段，
仍按既有成功/异常路径产生 completed/failed。

Quote binding 是跨 owner 的共享 required-data 事实，不随 snapshot 独立：先在全部
canonical statuses 上按 symbol 执行现有 binding 一致性校验；同一 symbol 出现多个
`(quote_snapshot_id, quote_receipt_relpath)` 时，将该 symbol 所有 completed status 降级为
`incomplete/quote_binding_conflict`，再按 owner 分区归约。

### 2.3 Early identity receipt lifecycle

```text
frozen account config
  -> prepared portfolio context validates account/config/source identity
  -> publish exactly one current-run portfolio source receipt
  -> required-data prefetch
     -> barrier failure: receipt remains available to fixed-failure authority
     -> pipeline nonzero: same receipt remains available
     -> pipeline success: full source graph revalidates and reuses same receipt
```

新的 private/internal 契约收敛在现有 account-source owner：

```python
publish_or_reuse_account_portfolio_source(...) -> dict[str, Any]
```

返回现有 `_receipt_record` 形状的 JSON-safe dict，包含 `producer_root`、
`receipt_path`、receipt 及 canonical portfolio identity fields；不新建 dataclass/schema。它只使用
已验证 prepared portfolio context，不重新读 broker、ledger、quotes 或 FX。

helper 拥有唯一的 locate/publish/reuse 契约：

1. 按现有 deterministic run key 定位
   `position_advice_producers/portfolio/<run_key>/`；
2. 目录不存在时使用现有 producer 发布一次；
3. 目录存在时必须恰有一份完整 receipt/payload，然后验证并复用；空目录、
   symlink、多 receipt 或不完整内容都 fail closed，不在同一 run 生成替代身份；
4. same-input replay 返回原 receipt bytes/path/hash/snapshot id，不生成新 `completed_at`。

复用时必须：

- 路径在当前 account state root 内且不是 symlink；
- 通过现有 receipt + payload validator；
- `source_kind=portfolio`、account、producer account run id、broker、included markets、
  normalized portfolio source 和 identity hash 全部与当前输入一致；
- payload 中的 portfolio context 与当前 prepared context 的 canonical content 一致；
- 任一不一致都抛 typed source error，不修补、不重发布、不退回新建第二份。

early tick 与后续 `publish_account_run_sources()` 都调用这一 owner helper，不把
receipt path/payload 穿透到 `AccountRunRequest`。recovery (`prefetch_done=True`) 也通过同一
helper 复用当前 run 已有 receipt；若 receipt 缺失但原 current-run prepared context
仍完整可验证，允许从该冻结 context 发布一次。recovery 不重读 broker，不从
stale/cross-run facts 合成 identity；prepared context 缺失、receipt 不完整/冲突均 no-send。

## 3. Public contract, schema, and compatibility decisions

- 不改 CLI、scheduler payload、snapshot schema、receipt schema、notification action 或用户文案。
- capture status 是 internal callback payload；对 `combo_yield` 将 `variant` 从 optional
  收紧为产生端必填，但 put/call 仍无 variant。
- `AccountRunRequest` 不新增 receipt 字段，避免把 source storage contract 泄漏到账户
  编排 payload。
- 当 deterministic run directory 未存在时，完整 source publisher 保持原有单次发布
  行为，便于独立调用和现有测试兼容。

## 4. Affected files and ownership

### Production files

- `src/application/pipeline_watchlist.py`
- `src/application/symbol_monitoring.py`
- `src/application/position_advice_account_sources.py`
- `src/application/account_run.py`
- `src/application/tick_account_execution.py`

### Test files

- `tests/test_pipeline_capture_status_routing.py` (new focused default-pipeline tests)
- `tests/test_symbol_monitoring_fetch_spec_merge.py`
- `tests/test_position_advice_account_sources.py`
- `tests/test_account_run.py`
- `tests/test_tick_account_execution_barrier.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- 仅在上述窄契约测试仍无法证明 tick -> preparation 组合时，才允许聚焦修改
  `tests/test_multi_account_tick.py`。

### Artifacts

- `docs/gateflow/hk-combo-capture-failure-notification/`
- 本 work unit 的 planreview / deepreview artifacts under `docs/reviews/`

`docs/DEPENDENCY_GRAPH.md` 当前属于用户脏改动，本 work unit 不触碰。方案仅沿用
`tick_account_execution -> account_run -> position_advice_account_sources` 的现有依赖方向：
tick 层通过 `account_run` 暴露的窄 helper 发布早期 receipt，不新增跨模块边。

## 5. Implementation slices

### Slice S1 — Owner-aware capture routing and snapshot status

- Objective: 修复 10:00 pipeline crash，并让 opening、SP+LC Combo、CC+LP 独立封存。
- Prerequisite: accepted plan commit.
- Allowed files:
  - `src/application/pipeline_watchlist.py`
  - `src/application/symbol_monitoring.py`
  - `tests/test_pipeline_capture_status_routing.py`
  - `tests/test_symbol_monitoring_fetch_spec_merge.py`
- Exact changes:
  1. 在 `pipeline_watchlist` 增加 private canonicalizer/router，保留 `variant`，拒绝
     empty/unknown mode、unknown combo variant 和 put/call 携带 variant。
  2. 用同一次 resolved watchlist traversal构建 opening / sp_lc / cc_lp expected scopes，
     并对每个 owner 独立执行 unexpected、duplicate、missing 校验。
  3. pair router 将 missing/empty variant 规范为 `sp_lc`，保留现有 SP+LC producer
     shape；显式 `cc_lp` 独立路由，其他显式 variant fail closed。
  4. opening seal 只接收 put/call statuses。SP+LC / CC+LP 各自过滤 status 和
     pair，然后用 §2.2 reducer 得到 explicit/derived opening status。
  5. 对 completed status 继续校验 quote snapshot/receipt binding；先在全部 owner 上按
     symbol 保留现有唯一 quote generation 不变式，然后分区归约。
  6. Combo success path 将 CC+LP summary 的 `not_applicable` + reason 原样投影到
     capture，candidates_found/no_candidate 投影为 completed，未知 status fail closed。
  7. `FrozenRequiredDataUnavailable` 为已启用 Combo 发布
     `strategy_mode=combo_yield,status=failed,variant=<canonical variant>`，与普通扫描
     失败契约一致。
- Invariants:
  - 不改 opening snapshot 的 `put|call` schema；
  - 不把 unknown status 当成 `data_unavailable` 吞掉；
  - 不从 pairs 的有无推断扫描是否成功；
  - 同一 expected scope 只能有一条 terminal capture status。
- Focused validation:

  ```bash
  ./.venv/bin/python -m pytest -q \
    tests/test_pipeline_capture_status_routing.py \
    tests/test_symbol_monitoring_fetch_spec_merge.py \
    tests/test_opening_candidate_snapshot.py \
    tests/test_combo_yield_candidate_snapshot.py \
    tests/test_cc_lp_candidate_snapshot.py
  ```

- Required assertions:
  - default `run_watchlist_pipeline_default()` 收到 put/call + sp_lc 状态时同时封存
    opening 和 combo，opening scopes 不含 combo；无 variant 的现有 SP+LC pair 被保留且
    生成 `candidates_found`；
  - `variant=cc_lp` 只封存 CC+LP snapshot，不污染 SP+LC snapshot；
  - all-completed empty 是 `no_candidate`，all-failed/missing 是 `data_unavailable`，
    success+failure 是 `partial_data`，all-not-applicable 是 `not_applicable`；
  - CC+LP no-stock summary 产生 `not_applicable` capture/snapshot；
  - FrozenRequiredDataUnavailable 产生带 canonical variant 的 Combo failed status；
  - put/combo 对同一 symbol 绑定不同 quote receipt 时，所有相关 owner 都降级；
  - unknown mode/variant、unexpected scope、duplicate scope 均抛错且不留下该 owner 的终态快照。
- Completion signal: focused tests pass; default pipeline no longer routes combo into opening.
- Stop condition: 要保持独立状态必须改 snapshot schema 或公开配置契约。

### Slice S2 — Early current-run portfolio receipt and fixed-failure liveness

- Objective: 修复 09:40 类型早期 operational failure 在 notification authority 校验前
  缺少 current-run portfolio identity 的问题。
- Prerequisite: accepted S1 commit.
- Allowed files:
  - `src/application/position_advice_account_sources.py`
  - `src/application/account_run.py`
  - `src/application/tick_account_execution.py`
  - `tests/test_position_advice_account_sources.py`
  - `tests/test_account_run.py`
  - `tests/test_tick_account_execution_barrier.py`
  - `tests/test_daily_decision_brief_service.py`
  - `tests/test_daily_decision_brief_notification_flow.py`
  - 如上述窄契约仍无法证明最终 preparation action，才允许
    `tests/test_multi_account_tick.py`
- Exact changes:
  1. 在 `position_advice_account_sources` 抽出单一 idempotent portfolio identity/source
     owner helper，复用现有 normalization/hash/producer，按 deterministic run directory 定位、
     验证、发布或复用唯一 receipt record。
  2. 让完整 `publish_account_run_sources` 直接调同一 helper；如果当前 run 已有
     receipt，严格验证并复用，否则保持原有发布一次的行为。
  3. 在 `account_run` 提供一个狭的 early-publish facade，复用
     `_position_advice_markets()` 与既有 broker/account 解析，避免 tick 层新增直接
     source-module 依赖。
  4. tick 层在 account config generation 冻结、prepared portfolio context 验证且
     prepared option authority 通过后，对仍在 scanning set 的账户发布 early receipt；
     任一发布/验证错误转为 account-scoped config/authority error，从 prefetch scope
     移除，不产生 partial-identity failure delivery。
  5. 不修改 `AccountRunRequest`；pipeline 成功时 full publisher 在 account state root 内定位
     并复用同一 receipt；pipeline nonzero 时早期返回不删除、不重写 receipt。
  6. barrier outcomes 继续由现有决策路径组装，不绕过 notification authority；
     本修复只提供它现有规则所需的 current-run identity evidence。
  7. recovery 路径调用同一 owner helper：优先复用当前 run 唯一合法 receipt；
     receipt 缺失时仅可从原 current-run prepared context 发布，不刷新 broker、不读
     stale/cross-run source；不完整/冲突目录不得覆盖。
  8. 串联 fixed-failure contract tests：early owner 发布 -> current-run identity reader /
     Daily Brief authority 验证 -> no-send notification preparation 选择 `fixed_failure`，不使用手工
     构造的 identity authority 替代新产物。
- Invariants:
  - 一个 account run 只有一份合法 portfolio receipt；
  - `completed_at` 由首次发布冻结，full source graph 不重写；
  - identity/config generation/receipt path/payload 任一冲突均 no-send；
  - 不因为 receipt 存在就把业务空结果误判为失败或允许普通报告。
- Focused validation:

  ```bash
  ./.venv/bin/python -m pytest -q \
    tests/test_position_advice_account_sources.py \
    tests/test_account_run.py \
    tests/test_tick_account_execution_barrier.py \
    tests/test_daily_decision_brief_service.py \
    tests/test_daily_decision_brief_notification_flow.py \
    tests/test_multi_account_tick.py
  ```

- Required assertions:
  - early helper 发布的 receipt 可被 current-run identity reader 验证；
  - full source publication 复用相同 receipt path/hash/snapshot id，portfolio receipt 目录仍只一个；
  - tampered/stale/wrong-run/wrong-account/wrong-market/context-drift receipt 被拒绝；
  - global prefetch barrier 在 identity 已准备时保留 `should_notify` 失败结果，且
    Daily Brief 实际读取该 receipt 后得到 `fixed_failure_delivery_allowed=True`；
  - pipeline nonzero 同样保留 current-run identity；typed config error 继续
    `should_notify=False`；
  - no-send notification preparation 对上述 blocked brief 选择 `decision=fixed_failure`，不调 provider；
  - identity publish conflict 不进入 prefetch/provider，不产生 delivery attempt。
- Completion signal: focused tests pass and both early operational-failure paths have identity evidence.
- Stop condition: 需要放宽 fixed-failure authority 或从 stale/cross-run source 合成 identity 才能通过。

## 6. Validation matrix

After S1 and S2 focused checks, run the aggregate suite:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_pipeline_capture_status_routing.py \
  tests/test_symbol_monitoring_fetch_spec_merge.py \
  tests/test_opening_candidate_snapshot.py \
  tests/test_combo_yield_candidate_snapshot.py \
  tests/test_cc_lp_candidate_snapshot.py \
  tests/test_position_advice_account_sources.py \
  tests/test_position_advice_account_identity_reader.py \
  tests/test_account_run.py \
  tests/test_tick_account_execution_barrier.py \
  tests/test_multi_account_tick.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_multi_tick_notify_format.py \
  tests/test_unified_tick_entrypoint.py

./.venv/bin/python -m compileall -q domain src scripts
git diff --check
```

验证全部使用 tmp runtime、fake subprocess/provider 或 no-send path，不读写生产 runtime，
不发送真实通知。

## 7. Docs decision

不修改用户文档：公开 CLI、配置、snapshot schema、通知文案和运维流程都未改变。
本 work unit 的设计、审查、测试与 residual risk 记录在 Gateflow artifacts。

## 8. Risks and residual-risk destinations

| Risk | Decision / destination |
|---|---|
| scheduler 在失败后仍将 target 标记 processed，后续 10 分钟窗口不重试 | Deferred to a dedicated scheduler reliability work unit; this patch restores one bounded failure notification, not retry semantics. |
| OpenD expiration lookup can still time out | Accepted operational failure; existing prefetch classification remains, now eligible for fixed-failure only when current-run identity is valid. |
| early receipt consumes the prepared portfolio observation freshness window before full graph completion | Bounded by the existing 30-minute portfolio receipt max age; stale receipt fails closed. Do not refresh it silently within the same run. |
| recovery starts without a previously published current-run portfolio receipt | The owner helper may publish once only from the immutable current-run prepared context; missing context or an incomplete/conflicting receipt directory stays fail closed/no-send. |
| mixed SP+LC and CC+LP symbols | Covered by owner/variant partition tests; each snapshot sees only its own statuses and pairs. |
| status stream contains duplicate, unknown, or out-of-plan scopes | Fail closed before the affected snapshot is sealed; never coerce to no-candidate. |
| production service still runs a version containing the bug | Release and remote upgrade remain separately authorized operations after this PR. |

Blocking open questions: none.

## 9. Why this is not overdesigned

候选侧只在已有封存边界加 owner-aware partition 和一个共享 reducer；身份侧只拆出
已有 portfolio producer 并传递其 receipt reference。不新增 schema、state machine、service、queue、
provider 或通知特例。

## 10. Completion report format

Final closeout 必须列出：

- 两个 root cause 与实际修复边界；
- 每个 slice 及 aggregate 的精确验证命令/结果；
- plan/code/aggregate/PR review 的 finding 处置；
- 所有 accepted Gateflow commit hashes 和 Draft PR URL；
- 未解决的 scheduler retry、OpenD timeout 与 release/deployment 风险归属；
- 明确说明未触碰、未暂存、未提交原有脏改动。
