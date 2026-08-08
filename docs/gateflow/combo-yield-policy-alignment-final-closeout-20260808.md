# Gateflow Final Closeout — Combo Yield 开仓策略对齐

## Gate

- Work unit：combo-yield-policy-alignment
- Gate：final closeout
- Date：2026-08-08
- PR：https://github.com/liuxie066/options-monitor/pull/137
- Head：`6983049a`

## What Changed

1. **put 腿硬门槛继承**（S1）：Combo put 扫描复用 Sell Put underwriting 配置（含 `min_net_income`），scan 层保持与主策略一致；测试验证配置透传与继承。
2. **成本约束统一**（S2）：删除 `funding_mode` / `max_call_cost_to_put_credit` / `max_debit` / `max_debit_native`，统一 `min_net_credit_retention=0.60`；validator 对旧字段明确拒绝；defaults / system.json / 示例同步。
3. **排序对齐**（S3）：同结构排序主键改为 `net_credit_retention` 优先；跨标的（staggered + shadow）用 `put_only_period_net_return` 期间非年化。
4. **候选真源**（S4）：新增 `combo_yield_candidate_snapshot.v1`（run/account 级 sealed snapshot）；Daily Brief 从 CSV 切换为快照读取，移除二次排序；空结果封存 `no_candidate`。
5. **设计文档**：STRATEGY_ARCHITECTURE / PRODUCT_ARCHITECTURE / plan / confirmation 同步新口径。

## What Was Verified

- 全量 pytest：4561 passed, 10 skipped（3 项失败逐项澄清：HTTP 沙箱限制沙箱外通过、依赖图已更新、futu 既有时间敏感基线失败）；
- ruff：本 work unit 全部改动通过；
- 依赖图重新生成并 `--check` 通过；
- US/HK config validate + build dry-run 通过；
- PR CI：guardrails / agent-plugin / CodeQL / Analyze×2 全部 pass；
- Combo snapshot：有候选 / 无候选 / 篡改 / 缺失身份测试覆盖。

## Docs Updates

- `docs/STRATEGY_ARCHITECTURE.md`：Combo 排序、成本约束、snapshot 章节更新；
- `docs/PRODUCT_ARCHITECTURE.md`：Combo 排序与 snapshot 状态更新；
- `docs/plans/combo-yield-policy-confirmation-20260808.md` / `...implementation-plan-20260808.md`：状态更新为实施完成。

## Finding Status

- plan review：5 findings 全部修复，re-review pass-with-risks；
- S1-S4 code review：各 slice pass（deferred finding 记录 owner）；
- aggregate deepreview：2 个实现期真实问题（run 级 seal 触发条件、二次排序残留）已修复；2 个 deferred finding；
- PR review：1 个 lint finding（`Any` 未 import）已修复；CI 全绿。

## Remaining Risks / Owners

- 全量 suite 既有时间敏感基线失败（`test_build_opend_exchange_rate_observation_uses_account_funds_conversion`）：owner = 既有 issue，独立于本 PR；
- `put_only_period_net_return` 显式正性校验：owner = 后续 work unit；
- shadow/research 层 `max_call_cost_to_put_credit` 字段语义清理：owner = 后续 work unit；
- run 级 seal 端到端断言（combo completed + 空 pairs → no_candidate）：owner = 后续集成测试。

## Issue Link Status

- 非 issue work unit，无需 closing keyword / issue closeout comment。

## Next Entry Point

- draft PR 已就绪且 CI 全绿。用户 merge 当前 PR（或标记 ready for review 后 merge）后可继续：release / 远端升级授权时再进入发布流程。
