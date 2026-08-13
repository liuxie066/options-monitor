# Gateflow Implementation — combo-yield-rank-int-seal slice-1

- Work unit: `combo-yield-rank-int-seal`
- Gate: `implementation` (slice-1)
- Branch: `fix/combo-yield-rank-int-seal`
- Date: 2026-08-13

## Slice

- id: `slice-1`
- objective: 修正 `build_yield_enhancement_rank_shadow` 的 `baseline_rank` / `shadow_rank` 列
  dtype，从 float64 修正为 nullable `Int64`。

## Changed Files

- `src/application/sell_put_call_helper.py`
  - `build_yield_enhancement_rank_shadow` 末尾 `return pd.DataFrame(out)` 改为构造 `frame` 后对
    `baseline_rank` / `shadow_rank` 两列 `astype("Int64")` 再返回。
- `tests/test_sell_put_linked_call_helper.py`
  - 新增 `test_yield_enhancement_rank_shadow_emits_nullable_int_ranks`。

## Validation

```bash
./.venv/bin/python -m pytest tests/test_sell_put_linked_call_helper.py tests/test_combo_yield_candidate_snapshot.py tests/test_combo_yield_steps.py -q
```

结果：`55 passed in 0.60s`。

新增测试单独验证：`1 passed`。

## Completion Signal

- `build_yield_enhancement_rank_shadow` 的 `baseline_rank` / `shadow_rank` dtype 为 `Int64`。
- `to_dict("records")` 后 selected 行 rank 为 Python `int`（非 bool），unselected 行为 `None`。
- 既有 helper / seal / steps 测试全部通过，无回归。

## Residual Risks

- 无本轮新增风险。
- deferred（plan 已记录，与本次无关）：`opening_candidate_snapshot.json` 中 `contract_symbol`
  别名 `HK.POP261029C177500` 线索，建议后续单独核查。
