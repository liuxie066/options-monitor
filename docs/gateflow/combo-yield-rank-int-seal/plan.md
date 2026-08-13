# Gateflow Plan — combo-yield-rank-int-seal

- Work unit: `combo-yield-rank-int-seal`
- Gate: `plan`
- Branch: `fix/combo-yield-rank-int-seal`
- Date: 2026-08-13

## Goal / Motivation / Success Signal

- Goal: 修复 combo yield（sp_lc）候选快照密封时 `baseline_rank` / `shadow_rank` 的类型契约，使
  producer 产出的 rank 列经 `to_dict("records")` + `normalize_json_value` 后为 Python `int`
  （选中行）或 `None`（未选中行），从而通过 `_rank_records` 的严格 int 校验。
- Motivation: 线上 HK 09:40 批次 `lx` / `sy` 均因 `rank record baseline_rank is invalid`
  失败，Daily Brief 渲染成固定失败文案「数据异常 · 09:40 批次失败」。
- Success signal: `build_yield_enhancement_rank_shadow` 返回的 `baseline_rank` / `shadow_rank`
  两列为 pandas nullable `Int64` dtype；`to_dict("records")` 后选中行 rank 是 Python `int`
  且非 `bool`；未选中行为 `None`；`_rank_records` 对真实 producer 输出不再抛错。

## Non-goals / Scope Boundary

- 不修改 `_rank_records` 的严格 int 校验语义（保留 `isinstance(rank, int)`、排除 bool、`> 0`）。
- 不修改 `candidate_snapshot_contract.normalize_json_value` 的全局 float 归一化语义（避免扩大 blast radius）。
- 不引入新的 dtype 抽象层、不新增配置项、不新增实体/状态。
- 不处理 `contract_symbol` 别名线索（`HK.POP...`），作为 deferred follow-up 记录。
- 不部署、不升级远端、不发消息、不改生产配置。

## Goal Alignment

- 唯一改动（producer dtype 修正）直接映射到 success signal 的第 1 条（rank 列 nullable Int64）。
- 唯一新增测试直接映射到 success signal 的第 2 条（to_dict 后 int/None，且非 bool）。
- 不触碰 non-goals 中列出的任何边界。

## First-principles Judgment & Direct Code Evidence

根因链（已逐一读代码确认，非猜测）：

1. `src/application/sell_put_call_helper.py:995` `build_yield_enhancement_rank_shadow` 末尾
   `return pd.DataFrame(out)`。`out` 中 `baseline_rank` / `shadow_rank` 是 `int | None` 混合
   （`enumerate(..., start=1)` 产生 int，未选中为 `None`），pandas 自动推断为 `float64`
   （int 变 `1.0`，None 变 `NaN`）。
2. `src/application/combo_yield_steps.py:344` 用 `rank_shadow.to_dict("records")` 转 dict，
   产出 `1.0` / `NaN`。
3. `src/application/candidate_snapshot_contract.py` `normalize_json_value` 把 NaN 归一为 `None`，
   但对 `Real` 保留 float，因此 `1.0` 仍是 float。
4. `src/application/combo_yield_candidate_snapshot.py:308` `_rank_records` 要求
   `isinstance(rank, int)`，float `1.0` 被拒，抛 `rank record baseline_rank is invalid`。
5. 子进程 exit 1 → Daily Brief 渲染固定失败文案，`lx` / `sy` 同时失败。

所有权判断：

- `build_yield_enhancement_rank_shadow` 是 `baseline_rank` / `shadow_rank` 的唯一 producer
  （`grep` 确认仅 `combo_yield_steps.py:261` 调用）。
- 类型缺陷在 producer 边界（把「整数位次」表达成 float64 列），密封端 `_rank_records` 的 int
  契约是正确的。因此根因修复应在 producer，而非放宽接收端。

## Affected Files / Modules

- `src/application/sell_put_call_helper.py` — `build_yield_enhancement_rank_shadow`（唯一源码改动）。
- `tests/test_sell_put_linked_call_helper.py` — 新增一个 focused 回归测试。

## Contract / Schema / State-machine / Public-interface Changes

- 无外部契约变化。`combo_yield_candidate_snapshot.v2` schema 不变；`baseline_rank` / `shadow_rank`
  的语义（int 或 null）不变，仅修正 producer 表达为 float64 的实现缺陷。
- 无状态机变化、无公共命令/工具 payload 变化。

## Implementation Decisions

在 `build_yield_enhancement_rank_shadow` 末尾，把两列显式转换为 pandas nullable `Int64`：

```python
    frame = pd.DataFrame(out)
    for column in ("baseline_rank", "shadow_rank"):
        frame[column] = frame[column].astype("Int64")
    return frame
```

依据（已本地验证）：

- `astype("Int64")` 后 dtype 为 `Int64Dtype()`，`to_dict("records")` 产出选中行 Python `int`
  （非 `numpy.int64`，非 float），未选中行 `None`。
- `_rank_records` 中 `normalize_json_value` 对 `Integral` 转 `int`，对 pandas `NA` 转 `None`，
  两列均能通过现有校验。
