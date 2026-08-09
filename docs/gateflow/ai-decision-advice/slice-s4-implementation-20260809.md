# Gateflow Slice Implementation — S4 冻结输入与一张合约风险投影

- Gate: `implementation`（slice S4）
- Work unit: `ai-decision-advice`
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` S4

## Changed files

- `src/application/ai_decision_advice/contexts.py`（新增）：
  `freeze_candidates`（仅已接受候选、SP/CC 分组、候选字段投影）、
  `freeze_portfolio`（相对权重、匿名化、无 NAV/成本/账户标识）、
  `freeze_option_positions`（开放持仓最小字段 + 同标的/同到期关系、无
  lot/event id）、`freeze_external_evidence`（按标的最新有效证据 +
  coverage）、`build_frozen_inputs`（四类输入 + 四个 hash +
  evidence_run_id，`input_bindings` 对齐设计文档第 10 节）；
- `src/application/ai_decision_advice/projection.py`（新增）：
  `project_one_contract` / `project_all_candidates`：方向（指派增加持股 /
  被叫走减少持股）、合约股数、指派名义金额、当前标的/币种集中度、
  同向期权叠加数量、到期重叠数量；
- `tests/test_ai_decision_advice_contexts.py`（新增，7 例）、
  `tests/test_ai_decision_advice_projection.py`（新增，5 例）。

## 设计裁决（与设计文档同步）

- v1 无行业维度（无可靠数据源）；输入与投影只覆盖 symbol/currency/到期日；
- 组合输入是相对权重，无法确定性推导新增一张后的绝对权重；投影只输出
  可确定计算的事实，不伪造 after-trade 权重。设计文档第 8 节已同步该口径。

## Validation

- `python3.12 -m pytest tests/test_ai_decision_advice_contexts.py
  tests/test_ai_decision_advice_projection.py -q` → 12 passed；
- 隐私断言：portfolio 输出不含 `avg_cost` / `futu_account_id` / 现金绝对值；
  option positions 输出不含 lot/event id；contexts 输出无 `industry` 键。

## Residual risks

- `shared_expiry_with_other_position` 的语义较粗（同标的多个到期即 true），
  模型应主要引用 `expiry_overlap_count`——在 S5 prompt 与校验中约束；
- 证据为空（无候选标的）时 `external_evidence` 仍生成空 symbols 列表——
  covered by S5 合法零候选短路。

## Completion status

Complete；进入 code review gate。
