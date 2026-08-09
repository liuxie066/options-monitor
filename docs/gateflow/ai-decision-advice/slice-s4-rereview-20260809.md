# Gateflow Review Artifact — S4 Code Review

- Gate: `code review` + `re-review`（slice S4）
- Work unit: `ai-decision-advice`
- Slice: S4 冻结输入与一张合约风险投影

## Review scope

`contexts.py` / `projection.py` 与测试；对照设计文档 7、8、18（隐私）与
plan S4 验收点。

## Findings（review → fix → re-review）

| # | Finding | 状态 |
|---|---|---|
| DR-S4-01 | projection 初版用恒定 `total_shares=1.0` 伪造 after-trade 权重 | 已修复：改为只输出可确定计算的事实（当前集中度 + 边际合约事实），`after_one_contract_weight` 字段删除；设计文档第 8 节同步；新增 `test_projection_no_fabricated_after_weight` |
| DR-S4-02 | `freeze_candidates` 可能泄露 candidate facts 中未列出的隐私字段 | rejected-with-reason：投影使用显式字段白名单（candidate_id/rank/symbol/strike/expiry/multiplier/dte/delta/period_net_return/annualized_gate/net_premium），不整体复制 facts |
| DR-S4-03 | `shared_expiry_with_other_position` 语义粗糙 | accepted，风险记录：同标的多个到期即 true；在 S5 prompt 中引导模型引用 `expiry_overlap_count`，该布尔仅作辅助——covered by S5 |

## 复查确认

- 四类输入 hash 全部稳定 64 位；`input_bindings` 与设计文档第 10 节字段一致；
- 隐私：无 NAV/成本/账户标签/订单号/文件路径；测试断言覆盖；
- 只消费已接受候选（`ranked_candidates`），不恢复拒绝候选；
- evidence 缺失标的显示 `no_evidence` 而非静默省略。

## Residual risks

- `shared_expiry_with_other_position` 语义——tracked by S5 prompt 约束；
- evidence_run_id 在真实编排中的传递——covered by S6。

## Conclusion

S4 review loop 通过；可创建 accepted slice commit。
