# Kimi 独立验证修复 — CC+LP（同到期）

- 验证者: router_kimi_oauth_k3 subagent（只读静态审阅）
- 日期: 2026-08-08
- 状态: 高/中 finding 已修复，低 finding 记录 deferred

## Findings 处置

| # | 严重度 | 验证发现 | 处置 |
|---|---|---|---|
| 1 | 高 | capture status 无 variant，pipeline cc_lp snapshot 生产链路永不封存 | 修复：`symbol_monitoring._report_capture` 透传 variant（仅非空写入），combo 分支从 policy 解析；新增 capture variant 测试 |
| 2 | 中 | 计划 §6 与 §7 objective 描述矛盾 | 修复：修订 §6（objective 保持 premium_funded_long_call） |
| 3 | 中 | Sell Call 收益门槛未继承（min_annualized_net_return=0.0） | 修复：读取 `sell_call_cfg.min_annualized_net_premium_return` 传入 scan |
| 4 | 低 | not_applicable reason 粒度不足 | deferred：不影响状态枚举正确性，诊断粒度后续提升 |
| 5 | 低 | delta/retention 阈值不可经 combo_yield 配置覆盖 | 观察项：确认文档未要求可配置，后续调参需求再议 |

## Changed Files

- `src/application/symbol_monitoring.py`：_report_capture 透传 variant；combo 分支 combo_variant 解析
- `src/application/cc_lp_steps.py`：Sell Call 收益门槛继承
- `docs/plans/cc-lp-same-expiry-implementation-plan-20260808.md`：§6 objective 描述修订
- `tests/test_symbol_monitoring_fetch_spec_merge.py`：新增 variant capture 测试

## Validation

- `pytest tests/test_symbol_monitoring_fetch_spec_merge.py`：19 passed
- 全量相关：166 passed（CC+LP + combo + symbol_monitoring + daily_brief + pipeline_watchlist）
- `ruff check`：全绿