- 空 DataFrame 分支（`pairs_df.empty` 时 `return pd.DataFrame(columns=...)`）保持不变；
  非空路径下 `out` 由非空 `rows` 构造，两列必然存在，`astype` 安全。

## Implementation Slices

单一 slice（修复足够小，拆分不产生可验证增量）：

### slice-1: producer 输出 nullable int rank

- id: `slice-1`
- objective: 修正 `build_yield_enhancement_rank_shadow` 的 rank 列 dtype。
- allowed files: `src/application/sell_put_call_helper.py`,
  `tests/test_sell_put_linked_call_helper.py`
- expected outcome: rank 两列为 `Int64`；`to_dict("records")` 后 selected 行 int、unselected 行 None。
- exact allowed changes: 仅上述 `astype("Int64")` 两列转换 + 一个 focused 回归测试。
- non-goals: 不改 normalizer、不改 seal 校验、不改其他列 dtype。
- completion signal: 新增测试通过；现有 helper/seal/steps 测试通过。

## Tests / Validation Commands & Expected Assertions

```bash
./.venv/bin/python -m pytest \
  tests/test_sell_put_linked_call_helper.py \
  tests/test_combo_yield_candidate_snapshot.py \
  tests/test_combo_yield_steps.py \
  -q
```

新增测试（置于 `tests/test_sell_put_linked_call_helper.py`）：

```python
def test_yield_enhancement_rank_shadow_emits_nullable_int_ranks() -> None:
    from src.application.sell_put_call_helper import build_yield_enhancement_rank_shadow

    rows = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C110",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C110",
                "funding_accepted": True,
                "premium_funding_score": 0.9,
                "net_credit_retention": 0.80,
                "call_cost_to_put_credit": 0.20,
                "call_delta": 0.18,
                "call_spread_ratio": 0.12,
                "call_open_interest": 500,
                "call_payoff_multiple_at_1_5_sigma": 1.8,
                "call_payoff_multiple_at_2_0_sigma": 4.0,
                "put_assignment_margin_pct": 0.05,
                "put_only_annualized_net_return": 0.14,
                "combo_spread_ratio": 0.20,
                "annualized_net_credit_yield": 0.09,
                "residual_premium_ratio": 0.80,
            },
            {
                "symbol": "NVDA",
                "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C115",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C115",
                "funding_accepted": True,
                "premium_funding_score": 1.1,
                "net_credit_retention": 0.88,
                "call_cost_to_put_credit": 0.12,
                "call_delta": 0.10,
                "call_spread_ratio": 0.08,
                "call_open_interest": 900,
                "call_payoff_multiple_at_1_5_sigma": 1.0,
                "call_payoff_multiple_at_2_0_sigma": 3.0,
                "put_assignment_margin_pct": 0.05,
                "put_only_annualized_net_return": 0.14,
                "combo_spread_ratio": 0.15,
                "annualized_net_credit_yield": 0.11,
                "residual_premium_ratio": 0.88,
            },
        ]
    )
    shadow = build_yield_enhancement_rank_shadow(rows)

    assert str(shadow["baseline_rank"].dtype) == "Int64"
    assert str(shadow["shadow_rank"].dtype) == "Int64"
    # 两行同 put、不同 call：baseline 与 shadow 各选一行，产生 selected(int) + unselected(None)。
    assert any(record["baseline_rank"] is None for record in shadow.to_dict("records"))
    assert any(record["shadow_rank"] is None for record in shadow.to_dict("records"))
    for record in shadow.to_dict("records"):
        for field in ("baseline_rank", "shadow_rank"):
            value = record[field]
            assert value is None or (isinstance(value, int) and not isinstance(value, bool))
```

断言目的：锁定 producer 不再产出 float64 rank；非空 rank 是 Python int 且非 bool（与
`_rank_records` 契约一致）；同时覆盖 selected(int) 与 unselected(None) 两个分支。

## Docs Decision

- 新增 `docs/gateflow/combo-yield-rank-int-seal/plan.md`（本文档）。
- 后续 gate 的 review/fix/closeout artifact 同目录落地。
- 无需更新 `docs/AGENT_WIKI.md`（无公共命令/契约变化）。

## Risks / Open Questions

- 无 blocking open question。
- Residual risk（本轮不处理，deferred）：`opening_candidate_snapshot.json` 中
  `contract_symbol` 出现别名 `HK.POP261029C177500` 的线索，与本次根因无关，建议后续单独核查。

## Completion Report Format

- changed files；
- test command + result；
- finding status（accepted/rejected/deferred 及原因）；
- residual risks（含 owner/destination）；
- draft PR URL；
- next entry point。

## Over-design / Goal-drift 说明

- 本方案只改 producer 两列 dtype，不新增层/实体/配置，不触碰正常化与密封逻辑，无 goal drift。
- 备选方向（在 `normalize_json_value` 对积分 float 转 int）被排除，因其全局影响所有 float
  字段、可能静默改变无关证据语义，blast radius 大于根因修复本身。
