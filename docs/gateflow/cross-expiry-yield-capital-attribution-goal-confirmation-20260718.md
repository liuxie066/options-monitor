# Gateflow Goal Confirmation — 跨期收益和资金占用归因

- Gate: goal confirmation
- Work unit: Combo Yield staggered/diagonal 跨期收益与资金占用归因
- Date: 2026-07-18
- Branch: `codex/diagonal-combo-yield-lifecycle`
- Status: awaiting CEO confirmation
- Artifact path: `docs/gateflow/cross-expiry-yield-capital-attribution-goal-confirmation-20260718.md`

## Why this work unit exists

错期 Combo Yield 的 Funding Put 比 Participation Call 更早到期。现有系统已经能保存两腿的 `strategy_group_id`、`leg_role`、不同到期日和 residual-call 生命周期，但绩效层仍主要按单腿事实与报表期间聚合：

1. Long Call 开仓现金流在买入期间记为 `premium_paid_gross`；只有 Call 平仓/行权时才产生 realized PnL。
2. Short Put 的 realized PnL 在 Put 关闭、到期、指派或行权发生的期间确认。
3. 资金占用按单腿 `notional_days_v1` 相加：Short Put 使用 `strike * multiplier * open_contracts`；Long option 使用 `open premium * multiplier * open_contracts`；Covered Call 为零增量；指派股票使用剩余成本基础。
4. 当前 `OptionEconomicAllocation` 虽保留 `strategy`、`leg_role`、`strategy_group_id`，但 `PerformanceFact` 和 period breakdown 没有 strategy-group/cycle 归因维度。

因此，一个 Put 在 7 月结束、Call 在 9 月结束时，月度总账 PnL 本身可以是正确的，但“哪个收益周期赚了多少钱”“哪个周期承担 Call 成本”“该周期用了多少资本”的管理归因会错配或无法回答。若直接把整个 Call debit 扣给首个 Put 周期，又会把仍存续的 Call 资产提前费用化，并在 Call 最终平仓时形成二次计损风险。

## First-principles accounting boundary

本 work unit 必须同时保留三种不同语义，不能互相替代：

1. **现金流时间（cash timing）**：按真实交易发生日记录收付，不搬移现金流。
2. **法定/经济 PnL 时间（economic PnL timing）**：Option lot 的 realized PnL 仍按 close allocation 发生日确认；未平仓 Call 通过期初/期末估值进入 period total PnL，不把买入 premium 直接当亏损。
3. **策略周期归因（management attribution）**：在不改写前两者的基础上，用明确的 group/cycle identity 把 Funding Put 收益、Participation Call 价值变化和资本日归入可审计的周期。

任何实现若通过改写成交时间、伪造 close、或把 Call premium 同时作为现金流成本和 realized loss 来得到“周期收益”，均不成立。

## Proposed target semantics to validate in plan

### 1. Cycle identity

- `strategy_group_id` 表示完整 Combo Yield 生命周期，不等同于单个收益周期。
- 每一个 Funding Put lot/campaign 是一个独立 `funding_cycle`；V1 最稳定的 cycle identity 应从 canonical open lot/event identity 派生，而不是仅用 expiration。
- Participation Call 是 group-level 跨周期资产。它可以跨越一个或多个 funding cycles；在当前一 Put + 一 Call 结构下，至少会跨越 Put 结束后的 residual-call 阶段。
- 若当前产品尚不支持给同一 residual Call 再挂新 Put，则 V1 仍要定义首个 funding cycle 与 residual-call tail，避免未来补周期时重写历史语义。

### 2. Call 成本归属

- Call premium debit 是 Participation Call 的成本基础和现金占用，不是首个 Funding Put 周期的即时 realized loss。
- “Put 权利金覆盖 Call 成本”是 funding-source / affordability 指标，不是 PnL 抵销规则。
- 周期报表可展示 `put_credit_applied_to_call_funding` 或类似管理指标，但不得从 Put PnL 和 Call PnL 两边重复扣减。
- Call 最终经济收益只在 Call lot 生命周期内计算：realized PnL，或报表期内的 opening-to-ending unrealized change。
- 如果业务需要每个 Put 周期分摊 Call 成本，必须另设明确、可重放、总额守恒的 management allocation；默认不通过“首期一次性费用化”实现。具体是否需要分摊，将在 plan gate 用报表需求和现有生命周期能力裁决。

### 3. Capital utilization

V1 的基础量应继续使用可加总的 time-weighted capital exposure：

```text
capital_days = Σ(incremental_notional × overlap_days)
```

腿级基础：

```text
Funding Put capital = strike × multiplier × open_contracts
Participation Call capital = remaining unreturned premium basis（V1 可先沿用 open premium debit，若部分关闭则按剩余 contracts）
Covered Call incremental capital = 0
Assigned stock capital = remaining stock cost basis
```

但策略周期层不能简单把 group 全生命周期所有腿的 capital-days 都放进 Put 到期月份。应按真实持有区间切段：

- Put capital-days 归 Funding Put cycle，直到 Put 关闭/到期/指派。
- Call capital-days 在 Call 持有期间持续存在；首个 funding cycle 期间属于 group 的并行 Participation exposure，Put 结束后属于 residual-call tail 或后续显式 funding cycle。
- 组合层 `capital utilization` 至少应同时给出：
  - `incremental_capital_days`：风险资本日，避免用月末快照代替全过程；
  - `average_incremental_capital = capital_days / period_days`；
  - `annualized_efficiency = attributable_pnl / capital_days × 365`，仅在 PnL scope 与 capital scope 完全一致时计算；否则 fail closed。
