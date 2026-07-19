# Plan Fix — Daily Decision Brief

- **Gate**: fix
- **Work unit**: `daily-decision-brief`
- **Target findings**: PR-1 through PR-5
- **Date**: 2026-07-19
- **Status**: complete; pending re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-plan-fix-20260719.md`

## Fixes

- **PR-1 已修复**：current/delivery/revision 全部 market-qualified；shared index 使用 `<MARKET>/<account>`；`scheduler_markets` 成为明确输入；手工 multi-market run 独立生成 lifecycle，但主动通知 fail closed 且不推进 pointer；只允许 CLI/Tool 分市场读取，避免跨市场 bundle 的部分 pointer crash 状态。
- **PR-2 已修复**：delta provider key 移除 current revision，改为 last-delivered brief digest + canonical material diff digest；revision 仅审计。
- **PR-3 已修复**：新增 shared-index lock，覆盖 shared current read-modify-write；account-market revision lock 保留。
- **PR-4 已修复**：account-wide blocked 收窄为 no-scan/pipeline failure、全部 structured decision sources 不可读、或全局必需 account-level source 不可用；symbol/strategy 局部失败只进入 data gaps。
- **PR-5 已修复**：Sell Put/Call 分别使用 canonical put/call ranking；Combo Yield 保持 pipeline group/pair 顺序并按 group/legs 去重，不在日报层 rerank。

## Validation

- Plan 重新检查了 identity、file path、state transition、delivery mapping、failure recovery 和 mixed schema boundary。
- 所有变更保持原 scope；未新增数据库、scheduler、queue 或策略评分。

## Residual risks

- Provider 无幂等支持时的极小 crash duplicate：assigned to later production observation。
- Multi-market 主动发送不在本 work unit 支持；通过显式 fail-closed skip 消除未分类状态，per-market read artifacts 保留。
- 无 unclassified risk。

## Gate transition

- **Current gate**: plan re-review
- **Next entry point**: 使用 `planreview` re-review updated plan。
