# Slice 2 Implementation — 配置与 CC+LP 扫描编排

- Work unit: cc-lp-same-expiry
- Slice: 2 (配置与 CC+LP 扫描编排)
- Date: 2026-08-08
- Status: 完成（待 code review）

## Changed Files

- `src/application/cc_lp_steps.py`（新增）：
  - `run_cc_lp_scan`：独立 Sell Call 扫描（`run_sell_call_scan`）+ required-data Long Put 加载 + domain 配对 + 保留率 ≥0.20 门槛 + 排序；
  - `summarize_cc_lp_result`：summary 状态（candidates_found / no_candidate / not_applicable）；
- `src/application/combo_yield_steps.py`：`run_combo_yield_for_symbol_and_summarize` 按 `variant` 分派；新增 `run_cc_lp_variant`（从 portfolio_ctx 取 stock，无 stock → not_applicable）；
- `src/application/yield_enhancement_config.py`：`YIELD_ENHANCEMENT_VARIANTS = {sp_lc, cc_lp}`；`variant` 默认 `sp_lc`；`derive_yield_enhancement_policy` 解析 variant；
- `src/application/config_validator.py`：`variant` 加入 allowed fields + 枚举校验；
- `tests/test_cc_lp_steps.py`（新增）：7 个测试。

## Validation

- `pytest tests/test_cc_lp_steps.py`：7 passed
- 回归：`test_combo_yield_steps.py` + `test_combo_yield_pairing.py` + `test_sell_call_strategy_unification.py`（30 passed）+ config 相关（103 passed）
- `ruff check`：All checks passed

## Key Decisions

- CC+LP 独立扫描复用 `run_sell_call_scan`（继承 Sell Call 门槛），不依赖 Sell Call step 状态；
- stock 从 `portfolio_ctx` 注入，无 stock → not_applicable（plan review Finding 4 修复）；
- `variant` 默认 `sp_lc` 保持现行为不变（plan review Finding 2/3 方案 A）；
- 测试用 mock `run_sell_call_scan_fn` 验证配对逻辑（Sell Call 扫描门槛已有专测覆盖）。

## Findings / Residual Risks

- 无 accepted findings；Slice 3 需将 CC+LP 候选写入 sealed snapshot 并接入消费。
