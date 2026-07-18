# Gateflow Implementation Plan — True Diagonal Combo Yield Lifecycle

- **Gate**: plan
- **Work unit**: 真正错期 Combo Yield 的端到端处理
- **Status**: accepted-pass
- **Branch**: `codex/diagonal-combo-yield-lifecycle`
- **Goal artifact**: `docs/gateflow/diagonal-combo-yield-goal-confirmation-20260718-122023.md`
- **Artifact path**: docs/gateflow/diagonal-combo-yield-plan-20260718-130000.md
- **Current gate / next entry point**: `implementation (S1)`

## 1. Goal, Motivation, Success Signals

支持显式 opt-in 的 true diagonal Combo Yield：一张较近到期 short Put 与一张严格更晚到期 long Call，在候选生成、持仓归组、事件投影、Put 平仓/到期/assignment、Call residual classification 和 Close Advice 中保留同一策略血缘。

成功信号：

1. `expiry_structure=diagonal` 时只接受 `call_expiration > put_expiration`，报告独立 Put/Call expiry、DTE 和 gap；默认 `same_expiry` 行为保持兼容。
2. Diagonal 开仓只使用当前可观察 funding/liquidity/participation 数据；依赖 Put 到期时未来 Call 价值的 terminal/scenario 字段为 `None`，并携带明确 `not_evaluable` 原因，绝不把未来 Call 价值当零。
3. Candidate 生成稳定 `combo_pair_fingerprint`，explicit-intent/manual intake 将 account-scoped `strategy_group_id` 写入两腿；Call-first、Put-first、restart、partial fill 和 duplicate intake 均通过持久化 metadata 重建。纯 broker payload 缺 intent 时 fail closed，不做跨 expiry 猜测。
4. 从 canonical lots/events 派生 compositional group inventory，可同时表达 open/closed/assigned Put contracts、open/closed Call contracts、assigned stock shares，不使用互斥 phase enum。
5. Put 完全关闭且 Call 仍开时，Call 分类为 residual Call；assignment 时 assigned-stock lot 继承 Combo group 血缘，形成 “assigned stock + residual Call” 语义，但不产生股票出售或 Call exercise 自动动作。
6. Close Advice 保留逐腿 evaluator，并在其后做纯组合级 synthesis；只有数量和行情证据完整时才给组合动作，歧义状态输出 `review_required`。
7. 正式配置、live state、broker-facing data 和通知开关不修改；新 diagonal 行为通过 additive opt-in 和 shadow/read-only 字段落地。

## 2. Non-goals / Scope Boundary

- 不复用同一长期 Call 为第二轮 Put 融资。
- 不实现 roll/replacement/order execution。
- 不预测 Put 到期日 Call residual value，不引入 Black-Scholes、未来 IV 或 spot path。
- 不自动卖 assigned stock，不自动 exercise Call。
- 不迁移或重写历史 event/group ID。
- 不改变现有 same-expiry 默认语义；历史缺 metadata 的组只做 best-effort read，不反写。
- 不修改生产 `config.yaml`、`config.us.json`、`config.hk.json` 或 live outputs。

## 3. First-principles Judgment and Direct Evidence

- 当前 `validate_yield_enhancement_pair()` 强制同 expiry，而 metrics 在单一 DTE 上计算 Call terminal intrinsic/scenario；仅删除 rejection 会制造错误经济指标（`domain/domain/engine/yield_enhancement.py:120-207`）。
- 当前 call fetch window 被强制覆盖为 sell-put window，pair join 只取 Put expiry（`src/application/required_data_planning.py:363-394`, `src/application/sell_put_call_helper.py:769-783`）。
- 当前 inferred group ID 含当前 deal expiry，companion 也要求同 expiry（`src/application/trades/resolver.py:704-732`）；对 diagonal 无法稳定归组。
- Canonical ledger 已支持 partial close，并将策略 metadata 保存在 lot snapshot；应派生 inventory，而不是新建 mutable group state（`domain/domain/ledger/lots.py:48-59`, `domain/domain/ledger/position_fields.py`）。
- Assignment event 当前由 target lot fields 构造，但 assigned-stock projection 没有显式继承 group metadata（`src/application/ledger/lifecycle.py:114-195`, `src/application/positions/reporting.py:1796+`）。
- Close Advice 当前按 group 取第一张 Call，未做数量匹配（`src/application/close_advice_runner.py:1512-1558`），会在多 lot/partial states 下虚构 pair economics。

