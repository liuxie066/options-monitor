# 验证证据
工作树 /private/tmp/om-auto-intake-t1-t7；base 248d9c9334274fa70d02383850c028d81c136120；主仓库 .venv/bin/python；工作树根执行，无 PYTHONPATH 前缀。所有持久化均 isolated tmp_path，provider/receipt 均测试替身。

| Task | 新增回归 |
|---|---|
| T1 | test_push_enqueue_failure_is_contained_and_next_push_survives，key/enqueue/audit/status/reporting 5种故障，真实Inbox与status readback、重复payload不增行 |
| T2 | test_intake_preparation_failure_keeps_batch_processing_and_retries，checkpoint/gateway 2种故障、双行顺序、pending seal重试、原status与持久降级诊断；既有 disabled settlement 用例要求继续process |
| T3 | test_reconcile_bad_row_does_not_block_good_rows_or_complete_its_action，normalize/proof异常、坏行首尾、独立action完成、state保留与幂等readback |
| T4 | test_execution_file_selector_rejection_is_row_local，无匹配/多匹配；test_deal_json_account_conflict_returns_clean_rejection；既有saved Inbox异常合同改为return2 |
| T5 | test_empty_pending_state_skips_evidence_reads_and_preserves_schema，preview/apply零read；test_empty_pending_explicit_deal_request_keeps_noop_action |
| T6 | test_inbox_fee_state_load_is_lazy，applied/归一化applied/untrusted/trusted failed 4边界 |
| T7 | test_deal_json_write_contract_requires_applied_result，6种mode/status |

切片 A: 11 passed；B: 7 passed；C: 14 passed（含既有相关用例）。
首次整体测试 445 passed、1 failed，失败为旧测试要求checkpoint失败跳过process，与T2任务相反；已更新断言，保留禁止gateway/timing，并添加status readback。单测修订后1 passed。
Ruff touched Python: exit0。guardrails --check-doc-wording --check-runtime-config-tracking --check-sensitive-artifacts: exit0。
Dependency generator: generated production_modules=629 cycles=0；--check exit0。唯一生成差异是测试导入边+7，production edges=3196不变。
.git diff --check: exit0。无冻结核心文件变更、无生产写入、无提交。

最终完整相关测试族：446 passed in 41.46s，exit0。原始日志 /private/tmp/om-final-tests.log。
