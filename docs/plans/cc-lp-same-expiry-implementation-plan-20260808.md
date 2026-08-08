# CC+LP（同到期）实施计划（2026-08-08）

> 状态：goal confirmation 通过，待 plan review
>
> 日期：2026-08-08
>
> 分支：`feat/cc-lp-same-expiry`
>
> 依据：`docs/plans/cc-lp-same-expiry-policy-confirmation-20260808.md`

## 1. Goal / Motivation / Success Signal

### Goal

在 `combo_yield` 模块下新增 **CC+LP（Covered Call + Long Put，同到期）** 开仓候选变体：

- Sell Call 资金腿：独立扫描、继承 Sell Call 全部门槛、复用共享 required-data、无持仓跳过；
- Long Put 反转腿：delta 0.10~0.25 召回；
- 组合约束：`call_strike > put_strike`、保留率 ≥0.20、净权利金口径、无 gap 硬门槛；
- 排序：保留率主键 + 反转腿 delta 趋近 0.12 次键，跨标的 Sell Call 期间非年化净收益；
- 候选写入独立 sealed snapshot，Agent/Daily Brief 消费快照。

### Motivation

SP+LC（Sell Put + Long Call）已上线，表达"下跌接货 + 上行参与"的看涨反转。CC+LP 是严格对称的看跌反转：
Sell Call 收权利金承担被叫走风险，Long Put 表达转跌观点。两者共享"资金腿 + 反转腿"哲学与全套机制。

### Success Signal

1. CC+LP 可独立扫描（Sell Call 腿继承全部现有门槛；Long Put 腿 delta 0.10~0.25 召回）；
2. 组合约束生效（方向、保留率 ≥0.20、净权利金口径、无 gap 硬门槛）；
3. 排序正确（保留率主键、反转腿 delta 趋近 0.12 次键、跨标的期间非年化净收益）；
4. 候选写入独立 sealed snapshot，Agent/Daily Brief 消费快照；
5. 测试覆盖新策略族，不破坏现有 SP+LC / Sell Call 行为。

## 2. Non-Goals / Scope Boundary

- staggered 错期结构（本轮只做同到期）；
- CC+LC（Sell Call + Long Call）——趋势延续，另一个 work unit；
- 持仓生命周期、平仓建议、通知格式的完整改造（本轮只到候选产出 + snapshot 消费）；
- 不新增 gap 硬门槛；
- 不新建抽象层（全部复用现有机制）。

## 3. Goal Alignment

| 设计决策 | 对应 Goal / Success Signal |
|---|---|
| Sell Call 独立扫描、继承门槛 | Goal 1（独立扫描） |
| Long Put delta 区间 | Goal 1 / 确认文档 §2 |
| call_strike > put_strike | Goal 2 / 确认文档 §3 |
| 保留率 ≥0.20 | Goal 2 / 确认文档 §4 |
| 排序主键保留率 + 次键 delta | Goal 3 / 确认文档 §5 |
| 独立 sealed snapshot | Goal 4 / 确认文档 §7 |
| 复用现有机制 | Non-goal（不新抽象） |

## 4. Design Document Alignment

全部决策来自 `cc-lp-same-expiry-policy-confirmation-20260808.md`，无新增策略决策；本 plan 只把已确认口径落到实现边界。

## 5. First-Principles Judgment & Direct Code Evidence

### 为什么放 combo_yield 模块下

CC+LP 与 SP+LC 共享同一套"资金腿 + 反转腿 + 保留率 + sealed snapshot"基础设施，放 `combo_yield` 下复用配置解析、snapshot、消费路径，避免复制第三个扫描管线。

### 直接代码证据

- 现有 SP+LC pair 校验/指标/策略/排序在 `domain/domain/engine/yield_enhancement.py`（`validate_yield_enhancement_pair`、`compute_yield_enhancement_metrics`、`ComboYieldResearchPolicy`、`combo_yield_proposed_rank_key`）；
- snapshot 模式在 `src/application/combo_yield_candidate_snapshot.py`（`combo_yield_candidate_snapshot.v1`）；
- Sell Call 扫描/underwriting 在 `src/application/sell_call_steps.py`（`run_sell_call_scan` + `enrich_and_filter_covered_call_underwriting`）；
- 配置解析/默认值在 `src/application/yield_enhancement_config.py`，validator 在 `src/application/config_validator.py`；
- Daily Brief 已消费 `combo_yield_candidate_snapshot.json`（`daily_decision_brief_service.py:841-879`）；
- pipeline 在 `pipeline_watchlist.py` 已封存 combo snapshot（`seal_combo_yield_candidate_snapshot`）。

## 6. Affected Files / Modules

### 新增

- `src/application/cc_lp_steps.py`：CC+LP 扫描编排（Sell Call 独立扫描 + Long Put 配对 + 组合指标 + 排序）；
- `src/application/cc_lp_candidate_snapshot.py`：CC+LP sealed snapshot（schema `cc_lp_candidate_snapshot.v1`，复用 combo snapshot 模式）；
- `tests/test_cc_lp_steps.py`、`tests/test_cc_lp_candidate_snapshot.py`；
- 策略确认文档（已存在）+ 实施文档（本文件）。

