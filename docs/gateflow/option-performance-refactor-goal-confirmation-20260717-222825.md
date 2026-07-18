# Gateflow Goal Confirmation — Option Performance Refactor

- **Gate**: goal confirmation
- **Work unit**: option-performance-refactor
- **Created at**: 2026-07-17 22:28:25（本机时钟）
- **Status**: awaiting-user-confirmation
- **Artifact path**: `docs/gateflow/option-performance-refactor-goal-confirmation-20260717-222825.md`
- **Design/review evidence**: `docs/ASSIGNED_STOCK_RETURN_DESIGN.md`、`docs/reviews/plan-review-20260717-221908.md`、当前 source/tests。

## Goal

重建期权收益/表现统计的事实、计算和公共查询边界，使同一份 canonical 交易事实能够稳定生成：

- `activity`：开仓权利金等交易活动，不冒充利润；
- `cash`：完整现金流，含期权成交与明确的正股交割/卖出现金；
- `pnl`：gross/net realized、在有历史 mark 时的 unrealized/total PnL；
- `capital`：只输出有明确定义和历史暴露证据的 capital efficiency；
- `assignment lifecycle`：可靠支持 Sell Put 指派接货、后续卖出/估值与 covered-call attribution；
- `quality`：费用、mark、FX、settlement、inventory 等缺失必须显式；
- `period`：统一支持 MTD、YTD、指定自然月、指定自然年和任意闭合日期范围；
- `public surfaces`：新的 Agent tool、CLI、analysis views、bridge 和 staged legacy adapter。

## Motivation

当前实现存在以下已证实问题：

1. `monthly_income_report` 只把 `month=YYYY-MM` 作为一等 period，MTD/YTD/year 不是正式 contract；
2. premium activity、option cashflow、realized PnL、assigned-stock lifecycle PnL 和 close-advice estimate 分散且命名冲突；
3. reporting 侧和 ledger lot 侧重复计算 realized PnL；
4. 历史月份可能使用当前 collateral，return denominator 不可靠；
5. 历史 FX/mark 缺少确定性 fact selection；
6. legacy `fees=0` 无法区分真实零和未知；
7. 当前 total-assets bridge 用 option cash carve out portfolio PnL，数学闭合但经济语义不成立。

## Success Signals

1. 所有 period 输入归一为显式的 local dates、UTC instants 和 valuation cutoff；月界测试覆盖 US/HK、UTC 与北京时间。
2. lot allocation/void/repair/assignment target 只有一个 owner；performance 层不再重新匹配生命周期。
3. native-currency gross realized/cash/activity 可从 canonical facts replay，且有 quantity/cash/PnL invariants。
4. net PnL 只有 fee coverage 完整或明确使用 approved estimate 时才有值；缺失不当零。
5. 历史 total PnL 只使用 versioned mark/FX facts，选择结果带 provenance；重复 replay 结果一致。
6. Sell Put assignment lifecycle 不重复计算 premium、stock basis 或 fees；没有 mark/stock event 时返回 incomplete。
7. 新 `option_performance_report` 支持 MTD/YTD/month/year/range；CLI/analysis consumers 切到新 contract。
8. PnL bridge 使用 total-assets/PnL 恒等式；cash bridge 只有在存在 opening/ending cash facts 时可用。
9. old/new metric reconciliation matrix、consumer inventory、shadow fixtures、rollback 和 deprecation gate 完整。
10. 所有 approved slices 通过 code review、aggregate deepreview、draft PR review 和 final closeout。

## Proposed Scope Boundary

### In scope

- Period/metric/money/quality contracts；
- canonical lot economic allocation 与 performance aggregation 的 ownership 重整；
- gross/net realized PnL 与 fee allocation；
- historical valuation mark / FX facts 和 deterministic selectors；
- Sell Put assignment -> assigned stock -> mark/sale -> covered call lifecycle；
- explicit capital efficiency based on defined notional-days；
- new tool/CLI/analysis contracts；
- total-assets PnL bridge；
- cash bridge contract/building blocks，且只有权威 opening/ending cash facts存在时返回 observed；
- legacy adapter、shadow comparison、consumer cutover、docs/tests。

### Recommended non-goals

- 不在本 work unit 内建设通用股票库存账本；
- Short Call assignment、Long Call/Long Put exercise 若缺股票 cost-basis owner，只记录 option close/settlement cash 和 `stock_pnl=incomplete`；
- 不把普通股票交易、分红、税务、拆股完整接入 OM stock ledger；
- 不声称提供 broker margin return 或 NAV return；没有权威 daily capital/NAV facts 时不输出；
- 不改写已有 immutable trade history；只加 adapter、append-only/correctable evidence facts 和 rebuildable projections；
- 不修改生产 config、Feishu、broker-facing data 或真实持仓状态；
- 不在本 work unit 内修改外部 portfolio-management 服务。

## First-Principles Decisions Recommended for Confirmation

