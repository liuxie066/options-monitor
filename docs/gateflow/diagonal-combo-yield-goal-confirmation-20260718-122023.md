# Gateflow Goal Confirmation — True Diagonal Combo Yield

- **Gate**: goal confirmation
- **Work unit**: 真正错期 Combo Yield 的端到端处理
- **Status**: confirmed-pass
- **Branch**: `codex/diagonal-combo-yield-lifecycle`

## Goal

支持一个明确、可审计的 true diagonal Combo Yield v1：一张较近到期的 short put 与一张严格更晚到期的 long call 组成一个经济组合，并在开仓识别、成交归组、持仓投影、Put 提前退出/到期/assignment、Call 留存和 Close Advice 中保持一致语义。

## Motivation

当前实现只支持同到期组合。真正错期结构如果仅删除 `expiration_mismatch`，会继续错误复用 same-expiry payoff/scenario 指标，并且当前 group identity、trade intake companion matching 和 assignment stock projection 都无法完整表达不同 expiry 生命周期。

## Proposed Success Signals

1. Opening pair contract 明确要求 `call_expiration > put_expiration`，并分别输出 Put/Call expiry 与 DTE。
2. Diagonal opening 不伪造 Put expiry 时的 Call residual value；无法可靠计算的 same-expiry payoff/scenario 字段明确为 not-evaluable/unsupported。
3. 一 Put + 一 Call 的 group identity 不依赖两腿同 expiry；Call-first、Put-first、restart、duplicate intake 和 partial fill 要么安全归组，要么 fail closed 并提供修复证据。
4. 生命周期 read model 使用 compositional inventory，而不是仅靠互斥 enum，能够表达部分 Put close、部分 assignment、assigned stock、部分 Call close。
5. Put 关闭后 Call 保留 Combo 血缘，但展示为 residual Call；assignment 后展示为 assigned-stock + Call，并保持股票写路径独立。
6. Close Advice 保留腿级 thesis，并产生确定的 group-level advisory；缺腿、数量不匹配、报价缺失时不会输出虚假的 `keep_call` 或 `combo take profit`。
7. 现有 same-expiry Combo、普通 Sell Put/Covered Call、历史 group ID 和正式通知在 shadow promotion 前不回归。
8. 聚焦测试、历史 replay/shadow 验证和文档契约完成。

## Scope Boundary

### In Scope

- diagonal opening pair validation/ranking contract；
- candidate/report schema 的双 expiry 表达；
- trade-intake group linking 与恢复语义；
- read-only Combo lifecycle/inventory view；
- Put close/expire/assignment 后的 Call 分类；
- option Close Advice 的 group action synthesis；
- assigned-stock reference/handoff，但不写股票退出；
- additive/shadow-first rollout、兼容测试和文档。

### Non-goals

- 一张长期 Call 连续复用到多个 Put 周期；
- roll/open replacement；
- 自动卖出 assigned stock、自动 exercise Call 或 broker-facing 交易执行；
- 引入 Black-Scholes/波动率曲面预测来估算 Put expiry 时的 Call residual value；
- 重写历史 trade events/group IDs；
- 修改生产 `config.yaml`、runtime JSON、通知开关或 live state；
- 在本 work unit 中优化无关 Sell Put/Covered Call 逻辑。

## Direct Code Evidence

- `domain/domain/engine/yield_enhancement.py:128-129` 拒绝不同 expiry。
- `domain/domain/engine/yield_enhancement.py:162-207` 使用单一 DTE 和 same-horizon Call intrinsic payoff，不能直接用于 diagonal。
- `src/application/sell_put_call_helper.py:769-783` 按 Put expiry join Call。
- `src/application/trades/resolver.py:704-732` group ID 和 companion matching 绑定 expiry。
- `src/application/close_advice_runner.py:1512-1558` 只按 group 取第一条 Call，未做数量匹配。
- `src/application/ledger/lifecycle.py:114-215` 支持部分 lifecycle close，因此状态模型必须支持混合库存。
- `src/application/positions/reporting.py:1796-1834` assigned-stock lot 当前没有 Combo group context。

## Required Product Decisions

1. **Recommended**: True diagonal 定义为 `call_expiration > put_expiration`，而不是允许 same-expiry 与 diagonal 混用一个新规则。
2. **Recommended**: v1 不估算未来 Call residual value；opening 只使用当前可观察的 funding、执行价格、流动性、Call delta/strike participation 和 expiry gap。所有依赖 same-expiry terminal payoff 的字段在 diagonal 模式显式 unsupported/not-evaluable。
3. **Recommended**: 一个 group 只包含一个 Put financing cycle 和一个 Call；多周期复用留给后续 work unit。
4. **Recommended**: assignment 后 Close Advice 只输出 stock-lifecycle manual-review handoff，不引入股票卖出 thesis 或自动执行。

## Product Decision

用户于 2026-07-18 明确确认以上四项产品边界，包括“不使用期权模型预测 Put 到期时的 Call residual value；未知不按零处理”。无 blocking open question。

## Residual Risks

- diagonal candidate ranking 的经济解释仍比 same-expiry 弱，但通过显式 unsupported 字段避免虚假精确度；分类：requiring explicit user decision。
- broker fill group propagation 需要在 plan gate 中选择最小、可恢复的 durable linking 方案；分类：covered by approved plan if goal confirmed。

## Next Entry Point

收到用户确认后进入 `plan` gate；未确认前不得实施。


## Clarification — Call Residual Value

“Put 到期时的 Call residual value”指：近月 Put 到期日当天，远月 Call 因仍有剩余期限而具有的市场价格。v1 的“不预测”口径含义是：

- 不在开仓时用 Black-Scholes、未来 IV、未来 spot path 或 volatility surface 假设，预测该未来日期的 Call 市值；
- 不把未知 residual value 当作 0；相关未来价值/payoff 指标必须标记为 `unsupported` / `not_evaluable`；
- 开仓仍使用当前可观察事实：Put bid、Call ask、净权利金、Call 成本、当前 delta、strike distance、流动性、两腿 DTE 和 expiry gap；
- 到 Put 实际到期或提前关闭时，Close Advice 使用当时真实可获得的 Call quote，而不是沿用开仓时预测值。


## Gate Decision

- **Decision**: pass
- **Validation**: 用户明确回复“确认”。
- **Current gate / next entry point**: `plan`
- **Artifact path**: `docs/gateflow/diagonal-combo-yield-goal-confirmation-20260718-122023.md`