### 修改

- `domain/domain/engine/yield_enhancement.py`：新增 CC+LP 角色变体（`funding_call` / `reversal_put`），复用既有指标函数；
- `src/application/yield_enhancement_config.py`：新增 `variant=cc_lp` 默认值/派生默认值；
- `src/application/config_validator.py`：`objective` 增加 `premium_funded_long_put`；`variant` 枚举校验；
- `src/application/combo_yield_steps.py` / `pipeline_symbol.py` / `pipeline_watchlist.py`：CC+LP 扫描接入与 snapshot 封存；
- `src/application/daily_decision_brief_service.py`：消费 `cc_lp_candidate_snapshot.json`（如 Daily Brief 需要展示）；
- `src/application/report_summaries.py`、`render_yield_enhancement_alerts.py`：CC+LP 汇总/告警字段（如需要）；
- `config.yaml`：示例启用（可选，保持默认关闭更稳妥——待 plan review 定）；
- `docs/candidate_strategy.md`：策略文档更新。

## 7. Contract / Schema / State-Machine / Public-Interface Changes

- 配置契约：`combo_yield.variant ∈ {sp_lc, cc_lp}`（默认 `sp_lc` 保持现行为不变）；**`objective` 保持 `premium_funded_long_call` 不变，不扩展枚举**；
- 新 snapshot schema：`cc_lp_candidate_snapshot.v1`（独立文件，不修改现有 `combo_yield_candidate_snapshot.v1`）；
- 不改变 SP+LC 现有 schema / 消费路径（向后兼容）。

## 8. Implementation Decisions

1. **变体承载**：`combo_yield.variant`（`sp_lc` / `cc_lp`），默认 `sp_lc` 保持现行为不变；
2. **Sell Call 独立扫描**：复用 `run_sell_call_scan`（scan 层）输出，再经 `enrich_and_filter_covered_call_underwriting`（underwriting 层）后进入配对；`stock` 从 `portfolio_ctx` 注入，无持仓 → `not_applicable`；
3. **Long Put 召回**：从 required-data put universe 按 delta 0.10~0.25 召回；
4. **组合指标**：复用 `compute_yield_enhancement_metrics` 角色互换（call 为资金腿、put 为反转腿）；
5. **排序**：复用 `combo_yield_proposed_rank_key` 模式，主键保留率、次键反转腿 delta 趋近 0.12；
6. **snapshot**：复用 `combo_yield_candidate_snapshot.py` 的 seal/validate/load 模式，新建 `cc_lp_` 变体；Daily Brief 的"消费"= 加载快照到决策数据源，**不改 renderer / 通知格式**；
7. **无持仓**：Sell Call 无持仓上下文 → `not_applicable` 跳过（与现有 Sell Call 一致）。

## 9. Implementation Slices

### Slice 1：Domain 层 CC+LP 角色与组合指标

**Objective**：让 `yield_enhancement.py` 支持"资金腿 call + 反转腿 put"角色，复用现有 pair 校验/指标。

**Expected outcome**：`compute_yield_enhancement_metrics` 可计算 CC+LP 角色；`strike_order` 校验方向正确（call > put）。

**Allowed files**：`domain/domain/engine/yield_enhancement.py`、`domain/domain/canonical_schema.py`、相关测试。

**Exact allowed changes**：
- `YieldEnhancementLeg` 增加角色字段或由调用方传入 `funding_leg=call, reversal_leg=put`；
- `validate_yield_enhancement_pair` 支持角色互换后的 `strike_order`（call_strike > put_strike）；
- 指标计算保留 `gap_width_pct` 诊断字段；
- 新增 `compute_cc_lp_metrics` 薄封装（复用底层指标，换角色与保留率下限）。

**Non-goals**：不新增 gap 硬门槛；不改 SP+LC 现有行为。

**Tests/validation**：
- 单测：CC+LP 角色下 `call_strike > put_strike` 通过、反转拒绝；
- 单测：指标含 `net_credit_retention`、`gap_width_pct`；
- 现有 SP+LC 测试全绿（回归）。

### Slice 2：配置与 CC+LP 扫描编排

**Objective**：配置支持 `variant=cc_lp`，CC+LP 可独立扫描（Sell Call + Long Put 配对 + 组合门槛 + 排序）。

**Expected outcome**：启用 `cc_lp` 变体后产出 CC+LP 候选；无持仓跳过；保留率 ≥0.20 生效。

**Allowed files**：`yield_enhancement_config.py`、`config_validator.py`、`cc_lp_steps.py`（新增）、`combo_yield_steps.py`、`pipeline_symbol.py`、`pipeline_watchlist.py`、相关测试。

