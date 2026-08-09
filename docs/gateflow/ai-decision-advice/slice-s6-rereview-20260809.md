# S6 Re-review — 自审发现与处置

- Slice: S6
- Date: 2026-08-09

## 自审发现

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 中 | tick 编排未显式调用 advice 节点 —— 实际由 brief 装配在 diff/渲染前调用，固定简报与候选提醒两条路径共享同一 brief 段，满足 design 13.1；但挂点隐含，靠调用时序保证 | 接受：在实现工件中记录时序依据（persist/render 均消费装配完成的 brief）；不引入 tick 层平行调用，避免同一账户一轮两次 30s 预算 |
| 2 | 中 | `evidence_run_id` 绑定用 `frozen_at` 代替真实 collector run id；冻结索引不含 collector 的 evidence_run_id | 接受为已知简化：reuse 判定只看 4 个语义 hash（design 13.2），evidence_run_id 仅审计展示；后续若 collector 暴露 run id 可直接替换，不改契约 |
| 3 | 低 | portfolio context 适配同时接受 dict 与 list 两种形状 | 接受：list 分支仅做形状归一，不含业务逻辑；生产 prepared context 为 dict |
| 4 | 低 | 模型输入隐私断言只在编排测试覆盖（无账户标签、无 NAV） | 接受：contexts 层 S4 已有字段白名单测试；编排层断言为端到端兜底 |
| 5 | 低 | plan S6 尾项（collector CLI + timer）移到 S8 | 已记录在实现工件“范围裁剪”；S8 交付时闭环 |

## 复测

- `python3.12 -m pytest tests/test_ai_decision_advice_*.py
  tests/test_daily_decision_brief_service.py
  tests/test_daily_decision_brief_notification_flow.py
  tests/test_multi_tick_contract_batch2.py
  tests/test_unified_tick_entrypoint.py -q` → 212 passed。

## 结论

S6 accepted：挂点、复用、降级与 domain diff 语义符合 design 13.1 / 13.2 /
14.1 与 plan §S6（含前置契约）。