1. **Reporting timezone**: operator-facing period 统一 `Asia/Shanghai`；内部区间使用 `[start, end_exclusive)` UTC instants。多市场 combined 使用同一 reporting timezone，避免不可比的 exchange-local month。
2. **Assignment scope**: v1 完整支持现有可靠链路的 Sell Put assignment；其它 assignment/exercise 明确 incomplete，通用 stock inventory 另立 work unit。
3. **Return contract**: 只提供带 `capital_basis` 的 notional-days capital efficiency；generic `return_rate` 删除/弃用。账户 NAV return 不由 OM 推导。
4. **Fee legacy policy**: legacy bare `fees=0` 默认视为 unknown，除非 provenance/source version 明确 actual zero；gross 始终可算，net 可为 null。
5. **Historical valuation/FX**: append/correction-aware facts，deterministic selector；非交易日允许 previous trading close，但必须输出 selected fact/staleness。CNY translation 与 FX PnL policy在 plan ADR 固化。
6. **Bridge boundary**: 本 work unit完成 PnL bridge；cash bridge 完成独立 contract和 pure builder，但若当前外部服务没有 opening/ending cash facts，public tool返回 unavailable，而不是伪造现金桥。
7. **Compatibility**: 新工具成为 primary；`monthly_income_report` 保留一个明确 deprecation window 的 adapter，本 PR 不立即删除公共入口，但仓库内 consumers 全部切新。

## Direct Code Evidence

- UTC 月归属：`src/application/positions/reporting.py:32-50`。
- 现有 return 使用 current collateral：`src/application/positions/reporting.py:733-838`。
- reporting 独立重算 realized PnL：`src/application/positions/reporting.py:2480-2547`。
- ledger lot 也累计 realized PnL：`domain/domain/ledger/lots.py:30-45,124-131`。
- bare fee 默认零：`domain/domain/ledger/events.py:36-66`。
- Sell Put assigned-stock 当前范围：`docs/ASSIGNED_STOCK_RETURN_DESIGN.md:3-5,37,166-184`。
- bridge 以 option cash carve out period PnL：`src/application/portfolio_capital_bridge.py:113-149`。
- 公开 contract/consumer：`src/application/agent_tools/positions.py:23-110,264-311`、`src/application/agent_tools/analysis.py:65-115,600-735`。

## Overengineering Guard

- 不预先承诺原计划列出的全部 18 个模块；先按 ownership 和 vertical slices 落文件。
- 不创建第二套 lifecycle state machine。
- 不为将来可能的股票账本扩展提前实现 dividend/tax/split/general inventory。
- 不在没有上游事实时生成“看起来完整”的 net PnL、return 或 cash bridge。
- materialization 只有在 correctness 完成且 benchmark 证明需要后才引入。

## Blocking Open Questions

1. 是否确认只把 Sell Put assignment lifecycle 纳入本 work unit 的完整 PnL 支持，其它 assignment/exercise 明确 incomplete？
2. 是否接受 cash bridge 在缺少外部 opening/ending cash facts 时只交付 contract/pure builder，并在 public surface 返回 unavailable？
3. 是否接受新工具 primary、legacy `monthly_income_report` 保留一个 deprecation window，而不是本次立即删除？

## Validation at this gate

- Preflight branch: `excited-rhino`，非 protected trunk。
- Dirty state: 仅本次 Gateflow/plan review 文档为未跟踪文件，scope ownership 清楚。
- Existing focused baseline: `102 passed, 10 skipped`。

## Residual Risks

- 通用 stock inventory：**requiring explicit user decision / recommended later work unit**。
- 外部 cash facts：**requiring explicit user decision / external dependency**。
- legacy public removal timing：**requiring explicit user decision**。

## Next Entry Point

用户确认上述目标和三个 blocking decisions 后，进入 `plan` gate；未确认前不得进入 implementation。

## User Confirmation

- **Confirmed at**: 2026-07-17 22:37:51 +0800（本机时钟）
- **Decision**: accepted recommended scope
- **Accepted decisions**:
  1. 完整 lifecycle PnL 聚焦 Sell Put assignment；其它 assignment/exercise 缺股票 basis 时 explicit incomplete。
  2. PnL bridge 完整交付；cash bridge 完成 contract/pure builder，缺 opening/ending cash facts 时 public surface unavailable。
  3. 新 option performance surface 成为 primary；monthly_income_report 保留一个 deprecation window。
- **Gate decision**: goal-confirmation-pass
- **Next entry point**: plan

## Final Goal-Confirmation Status

- **Completion status**: pass
- **Blocking open questions**: none for plan entry
- **Residual risks classification**:
  - 通用 stock inventory：assigned to later work unit。
  - 外部 opening/ending cash facts：covered by cash-bridge unavailable semantics in current work unit；外部接入 assigned to later work unit。
  - legacy removal：covered by current deprecation plan and later removal work unit。
