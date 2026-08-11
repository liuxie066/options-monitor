# Gateflow Implementation Artifact — S3 通知/展示 + research/shadow + 文档 + 测试清理

- Gate: implementation
- Work unit: `combo-yield-remove-staggered`
- Slice: S3
- Date: 2026-08-11
- Branch: `design/combo-yield-remove-staggered`
- Base: `c9238062`（accepted S2 commit + main `afd670b8` clean merge）
- Plan: `docs/gateflow/combo-yield-remove-staggered/plan-20260810.md` §9 Slice 3
- Artifact path: `docs/gateflow/combo-yield-remove-staggered/implementation-s3.md`

## Objective

输出与研究工具不再存在错期：通知/展示单一只渲染同期组合，research/shadow/strategy-lab 只消费
`same_expiry_pair`，文档与测试与源码一致。ledger/生命周期保留面（Q2 用户决策）不动。

## Changed Files

源码（12）+ 文档（3）+ 测试（6）：

- `domain/domain/engine/yield_enhancement.py`
- `domain/domain/performance/strategy_attribution.py`
- `src/application/alert_engine.py`
- `src/application/notify_symbols.py`
- `src/application/render_yield_enhancement_alerts.py`
- `src/application/report_summaries.py`
- `src/application/shadow_replay/capture.py`
- `src/application/shadow_replay/combo_capture.py`
- `src/application/shadow_replay/combo_evaluation.py`
- `src/application/shadow_replay/combo_settlement.py`
- `src/application/shadow_replay/combo_variants.py`
- `src/application/strategy_lab/combo_evaluator.py`
- `docs/examples/combo-yield-shadow-variants.json`
- `docs/STRATEGY_ARCHITECTURE.md`
- `docs/PRODUCT_ARCHITECTURE.md`
- `tests/test_render_yield_enhancement_alerts.py`
- `tests/test_notify_symbols_markdown.py`
- `tests/test_report_summaries.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_combo_yield_research.py`
- `tests/test_strategy_lab.py`

合计 +108 / -641 行（相对 HEAD `c9238062`，即 S3 全部未提交改动，含 code review
修复的 removed-gap fail-closed 校验与回归测试）。

## Exact Changes

### `domain/domain/engine/yield_enhancement.py`（ComboYieldResearchPolicy）

- 删除 `min_expiry_gap_days` / `target_expiry_gap_days` / `max_expiry_gap_days` 字段。
- `__post_init__`：`structure_mode` 只接受 `same_expiry_pair`；删除 gap 三元组存在性/边界校验分支。
- `combo_yield_proposed_gate_reasons`：删除 staggered 分支，恒为同期 gate——
  `_expiry_gap_days(row)` 非 0/None 记 `same_expiry`，`call strike < spot` 记
  `call_strike_below_spot`（两处均为同期既有 gate，保留 `_expiry_gap_days` 日期推导 helper）。
- `combo_yield_proposed_rank_key`：删除 `target_expiry_gap_days` 距离项，排序键恒为同期路径。

### `domain/domain/performance/strategy_attribution.py`

- 仅从共享结构校验 set 移除 `staggered_expiry_pair`，保留 `diagonal` 分支与
  `invalid_diagonal_expiry_order` 校验（plan §8 Decision 5）。

### `src/application/notify_symbols.py`

- 删除 `_format_staggered_combo_alert()`（compact 与普通两套“组合·跨期”渲染，-118 行）。
- `_format_alert_line` / `_format_alert_line_compact` 删除 `staggered_expiry_pair` dispatch，
  Combo Yield 恒走同期渲染。

### `src/application/render_yield_enhancement_alerts.py`

- `render_one` 删除 staggered 分支与“错期全额融资”文案（-41 行）；同期模板不变。

### `src/application/alert_engine.py`

- `classify_alert` 删除 `staggered_expiry_pair + funding_accepted → high` 特判，
  Combo Yield 统一按 `annual > 0` 判定高低。

### `src/application/report_summaries.py`

- `summarize_yield_enhancement` note 恒为“已按组合收益筛出推荐Call”，删除
  staggered 专属“Put已独立通过接货、现金、事件、收益和流动性门槛”分支。

### `src/application/shadow_replay/capture.py`

