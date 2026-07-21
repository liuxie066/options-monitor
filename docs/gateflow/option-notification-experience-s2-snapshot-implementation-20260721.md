# Gateflow Slice 2 Implementation — Successful snapshot funds and candidate index

- Gate：implementation slice 2
- Work unit：期权监控通知体验升级
- Base commit：`a8f4aeb3`
- Scope：Daily Brief additive funds/candidate-index contract、run-scoped funds assembly、candidate identity/eligibility、failure/no-op characterization
- Implementation completed at：2026-07-21 18:48:34 CST

## Changed files

- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_domain.py`
- `tests/test_daily_decision_brief_service.py`
- `docs/gateflow/option-notification-experience-s2-snapshot-implementation-20260721.md`

## Implementation decisions

1. 保留 `daily_decision_brief.v1`，增加向后兼容的 `funds` 与 `candidate_index`；旧 revision 缭字段时 normalizer 返回明确 unavailable funds，并从现有 opening actions best-effort 派生候选身份。
2. 候选身份使用 pure function：`candidate:v1:<account>:<market>:<canonical_symbol>:<strategy_family>`；账户小写、市场/标的大写、Covered Call 统一为 `covered_call`，非法市场/标的/策略族 fail closed。
3. 资金只读取本轮 `output_runs/<run_id>/accounts/<account>/state/` 下的 `portfolio_context.json` 与 `option_positions_context.json`，不调用 broker/context query；按原币种计算 `cash total - reliable secured usage`。
4. 现金总额未知时 brief blocked，避免把未知伪装成 0；担保占用不可靠时仍保留可靠现金总额，但 opening funds 为空、`available=false`、status degraded。
5. `candidate_index` 从完整 canonical candidate rows 构造，不受 top-3 展示上限影响；同一账户/市场/标的/策略族只保留一个 identity，representative 取 canonical order 第一项，`contract_count` 统计全部合格合约。
6. 仅 `live_actionable` brief 生成检测 index；capacity < 1、字段不完整、非法 identity、blocked/planning brief 均不进入 eligible index。
7. pipeline failure 与 `ran_scan=false` no-op 继续形成 blocked brief；本 slice 不写 repository current，success-only persistence 在 Slice 3 接入。

## Data flow and invariants

```text
run-scoped portfolio context + option-position context
  -> funds (native currency, explicit unavailable)
canonical accepted candidate artifacts
  -> existing rank/capacity authority
  -> all eligible rows grouped by stable candidate identity
  -> bounded representative + contract_count
normalized daily_decision_brief.v1
```

- 未新增 broker fetch、scanner、ranking、database 或 sender。
- candidate identity 不包含 expiration/strike/contract/rank/price/yield/capacity。
- built-in `hash()` 不参与 identity；不同 `PYTHONHASHSEED` 输出一致。
- 原 `candidates/actions/capacity` 保持现有 top-N 展示与审计语义。
- option secured reliability flag malformed/fail-closed 时 opening funds fail closed。

## Validation

- Complete Daily Brief suite：`137 passed in 1.33s`。
- Existing notification/tick formatting regression：`43 passed in 0.78s`。
- `python3.12 -m ruff check <changed Python files>`：pass。
- `python3.12 -m compileall -q <changed Python files>`：pass。
- `git diff --check`：pass。

## Docs decision

本 slice 只记录内部 snapshot contract；用户可见资金/候选消息与查询文档在 Slice 5 一次更新，避免提前描述尚未接入的 renderer/CLI 行为。

## Residual risks and uncovered areas

- blocked/failure brief 目前仍可能被旧 repository flow 当作 current 准备：covered by later approved Slice 3 success-only persistence；中间版本不得部署。
- `funds/candidate_index` 尚未用于 notification decision 或用户渲染：covered by later approved Slice 4/5。
- Combo Yield 只有在 canonical pair row 携带可验证的 put-side cash capacity 且至少 1 手时才进入 candidate index：accepted contract，真实 rollout 需观察数据完整率。
- Additive normalizer 会改变旧 revision 的 canonical digest：covered by Slice 3 explicit v1 -> v2 migration，禁止在 migration 前单独部署本 slice。

## Completion status

- Slice 2 implementation：pass
- Unclassified residual risks：0
- Slice 2 code review：pass after two accepted findings were fixed and re-reviewed。
- Review artifact：`docs/reviews/code-review-20260721-184937.md`。
- Current gate / next entry point：accepted Slice 2 commit