因此最小安全方案是：**additive expiry contract + explicit diagonal identity + event-derived inventory + leg-first/group-second advice**，而不是改写 ledger schema 或增加 mutable state machine。

## 4. Contract / Schema / State Semantics

### 4.1 Config contract (additive)

`combo_yield` 新增：

```yaml
expiry_structure: same_expiry   # same_expiry | diagonal; default preserves compatibility
min_expiry_gap_days: 1          # diagonal only, integer >= 1
call:
  min_dte: null                 # diagonal requires explicit positive integer
  max_dte: null                 # diagonal requires >= min_dte
```

Rules:

- `same_expiry`: existing behavior; call DTE window continues to derive from Sell Put.
- `diagonal`: `call.min_dte` and `call.max_dte` are required; pair must satisfy configured Call window and calendar gap `>= min_expiry_gap_days`.
- Invalid diagonal config fails validation; no silent fallback to same expiry.
- Defaults/templates remain `same_expiry`, so existing runtime snapshots are behavior-compatible.

### 4.2 Opening candidate/report contract

Every pair row adds:

- `expiry_structure`
- `put_expiration`, `call_expiration`
- `put_dte`, `call_dte`, `expiry_gap_days`
- `terminal_metrics_status`: `evaluable_same_expiry | not_evaluable_diagonal`
- `terminal_metrics_unsupported_reason`
- `combo_pair_fingerprint`（由 canonical symbol + Put contract + Call contract 稳定生成）
- `strategy_group_id`（当 candidate row 有 account 时生成 account-scoped 值；否则由 structured/manual intake 用 account + fingerprint 补全）

Compatibility aliases `expiration` and `dte` remain Put horizon fields.

For diagonal rows:

- Known opening fields stay populated: execution net credit/debit, Put premium, Call cost, funding ratio/cost ratio, cash basis, current strikes/deltas/liquidity/spreads, Put-horizon annualized opening cashflow.
- Future-horizon/terminal fields are `None`: `expected_move`, Call payoff multiples, `scenario_score`, `annualized_scenario_score`, `upside_scenario_price`, `upside_lift`, `upside_net_lift`, lift multiples, and same-horizon upside breakeven fields.
- Ranking uses accepted funding, current net-credit retention/cost, Call delta/strike participation, Put assignment margin, spread and expiry gap; it must not coerce unsupported fields to attractive zeroes.

### 4.3 Durable identity contract

- Existing same-expiry inference/group ID stays unchanged for compatibility.
- Candidate generation emits stable `combo_pair_fingerprint = combo_yield|<symbol>|<put_contract>|<call_contract>` using canonical contract identities. When account is known it also emits `strategy_group_id = combo_yield:<account>:<fingerprint>`.
- The v1 executable handoff is explicit-intent only: existing structured/manual open surfaces copy the candidate group ID into `strategy`, `expiry_structure`, `strategy_group_id` and `strategy_snapshot` for both legs. Tests cover candidate row → open preview → projected lot → restart reconstruction.
- A raw broker fill that lacks this explicit diagonal intent is not auto-matched across expiries. It returns unresolved/review diagnostics naming the missing group metadata. This is a deliberate safe fallback, not an automatic end-to-end path.
- Resolver preserves explicit group ID verbatim and only links companion record IDs when the opposite open lot has the same group ID and compatible account/symbol/leg role/expiry ordering.
- Conflicting group ID, ambiguous multiple companions, invalid expiry ordering, or quantity conflict returns unresolved/review diagnostics; no “nearest expiry” heuristic.
- Duplicate deal idempotency remains owned by existing intake state; restart recovery comes from persisted lot metadata, not process memory.
- Partial fills may create multiple lots under one group; inventory sums contracts and marks mismatch instead of selecting one row.

