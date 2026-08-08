# Gateflow Final Closeout — CC+LP（同到期）

- Work unit: cc-lp-same-expiry
- Branch: feat/cc-lp-same-expiry
- Draft PR: https://github.com/liuxie066/options-monitor/pull/138
- Date: 2026-08-08
- Status: **final closeout pass**（draft-PR-pass 达成，待用户 merge）

## What Changed

在 `combo_yield` 模块下新增 **CC+LP（Covered Call + Long Put，同到期）** 开仓候选变体：

- `domain/domain/engine/cc_lp.py`：CC+LP 角色校验（call_strike > put_strike）、指标（净权利金/保留率/期间净收益，资金占用=spot×multiplier）、排序（保留率主键 + 反转腿 delta 趋近 0.12 次键）；
- `src/application/cc_lp_steps.py`：独立扫描编排（Sell Call 独立扫描继承全部门槛 + Long Put delta 0.10~0.25 配对 + 保留率 ≥0.20）；
- `src/application/cc_lp_candidate_snapshot.py`：独立 sealed snapshot `cc_lp_candidate_snapshot.v1`；
- `src/application/combo_yield_steps.py`：`variant` 分派（`sp_lc` 默认保持现行为，`cc_lp` 走新路径）；
- `src/application/pipeline_watchlist.py`：CC+LP snapshot 封存（仅 CC+LP status 存在时）；
- `src/application/yield_enhancement_config.py` / `config_validator.py`：`variant` 配置 + 枚举校验；
- `src/application/daily_decision_brief_service.py`：CC+LP snapshot 加载到数据源（不改 renderer）；
- 文档：策略确认、实施计划、review 链、`candidate_strategy.md` 更新。

## What Was Verified

- 全量相关 pytest：252 passed（CC+LP + combo_yield + sell_call + daily_brief + pipeline_watchlist + config）；
- ruff 全绿；
- gateflow 全流程：goal confirmation → plan → plan review（pass-with-risks → fix → re-review）→ S1-S3（各自 code review → fix → re-review）→ aggregate deepreview（4 修复 + 1 deferred）→ ready-to-open-draft-PR → draft PR #138 → PR review（pass，无 accepted findings）。

## Docs Updates

- `docs/plans/cc-lp-same-expiry-policy-confirmation-20260808.md`（策略口径真源）；
- `docs/plans/cc-lp-same-expiry-implementation-plan-20260808.md`；
- `docs/reviews/plan-review-*`、`code-review-*`、`code-re-review-*`、`aggregate-deepreview-*`、`pr-138-review-*`；
- `docs/gateflow/cc-lp-same-expiry/*`；
- `docs/candidate_strategy.md`（附录 CC+LP 变体）。

## Finding Status

- 所有 review/fix finding 已裁决：accepted 修复或 rejected-with-reason；
- aggregate 4 项修复完成，1 项测试缺口 deferred。

## Remaining Risks / Owners

| 风险 | Owner / 去向 |
|---|---|
| pipeline 封存 CC+LP 分支端到端测试 | 后续 work unit |
| Daily Brief 展示 CC+LP（renderer） | 后续 work unit（plan 非目标） |
| CC+LP 未启用时 Daily Brief data_gap（跟随既有 combo 行为） | 后续 work unit（既有问题，非本 PR 引入） |
| staggered 错期、CC+LC | 独立 work unit |

## Issue Link Status

- 无关联 GitHub issue（纯 feature work unit）。

## Next Entry Point

- 用户 merge PR #138 到 main 后，可继续：发布/升级（需独立授权）、或开展后续 work unit（CC+LC、CC+LP 错期、Daily Brief 展示、端到端封存测试）。
