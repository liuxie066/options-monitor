# S8 Re-review — 自审发现与处置

- Slice: S8
- Date: 2026-08-09

## 自审发现

| # | 严重度 | 发现 | 处置 |
|---|--------|------|------|
| 1 | 高 | 不完整 config.yaml（缺 markets）或缺失 config.yaml 使 `render_service_bundle` 抛 CONFIG_ERROR，回归 8 个既有测试（feishu_ws / wechat / upgrade / rollback） | 已修复：可选 add-on 判定降级到 JSON runtime config，不阻断 bundle render |
| 2 | 中 | 新 symbol 无首次搜索 cutoff（`compute_cutoffs` 只按 last_success 键） | 已修复：逐 observed symbol 生成 cutoff，回归断言 `NVDA in cutoffs` |
| 3 | 中 | 用户指出设计文档冗余：7.2“关键集中度”与权重重复；§18 残留 industry 维度 | 已修复：删除冗余表述，§18 改为 symbol/currency |
| 4 | 低 | CLI dry-run 在模块 disabled 时直接 skipped，不输出规划细节 | 接受：与 config contract 一致（disabled = 不运行两个阶段） |
| 5 | 低 | collector timer 未加入 service drift / profile 声明 | 接受为 v1 限制：unit 在 bundle 内渲染安装；profile/drift 扩展留待后续 |

## 已知限制

- timer 触发时不感知 tick 锁，可能与 tick 并发（collector 只写自己的 state 目录，
  且 4 小时粒度下重叠影响小）；
- observation set v1 只来自 scan symbols（持仓/期权标的来源接入留待后续，
  不影响 enabled=false 的默认行为）。

## 复测

- `tests/test_service_deploy.py` 147 passed；`tests/test_ai_decision_advice_collector_cli.py` 4 passed；
  `tests/test_daily_decision_brief_agent_tool.py` 11 passed；agent contract 与 dependency graph passed。

## 结论

S8 accepted：CLI/timer/读面/文档符合 plan §S8 与设计合同 4.1、19。