### 4.4 Compositional lifecycle inventory and evidence scopes

Add one pure domain classifier over normalized facts, with two application adapters:

1. `option_group_inventory`: built from projected option-lot snapshots and included in option-position context. It is the only lifecycle evidence Close Advice may consume.
2. `full_group_lifecycle`: built in positions reporting from canonical trade events plus assigned-stock lots/sales. It adds assignment and stock quantities for audit/reporting, but is not queried from Close Advice runner.

Canonical quantities/labels remain independent:

- option scope: `put_contracts_opened/open/closed`, `call_contracts_opened/open/closed`, Put/Call expirations;
- full scope additions: `put_contracts_assigned`, `assigned_stock_shares_open`, `expected_shares_from_assignment`;
- `inventory_labels[]`: any of `put_open`, `put_closed`, `put_assigned`, `call_open`, `call_closed`, `assigned_stock_open`;
- `inventory_issues[]`: missing group identity, wrong expiry ordering, duplicate/multiple expiries, quantity mismatch, missing assignment settlement, etc.;
- `evidence_scope`: `option_lots | full_lifecycle`.

A single ordered derived `summary_classification` contract is shared by both adapters:

1. any structural/quantity issue or unresolved mixed facts → `review_required`;
2. Put open and equal Call open → `active_combo`;
3. Put open and Call absent → `missing_call`;
4. Put absent, Call open, assigned stock open (full scope only) → `assigned_stock_with_residual_call`;
5. Put absent, Call open → `residual_call`;
6. no open options, assigned stock open (full scope only) → `assigned_stock_only`;
7. otherwise → `closed`.

The classification is presentation-only and never persisted as mutable truth. Close Advice must not infer assigned stock from option-lot `last close_type`; it can only emit the option-scope subset.

### 4.5 Assignment handoff

- Lifecycle assignment/expiry close events copy strategy family, leg role, group ID, expiry structure and snapshot from the target lot into event payload.
- Assigned-stock projection copies group ID and strategy snapshot into the assigned-stock lot, with `leg_role=assigned_stock` and source assignment event/option lot IDs.
- Assigned-stock read output includes the group fields so it can be joined with the still-open residual Call.
- This is metadata/read-model propagation only; existing assigned-stock sale write model remains independent and unchanged.

### 4.6 Close Advice truth table

Leg evaluator remains authoritative for each open option. Group synthesis is a pure post-processing step over all rows plus derived inventory:

| Option inventory facts | `summary_classification` | Allowed Close Advice result |
|---|---|---|
| Put open == Call open > 0, one Put expiry < one Call expiry, quotes complete | `active_combo` | Preserve Put/Call leg advice; Put exit may be `close_put_keep_call`; optional `close_both` only when both real quotes and full matched quantity exist |
| Put open > 0, Call open == 0 | `missing_call` | Put leg advice remains, but action text must not claim “keep Call”; `combo_group_action=review_required` |
| Put open == 0, Call open > 0 | `residual_call` | Call uses long-call leg advice and the actual current quote; no Put/combo profit action; do not claim whether assignment stock exists |
| Any partial/multiple-expiry/quantity mismatch or structural issue | `review_required` | Leg advice remains visible; combo economics/action suppressed with explicit quantities/issues |
| No open option inventory | `closed` | No active option action |

`assigned_stock_with_residual_call` and `assigned_stock_only` are emitted only by full lifecycle reporting. They are never synthesized by Close Advice from option-only evidence.

Pair economics are quantity-matched at group totals; no `setdefault(first call)` behavior. Missing quotes/fees produce explicit `not_evaluable` instead of zero.

## 5. Affected Modules

Primary planned files:

