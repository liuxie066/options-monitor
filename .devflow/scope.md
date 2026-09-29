goal: "修复已确认期权腿平仓但原因待定时账户级风险占用仍按旧合约数计入的问题"
non_goals:
  - "不推断指派原因，不写生产账本或券商数据"
  - "不提交、推送、建 PR、合并、发布或升级"
  - "不把无账户生命周期快照的全账户汇总用于本次决策"
scope: "带可信快照的账户级期权上下文 Put 现金担保和 Call 锁股、下游日报资金读取；复用既有生命周期模型"
success_signals:
  - "S1: 已接受完整平仓的 Put/Call 不再占用账户风险容量"
  - "S2: 部分平仓仅保留实际未平仓比例；无可信事实或冲突不推断释放"
  - "S3: 日报读取修正后的账户汇总；原始账本持仓和原因状态不改"
authorized_slices:
  - {slice: "account-risk-capacity", design_doc_ref: "docs/FUTU_TRADE_HOLDINGS_SYNC.md#已确认期权平仓的风险占用设计", success_signal: "S1,S2,S3", depends_on: []}
slice_checkpoints:
  - {slice: "account-risk-capacity", diff_fingerprint: "sha256:f0e4e59b055319d7b16d209e75a2a01fe9b50652168d5478bdfaa33d1b4dfbab", validation: "3 red before fix; 3 green after fix; 143 affected tests passed; ruff and diff check passed", done: true}
user_confirmation:
  - "用 devflow 修这个 bug"
  - "完整链路（推荐）"
prd_doc: "not-applicable"
prd_doc_ref: "not-applicable"
design_doc: "docs/FUTU_TRADE_HOLDINGS_SYNC.md"
design_ref: "docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:2d7069739945f615212754c888338ea116aeace15ba7df7a0b3ab57bc8523315"
implementation_workspace: "<workspace>/brief-closed-put-collateral/options-monitor"
review_base: "HEAD@975351a4e50781aa5dd3483d3805e4270705e839; origin/main@b199beab282a0b1460e769213703945dec1c2e6c, ahead by 3 commits, overlap only .devflow/scope.md workflow metadata"
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: null
status: completed
next_action: "研发已完成；如需提交推送或生产升级，分别另行授权"
approved_scope_ref: "本对话用户消息: 用 devflow 修这个 bug"
path_approval_ref: "本对话用户回复: 完整链路（推荐）"
implementation_baseline:
  design_doc: "docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:2d7069739945f615212754c888338ea116aeace15ba7df7a0b3ab57bc8523315"
  implementation_workspace: "<workspace>/brief-closed-put-collateral/options-monitor"
  review_base: "HEAD@975351a4e50781aa5dd3483d3805e4270705e839"
  head: "975351a4e50781aa5dd3483d3805e4270705e839"
  git_status: |
     M .devflow/scope.md
     M docs/FUTU_TRADE_HOLDINGS_SYNC.md
     M src/application/positions/context_builder.py
     M tests/test_lifecycle_redesign_contracts.py
     M tests/test_positions_context_builder_partial_close.py
  staged: []
  unstaged:
    - {path: ".devflow/scope.md", hash: "58ade5c6297b0b7d94ac1158868400fe237c6b670d97f758ec36fd7797c4f697", size: 3549}
    - {path: "docs/FUTU_TRADE_HOLDINGS_SYNC.md", hash: "2d7069739945f615212754c888338ea116aeace15ba7df7a0b3ab57bc8523315", size: 137081}
    - {path: "src/application/positions/context_builder.py", hash: "61dabf8c19e063027b601918c34391f0f6cf190521cfc06f5858490c253901b7", size: 29671}
    - {path: "tests/test_lifecycle_redesign_contracts.py", hash: "07426db27c7ba1ebc5780f7e64637f3d3711e4c3b5d3b4f12f0f8f3114aeeccc", size: 68757}
    - {path: "tests/test_positions_context_builder_partial_close.py", hash: "62b7e768feef4cccfd3ec515c07e731694b20b0e8ecc9f0ef8ed14e17a1fb2aa", size: 25586}
  untracked: []
inventory:
  - {path: ".devflow/scope.md", status: " M", hash: "sha256:self-reference", size: 4877, type: file, mode: "0644", classification: planned, evidence_ref: "scope contract and slice checkpoint"}
  - {path: "docs/FUTU_TRADE_HOLDINGS_SYNC.md", status: " M", hash: "sha256:2d7069739945f615212754c888338ea116aeace15ba7df7a0b3ab57bc8523315", size: 137081, type: file, mode: "0644", classification: planned, evidence_ref: "design_ref and doc hygiene"}
  - {path: "src/application/positions/context_builder.py", status: " M", hash: "sha256:3bc07723293d75b91c0ccffd8673701bb7fc312e4126bf1dcefd45df2d3e6cc9", size: 30552, type: file, mode: "0644", classification: planned, evidence_ref: "143 passed and ruff"}
  - {path: "tests/test_lifecycle_redesign_contracts.py", status: " M", hash: "sha256:ade969483128ca62ba68563eb6a03e6294e19523db9f449af0b042dab2dfa340", size: 69092, type: file, mode: "0644", classification: planned, evidence_ref: "trusted snapshot to Daily Brief"}
  - {path: "tests/test_positions_context_builder_partial_close.py", status: " M", hash: "sha256:780a634fe2bc2946a72fd732b932d7fc5f06296357277c79d033c4b4b2846fe9", size: 27965, type: file, mode: "0644", classification: planned, evidence_ref: "3 red and 3 green targeted tests"}
content_revision: "sha256:2d7069739945f615212754c888338ea116aeace15ba7df7a0b3ab57bc8523315"
panel:
  reviewer_backend: native-subagent
  reviewer_model: unknown
  independence: unverified
  original_design_sha256: "476abc8b666e2336ff122ff1f2a65e787464d971c855a834a8a7a3c2cd714208"
  reports: [panel_a, panel_b, panel_c, panel_d]
  accepted:
    - "所有 reviewer: 重叠 case 的 conflict 不覆盖 closure_fact；增加状态门与反例"
    - "所有 reviewer: 限定只保证有可信快照的日报；直接现金和 Wheel 入口列后续风险"
    - "所有 reviewer: 绑定快照与持仓行代次；不一致保守计入"
    - "panel_c: Put/Call 统一有效数量，避免现有草稿只修 Put"
  rejected_with_reason:
    - "直接现金/Wheel 入口本轮接线：用户报错入口为日报，扩大到其它入口需单独范围和入口验证"
planreview_round: 1
deepreview_round: 1
in_flight: []
evidence_paths:
  - "docs/FUTU_TRADE_HOLDINGS_SYNC.md#已确认期权平仓的风险占用设计"
  - "docs/reviews/plan-review-20260929-225815.md"
  - "docs/reviews/code-review-20260929-230518.md"
  - "pytest: 143 passed in 2.70s"
blocking_findings: []
residual_risks:
  - {item: "全账户汇总缺账户生命周期快照", classification: "needs-new-issue-or-user-decision", owner: "期权上下文 owner", destination: "后续单独设计可信账户隔离聚合"}
  - {item: "直接现金查询及 Wheel 容量入口未传可信账户快照", classification: "needs-new-issue-or-user-decision", owner: "现金查询和 Wheel 容量 owner", destination: "后续单独接入可信账户快照并验收"}
  - {item: "账本快照与独立券商现金/正股快照可能存在交收时序差", classification: "needs-new-issue-or-user-decision", owner: "日报资金和组合上下文 owner", destination: "若需可下单额度，另行设计跨来源交收证据和券商容量验收"}
