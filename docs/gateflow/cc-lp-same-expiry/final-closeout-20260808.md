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

## 附录：策略规则一致性核对（Kimi 验证后补查）

| # | 已确认策略规则（文档） | 代码落实 | 状态 |
|---|---|---|---|
| 1 | Sell Call 资金腿 + Long Put 反转腿，同到期 | cc_lp.py validate/compute；cc_lp_steps 配对 | ✅ |
| 2 | call_strike > put_strike（结构方向） | cc_lp.py:74-75 strike_order reject | ✅ |
| 3 | Sell Call 独立扫描、继承全部硬门槛（收益、strike 含 avg_cost*1.02、max_strike、流动性、期限、underwriting） | cc_lp_steps：run_sell_call_scan + **enrich_and_filter_covered_call_underwriting（本轮补）** | ✅（补 underwriting 层） |
| 4 | Long Put delta 0.10~0.25，fail closed | cc_lp.py:76-80 | ✅ |
| 5 | 保留率 net_credit/call_net_credit ≥0.20，不允许净 debit | cc_lp.py:107-109 + cc_lp_steps 门槛 | ✅ |
| 6 | 资金占用 = spot × multiplier（1 张合约覆盖股数），不扣净权利金 | cc_lp.py net_return / covered_notional；cc_lp_steps covered_notional=spot*multiplier | ✅ |
| 7 | 排序保留率主键 + 反转腿 delta 趋近 0.12 次键 | cc_lp.py cc_lp_rank_key | ✅ |
| 8 | 无 gap 硬门槛（gap_width_pct 仅诊断） | cc_lp.py 仅计算输出 | ✅ |
| 9 | 无持仓 → not_applicable 跳过 | cc_lp_steps 空返回 + run_cc_lp_variant not_applicable | ✅ |
| 10 | 候选写入独立 cc_lp_candidate_snapshot.v1 | cc_lp_candidate_snapshot.py + pipeline 封存（含 variant 透传修复） | ✅ |
| 11 | Daily Brief 加载快照到数据源（不改 renderer） | daily_decision_brief_service._load_cc_lp_snapshot_family | ✅ |
| 12 | variant 默认 sp_lc，objective 不动 | yield_enhancement_config + config_validator | ✅ |

本轮修复：Sell Call 资金腿补跑 `enrich_and_filter_covered_call_underwriting`（继承 min_net_income、IV/RV、earnings 等 underwriting 硬门槛），并新增回归测试。

## 附录 2：发布与远端升级（2026-08-08）

### Release v1.11.0

- VERSION: 1.10.19 -> 1.11.0（CC+LP 为 New Features，按项目历史 minor bump）
- CHANGELOG: `## 1.11.0 - 2026-08-08`，`### New Features` 3 条
- delta coverage: release/coverage/v1.11.0.json（基线 v1.10.19，11 commits / 3 notes / 4 no-note）
- 依赖图: 先提交 `44c2d62f`（release 前置，修复 release_check POST_REVIEW 约束），再提交 release `7255e0a0`
- Release 工作流: run 31242226424 全绿
- GitHub Release: v1.11.0, target 7255e0a0, assets: options-monitor-v1.11.0.tar.gz / om-agent-spec.json / VERSION

### 远端升级 liuxie-incus

- dry-run: audit_20260808T054411Z_7c4800e46e（write_applied=false）
- apply: audit_20260808T054452Z_f2dfbcf1ec（write_applied=true, symlink_switched）
- 后验: update verify ok；version current=1.11.0 latest=1.11.0 no_upgrade_available；6 核心服务 active + feishu-ws/wechat checks ok；failed_checks=[]
- pre-existing: options-monitor-position-advice-promotion failed（8/7 21:15 UTC timeout，升级前已存在，非本升级引入）
