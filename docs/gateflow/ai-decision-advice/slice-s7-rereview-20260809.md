# S7 Re-review — 自审发现与处置

- Slice: S7
- Date: 2026-08-09

## 自审发现

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 高 | switch 结论缺合约与排序：`_ai_candidate_fact_maps` 只读 brief 形（dict of family lists），但渲染层拿到的是 view 形（扁平候选行），映射为空导致“建议建议改选。” | 已修复：`_ai_candidate_fact_maps` 同时接受 brief 形与 view 形；`_candidate_views` 输出补 `candidate_id`；新增端到端回归测试 |
| 2 | 中 | switch 文案 `建议建议改选` 重复（conclusion 已含“改选”） | 已修复：直接拼接 conclusion |
| 3 | 中 | 零候选家族（如 CC 无候选但 SP 有候选）仍显示 `## Covered Call` 模块 | 已修复：模块可见性 = 有候选 ∪ 有非零候选 AI 决策 |
| 4 | 中 | 折叠汇总行（“Sell Put 另有 N 个…”）全部落在最后一个模块之后，归属错位 | 已修复：按家族前缀归属到各自模块内 |
| 5 | 低 | 候选事实 `_candidate_view` 与 metrics 缺 `candidate_id` 和持有期收益字段 | 已修复：service 层传播 `candidate_id` 与 `period_net_return*` |
| 6 | 低 | 扁平断言全面禁止 `###`，与 design 15.1 的 `### AI建议` 冲突 | 已修复：断言放开唯一例外 `### AI建议`，其他 `###` 仍禁止 |
| 7 | 低 | 卡片持仓表格与逐项列表重复展示决策明细 | 已修复：卡片改逐项列表（15.5），决策明细块保留 |

## 已知限制

- 组合增强（combo_yield）无 AI 决策（v1 只覆盖 SP/CC），模块只渲染策略候选；
- AI 引用的候选行以 `candidate_id` 匹配，旧 run 无 `candidate_id` 的封存
  快照不回溯修复（仅当轮起生效）。

## 复测

- 全量 pytest：见实现工件“验证”节。

## 结论

S7 accepted：渲染符合 design 15.1–15.6 与 plan §S7。
