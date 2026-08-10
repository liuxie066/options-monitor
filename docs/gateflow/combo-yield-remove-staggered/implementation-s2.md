# Gateflow Implementation Artifact — S2 配置/校验/数据需求规划删除错期

- Gate: implementation
- Work unit: `combo-yield-remove-staggered`
- Slice: S2
- Date: 2026-08-10
- Branch: `design/combo-yield-remove-staggered`
- Base: `fe967db9`（accepted S1 commit）
- Plan: `docs/gateflow/combo-yield-remove-staggered/plan-20260810.md` §9 Slice 2
- Artifact path: `docs/gateflow/combo-yield-remove-staggered/implementation-s2.md`

## Objective

配置面无法表达错期；数据需求规划（required data planning / prefetch planning）单一化到同期语义。
旧配置若含 `min_expiry_gap_days` / `max_expiry_gap_days`，validator 必须显式报错（fail closed），
不静默兼容。

## Changed Files

- `src/application/yield_enhancement_config.py`
- `src/application/config_validator.py`
- `src/application/required_data_planning.py`
- `src/application/required_data_prefetch_planning.py`
- `configs/examples/user.example.us.json`
- `tests/test_sell_put_yield_enhancement_validate_config.py`
- `tests/test_sell_put_yield_enhancement_required_data_planning.py`
- `tests/test_required_data_prefetch_inprocess.py`

合计 +40 / -258 行。

## Exact Changes

### `src/application/yield_enhancement_config.py`

- `YIELD_ENHANCEMENT_STRUCTURE_MODES` 只保留 `{"same_expiry_pair"}`。
- 删除 `DEFAULT_STAGGERED_MIN_EXPIRY_GAP_DAYS` / `DEFAULT_STAGGERED_MAX_EXPIRY_GAP_DAYS`。
- 删除 `YIELD_ENHANCEMENT_STRUCTURE_DEFAULTS` 整块（错期默认值）。
- 删除 `resolve_staggered_expiry_gap_days()`。
- `apply_yield_enhancement_defaults()`：删除 structure_defaults 合并，直接
  `_deep_merge_dict(defaults, raw_cfg)`。
- `derive_yield_enhancement_policy()`：删除 structure_defaults 合并；`structure_mode` 读取与
  `cfg["structure_mode"]` 透传保留（数据驱动字段契约，配置路径已无法表达 staggered）。

### `src/application/config_validator.py`

- `YIELD_ENHANCEMENT_ALLOWED_FIELDS` 移除 `min_expiry_gap_days` / `max_expiry_gap_days`。
- `_validate_yield_enhancement_cfg()` 在 `_reject_unknown_keys(...)` 之前新增显式 removed-fields
  检查：命中任一 gap 字段即
  `die("...has removed staggered-expiry gap fields: ...; Combo Yield supports same_expiry_pair only")`。
  仿照既有 `removed_funding_keys` 先例（fail closed，旧配置不得偷偷改变语义）。
- 删除 gap 数值/大小/组合校验分支（含 min>max、`structure_mode=staggered` 关联检查）。
- `structure_mode` 校验的 else 分支删除（`structure_mode` 局部变量不再被消费）。
- `call.min_dte/max_dte` 的报错文案更新：不再提示 staggered gap，
  改为 "combo_yield.call DTE is derived from sell_put; use sell_put.min_dte/max_dte instead"。

### `src/application/required_data_planning.py`

- 删除 `resolve_staggered_expiry_gap_days` import；删除 `DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW`
  import（同期路径不再使用）。
- 删除 `_filter_staggered_call_expirations()` 整个函数。
- `_resolve_combo_yield_call_plan()` 单一化：删掉 staggered 分支与 gap 后处理块，恒走同期路径——
  call DTE 从 sell_put 窗口继承、`dte_source_prefix="sell_put"`、返回 `_resolve_call_side_plan` 结果。

### `src/application/required_data_prefetch_planning.py`

