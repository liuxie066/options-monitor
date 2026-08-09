# Gateflow Fix Artifact — Aggregate DeepReview

- Gate: `fix`
- Work unit: `ai-decision-advice`
- Review artifact: `docs/reviews/code-review-20260809-144701.md`
- Status: `fix complete; pending aggregate re-review`

## Finding decisions and fixes

### DR-AGG-01 — accepted — fixed（高）

AI 建议引用的外部证据来源无法渲染：evidence 记录无稳定 ref、validation 不校验
refs、renderer 拿不到证据映射，三者共同导致 design 15.4 来源行静默缺失。

修复（同一数据链路端到端）：

- `collector.py`：`symbol_evidence` 记录分配确定 ref
  `ev-<content_fingerprint 前 12 位>`（fingerprint 真源不变，向后兼容）；
- `contexts.freeze_external_evidence`：历史记录无 ref 时按 fingerprint 重新推导
  ref，冻结视图始终携带 ref；
- `validation.py`：`ScopeSpec` 增加 `allowed_evidence_refs`（按 scope 候选 symbol
  池聚合）；模型输出的 `external_evidence_refs` 必须解析到冻结视图，否则按
  demotion 规则降级 `needs_review`（`unresolved_evidence_refs`）；
- `advice.py`：`AdviceRunResult.to_evidence_index_view(frozen)` 产出冻结证据视图；
- `orchestration.py`：brief view 附带 `evidence_index`（frozen_at + symbols）；
- `daily_decision_brief_service.py`：拆出 `ai_decision_advice_evidence_index`
  顶层字段（不影响 `ai_decision_advice` 的 normalize/diff 契约）；
- `domain/daily_decision_brief.py`：normalize 保留该字段；
- `daily_decision_brief_renderer.py`：`_ai_evidence_ref_map` 从 brief 解析
  ref→记录映射并传入 `render_family_advice_lines`，来源行（标题/发布方/日期/
  URL，≤3 条）恢复渲染；
- prompt `06_output.md`：明确 refs 只能逐字引用输入证据行的 `ref` 字段，
  无证据时空数组。

回归测试：

- `test_derive_scopes_collects_allowed_evidence_refs`
- `test_unresolvable_evidence_ref_demotes_to_needs_review`
- `test_resolvable_evidence_ref_is_kept`
- orchestration 完成路径断言 `view["evidence_index"]` 携带冻结符号行

### DR-AGG-02 — accepted — fixed（中）

“checked-at-only 更新不失效复用”（design 13.2）未实现：`last_success_at`
进入 `index_hash`，collector 每 4 小时成功刷新即失效复用。

修复：`EvidenceIndex.index_hash` payload 移除 `last_success_at`（coverage 与
semantic_hash 已表达 staleness 与语义内容；纯时间戳不构成语义输入）。

回归测试：`test_index_hash_stable_when_only_last_success_refreshes`（同一语义
证据、不同 last_success_at，两次 freeze hash 相等）。

## 验证

- AI Decision Advice 相关测试 183 passed（advice/validation/orchestration/
  evidence_store/collector/render/contexts/domain_diff/service/agent_tool/
  notify format）；
- dependency graph 重新生成，`test_dependency_graph_generator_check_passes` 通过；
- 全量套件残留失败均为非本 work unit 的 dirty-worktree 改动或既有基线失败
  （copilot_phase1 ×2、copilot_eval、runtime_status profile、
  futu_portfolio_context、sell_put_linked_call_helper），修复前后无变化。
