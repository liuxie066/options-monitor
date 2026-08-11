# Gateflow Final Closeout — combo-yield-remove-staggered-ledger

- Gate: final closeout
- Work unit: `combo-yield-remove-staggered-ledger`
- Branch: `combo-yield-remove-staggered-ledger`（已 merge 并删除）
- Draft PR: https://github.com/liuxie066/options-monitor/pull/145
- Merge commit: `1340876f`（Merge pull request #145，2026-08-11）
- Status: `final closeout pass` → **work unit completed**（用户已授权 merge，merge 后分支已删除）

## What Changed

在上一 work unit（`combo-yield-remove-staggered`，策略配置面）基础上，把 ledger/生命周期/
归组/归因/CLI 面的错期（`staggered_expiry_pair` / `diagonal`）支持一并移除，使组合收益
只保留 same-expiry（同期）一种结构：

- **resolver（S1）**：删除错期开仓 enrichment、校验与 `unresolved` 失败分支；显式
  pair-intent 错期开仓与普通单腿一致落账（组合关系 pending），不再产生错期推理。
- **手动配对（S2）**：删除 `src/application/positions/combo_pairing.py`（428 行）、
  CLI `om option-positions pair-combo-yield` 子命令及其 import/parser/write_controls/dispatch。
- **lifecycle（S3）**：inventory 只接受 `{same_expiry}`；`staggered_expiry_pair` /
  `diagonal` 历史行落入 `unsupported_expiry_structure` → `review_required`
  （fail-closed，不误报 `same_expiry_mismatch`）；删除 `invalid_diagonal_expiry_order`。
- **归组（S3）**：`_build_edge` 要求 put 到期 == call 到期，不再构造 staggered 边。
- **归因（S3 + code review fix）**：删除 diagonal 到期序校验；`structure_mode` fallback
  （`staggered_expiry_pair -> diagonal`、`same_expiry_pair -> same_expiry`）与 lifecycle
  保持一致；显式非 same-expiry 结构 fail-closed 为 partial；无元数据但两腿到期不同的
  组合报 `same_expiry_mismatch`，不再被静默按同期归因。
- **测试/文档**：改写/删除约 21 个文件中的错期用例；`docs/STRATEGY_ARCHITECTURE.md`
  与旧 post-trade 计划文档标注范围收窄；依赖图重新生成。

## What Was Verified

- 专项套件（resolver/open、CLI、lifecycle、reconciliation、attribution×2、receipt、
  context builder、dependency graph）：**176 passed**。
- 全量套件：**4667 passed**；2 个失败均为环境/文档问题而非代码缺陷：
  HTTP 质量门为沙箱禁止 localhost socket bind（unsandboxed 单测通过）；
  dependency graph 已重新生成并修复。
- `compileall`、`git diff --check` 干净；`rg` 对
  `staggered_expiry_pair|staggered_combo_yield_|execute_staggered_combo_yield_pairing|
  pair-combo-yield|invalid_diagonal_expiry_order` 源码零命中（仅保留 removed 配置文案
  与历史文档）。
- config us/hk 校验通过。
- 生产存量无错期数据：`trade_events` 3 行、`position_lots` 2 行、`combo_pair_inferences`
  0 行，均无 staggered/diagonal 字段；删除不触碰存量数据。
- PR #145 CI：CodeQL、agent-plugin、guardrails、Analyze(actions/python) 全部 `pass`
  后执行 merge。

## Docs Updates

- `docs/STRATEGY_ARCHITECTURE.md`：错期 ledger/CLI 示例移除，改为「组合收益仅支持同期」。
- `docs/plans/post-trade-combo-reconciliation-v1-plan-20260731.md`：范围收窄标注。
- `docs/DEPENDENCY_GRAPH.md` / `docs/dependency_graph.mmd`：模块删除后重新生成。
- `docs/gateflow/combo-yield-remove-staggered-ledger/`：plan、plan review、fix、
  本 closeout。
- `docs/reviews/`：plan review、code review（含严重 finding 修复记录）。

## Finding Status

- plan review：3 个 finding（lifecycle 旧行分类、归因静默接受、验证漏扫）accepted → 已修复。
- code review：1 个严重 finding（归因把生产「未写 expiry_structure」误判为不支持结构）
  accepted → 已修复（fallback + 空值放行 + assigned-stock 复用 + 回归测试）。
- PR review：merge 前全部 CI 通过，无 blocking finding。

## Remaining Risks / Owners

- 旧 diagonal/错期历史数据落入 `unsupported_expiry_structure` → `review_required`
  （fail-closed）；生产当前无存量，风险低。
- 错期成交未来不入组：归组不再生成 staggered 边，由 review 流程处理。
- 非错期疑似死代码（`_with_combo_yield_long_call_payload` /
  `_with_combo_yield_sell_put_payload`）不在本轮范围，owner = 后续死代码清理 work unit。
- 「resolver 落账 → projection → build_period_performance」端到端集成回归
  属 performance 报告入口验证，建议后续 performance 专项补测。

## Next Entry Point

- 已完成：用户授权 push → PR #145 → CI pass → merge（`1340876f`）→ 删除本地/远端分支 →
  本 closeout。
- 后续可选项：非错期死代码清理、performance 端到端集成回归，另开独立 work unit。
