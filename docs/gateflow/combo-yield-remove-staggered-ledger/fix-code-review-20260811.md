# Gateflow Fix Artifact — Code Review Finding 1

- Gate: `code review -> fix -> re-review`
- Work unit: `combo-yield-remove-staggered-ledger`
- Review artifact: `docs/reviews/code-review-20260811-091411.md`
- Fix artifact path: `docs/gateflow/combo-yield-remove-staggered-ledger/fix-code-review-20260811.md`
- Status: `fix complete; re-review passed`

## Finding and decision

### DR-CODE-01 — accepted — fixed

归因 fail-closed 检查把“生产未写 expiry_structure”误判为不支持结构，所有生产同期组合将落入 partial。

根因：`resolve_event_attribution` 只从 `_KEYS` 取 `expiry_structure`，无 `structure_mode` fallback；而应用层没有任何
`expiry_structure` 写入方（`rg -n 'expiry_structure' src/ --glob '*.py'` 零命中），旧错期数据写入的是
`strategy_snapshot.structure_mode=staggered_expiry_pair`。`_build_topology` 把 `None`/`""` 判定为非 `same_expiry`，
导致所有生产组合 fail-closed 为 partial。

## Fixes applied

1. `domain/domain/performance/attribution.py`
   - 新增 `resolve_expiry_structure_from_fields(snapshot, payload)`：显式 `expiry_structure` 优先（lower）；
     `structure_mode=staggered_expiry_pair -> diagonal`、`same_expiry_pair -> same_expiry`；缺省返回 `None`
     （与 `combo_yield_lifecycle._expiry_structure` 的 fallback 语义一致，仅缺省保留 `None` 以维持序列化契约）。
   - `resolve_event_attribution` 在 `values["expiry_structure"]` 为空时调用该 helper。
   - `__all__` 增加 `resolve_expiry_structure_from_fields`。
2. `domain/domain/performance/strategy_attribution.py:144-146`
   - `_build_topology` 改为 `if structure and structure != "same_expiry":`，空值/None 按同期默认放行。
3. `domain/domain/performance/engine.py`
   - `_assigned_stock_attribution` 的 `expiry_structure` 取值改为复用 `resolve_expiry_structure_from_fields(snapshot, row)`，
     与归因解析器保持同一语义，避免 assigned-stock 语义分叉。

## Regression coverage

- `tests/test_performance_attribution.py`：
  - `test_production_form_without_expiry_structure_keeps_none`：生产形态（snapshot 无 expiry_structure/structure_mode）
    -> `resolve_event_attribution` 返回 attribution 且 `expiry_structure is None`。
  - `test_legacy_structure_mode_maps_to_expiry_structure`：`staggered_expiry_pair -> diagonal`、
    `same_expiry_pair -> same_expiry`。
- `tests/test_performance_strategy_attribution.py`：
  - `_event` helper 支持 `structure=None` 与 `structure_mode` 参数，可构造真实生产/旧错期数据形态。
  - `test_production_form_without_expiry_structure_is_ready`：生产形态组合 coverage `observed`、1 个 group、无 issues。
  - `test_legacy_staggered_structure_mode_fails_closed_to_partial_attribution`：旧错期形态 partial +
    `unsupported_expiry_structure`。
  - `test_legacy_same_expiry_structure_mode_is_ready`：`same_expiry_pair` 形态放行。

## Validation

- `./.venv/bin/python -m pytest tests/test_performance_attribution.py tests/test_performance_strategy_attribution.py tests/test_combo_yield_lifecycle.py tests/test_combo_reconciliation_domain.py -q` -> 49 passed
- `./.venv/bin/python -m pytest tests/test_trades_resolver_open.py tests/test_option_positions_cli.py tests/test_combo_yield_lifecycle.py tests/test_combo_reconciliation_domain.py tests/test_performance_attribution.py tests/test_performance_strategy_attribution.py tests/test_trades_receipt.py tests/test_positions_context_builder_partial_close.py -q` -> 173 passed
- `git diff --check` -> clean
- `compileall`（attribution/strategy_attribution/engine/resolver）-> ok

## Re-review conclusion

- Finding 1 状态：`已修复`。
- 建议改法全部落实：归因解析器 fallback、`_build_topology` 空值放行、`_assigned_stock_attribution` 复用同一 fallback、
  生产形态与旧错期形态回归测试。
- 无 blocking open question。

## Residual risks

- `fixed in current slice`：Finding 1 本身。
- `assigned to later work unit`：本 review 提及的「resolver 落账 -> projection -> build_period_performance」端到端集成回归
  属于 performance 报告入口的端到端验证，不在本 work unit 计划范围内，建议后续 performance 专项补测。
- `covered by later approved slice`：无。
- 混合单腿带结构字段的边界未单独测试（review residual risk，语义已由 fallback 覆盖，不阻塞本 gate）。