- Domain opening policy: `domain/domain/engine/yield_enhancement.py`, `domain/domain/engine/__init__.py`
- Domain lifecycle projection: new `domain/domain/combo_yield_lifecycle.py` (or the closest existing domain package if review finds a stronger owner)
- Config/default/validation: `src/application/yield_enhancement_config.py`, `src/application/config_validator.py`, `src/application/config_defaults.py`, `configs/system.json`, example config docs only
- Fetch/pair/report: `src/application/required_data_planning.py`, `src/application/sell_put_call_helper.py`, `src/application/render_yield_enhancement_alerts.py`
- Trade intake: `src/application/trades/resolver.py`
- Lifecycle metadata: `src/application/ledger/lifecycle.py`, `src/application/positions/reporting.py`, `src/application/positions/context_builder.py`
- Close Advice: `src/application/close_advice_runner.py`
- Contracts/docs: relevant agent output contract metadata and `docs/AGENT_WIKI.md` only if public read fields change
- Focused tests under existing test modules; add a narrowly scoped lifecycle domain test file if needed.

No database migration or new mutable persistence table is planned.

## 6. Implementation Slices

### Slice S1 — Diagonal opening contract and shadow candidate output

- **Objective**: Produce correct opt-in diagonal pairs without predicting future Call residual value.
- **Allowed files**: domain engine, Combo config/default/validator/system template, required-data planning, pair helper, alert renderer, directly related tests/docs.
- **Exact changes**:
  1. Add/validate additive config fields and explicit call window.
  2. Parameterize pair validation by expiry structure; same-expiry defaults unchanged.
  3. Split current metric calculation into common opening facts and same-expiry-only terminal facts; diagonal terminal facts are `None` with status/reason.
  4. Fetch configured later Call expiries and join all eligible later expiries satisfying the gap.
  5. Emit dual-expiry schema, stable pair fingerprint/account-scoped group ID, and render both expiries/DTEs; suppress unsupported scenario lines or label them not evaluable.
  6. Make ranking null-safe and ensure diagonal unsupported fields never improve rank by zero fallback.
- **Invariants/error handling**: invalid diagonal config raises validation error; no Call window/no later expiry yields diagnostic rejection; same-expiry fixture outputs remain byte/schema-compatible except additive columns.
- **Non-goals**: no trade grouping or lifecycle behavior.
- **Validation**:
  - `python3 -m pytest tests/test_sell_put_linked_call_helper.py tests/test_sell_put_yield_enhancement_required_data_planning.py tests/test_sell_put_yield_enhancement_validate_config.py tests/test_render_yield_enhancement_alerts.py`
  - New assertions: later Call accepted; same/earlier Call rejected; dual DTE fields correct; future-value fields null; same-expiry regressions pass.
- **Completion signal**: accepted diagonal candidate and diagnostics artifact generated entirely from current quotes, with no terminal prediction.

### Slice S2 — Durable diagonal intake identity and compositional inventory

- **Objective**: Persist safe grouping metadata and derive quantity-aware lifecycle inventory across order/restart/partial states.
- **Allowed files**: trade resolver, pure domain lifecycle projection/export, ledger query/context adapter as required, focused resolver/domain/context tests.
- **Exact changes**:
  1. Add the candidate fingerprint/account-scoped group-ID helper and ensure structured/manual open metadata can preserve it through preview/write/projection.
  2. Preserve explicit diagonal group metadata; do not replace it with expiry-derived IDs.
  3. Match diagonal companions only by same explicit group ID plus structural compatibility; raw broker-only missing intent fails closed with diagnostics.
  4. Keep current same-expiry inference path unchanged.
  5. Build pure option-group inventory from normalized position snapshots; aggregate multiple lots and partial quantities.
  6. Add read-only `combo_yield_groups` with `evidence_scope=option_lots` to option-position context so downstream advice consumes facts without querying ledger internals.
