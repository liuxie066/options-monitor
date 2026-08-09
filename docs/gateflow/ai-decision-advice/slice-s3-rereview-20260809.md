# Gateflow Review Artifact — S3 Code Review

- Gate: `code review` + `re-review`（slice S3）
- Work unit: `ai-decision-advice`
- Slice: S3 External Evidence Collector + 证据存储

## Review scope

`evidence_store.py` / `collector.py` / `prompts/` 与测试；对照设计文档 6、
11、12.2 与 plan S3 验收点。

## Findings（review → fix → re-review）

| # | Finding | 状态 |
|---|---|---|
| DR-S3-01 | collector timer 渲染会指向不存在的 CLI 入口 | 已修复：timer 顺延至 S6（编排宿主确定后落地），implementation artifact 明确记录 |
| DR-S3-02 | `last_checked_at` 更新但无新证据时 evidence hash 是否会误变 | rejected-with-reason：`EvidenceIndex.index_hash` 只含 coverage/semantic_hash/last_success_at，不含 checked 时间；测试 `test_index_hash_changes_with_semantics_not_checked_time` 覆盖复用条件（设计文档 13.2） |
| DR-S3-03 | 修复重试把修复指令追加进静态 prompt 文本，违反"动态内容不进指令" | rejected-with-reason：修复指令是固定的静态后缀（非动态数据），设计文档 6.7 允许一次格式修复；动态输入始终走 JSON data 通道 |
| DR-S3-04 | JSONL 追加与 tick 进程并发 | accepted：追加写 + 每行自包含 + freeze 时按 appended_at 取最新，容忍行间交错；测试覆盖乱序 appended_at 去重 |

## 复查确认

- 覆盖判定：completed（可审计、无错误、≤8h，零证据也算）/ no_evidence /
  stale / identity_unavailable 四态正确，UTC + `last_success_at` 基准；
- 证据-身份 hash 绑定：`symbol_evidence` / `symbol_status` / `batch_audit`
  均携带 `identity_snapshot_hash`；
- 预算：全局 monotonic deadline，超预算批次标记 unfinished 供 requeue；
- 修复：预算内一次，二次仍 invalid → failed，不启发式解析；
- prompt pack：4 片段有序编译、hash 稳定、审计 payload 完整；
- 不维护第二真源：latest 视图完全从追加日志重建。

## Residual risks

- `web_search` 真机参数形态：assigned to release gate canary；
- shared-state 写入 symlink 防护统一审视：assigned to aggregate deepreview；
- collector CLI/timer：tracked by S6。

## Conclusion

S3 review loop 通过；可创建 accepted slice commit。
