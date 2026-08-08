# Slice 1 Implementation — CC+LP Domain 层

- Work unit: cc-lp-same-expiry
- Slice: 1 (domain 层 CC+LP 角色与组合指标)
- Date: 2026-08-08
- Status: 完成（待 code review）

## Changed Files

- `domain/domain/engine/cc_lp.py`（新增）：
  - `validate_cc_lp_pair`：call_strike > put_strike、同到期/symbol/currency/multiplier、put delta 0.10~0.25、execution price；
  - `compute_cc_lp_metrics`：净权利金 = call bid×mult − put ask×mult；保留率 = net_credit / call_net_credit；收益率 = net_credit / covered_notional（不扣净权利金）；
  - `cc_lp_rank_key` / `rank_cc_lp_rows`：保留率主键 + 反转腿 delta 趋近 0.12 次键；
- `domain/domain/engine/__init__.py`：导出 CC+LP 符号；
- `tests/test_cc_lp_domain.py`（新增）：8 个测试。

## Validation

- `pytest tests/test_cc_lp_domain.py`：8 passed
- `pytest tests/test_combo_yield_pairing.py tests/test_combo_yield_steps.py`：25 passed（SP+LC 回归）
- `ruff check domain/domain/engine/cc_lp.py domain/domain/engine/__init__.py tests/test_cc_lp_domain.py`：All checks passed

## Key Decisions

- 不改现有 `compute_yield_enhancement_metrics` / `validate_yield_enhancement_pair` 签名（SP+LC 零改动）；
- 新增独立 `cc_lp.py` 模块承载 CC+LP 角色（资金腿 call / 反转腿 put）；
- covered_notional 由外部传入，不扣净权利金（与已确认口径一致）；
- 角色参数化解决 plan review Finding 1（strike_order 方向）。

## Findings / Residual Risks

- 无 accepted findings；covered_notional 必须由 Slice 2 的持仓上下文提供（后续 slice 覆盖）。