- **Invariants/error handling**: no process-local mapping; no fuzzy expiry matching; duplicates remain idempotent; group metadata from historical lots is never rewritten.
- **Non-goals**: no assignment stock propagation and no advice action changes.
- **Validation**:
  - `python3 -m pytest tests/test_trades_resolver_open.py tests/test_positions_context_builder_partial_close.py <new lifecycle tests>`
  - Fixtures: Call-first and Put-first with explicit ID; restart from persisted companion; duplicate fill; two partial Call lots; missing/conflicting ID unresolved; same-expiry legacy unchanged.
- **Completion signal**: deterministic inventory rows reconstruct from persisted facts with correct quantities and issues.

### Slice S3 — Assignment lineage and residual Call classification

- **Objective**: Preserve Combo semantics after Put close/expiry/assignment without coupling stock execution into option advice.
- **Allowed files**: ledger lifecycle event builder, assigned-stock reporting, context/domain adapter if needed, relevant CLI/reporting tests.
- **Exact changes**:
  1. Copy target-lot Combo metadata into assignment/expire-close event payload.
  2. Propagate group/snapshot to assigned-stock lots and read rows.
  3. Build `full_group_lifecycle` in reporting from canonical trade events and assigned-stock lots/sales; do not route these facts through Close Advice.
  4. Classify full lifecycle as `residual_call`, `assigned_stock_with_residual_call`, `assigned_stock_only`, `closed`, or `review_required` using the shared ordered contract.
- **Invariants/error handling**: assignment stock settlement validation unchanged; partial assignment sums quantities; missing settlement produces `review_required`; assigned-stock sale remains independent.
- **Non-goals**: no automatic stock or Call action.
- **Validation**:
  - `python3 -m pytest tests/test_positions_reporting.py tests/test_option_positions_cli.py tests/test_option_positions_domain.py`
  - Fixtures: Put normal close + Call open; expiry + Call open; full assignment + Call open; partial assignment + Put remainder + Call; assigned-stock sale does not erase group history.
- **Completion signal**: reports expose auditable “assigned stock + residual Call” linkage and mixed inventory quantities.

### Slice S4 — Quantity-aware group Close Advice and compatibility replay

- **Objective**: Implement deterministic group synthesis after leg advice and prove no false combo action in ambiguous states.
- **Allowed files**: close-advice runner/policy helpers, output contract metadata/docs, relevant advice/replay tests.
- **Exact changes**:
  1. Replace first-Call lookup with group aggregation and quantity matching.
  2. Apply the option-scope truth table in §4.6 after all leg rows are evaluated; never infer assigned-stock state.
  3. Preserve each leg’s `strategy_exit_mode`, tier and reason; add group fields rather than replacing thesis.
  4. For residual Call, use real current quote and existing long-call evaluator; remove wording implying an open Put remains.
  5. Suppress combo economics/actions on quantity mismatch, missing quote, missing group ID or mixed inventory; emit explicit data-quality flags.
  6. Add additive shadow/read fields; do not change notification enablement or production config.
- **Invariants/error handling**: group advice cannot invent stock sale/exercise; unsupported economics stay null; same-expiry existing expectations remain.
- **Validation**:
  - `python3 -m pytest tests/test_close_advice_runner.py tests/test_close_advice_action_policy.py tests/test_close_advice_reallocation_shadow.py tests/test_notify_symbols_markdown.py`
  - Add truth-table fixtures including multi-lot quantity match/mismatch and missing quotes.
  - Broader regression: `python3 -m pytest tests/test_combo_yield_steps.py tests/test_strategy_policy.py tests/test_strategy_lab.py tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py`
- **Completion signal**: every supported inventory row has a deterministic action/status and every ambiguous row is fail-closed with explicit reason.

Each slice stops after implementation artifact + deepreview-based code review/fix/re-review + accepted slice commit, per Gateflow.

## 7. Plan-level Validation and Expected Assertions

After all slices:

