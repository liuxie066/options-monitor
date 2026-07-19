# Fix — Daily Decision Brief S1

- **Gate**: fix
- **Work unit**: `daily-decision-brief`
- **Slice**: S1
- **Target findings**: CR-1, CR-2, CR-3
- **Date**: 2026-07-19
- **Status**: complete pending re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-s1-fix-20260719.md`

## Fixes

- **CR-1 已修复**：material digest 的 action projection 只保留 stable identity/state/priority fields；完整 `changes` 仍保留 title/reason 供 renderer 使用。
- **CR-2 已修复**：normalizer 检测重复 `action_id` 并 fail fast；S2 assembler 必须先显式 deduplicate。
- **CR-3 已修复**：P0->P1、P0/P1->P2 均生成 material `priority_downgraded`；同 tier rank 变化仍静默。
- **Residual canonicalization 已修复**：digest 递归把 NaN/Infinity 归一为 null，并使用 `allow_nan=false`。

## Validation planned

- Focused domain tests including four new regressions。
- Compileall and diff check。

## Residual risks

- Upstream explicit dedup policy remains S2-owned。
- No unclassified residual risk。

## Gate transition

- **Current gate**: S1 re-review
- **Next entry point**: rerun validation and deepreview changed S1 target。
