# S5 Implementation — Advice 节点、校验降级与持久化

- Slice: S5
- Date: 2026-08-09
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` §S5

## Files

- `src/application/ai_decision_advice/validation.py`（新增）
  - `derive_scopes`：Sell Put 单 scope（共享现金池），Covered Call 按标的
    一个 scope；baseline = Candidate Engine rank-1；allowed pool = 该 scope
    的合格候选集合；`symbol_evidence_complete` = 池内全部标的证据
    `completed`。
  - `zero_candidate_flags`：SP/CC 合法零候选标记。
  - `validate_advice_payload`：结构失败（schema / run_id / account_ref /
    market / input_bindings / 重复 scope）→ 整体 `unavailable`；语义违规
    （baseline 不符、未知 scope、switch 出池含 CC 跨标的、defer/needs_review
    带 selected、keep selected≠baseline）→ 降级 `needs_review`；
    上下文缺失 → 所有动作上限 `needs_review`；证据覆盖不完整（stale /
    no_evidence / identity_unavailable）→ `keep` 降级为 `defer`。
- `src/application/ai_decision_advice/advice_store.py`（新增）
  - `ai_decision_advice.jsonl` 追加写/读（`output_runs/<run_id>/accounts/
    <account>/state/`，docs 12.3）；
  - 复用判定：4 个语义输入 hash + prompt/model/schema 版本全等
    （`external_evidence_run_id`、checked-at-only 更新不触发重新调用）；
  - `build_reuse_record`：复制正式记录并绑定 `reuse_of_advice_id` 与当前
    run / 新证据覆盖绑定。
- `src/application/ai_decision_advice/advice.py`（新增）
  - `ADVICE_OUTPUT_SCHEMA`：`ai_decision_advice.v1` 严格 JSON Schema；
  - `run_decision_advice`：合法零候选短路（不调模型、不生成动作，
    `not_applicable / zero_candidate`）→ 复用查找 → 模型调用（30s 账户
    总预算，一次预算内结构修复，不做启发式解析）→ 校验/降级 → 持久化；
  - `AdviceRunResult.to_brief_view`：输出 plan §S6 前置契约的 brief view
    结构。
- `src/application/ai_decision_advice/prompts/advice/01-06*.md`（新增）
  - 角色 / 决策边界 / 动作合同 / 策略视角 / 一致性检查 / 输出合同，
    复用 S3 prompt pack 编译机制。
- 测试：`tests/test_ai_decision_advice_validation.py`（16）、
  `tests/test_ai_decision_advice_advice.py`（11）。

## 关键行为

- 合法零候选：`status=not_applicable`、`unavailable_reason=zero_candidate`，
  不调模型、不伪造 defer（docs 9.8）。
- 复用：同输入 hash + 同版本时复制旧记录，绑定新 evidence run，不重复
  调用模型（docs 13.2）。
- 超时/异常/二次非法输出 → `unavailable`，raw_response 与
  repair_attempted 留痕（docs 10）。
- `account_ref` = sha256(run_id:account)[:12]，单次运行匿名引用（docs 10）。

## 验证

- `python3.12 -m pytest tests/test_ai_decision_advice_*.py -q` → 83 passed。