```bash
python3 -m pytest \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_sell_put_yield_enhancement_required_data_planning.py \
  tests/test_sell_put_yield_enhancement_validate_config.py \
  tests/test_render_yield_enhancement_alerts.py \
  tests/test_trades_resolver_open.py \
  tests/test_positions_context_builder_partial_close.py \
  tests/test_positions_reporting.py \
  tests/test_option_positions_cli.py \
  tests/test_option_positions_domain.py \
  tests/test_close_advice_runner.py \
  tests/test_close_advice_action_policy.py \
  tests/test_close_advice_reallocation_shadow.py \
  tests/test_combo_yield_steps.py \
  tests/test_strategy_policy.py \
  tests/test_strategy_lab.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py
```

Also run:

```bash
python3 -m compileall domain src
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run
```

Expected final assertions:

- diagonal candidate acceptance requires strictly later Call and configured gap/window;
- terminal future-value fields are null/not-evaluable, never zero-filled;
- explicit group ID survives both fill orderings and restart;
- partial/mixed inventory quantities are preserved;
- assignment stock inherits group lineage;
- residual Call advice uses current quote and leg thesis;
- mismatch/missing facts suppress group action;
- same-expiry and non-Combo tests remain green.

No live notification, broker write, production config mutation or runtime-state write is part of validation.

## 8. Docs Decision

Update public documentation only for additive user-visible contracts:

- Combo config fields and opt-in example using non-production example config.
- Candidate/report dual-expiry and not-evaluable semantics.
- Position/assigned-stock group lineage fields.
- Close Advice leg-vs-group semantics and fail-closed states.

Do not document future roll/reuse/automation as implemented.

## 9. Risks, Open Questions, Tracking

No blocking product question remains after user confirmation.

Classified residual risks:

1. **Ranking quality without future residual-value model** — accepted product constraint; tracked as `assigned to later work unit` for scenario/research modeling, not production advice.
2. **Historical groups missing explicit diagonal metadata** — `assigned to later work unit`; current work reads best-effort and flags review, never rewrites.
3. **Broker payload cannot carry strategy intent automatically** — `assigned to later work unit`; v1 candidate emits a stable fingerprint/group ID and the structured/manual path is end-to-end, while broker-only missing-intent fills explicitly fail closed.
4. **Notification promotion** — `assigned to later work unit / explicit CEO decision`; this work adds shadow/read fields only and does not change notification switch behavior.
5. **Assigned-stock sale thesis** — `assigned to later work unit`; current owner remains assigned-stock workflow/manual review.

## 10. Why This Is Not Overdesigned

- Reuses existing config, canonical ledger, strategy metadata, context and leg evaluators.
- Adds no table, migration, mutable group state, pricing model, orchestrator or execution path.
- Uses one pure classifier with two evidence-scoped adapters instead of a new state machine or cross-layer query.
- Requires explicit identity rather than adding a speculative order-intent subsystem.
- Keeps stock execution independent and only propagates lineage metadata.
- Preserves same-expiry defaults and introduces diagonal as additive opt-in.

## 11. Completion Report Format

Final closeout must include:

- changed contracts/modules by slice;
- tests/config validations and results;
- plan/code/deepreview/PR finding status;
- docs updated;
- residual risks with owner/destination;
- draft PR URL and branch/commit summary;
- explicit statement that production config/live state/notifications were not modified;
- next entry point: user reviews and merges the draft PR, then separately decides diagonal config/notification promotion.


## 12. Plan Review Fix Record

- **Review artifact**: `docs/reviews/plan-review-20260718-130142.md`
- **PR-1**: accepted; fixed by splitting option-only context inventory from event/assigned-stock full lifecycle reporting and removing assigned-stock inference from Close Advice.
- **PR-2**: accepted; fixed by adding candidate fingerprint/group-ID producer plus explicit structured/manual handoff contract; broker-only missing intent is an explicit fail-closed path.
- **PR-3**: accepted; fixed by defining one ordered `summary_classification` set and keeping quantities/labels/issues canonical.
- **Residual risks**: classified in §9.
