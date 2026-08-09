# Gateflow Slice Implementation — S3 External Evidence Collector + 证据存储

- Gate: `implementation`（slice S3）
- Work unit: `ai-decision-advice`
- Plan: `docs/gateflow/ai-decision-advice/plan-20260809.md` S3

## Changed files

- `src/application/ai_decision_advice/evidence_store.py`（新增）：
  `append_evidence_records`（追加写共享 JSONL、每行自带 evidence_run_id +
  appended_at）、`read_evidence_records`（容错读）、`freeze_evidence_index`
  （从追加日志重建最新标的视图：completed / no_evidence / stale /
  identity_unavailable，UTC + `last_success_at` 基准、8h 过期）、URL+内容
  指纹去重、`EvidenceIndex.index_hash`（语义 hash，不随 checked 时间变化）；
- `src/application/ai_decision_advice/collector.py`（新增）：
  `run_evidence_collector`（批 5、全局 5min 预算、超预算标记 unfinished、
  identity_unavailable 跳过模型）、`_call_with_repair`（预算内一次格式修复
  重试）、严格 JSON Schema（`EVIDENCE_OUTPUT_SCHEMA`）、batch 审计 +
  标的证据 + 状态记录三类 JSONL 记录（证据携带身份快照 hash）、
  `compute_cutoffs`（首次 30 天 vs 增量）、`validate_evidence_payload`；
- `src/application/ai_decision_advice/prompts/__init__.py`（新增）：
  Prompt Pack 编译（有序 Markdown 片段 + 编译 SHA-256 + 审计 payload）；
- `src/application/ai_decision_advice/prompts/evidence/01-04*.md`（新增）：
  角色、身份约束、来源等级与去噪、输出合同；
- `tests/test_ai_decision_advice_evidence_store.py`（新增，8 例）、
  `tests/test_ai_decision_advice_collector.py`（新增，12 例）。

## 未包含（顺延说明）

- opt-in collector systemd timer：运行入口（CLI 命令）尚不存在；在 S6 编排
  确定 Advice/Collector 的运行宿主后一并落地，避免先渲染指向不存在命令的
  unit。已在 plan 风险表跟踪。

## Validation

- `python3.12 -m pytest tests/test_ai_decision_advice_collector.py
  tests/test_ai_decision_advice_evidence_store.py -q` → 20 passed。

## Residual risks

- DeepSeek `web_search` 真机参数形态：assigned to release gate canary；
- shared-state 写入的 symlink/no-follow 防护：evidence_store 与 identity
  publish 同一目录，追加写 JSONL 模式与既有 runtime 追加写（如
  strategy_scan_failures）一致，统一在 aggregate deepreview 审视；
- collector timer 与 CLI 入口：tracked above，S6 落地。

## Completion status

Complete（timer 顺延至 S6）；进入 code review gate。