**Exact allowed changes**：
- `yield_enhancement_config.py`：新增 `variant` 字段（默认 `sp_lc`）；`cc_lp` 派生默认值（call delta 不做门槛、put delta 0.10~0.25、保留率 ≥0.20）；**不改 `objective` / `YIELD_ENHANCEMENT_OBJECTIVES` / `mode` 推导**；
- `config_validator.py`：新增 `variant` 枚举校验（`sp_lc`/`cc_lp`），**不改 `objective` 枚举校验**；
- 新增 `cc_lp_steps.py`：`run_cc_lp_scan_and_summarize`（Sell Call 独立扫描 + Long Put 配对 + 组合门槛 + 排序）；
- `combo_yield_steps.py`：按 `variant` 分派（`sp_lc` 走现有路径，`cc_lp` 走新路径）；
- `pipeline_watchlist.py`：CC+LP snapshot 封存接入。

**Non-goals**：不改 Sell Call step 本身；不改 SP+LC 现有路径。

**Tests/validation**：
- 配置测试：`variant=cc_lp` 解析正确、非法 variant 拒绝；
- 扫描测试：Sell Call 独立扫描 + Long Put 配对产出候选；
- 无持仓 → `not_applicable`；
- 保留率 <0.20 候选被拒；
- 排序主键保留率、次键 delta。

### Slice 3：CC+LP sealed snapshot 与消费

**Objective**：CC+LP 候选写入独立 sealed snapshot，Daily Brief 消费快照。

**Expected outcome**：`cc_lp_candidate_snapshot.v1` 封存；Daily Brief 读取该快照，无二次排序。

**Allowed files**：`cc_lp_candidate_snapshot.py`（新增）、`daily_decision_brief_service.py`、相关测试。

**Exact allowed changes**：
- 新增 `cc_lp_candidate_snapshot.py`：schema `cc_lp_candidate_snapshot.v1`，复用 combo snapshot 的 seal/validate/load；
- `daily_decision_brief_service.py`：读取 `cc_lp_candidate_snapshot.json`；
- 空结果也封存 `no_candidate`。

**Non-goals**：不改 combo_yield snapshot 现有 schema；不改通知格式。

**Tests/validation**：
- snapshot seal/validate/load 单测；
- Daily Brief 消费快照单测（无二次排序）。

## 10. Tests / Validation Commands

```bash
# Focused
./.venv/bin/python -m pytest tests/test_cc_lp_steps.py tests/test_cc_lp_candidate_snapshot.py -q

# Config
./.venv/bin/python -m pytest tests/test_combo_yield_* tests/test_layered_config.py -q

# Regression (SP+LC / Sell Call)
./.venv/bin/python -m pytest tests/test_combo_yield_pairing.py tests/test_combo_yield_steps.py tests/test_sell_call_strategy_unification.py -q

# Agent contract
./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q

# Lint
ruff check src tests
```

Expected assertions：
- CC+LP 候选存在且门槛生效（保留率 ≥0.20、call_strike > put_strike）；
- 无持仓 → `not_applicable`；
- SP+LC / Sell Call 现有测试全绿；
- ruff 全绿。

## 11. Docs Decision

- 策略确认文档（已存在）作为口径真源；
- `docs/candidate_strategy.md` 增加 CC+LP 小节；
- 实施完成后更新本 plan 的 gate 状态与 `docs/gateflow/` 产出。

## 12. Risks / Open Questions

### Risks

- **Sell Call 独立扫描可能重复取数**：与 SP+LC 的 put 腿独立扫描共享 required-data（已有机制），OpenD 调用次数由共享 required-data 覆盖；新增配对只在内存中做。计划中不新增网络取数。
- **Daily Brief 展示**：CC+LP 是否进入 Daily Brief 的展示区由本 plan 的 Slice 3 决定为"消费快照但不改通知格式"；若需完整展示，作为后续 work unit。
- **snapshot 双文件**：CC+LP 用独立 `cc_lp_candidate_snapshot.v1`，避免与现有 combo snapshot 混 schema；后续如需统一可在专门 work unit 处理。

### Open Questions（已收敛）

- 无持仓 → `not_applicable`（已确认）；
- 配置入口 → `combo_yield.variant`（已确认）；
- 保留率 ≥0.20、delta 0.10~0.25、目标 delta 0.12（已确认）。

## 13. Completion Report Format

每个 slice 完成后输出：

- changed files / modules；
- tests/validation 运行结果与断言；
- findings / residual risks 分类；
- completion status；
- artifact path。

全部 slice 完成后：

- aggregate deepreview artifact；
- PR review artifact；
- final closeout summary（含 draft PR URL）。

## 14. 为什么没有过度设计 / Goal Drift

- 全部决策映射到已确认 goal（§3 表）；
- 复用现有机制（pair 校验、指标、配置、snapshot、排序框架），只新增 CC+LP 结构与变体分派；
- 非目标明确排除 staggered / CC+LC / 生命周期改造 / gap 硬门槛；
- 3 个 slice 以可验证行为增量为界（domain → 编排 → snapshot），每 slice 可独立验收。
