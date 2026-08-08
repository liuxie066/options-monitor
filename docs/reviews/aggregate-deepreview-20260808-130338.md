# Aggregate DeepReview — CC+LP（同到期）

- Work unit: cc-lp-same-expiry
- Branch: feat/cc-lp-same-expiry
- Base: main
- Date: 2026-08-08 13:03:38
- Scope: 全部 3 个 slice（domain / 配置+扫描编排 / snapshot+封存+消费），含 plan/review artifacts
- Output: docs/reviews/aggregate-deepreview-20260808-130338.md

## Review Method

- 逐 slice 复读实现与 review/fix/re-review 状态；
- 跨 slice 一致性检查：配置契约、strategy_family/variant、snapshot schema、消费端、排序/指标口径、候选真源；
- adversarial pass：未启用 variant 的封存行为、候选丢失路径、指标口径漂移、死代码、docs 遗漏。

## Findings

### 1-已修复-中-Daily Brief 未消费 CC+LP snapshot（plan Slice 3 承诺缺失）
- **入口/函数**: `daily_decision_brief_service`
- **位置**: plan `cc-lp-same-expiry-implementation-plan-20260808.md` §9 Slice 3；实现
- **问题类型**: 目标漂移 / 实施缺口
- **当前写法**: plan 承诺"Daily Brief 消费快照（加载到数据源，不改 renderer）"
- **实际行为**: 实现只做了 pipeline 封存，未在 `daily_decision_brief_service` 加载 `cc_lp_candidate_snapshot.json`
- **修复**: 新增 `_load_cc_lp_snapshot_family`，在 `_load_combo_yield_snapshot_family` 后调用，source_artifacts 加 `cc_lp_snapshot` 条目（不改 renderer）
- **验证**: `tests/test_daily_decision_brief_service.py` + `test_daily_decision_brief_domain.py` 86 passed

### 2-已修复-低-`CC_LP_STATUSES` 死常量与 `portfolio_ctx` 未用参数（Slice 2 F4 部分遗漏）
- **位置**: `src/application/cc_lp_steps.py`
- **问题类型**: 最佳实践偏离
- **实际行为**: `CC_LP_STATUSES` 未使用；`run_cc_lp_scan` 的 `portfolio_ctx` 参数未用
- **修复**: 删除死常量与未用参数；`run_cc_lp_variant` 同步去掉传参
- **验证**: ruff 全绿、测试通过

### 3-已修复-低-`combo_spread_ratio` 口径与 SP+LC 不一致且无注释
- **位置**: `domain/domain/engine/cc_lp.py:118-120`
- **问题类型**: 语义漂移风险
- **实际行为**: CC+LP `combo_spread_ratio = call_spread_ratio + put_spread_ratio`（诊断字段），SP+LC 是 `spread notional / max(|net_credit|, floor)`；若被误用于 SP+LC 式门槛会失真
- **修复**: dataclass 加注释明确"两腿 spread_ratio 之和，诊断用，勿用于 SP+LC 式门槛"；排序用 `max(call_spread, put_spread)` 不受影响
- **验证**: ruff 全绿

### 4-已修复-低-`docs/candidate_strategy.md` 未更新（plan §11 docs decision）
- **位置**: `docs/candidate_strategy.md`
- **问题类型**: docs 决策未完成
- **修复**: 文末加"附：CC+LP 变体"小节，指向确认文档，记录关键口径与默认关闭状态
- **验证**: 文档内容与确认文档一致

### 5-未修复-低-测试缺口：pipeline 封存 CC+LP 分支无端到端集成测试
- **位置**: `pipeline_watchlist.py` seal 区
- **问题类型**: 测试缺口
- **实际行为**: `run_cc_lp_variant` 的 sink 转发有单测，snapshot 有单测，但 `run_watchlist_pipeline_default` 的 `if cc_lp_statuses:` 封存分支无集成测试
- **影响**: 未来改动可能破坏封存链路而测试不报
- **建议**: 作为 residual risk 记录；后续 work unit 或补测时覆盖
- **修复风险（低/中/高）**: 低
- **严重程度（低/中/高/严重）**: 低

## Open Questions

- 无。

## Residual Risk

- pipeline 封存 CC+LP 分支缺端到端测试（Finding 5，deferred，owner=后续 work unit）；
- Daily Brief 展示 CC+LP（renderer）是 plan 非目标，deferred；
- CC+LP 候选真源：`cc_lp_candidates.csv`（report 诊断）+ `cc_lp_candidate_snapshot.v1`（正式），CSV 仅供诊断，正式消费只走 snapshot（与 SP+LC 一致）。

## Conclusion

**pass**。全部 3 个 slice 实现与已确认策略口径一致；aggregate 发现 5 项（1 中 4 低），其中 4 项已修复，1 项低严重度测试缺口记录为 deferred residual risk。无 blocking open question。
