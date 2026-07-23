# Gateflow Plan Review Fix — Close Evidence Collection

- Gate: plan review fix
- Work unit: `close-evidence-collection`
- Plan: `docs/gateflow/close-evidence-collection-plan-20260723.md`
- Review: `docs/reviews/plan-review-20260723-083540.md`
- Artifact path: `docs/gateflow/close-evidence-collection/plan-review-fix.md`
- Completion status: fixed; pending re-review

## Finding decisions and fixes

### PR-01 — accepted — 已修复

Close capture 与 candidate capture 改为独立写入单元。正常路径仍 close-first，确保同 run 生成完整 dataset；若 strict Close capture 抛出 `ValueError`，orchestrator 必须保存异常、执行原 candidate build、再重新抛出，从而同时保持 Close fail-closed 与既有 candidate accumulation。

验证要求新增 distinct-run 和 same-run malformed cases：调用必须失败，但 candidate dataset 已写入且不伪造 close facet。

### PR-02 — accepted — 已修复

计划已冻结双 build 的兼容输出：原 singular summary 字段只描述 candidate build；新增四个 close-specific summary 字段；顶层 status 与 safety 对任一 persistent build 聚合。S1 明确加入 `tests/test_research.py`，覆盖 CLI forwarding 和两个非法参数组合。

### PR-03 — accepted — 已修复

计划已区分 operator-authored runtime config 与 service profile observability field。S2 不新增前者，仅增加既定部署事实字段。

## Validation

- 修订后 plan 逐项回应三个 accepted findings。
- Scope、non-goals、service/timer cadence 和生产 authority 均未扩大。
- 待独立 re-review 验证 finding 状态。

## Residual risks

- 6h sampling coverage：assigned to later S5 readiness evaluation；不在本 work unit 提升 cadence。
- Existing candidate-only dataset collision：assigned to later work unit only if production evidence shows material loss；本 work unit fail-safe skip。
- Active-run partial write race：requiring new issue or explicit user decision only if production canary/runtime evidence shows recurrence；当前不新增 run-complete state。
