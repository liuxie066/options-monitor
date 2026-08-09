# S7 Implementation — Daily Brief 渲染

- Slice: S7
- Date: 2026-08-09
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` §S7

## Files

- `src/application/ai_decision_advice/render.py`（新增）
  - `render_family_advice_lines`：SP 单条 / CC 按标的聚合的 `### AI建议`
    区块；keep / switch / defer / needs_review / unavailable / 合法零候选
    六类文案（design 15.2）；switch 结论带完整合约；原因最多两句
    （15.3）；来源最多 3 条且仅 switch/defer/needs_review 展示（15.4）。
- `src/application/daily_decision_brief_renderer.py`（改动）
  - 固定简报改模块化布局：`## Sell Put` / `## Covered Call` /
    `## 组合增强` 模块内 `### AI建议` + `策略候选`；候选提醒只展示受影响
    模块的 AI 区块（13.1 / 15.1）；
  - 候选标签 `首选/备选 N` → `策略排序 N`（15.1）；
  - 候选指标：主显示“持有期净收益”（period_net_return*），年化改为
    “门槛年化”（15.5）；
  - AI 引用候选（baseline + selected）超出 top-N 也强制展开且保留真实
    排序（15.6）；
  - 持仓卡片从 Markdown 表格改为逐项列表（15.5）；
  - AI 区块缺省（disabled / not_applicable）不产生空模块；
  - 折叠汇总按家族归属到各自模块。
- `src/application/daily_decision_brief_service.py`（改动）
  - `_candidate_view` 传播 `candidate_id`；`_candidate_metrics` 增加
    `period_net_return` / `period_net_return_on_cash_basis` /
    `period_net_premium_return`。
- `tests/notification_format_assertions.py`（改动）
  - 扁平断言放开唯一例外 `### AI建议`（15.1 明确的子模块标题）。
- 测试更新：`test_daily_decision_brief_renderer.py`（新文案断言 + AI
  引用强制展开端到端）、`test_daily_decision_brief_notification_flow.py`
  （策略排序标签）、`test_feishu_bot.py`（模块化标题）、新增
  `tests/test_ai_decision_advice_render.py`（10 个文案合同测试）。

## 依赖图

- 重新生成 `docs/dependency_graph*`（新增 render 模块）。

## 验证

- 全量 `pytest tests/`：4707 passed，2 failed（
  `test_futu_portfolio_context` 汇率观察、`test_sell_put_linked_call_helper`
  模板默认值 —— 已在 main 基线复现，与本 work unit 无关）；
  `quality/test_om_quality_gate_http` 因沙箱禁止 bind socket 失败，同无关。
