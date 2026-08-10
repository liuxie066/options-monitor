输出合同：

1. 只输出一个严格 JSON 值，符合调用方给出的 JSON Schema（
   ai_decision_advice.v1）；不输出任何解释性文字、Markdown 或代码围栏。
2. 顶层包含 schema、run_id、account_ref、market、input_bindings 和
   strategies。input_bindings 原样回传输入给出的五个语义 hash 与
   external_evidence_run_id：candidate_snapshot_hash、
   portfolio_distribution_hash、option_positions_hash、fact_registry_hash 和
   external_evidence_hash。
3. 每个 strategy 包含 strategy_family（sell_put 或 covered_call）、status
   （completed）和 decisions。Covered Call 的每个 decision 用 scope_symbol
   标的标识；Sell Put 的 scope_symbol 为 null。
4. 每个 decision 包含 baseline_candidate_id、action、selected_candidate_id、
   rationale（risk_mechanism / candidate_effect / decision_reason）、
   internal_fact_refs 和 external_evidence_refs。
5. internal_fact_refs 只能逐字引用 fact_registry 中的 candidate:、
   projection:、portfolio:、position:、coverage: 或 gap: ID。keep 至少
   引用 baseline candidate、它的 projection 和所有 coverage 事实。
6. external_evidence_refs 只能逐字引用 fact_registry 中形如
   evidence:xxxxxxxxxxxxxxxx 的证据 ID；不得编造、改写或推断 ID。
   没有可引用证据时输出空数组。