- 若将 Put credit 视为降低净现金投入，可以另报 `net_cash_invested`，但不得用它替代 cash-secured Put 的风险资本；否则会因收到 premium 而虚假提高 utilization。

## Goal

建立一个可重放、守恒、跨期限不串账的 Combo Yield 归因模型，并通过 canonical ledger/performance read model 输出：

1. 清楚回答 Call 成本属于哪个生命周期/管理周期；
2. 清楚回答每个 funding cycle、residual-call tail 和完整 strategy group 的资本日、平均资本与资本效率；
3. 同一经济事实在 cash、PnL、management attribution 三个视图中不重复计算；
4. 月度/MTD/YTD 报表切割不会把未来 Call 收益提前记入 Put 周期，也不会把历史 Call debit 再次扣除；
5. 缺失 cycle identity、估值、fee、FX 或资本区间证据时明确 partial/not observed，而不是推断为零。

## Success signals

- 有明确且稳定的 group/cycle/tail identity，能从 trade events/projection 重放。
- 跨月案例满足守恒：所有 cycle/tail/group attribution 合计等于底层 period economic PnL，不多不少。
- Call 买入月、Put 到期月、Call 最终关闭月分别展示正确的 cash、realized/unrealized PnL 和 capital-days。
- 同月结构保持现有 Option Performance v1 结果兼容，新增维度不改变 canonical 总账。
- 部分平仓、Put 指派、Call residual、同日边界和报表 cutoff 有测试。
- strategy/cycle PnL 与 capital scope 不一致时，不输出误导性的 annualized efficiency。
- 公共 payload、CLI/Agent 输出或文档若变化，均有契约测试和迁移说明。

## Scope boundary

Primary ownership candidates:

- `domain/domain/ledger/economics.py`：close allocation 已携带 strategy/group metadata；仅在 cycle provenance 必须成为 canonical allocation evidence 时扩展。
- `domain/domain/performance/engine.py`、`models.py`：performance facts、capital segments、strategy/cycle breakdown 与守恒。
- `src/application/performance/`：读取、证据收集和公共 payload 适配。
- `domain/domain/combo_yield_lifecycle.py`：group lifecycle 与 residual-call 分类；仅负责生命周期事实，不承载通用 PnL 算法。
- 对应 performance、ledger、combo lifecycle、portfolio bridge tests 和 authoritative docs。

现有 symbol scanning/runtime-decoupling dirty changes 是已确认的同分支上下文；除非 plan 证明归因需要，否则不继续扩大或重写这些 scanning 文件。

## Non-goals

- 不修改生产 `config.yaml` / runtime config。
- 不发送通知，不写真实持仓或 broker/Feishu state。
- 不改变交易成交、税务或券商会计口径。
- 不用启发式按到期月份猜 cycle identity。
- 不在本 work unit 实现自动 roll/new Funding Put，除非代码事实证明这是完成归因所必需；默认只为其保留不破坏历史的 identity 边界。
- 不用复杂 IRR、VaR、broker-margin 模拟替代当前可解释的 incremental notional-days；这些只有在明确需求和可靠事实源存在时另开 work unit。

## Direct code evidence

- `docs/STRATEGY_ARCHITECTURE.md`：错期结构明确 Put 早于 Call，并禁止把不同风险期限压成单一组合年化指标。
- `domain/domain/ledger/economics.py::OptionEconomicAllocation`：已保存 `strategy`、`leg_role`、`strategy_group_id`，但没有 cycle identity。
- `domain/domain/performance/engine.py::build_period_performance`：facts 按 period event/close allocation 与 valuation boundary 形成，属于正确的 period accounting boundary。
- `domain/domain/performance/engine.py::_append_option_capital_segment`：Short Put 使用 strike notional，Long option 使用 premium debit，Covered Call 为零增量。
- `domain/domain/performance/engine.py::_capital_report`：当前只按 currency 汇总 capital-days，并用 period 总 PnL 计算 efficiency，没有 strategy group/cycle scope。
- `domain/domain/combo_yield_lifecycle.py`：已经区分 `active_combo`、`residual_call`、assigned-stock 等 lifecycle 状态，证明 group 生命周期可跨 Put 结束继续存在。

## Blocking open questions

进入 plan 前需要 CEO 确认以下业务语义：

1. 接受“Call premium 是 group-level Participation asset basis，不一次性归首个 Put 周期为损失”；Put 对 Call 的覆盖仅作为 funding attribution 指标。
2. 接受资本效率主口径使用风险资本日（cash-secured Put strike notional + Long Call debit + assigned-stock basis），而不是 `净现金支出 = Call debit - Put credit`。
3. 本 work unit 是否只覆盖当前一 Put + 一 Call + residual tail，还是必须同时设计并实现同一 Call 后续滚动多个 Funding Put cycles。建议 V1 前者，identity 保留后者扩展能力，但不实现自动 roll。

## Residual risks classification at this gate

- Broker margin 与 cash-secured notional 不同：assigned to later work unit，仅在存在可靠 broker margin facts 时处理。
- 税务 PnL 与管理归因不同：out of scope；canonical trade economics 不改写。
- 历史交易缺 strategy/cycle evidence：本 work unit必须 fail closed/partial；数据 repair 另行负责。
- 后续多 Put roll：建议 covered by later work unit；本轮只保证 identity 不阻塞演进。