- `_combo_pair_group_id` 删除 staggered 分支（两腿各自 expiration 的 group 键），
  恒用共享 `expiration/exp` 构造 group id。

### `src/application/shadow_replay/combo_capture.py`

- `_variant_combo_config` 删除三个 gap 字段透传与 staggered `_explicit_fields` 更新。

### `src/application/shadow_replay/combo_evaluation.py`

- `_superset_combo_config` 删除 staggered 分支的 gap min/max 合并与 explicit 更新。

### `src/application/shadow_replay/combo_settlement.py`

- `_settle_one` 删除按 `structure_mode` 选 `call_horizon` 的分流，恒
  `call_horizon = "put_expiry"`（同期结算；capital_days / funding_horizon 语义保留）。

### `src/application/shadow_replay/combo_variants.py`

- `combo_research_policy_from_dict` 删除三个 gap 字段解析。
- code review 修复：`combo_research_policy_from_dict` 对残留的
  `min_expiry_gap_days` / `target_expiry_gap_days` / `max_expiry_gap_days`
  显式 `ValueError`（fail closed，与 runtime config_validator 语义一致），
  避免 same_expiry_pair variant 携带已移除字段时被静默忽略。
- `build_combo_pair_decisions` 删除 `expiry_gap_target_distance` 输出；
  `_rank_change_reason` 删除 `policy` 参数与 staggered 文案，恒
  `"funding_put_rank_then_delta_liquidity"`。
- 删除 `_gap_target_distance`；`_expiry_gap_days` 保留（行字段 `expiry_gap_days`
  透传契约，同期值 0）。

### `src/application/strategy_lab/combo_evaluator.py`

- 删除 `_STAGGERED_EXPIRY_PAIR`；`_group_structure_mode` 只接受 `same_expiry_pair`。
- `_identity_blockers` 删除 `structure_mode` 参数与 staggered 到期顺序分支：
  expiration 缺失记 `combo_yield_expiration_missing`，多到期记
  `combo_yield_expiration_mismatch`（当前唯一 expiry blocker）。
- 删除 `_expiration_date` 与 `combo_yield_multi_horizon_outcome_evidence_insufficient`
  blocker（staggered 专用）。

### 文档

- `docs/examples/combo-yield-shadow-variants.json`：删除两个 staggered variant，
  仅保留 same-ret75-d20 / same-ret75-d25（JSON 校验通过）。
- `docs/STRATEGY_ARCHITECTURE.md`：结构表只列 `same_expiry_pair`；新增
  “错期结构（已移除）”说明（历史错期持仓仍由 ledger 生命周期与 `pair-combo-yield`
  管理）；召回/硬筛、配对约束、配置示例边界、required-data 步骤、跨期归因章节
  全部单一化为同期语义。
- `docs/PRODUCT_ARCHITECTURE.md`：开仓机会监控与当前对齐段落改写为仅支持
  `same_expiry_pair`；保留 ledger 面“含历史错期组合”精确配对说明。

### 测试

- `tests/test_render_yield_enhancement_alerts.py`：删除错期渲染测试。
- `tests/test_notify_symbols_markdown.py`：删除 `_staggered_combo_summary()` 与两个错期通知测试。
- `tests/test_report_summaries.py`：删除错期汇总测试（note 恒为同期文案）。
- `tests/test_daily_decision_brief_service.py`：三处 `staggered_expiry_pair` →
  `same_expiry_pair`、`call_expiration` 与 put 同期；断言顺序保持 `pair-c, pair-a`。
- `tests/test_combo_yield_research.py`：删除
  `test_staggered_combo_settlement_models_assignment_residual_call_and_capital`，改写为
  `test_combo_settlement_models_assignment_capital_at_same_expiry`（同期 marks、
  `post_put_expiry_state == "terminal"`；断言 put_pnl=-500、call_pnl=-100、
  assigned_stock_continuation_pnl=0、full_shadow_group_pnl=-600、
  funding_horizon_pnl=-600、assigned_stock_capital_days=0、
  early_assignment_stress_status="incomplete"）；baseline/proposed 隔离测试只留同期
  same-d20；`expiry_gap_days: 0`、`mode="put"`、`structure_mode="same_expiry_pair"`
  保留（字段契约/卖 Put rank）。
