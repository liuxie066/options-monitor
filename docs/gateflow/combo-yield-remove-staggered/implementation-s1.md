# Gateflow Implementation Artifact — Combo Yield 删除错期 S1

## Gate State

- Work unit: combo-yield-remove-staggered
- Slice: S1 — domain 排序/指标单一化 + 开仓扫描只产出同期
- Previous gate: accepted plan commit `5461d566`
- Current decision: implementation complete; enter mandatory S1 code review
- Next entry point: code review (S1)

## Implemented Scope

1. `validate_yield_enhancement_pair` 删除 `structure_mode`、`min_expiry_gap_days`、`max_expiry_gap_days`
   参数；删除 `normalized_structure_mode` 判断与 staggered 分支；`expiration !=` 改为无条件拒绝。
2. `compute_yield_enhancement_metrics` 删除 `structure_mode` 参数与 `is_staggered` 派生；四项
   `None if is_staggered else X` 终端/场景指标改为直接取值（`combo_breakeven`、
   `downside_breakeven_penalty`、`upside_breakeven`、`max_loss_if_zero`）。
3. `compute_yield_enhancement_funding_decision` 删除 `structure_mode` 参数。
4. 删除 `_is_staggered_expiry_pair`、`_funding_accepted`、`yield_enhancement_staggered_call_rank_key`、
   `yield_enhancement_staggered_rank_key`；`yield_enhancement_rank_key` 删除顶部 staggered dispatch；
   `rank_yield_enhancement_calls_for_put` 恒走 `rank_yield_enhancement_rows(copied)`。
5. `sell_put_call_helper.py`：删除 `resolve_staggered_expiry_gap_days` import；`_build_pair_row`
   删除 `is_staggered` 与全部错期分支（`expiration_scope`/`dte_scope` 固定 `"shared"`、metrics/funding
   字段直接取值、funding 参数直接传、末尾重复置 None 块删除），保留 `structure_mode` 行字段透传；
   `_load_yield_enhancement_call_legs_by_expiration` 删除 `structure_mode` 参数且
   `effective_min_strike = max(configured_min_strike, spot)` 改为无条件；
   `_build_yield_enhancement_pair_rows` 删除三个参数与 `policy_min/max_expiry_gap_days` 诊断字段、
   跨期 join 分支（固定 `call_legs_by_expiration.get(put_leg.expiration, [])`）、expected_iv 例外；
   主函数删除 structure/gap 解析块与 `resolve_staggered_expiry_gap_days` 调用，`call_window = put_window`。
6. 删除 `tests/test_sell_put_linked_call_helper.py` 中 3 个 staggered 用例：
   `test_staggered_expiry_policy_uses_full_put_premium_funding_defaults`、
   `test_staggered_expiry_pair_uses_later_call_and_explicit_leg_horizons`、
   `test_yield_enhancement_staggered_rank_uses_period_put_return`。
7. `domain/domain/engine/__init__.py` 无需修改：确认其未导出任何 staggered rank key，三个
   `compute_*/validate_*` 导出名不变。

## Code Review Fix（Finding 1）

- `_build_pair_row` 的 `structure_mode` 不再从 `enhancement_cfg` 读取，直接固定为
  `"same_expiry_pair"`（row 字段契约不变，值恒为同期）；消除 S1→S2 窗口内 staggered 配置
  产出"标签 staggered、内容同期"中间态行的风险。
- 复验：`test_sell_put_linked_call_helper.py` 21 passed；ruff 通过；
  `rg "is_staggered|staggered_expiry_pair" src/application/sell_put_call_helper.py` 零命中。

## Files Changed

- `domain/domain/engine/yield_enhancement.py`
- `src/application/sell_put_call_helper.py`
- `tests/test_sell_put_linked_call_helper.py`

## Validation

```text
PYTHONPYCACHEPREFIX=/tmp/om_combo_yield_s1 <PYTHON_BIN>/python \
  -m pytest tests/test_sell_put_linked_call_helper.py -q -p no:cacheprovider
```

Result: `21 passed in 1.16s`。

环境说明：`.venv/bin/python` 无 pytest（venv 未安装测试依赖），故使用 pyenv 的
`python -m pytest`（cwd 入 sys.path，`src` 可导入）；与计划命令等价，测试面相同。

Additional checks:

- `ruff check domain/domain/engine/yield_enhancement.py src/application/sell_put_call_helper.py` → `All checks passed!`
- `rg "yield_enhancement_staggered_rank_key|yield_enhancement_staggered_call_rank_key|_is_staggered_expiry_pair|is_staggered"`
  在 `domain/`、`src/application/sell_put_call_helper.py`、`tests/test_sell_put_linked_call_helper.py`
  零命中；全仓库仅剩 `ComboYieldResearchPolicy` 的 staggered/gap 支持（S3 范围，计划明确保留）。
- 全仓库 `rg "yield_enhancement_staggered|_is_staggered_expiry_pair|is_staggered"`（排除 output/docs）零命中。
- 三个 domain 函数的全部调用点仅剩 helper 内部与 `engine/__init__.py` 导出，签名同步干净。

## Covered Scenarios

- 同期组合指标不再出现错期 None（terminal/scenario 字段恒有值）。
- 同期配对只 join 同一 expiration 的 call leg。
- call 筛选 strike 下限恒为 `max(configured_min_strike, spot)`，不再有错期例外。
- 排序单一路径：`rank_yield_enhancement_calls_for_put` 恒走 `yield_enhancement_rank_key` 主键。
- 既有同期用例全部保持绿色（21 passed）。

## Non-Goals Preserved

- `ComboYieldResearchPolicy` 的 staggered/gap 支持保留到 S3。
- 配置/校验/数据需求规划（S2）、通知/research/文档（S3）不动。
- ledger、trades resolver、combo_pairing、CLI positions、lifecycle、reconciliation 不动。
- position advice dirty worktree 改动完整保留，未触碰重叠文件（`config_validator.py` 等）。
