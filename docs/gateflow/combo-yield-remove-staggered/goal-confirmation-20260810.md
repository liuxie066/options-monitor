# Gateflow Goal Confirmation — Combo Yield 移除错期（staggered_expiry_pair）

- Gate: goal confirmation
- Work unit: `combo-yield-remove-staggered`
- Date: 2026-08-10
- Branch: `design/controlled-auto-upgrade`（待用户确认是否另建工作分支）
- Status: confirmed by user（2026-08-10，用户指示"拉一个新分支，然后开始实施"）
- Artifact path: `docs/gateflow/combo-yield-remove-staggered/goal-confirmation-20260810.md`

## 背景与动机

用户提出三个问题并要求逐一审查 Sell Put / Covered Call / Combo Yield 三策略的偏移与冗余代码：

1. 错期组合的排序策略是否应该和同期保持逻辑一致？
2. 在现有投资策略思想下，是否应该做错期？同期和错期同时存在时如何排序？
3. 倾向删除全部错期部分代码，结合之前几点汇总所有修改点。

## 直接代码证据

### 排序不一致（偏移）

`domain/domain/engine/yield_enhancement.py` 存在三套排序键，结构不同：

- 同期 `yield_enhancement_rank_key`：主键 `funding_accepted` → `net_credit_retention` → `call_delta`（参与度）→ `combo_spread_ratio` → min OI → `put_assignment_margin_pct` → `annualized_net_credit_yield` → ...（PR #137 对齐后的唯一主键口径）。
- 错期 `yield_enhancement_staggered_rank_key`：主键 `funding_accepted` → `put_only_period_net_return`（put 期间、非年化）→ `net_credit_retention` → assignment margin → `call_delta` → ...
- 错期 call 选择 `yield_enhancement_staggered_call_rank_key`：主键直接变成 `call_delta` → `net_credit_retention` → spread → call OI → call DTE。
- `rank_yield_enhancement_calls_for_put()` 对错期走 `staggered_call_rank_key`，对同期走主 rank key；同一条"为 Put 选 Call"的流水线上，两套口径并存。
- `yield_enhancement_pair_shadow_rank_key`（shadow）又把 `put_only_period_net_return` 作为主键，与同期主键也不同。

结论：错期与同期排序**逻辑不一致**，且是 2026-07-18 cross-expiry 归因 work unit 与 2026-08-08 S3 政策对齐时**刻意保留**的不一致（closeout 原文："跨标的（staggered + shadow）用 `put_only_period_net_return` 期间非年化"）。理由见 `docs/STRATEGY_ARCHITECTURE.md`：错期 Put/Call 风险期限不同，压成单一到期日年化会制造错误精度。

### 指标退化与字段置 None（偏移 + 冗余）

- `compute_yield_enhancement_metrics()`：`is_staggered` 时把 `combo_breakeven`、`downside_breakeven_penalty`、`upside_breakeven`、`max_loss_if_zero` 全部置 None。
- `sell_put_call_helper._build_pair_row()`：约 20 处 `None if is_staggered else ...`，并额外追加两行把 `annualized_net_credit_yield` / `annualized_return` 再置 None（重复）。
- 错期分支使 `expiration_scope` / `dte_scope` / call window / cross-expiration join / `call strike >= spot` 例外 / expected IV 例外全部独立成路。

### 零生产使用

- `config.yaml`、`config.us.json`、`config.hk.json`：全部 `structure_mode: same_expiry_pair`；错期只出现在 `configs/examples/user.example.us.json`。
- `output_shared/state/option_positions.sqlite3`：`trade_events` / `position_lots` / `combo_pair_inferences` 中 staggered/diagonal 记录均为 0。
- `output_shared` / `output_runs` 中无 staggered 运行产物（仅一份 research stash-backup patch）。

## 目标

从 Combo Yield **开仓策略路径**完整移除错期支持，使同期 `same_expiry_pair` 成为唯一策略结构、单一排序路径：

1. 删除错期排序键与 rank dispatch（staggered 两个 rank key、`rank_yield_enhancement_calls_for_put` 错期分支）。
2. 删除指标层 `is_staggered` 分支与错期 None 字段语义，`_build_pair_row` 消除全部 `None if is_staggered` 分支。
3. 删除错期配置面：`YIELD_ENHANCEMENT_STRUCTURE_MODES` 中 staggered、`YIELD_ENHANCEMENT_STRUCTURE_DEFAULTS`、gap 默认值、`resolve_staggered_expiry_gap_days`、config validator gap 校验、示例配置同步。
4. 删除错期开仓扫描/配对/数据需求（`sell_put_call_helper` 跨期 join 与 call window、`required_data_planning` / `required_data_prefetch_planning` 错期分支）。
5. 删除错期通知/展示（`render_yield_enhancement_alerts` 错期告警、`notify_symbols` 错期格式化、`alert_engine` 特判、`report_summaries` note、Daily Brief 相关测试数据）。
6. 删除错期 research/shadow replay 变体（`shadow_replay/*`、`strategy_lab/combo_evaluator`、`strategy_attribution` diagonal 分支、示例 variants 文档）。
7. 汇总全部修改点（见下节），清理相关测试与文档，确保 `rg staggered_expiry_pair` 只剩历史 gateflow artifacts / CHANGELOG / 文档保留记录。

## 非目标 / Scope Boundary

