# Final Closeout — candidate-filter-run-resolution

- Gate: `final closeout`
- Work unit: `candidate-filter-run-resolution`
- Branch: `feat/candidate-filter-run-resolution` (base `main@421591dd`)
- Draft PR: https://github.com/liuxie066/options-monitor/pull/153
- Status: `final closeout pass`

## What changed

- `candidate_filter_explain` 新增通知轮次定位能力：
  - 新输入 `run_selector=latest_notification` + `notification_date`（ISO YYYY-MM-DD，默认运行时主机本地"今天"）；
  - 从 shared notification perception audit 中定位"当日实际投递到该账户的最近一次通知"对应的 run，再加载其 manifest-bound opening candidate snapshot；
  - 优先级：显式 `run_id` > `latest_notification` > 默认 latest（默认路径行为不变）；
  - 输出 `source.run_resolution` provenance（selector / notification_date / timezone / resolved_run_id / matched_event_created_at_utc）。
- Fail-closed 错误（`DEPENDENCY_MISSING` + `details.reason`）：`no_notification_run`、`audit_window_truncated`、`snapshot_unavailable_for_notification_run`。
- `notification_perception_read.py` 新增内部 helper `iter_notification_perception_events`（上限 5000，返回 `truncated` 标志）；公共工具 50-row cap 不变。
- 工具 schema/description/examples/`copilot_input_fields` 与 copilot normalizer 同步扩展；`docs/TOOL_REFERENCE.md` 补充语义说明。

## What was verified

- 165 passed：`tests/test_candidate_filter_run_resolution.py`（16 个新增）+ candidate trace/snapshot manifest/agent plugin contract+smoke + notification perception read/event 回归；1 个 pre-existing deprecation warning。
- CI on PR #153 全部 pass：Analyze (actions)/(python)、CodeQL、agent-plugin、guardrails。
- Gate artifacts 齐备：plan、plan review fail->fix->pass、S1 implementation、code review pass-with-risks->fix->pass、aggregate deepreview pass-with-risks->fix->pass、PR review pass。

## Docs updates

- `docs/TOOL_REFERENCE.md`：`run_selector=latest_notification` 语义说明。
- `docs/gateflow/candidate-filter-run-resolution/`：plan / s1-implementation / s1-review-fix / aggregate-fix / final-closeout（本文件）。
- `docs/reviews/`：plan-review x2、code-review x4、pr-153-review x1。

## Finding status

- Plan review F1-F5：已修复，re-review pass（`plan-review-20260813-141818.md`）。
- Code review CR-1：已修复（截断不可诊断），re-review pass（`code-review-20260813-145637.md`）。
- Aggregate deepreview DR-1：已修复（resolved run_id 丢失），re-review pass（`code-review-20260813-150727.md`）。
- PR review：未发现实质性问题（`pr-153-review-20260813-153028.md`）。

## Remaining risks / owners

- O(file) 全量 audit scan：accepted risk；若成为热点，后续 performance work unit 加索引读取。
- CR-2（pre-existing）：candidate 工具 base 不经 `resolve_runtime_root`（`OM_RUNTIME_ROOT`）；后续 work unit 统一接线。
- 低：过去日期 + 多账户混合事件的组合场景未单测；同一谓词/日期映射函数驱动，风险低。

## Issue link status

- 非 issue work unit，PR body 无 closing keyword，无需 issue closeout comment。

## Next entry point

- 用户 merge PR #153 后：可选后续 work unit —— candidate 工具统一接 `resolve_runtime_root`；audit 索引读取（如 scan 成为热点）。
- merge / approve / mark ready for review / delete branch 均需棒棒的liuxie 单独授权。
