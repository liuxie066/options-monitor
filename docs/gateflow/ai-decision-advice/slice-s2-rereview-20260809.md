# Gateflow Review Artifact — S2 Code Review

- Gate: `code review` + `re-review`（slice S2）
- Work unit: `ai-decision-advice`
- Slice: S2 观察集合与身份快照

## Review scope

`src/application/ai_decision_advice/identity.py` 与测试；对照设计文档 5、5.2、
6.1（优先级与饥饿保护）与 plan S2 验收点。

## Findings（review → fix → re-review）

| # | Finding | 状态 |
|---|---|---|
| DR-S2-01 | market snapshot / basicinfo provider 可能返回 OpenD 代码形态（`US.NVDA`）或请求集合外的行，身份事实被污染 | 已修复：只接受 canonical 后属于观察集合的行；新增 `test_identity_ignores_rows_outside_requested_set` 与 OpenD 代码形态断言 |
| DR-S2-02 | OpenD 调用未被 5 分钟预算包裹 | rejected-with-reason：预算归属 collector 编排（S3）；identity 只做解析与快照，符合 plan 分层 |
| DR-S2-03 | 快照原子写使用 tmp+replace，未走 `tick_run_workspace` no-follow 链 | deferred-with-owner：shared state 目录与 run 目录安全模型不同；shared JSONL/JSON 写入统一在 S3 evidence_store 一起审视 symlink 防护（owner: S3） |

## 复查确认

- 观察集合：canonical 去重、四来源优先级、alias/空值丢弃，测试覆盖；
- 身份快照：schema + 内容 hash + 原子重写幂等 + 加载容错（缺失/坏 JSON/
  错 schema → None）；
- 队列：优先级 tier + tier 内最久未尝试 + requeue 到 tier 队首；
- 无 domain/src 反向依赖；不引入新数据库；不复用 Copilot 任何组件。

## Residual risks

- shared-state 写入的 symlink/no-follow 防护统一审视——tracked by S3
  （evidence_store + identity publish 同一目录）；
- 真实 OpenD 行格式映射——covered by S3 adapter。

## Conclusion

S2 review loop 通过；可创建 accepted slice commit。
