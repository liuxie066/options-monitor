# Aggregate Deepreview — Combo Yield 策略对齐

## Scope

- Gate：aggregate deepreview
- Work unit：combo-yield-policy-alignment
- Base：`97ed8b4f`（accepted plan commit）
- Reviewed range：S1-S4 全部 committed changes（`97ed8b4f..HEAD`）+ aggregate 期间修复
- Files：26 files changed, ~1100 insertions
- Excluded：`docs/candidate_strategy.md`（v1.10.17 遗留无关改动，不入 commit）

## Method

走读真实数据路径：

1. `config_defaults / system.json / yield_enhancement_config / config_validator`（S2 删除路径）
2. `combo_yield_steps -> symbol_monitoring -> pipeline_symbol -> pipeline_watchlist`（S1/S4 sink 与 seal 链路）
3. `domain/engine/yield_enhancement.py` rank keys（S3）
4. `daily_decision_brief_service -> combo snapshot`（S4 消费）
5. shadow / research 兼容路径（确认文档 non-goal）
6. 全量测试 + 依赖图 + config validate/build

## Findings

### 1-已修复-高-run 级 Combo snapshot 永不封存：seal 触发条件与 capture 数据字段不匹配
- **位置**: `pipeline_watchlist.py`（combo seal 条件）；`symbol_monitoring.py`（combo status 发布）
- **问题类型**: 状态机漏洞 / 数据不一致
- **当前写法（修复前）**: watchlist 过滤 `capture_statuses` 中 `item.get("family") == "combo_yield"`，而 `capture_statuses` 收集的是 `candidate_capture_status_sink_fn` 收到的 dict（字段为 `symbol/strategy_mode/status/reason`，无 `family`），且 symbol_monitoring 的 combo 分支从不调用该 sink。
- **反例/失败场景**: 生产 Combo 扫描完成后，`combo_statuses` 恒为空 → `if combo_statuses:` 恒 False → `combo_yield_candidate_snapshot.json` 永不生成 → Daily Brief 只能读到 `combo_snapshot_unavailable`，Combo 候选丢失。
- **为什么有问题**: S4 的核心交付（sealed snapshot 唯一真源）在真实链路中不生效，空结果与有结果都无法封存。
- **直接证据**: `symbol_monitoring.py` combo 成功/失败分支只调 `_publish_status`（写文件），不调 `_report_capture`（sink）；`capture_statuses` 条目无 `family` 字段；watchlist 用 `family` 过滤。
- **影响**: 生产 Combo 候选静默丢失，Daily Brief 降级为缺数据。
- **修复**: symbol_monitoring combo 成功/失败分支补 `_report_capture(strategy_mode="combo_yield", ...)`；watchlist 改为 `strategy_mode == "combo_yield"` 过滤。新增 `test_symbol_monitoring_reports_combo_capture_status`。
- **修复风险（低/中/高）**: 低
- **严重程度（低/中/高/严重）**: 高
- **状态**: 已修复

### 2-已修复-中-snapshot `ranked_pairs` 顺序非正式排序，Daily Brief 仍二次排序
- **位置**: `pipeline_watchlist.py`（seal）；`daily_decision_brief_service.py:311`
- **问题类型**: 契约 / 语义所有权
- **当前写法（修复前）**: watchlist 按 symbol 处理顺序 append pairs 直接 seal；Daily Brief 仍调 `select_best_yield_enhancement_per_symbol` 二次排序。
- **反例/失败场景**: snapshot 中 pairs 顺序是 capture 顺序而非正式排序；Daily Brief 依赖自己的二次排序补位，与“消费者只读快照正式排序结果”的确认文档冲突，且两处排序实现可能漂移。
- **为什么有问题**: 确认文档 §7 明确 Daily Brief 不得二次排序；正式排序应唯一存在于 domain / seal 时。
- **直接证据**: `daily_decision_brief_service.py:311`；`pipeline_watchlist.py` seal 前未排序。
- **修复**: watchlist seal 前对聚合 pairs 调用 `select_best_yield_enhancement_per_symbol`；Daily Brief 直接读 snapshot 顺序；fixture 同步用同一排序函数。
- **修复风险（低/中/高）**: 低
- **严重程度（低/中/高/严重）**: 中
- **状态**: 已修复

### 3-未修复-低-`put_only_period_net_return` 依赖上游主策略 fail closed，Combo 层无显式正性校验
- **位置**: `domain/domain/engine/yield_enhancement.py` rank keys
- **问题类型**: 契约边界（deferred）
- **当前写法**: rank key 读 `put_only_period_net_return`，缺失 fallback `period_net_return_on_cash_basis`；无显式非正处理。
- **直接证据**: 主策略 `candidate_engine.py:461-469` 对 `net_cash_basis <= 0` fail closed；Combo put universe 只含主策略 underwriting 通过的正收益行，当前不可达。
- **影响**: 低；上游保证兜底。
- **建议**: 后续 work unit 在 `_build_pair_row` 加显式校验；本 slice 记录 owner。
- **修复风险（低/中/高）**: 低
- **严重程度（低/中/高/严重）**: 低
- **状态**: deferred-with-owner（后续 work unit）

### 4-未修复-低-shadow/research 层保留 `max_call_cost_to_put_credit` 字段语义
- **位置**: `shadow_replay/combo_capture.py`、`domain/engine/yield_enhancement.py`（`ComboYieldResearchPolicy`）、`research/evidence.py`
- **问题类型**: 契约一致性（deferred）
- **当前写法**: research/shadow 仍读写 `max_call_cost_to_put_credit`；生产配置已删除。
- **直接证据**: 确认文档 non-goal“不把 Shadow Replay / research 层的历史兼容读取移除”；research 测试通过。
- **影响**: 低；不影响生产路径。
- **建议**: 后续 work unit 清理 research 层字段语义。
- **修复风险（低/中/高）**: 低
- **严重程度（低/中/高/严重）**: 低
- **状态**: deferred-with-owner（后续 work unit）

## Open Questions

- 无 blocking open question。

## Residual Risks

- run 级 seal 的“combo completed + 空 pairs → no_candidate”端到端断言缺失（单测覆盖 seal 本身，symbol_monitoring 覆盖 status 发布，但真实 pipeline 全链路未合流断言）：fixed in later approved slice / 后续集成测试。
- 依赖图已重新生成（新增模块）；CI 全量在 base 上有一个时间敏感既有失败（`test_build_opend_exchange_rate_observation_uses_account_funds_conversion`，硬编码 2026-08-07 时间戳，base 29e7132d 同样失败），非本 work unit 引入。
- HTTP quality gate 单测在沙箱内因 socket bind 限制失败，沙箱外 4 passed；与本 work unit 无关。

## Validation

- focused：combo / daily brief / watchlist / symbol monitoring / config / snapshot 全绿；
- 全量：4561 passed, 10 skipped；3 failures 已逐项澄清（HTTP 沙箱限制、依赖图过期已修、futu 既有时间敏感基线失败）；
- `generate_dependency_graph.py` 重新生成后 `--check` 通过；
- US/HK config validate + build dry-run 通过。

## Conclusion

**pass**（aggregate 修复后）。两个实现期发现的真实问题（seal 触发条件错误、二次排序残留）已修复并测试；剩余为 deferred 的 research 层字段清理与显式正性校验。
