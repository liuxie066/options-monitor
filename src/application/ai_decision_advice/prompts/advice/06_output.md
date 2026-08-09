输出合同：

1. 只输出一个严格 JSON 值，符合调用方给出的 JSON Schema（
   ai_decision_advice.v1）；不输出任何解释性文字、Markdown 或代码围栏。
2. 顶层包含 schema、run_id、account_ref、market、input_bindings 和
   strategies。input_bindings 原样回传输入给出的四个 hash 与
   external_evidence_run_id。
3. 每个 strategy 包含 strategy_family（sell_put 或 covered_call）、status
   （completed）和 decisions。Covered Call 的每个 decision 用 scope_symbol
   标的标识；Sell Put 的 scope_symbol 为 null。
4. 每个 decision 包含 baseline_candidate_id、action、selected_candidate_id、
   rationale（risk_mechanism / candidate_effect / decision_reason）、
   internal_fact_refs 和 external_evidence_refs。
5. switch / defer 必须在 rationale 与 refs 中给出可核验的事实引用；keep
   引用支持维持判断的覆盖事实。