- `tests/test_strategy_lab.py`：改写为
  `test_shadow_replay_capture_and_evaluator_preserve_same_expiry_combo_horizons`
  （同期 TSLA 2026-08-21）与
  `test_combo_yield_group_evaluator_rejects_expiration_mismatch`
  （`combo_yield_expiration_mismatch` 是唯一 expiry blocker）。
- `tests/test_combo_yield_research.py`：新增
  `test_combo_research_policy_rejects_removed_expiry_gap_fields`（三个 removed
  gap 字段任一存在即 `ValueError`）。

## Validation

```text
PYTHONPYCACHEPREFIX=/tmp/om_s3 python3.12 -m pytest \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_sell_put_yield_enhancement_validate_config.py \
  tests/test_sell_put_yield_enhancement_required_data_planning.py \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_render_yield_enhancement_alerts.py \
  tests/test_notify_symbols_markdown.py \
  tests/test_report_summaries.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_combo_yield_research.py \
  tests/test_strategy_lab.py -q -p no:cacheprovider
→ 238 passed in 3.07s
```

- ruff（plan §9 S3 文件清单）→ `All checks passed!`
- code review 修复后：`tests/test_combo_yield_research.py` → 17 passed；
  10 文件复跑（含 performance attribution / lifecycle / positions context）→
  215 passed；ruff 变更文件 → `All checks passed!`
- `./om config validate --source yaml --market us/hk --config-yaml configs/examples/config.yaml.example` → ok
- `./om config build --source yaml --market us/hk --config-yaml configs/examples/config.yaml.example --dry-run`
  → ok（write_applied: false）
- 最终扫描 `rg -i "staggered|错期" domain/ src/ tests/`：剩余命中逐条确认全部属于保留面：

| 归属 | 文件 |
|---|---|
| ledger 生命周期/保留面 | `domain/domain/combo_reconciliation.py`、`domain/domain/combo_yield_lifecycle.py`、`src/interfaces/cli/option_positions.py`、`src/application/trades/resolver.py`、`src/application/positions/combo_pairing.py` |
| 保留面测试 | `tests/test_combo_yield_pairing.py`、`tests/test_combo_reconciliation_domain.py`、`tests/test_trades_receipt.py`、`tests/test_trades_resolver_open.py` |
| S2 有意保留（removed-fields fail closed） | `src/application/config_validator.py`（报错文案）、`tests/test_sell_put_yield_enhancement_validate_config.py`（removed-gap 拒绝测试） |

S3 范围文件零残留。

## Completion Signal 核对

plan §9 Slice 3 completion signal（各通知/研究测试通过、ruff 通过、最终扫描剩余命中
逐条人工确认）已满足。12 个 S3 源文件与 3 个文档在 S3 范围外无 `staggered` 引用
（`_expiry_gap_days` 仅剩同期 gate 日期推导与 `expiry_gap_days: 0` 字段契约两处有意保留）。

## Residual Risks

- 历史错期持仓仍由 ledger/生命周期面管理（trades resolver、combo_pairing、CLI、lifecycle、
  reconciliation），与“候选/研究/通知不再产出错期”并存；文档已注明边界。
- `structure_mode` / `expiry_gap_days` 数据字段契约保留（值恒 `same_expiry_pair` / 0），
  下游透传面未做字段删除，避免破坏既有 artifact 读路径。
- `.venv/bin/python` 无 pytest，验证使用 pyenv `python3.12 -m pytest`（与 S1/S2 相同）。
- 历史 shadow artifact 兼容：已归档的旧错期 trace/decision 若被重新 capture/settle，
  会按同期语义统一 group id（capture）或在 `call_expiry` horizon mark 缺失时
  fail closed（settlement）；research/shadow 为候选侧，生产路径不产生旧错期数据，
  归入 later work unit。
- position-advice 与 S3 测试文件的物理重叠已随 main merge（`c9238062`）清除；
  当前工作树无 config_validator.py 改动，提交无需 hunk 隔离。
- 全量 pytest 既有基线：HTTP/时间敏感用例不作为本 work unit 回归判据。

## Completion Status

S3 implementation 完成；code review 通过（1 个 accepted 低危 finding 已修复并
re-review，1 个候选 finding 证据不足拒绝），待 accepted slice commit。