- 不改变同期 `same_expiry_pair` 的现有排序/过滤/通知策略（`net_credit_retention` 主键保持不变）。
- 不删除 CC+LP 同期变体（`cc_lp`）。
- 不重构 Sell Put / Covered Call 与 Combo Yield 无直接关系的代码。
- **待确认**：持仓形态侧（ledger/trade intake/生命周期/reconciliation）的错期支持是否一并删除：
  - `src/application/trades/resolver.py`（staggered 成交 enrichment/校验，31 处）
  - `src/application/positions/combo_pairing.py` + `option_positions pair-combo-yield` CLI（手工配对 V1）
  - `domain/domain/combo_yield_lifecycle.py` / `combo_reconciliation.py`（已存在错期持仓的记账/对账）
- **待确认**：research/shadow replay 错期变体是否随开仓路径一起删除，还是保留为研究工具。
- 本轮不做：错期排序的"统一口径修正"（既然倾向删除，不做修复）；新增同期排序字段；重构无关 dirty 改动（position advice 移除 + 生产升级设计）。

## 修改点汇总（一二三四五六七）

1. **Domain 排序/指标核心**：`domain/domain/engine/yield_enhancement.py` — 删除 `staggered_expiry_pair` 校验分支、`is_staggered` 指标 None 分支、两个 staggered rank key、rank dispatch、`rank_yield_enhancement_calls_for_put` 错期分支、research policy 的 gap 参数约束。
2. **开仓扫描/配对**：`src/application/sell_put_call_helper.py` — 删除跨期 join、独立 call window、strike≥spot 例外、expected_iv 例外、`_build_pair_row` 约 20 处 `None if is_staggered`。
3. **配置与校验**：`src/application/yield_enhancement_config.py`、`src/application/config_validator.py`、`configs/examples/user.example.us.json` — 删除 staggered mode、结构默认值、gap 参数、validator 分支。
4. **数据需求规划**：`src/application/required_data_planning.py`、`src/application/required_data_prefetch_planning.py` — 删除 `_filter_staggered_call_expirations` 与 gap 分支。
5. **通知/展示**：`src/application/render_yield_enhancement_alerts.py`、`src/application/notify_symbols.py`、`src/application/alert_engine.py`、`src/application/report_summaries.py` — 删除错期告警文案与格式化分支。
6. **Research/shadow replay**：`src/application/shadow_replay/*`、`src/application/strategy_lab/combo_evaluator.py`、`domain/domain/performance/strategy_attribution.py`、`docs/examples/combo-yield-shadow-variants.json` — 删除错期 variant 支持（待确认）。
7. **Ledger/生命周期（待确认）**：`src/application/trades/resolver.py`、`src/application/positions/combo_pairing.py`、`src/interfaces/cli/option_positions.py`、`domain/domain/combo_yield_lifecycle.py`、`domain/domain/combo_reconciliation.py` — 删除错期成交/配对/生命周期支持，或保留为后续独立 work unit。

另含：约 10 个测试文件的错期用例清理；`docs/STRATEGY_ARCHITECTURE.md`、`docs/PRODUCT_ARCHITECTURE.md` 策略文档更新。

## 成功信号

- `rg -i "staggered|错期"` 在 `domain/ src/ tests/` 中不再命中错期开仓路径（历史 gateflow artifacts、CHANGELOG 除外）。
- `yield_enhancement_rank_key` 不再分支到 staggered；同期排序逻辑单一。
- `_build_pair_row` / `compute_yield_enhancement_metrics` 不再存在 `is_staggered` 分支。
- 全量 pytest 与 ruff 通过；US/HK `config validate` + `config build --dry-run` 通过。
- Daily Brief / 通知 / 报告不再输出错期（"错期全额融资"）文案。

## 过度设计声明

本轮只做"删除未启用的错期开仓路径 + 同步清理"。不为删除引入新抽象、新配置、新排序字段；不重做错期排序；不扩大范围到 Sell Put/CC 的其他既有实现。

## Blocking Open Questions

1. 分支策略：当前分支 `design/controlled-auto-upgrade` 工作树有大量既有 dirty 改动（position advice 移除 + 生产升级设计，与本任务无文件语义重叠，但 `tests/test_daily_decision_brief_service.py` 与 `src/application/config_validator.py` 两文件有物理重叠）。继续当前分支、从 HEAD 新建 `design/combo-yield-remove-staggered`、还是用独立 worktree？
2. 第 7 项 ledger/生命周期错期支持：一并删除，还是保留为 non-goal 后续独立 work unit（推荐保留，避免触碰交易录入契约；生产 ledger 当前零错期数据）？
3. 第 6 项 research/shadow replay 错期变体：随开仓路径一并删除，还是保留研究工具？

## 用户决策记录（2026-08-10）

1. 分支：从当前 HEAD 新建 `design/combo-yield-remove-staggered`（用户："拉一个新分支"）。既有 dirty 改动随工作树保留在新分支，提交时按文件/hunk 隔离，只 stage 本 work unit 相关改动。
2. 第 7 项 ledger/生命周期错期支持：**保留为 non-goal**，归后续独立 work unit（采纳推荐；生产 ledger 零错期数据，不动交易录入契约）。
3. 第 6 项 research/shadow replay 错期变体：**随开仓路径一并删除**（采纳推荐，保持策略面一致）。

## Binding Scope Contract

- 本 work unit 删除范围：开仓策略路径（domain 排序/指标、扫描配对、配置校验、数据需求规划、通知展示、research/shadow replay）+ 相关测试与文档。
- 保留范围：ledger/trade-intake/生命周期/reconciliation 错期支持（`trades/resolver.py`、`positions/combo_pairing.py`、CLI、`combo_yield_lifecycle.py`、`combo_reconciliation.py`）。
- 同期 `same_expiry_pair` 排序口径（`net_credit_retention` 主键）不变；CC+LP 变体不变。