- 删除 `resolve_staggered_expiry_gap_days` import；删除 `DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW`
  import。
- `strategy_prefetch_kwargs()` 的 `want_yield_call` 块单一化：删掉 staggered 分支，
  恒按 sell_put 窗口派生 call DTE。

### `configs/examples/user.example.us.json`

- `structure_mode` 改为 `"same_expiry_pair"`；删除 `min_expiry_gap_days` / `max_expiry_gap_days`。

### 测试

- `tests/test_sell_put_yield_enhancement_validate_config.py`：
  - `test_validate_config_accepts_staggered_expiry_gap_bounds` → 改写为
    `test_validate_config_rejects_removed_staggered_expiry_gap_fields`（旧配置报错，断言
    "has removed staggered-expiry gap fields"）。
  - `test_validate_config_rejects_absolute_call_dte_for_combo_yield` 的 `structure_mode` 改为
    `same_expiry_pair`（call DTE 拒绝语义不变）。
  - 删除 `test_validate_config_rejects_invalid_staggered_expiry_gap_bounds`（gap bounds 校验已不存在）。
- `tests/test_sell_put_yield_enhancement_required_data_planning.py`：删除
  `test_staggered_combo_yield_fetches_call_on_independent_later_dte_window`（同期路径由既有
  same-expiry 用例覆盖，该用例前提"独立错期窗口"已不成立）。
- `tests/test_required_data_prefetch_inprocess.py`：
  `test_strategy_prefetch_kwargs_requests_combo_put_and_call_when_sell_put_disabled` 删除
  staggered/gap 配置，期望 `max_dte` 从 90 改为 60（同期窗口 = sell_put 窗口 20-60）。

## Validation

```text
pytest tests/test_sell_put_yield_enhancement_validate_config.py tests/test_sell_put_yield_enhancement_required_data_planning.py tests/test_required_data_prefetch_inprocess.py -q
→ 67 passed

pytest tests/test_combo_yield_steps.py tests/test_cc_lp_steps.py tests/test_config_yaml.py tests/test_sell_put_linked_call_helper.py tests/test_config_template_inheritance.py tests/test_config_loader_validation_cache.py -q
→ 124 passed（配置解析/校验邻近回归）

ruff check（4 个 src 文件）
→ All checks passed!

./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
→ ok
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
→ ok
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
→ ok（write_applied: false）
```

## Completion Signal 偏差说明

Plan 的 S2 completion signal 写的是 4 个 src 文件 `rg -i "staggered|gap_days"` 零命中；但 plan §8
Decision 3 同时要求 config_validator 增加 removed-fields 显式检查，该检查必须包含
`min_expiry_gap_days` / `max_expiry_gap_days` 字段名才能检测旧配置（与 `removed_funding_keys`
先例一致）。因此 `config_validator.py` 保留两处字面量（removed-fields 检测与报错文案）为**有意命中**：
- 其余三个 src 文件 + 示例配置：`rg -i "staggered|gap_days"` 零命中。
- `config_validator.py` 剩余命中全部属于 removed-fields 检查本身，无任何 staggered 分支逻辑。

## Residual Risks

- 未覆盖区域：S3 文件（render/notify/alert/report/research/shadow/文档）仍含 staggered/gap 引用，
  属于 plan §9 Slice 3，后续 slice 清理。
- 旧运行时配置若含 gap 字段，validator 会 fail closed（目标行为）；`config.yaml` /
  `config.us.json` / `config.hk.json` 生产配置无 gap 字段，无需迁移。
- ledger/生命周期（trades/combo_pairing/CLI）staggered 支持保留，属 work unit non-goal。
- `structure_mode` 数据字段契约保留（值为 `same_expiry_pair`），`derive_yield_enhancement_policy`
  仍按原始配置透传该字段；validator 收紧后非法值无法通过配置面进入。

## Completion Status

S2 implementation 完成，待 code review。
