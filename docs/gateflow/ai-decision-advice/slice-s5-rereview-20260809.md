# S5 Re-review — 自审发现与修复

- Slice: S5
- Date: 2026-08-09

## 发现与处置

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 高 | 跨策略候选引用未拦截：CC scope 可引用 Sell Put 候选 id 并通过校验，违反 docs 9.2 策略边界 | 已修复：`selected` 不在本 scope 合格池内一律降级 `needs_review`（switch → `switch_out_of_pool`，其他动作 → `selected_out_of_strategy_pool`）；新增回归测试 |
| 2 | 中 | brief view 语义错位：`covered_call` 家族合法零候选时返回 `[]` 而非契约要求的 `None`；SP 家族零候选但模型仍返回 SP decision 时会泄露 decision 而非 `None` | 已修复：`result_for` 按 `zero_candidate` 标记生成 `None`；新增回归测试 |
| 3 | 低 | 模型遗漏某个 scope 的 decision 时校验静默放行，该 scope 在 brief 中显示为无建议 | 接受为设计内行为：输出 schema 未要求穷举 scopes，渲染层对缺失 scope 显示“无 AI 建议”；记录为已知限制，不在 v1 扩展 schema |

## 复测

- `python3.12 -m pytest tests/test_ai_decision_advice_*.py -q` → 85 passed。

## 结论

S5 accepted：实现与 docs 9.7 / 9.8 / 10 / 12.3 / 13.1 / 13.2 及 plan §S5、
§S6 前置契约一致。
