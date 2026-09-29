goal: "修复富途期权被指派时股票成交与零价期权腿乱序匹配、同合约多 lot 漏接及旧案例误匹配，并提供受控历史 skipped 恢复"
non_goals:
  - "不把价内状态或人工通知文本当作实际指派证明"
  - "不新建提前指派候选池表、平行账本或通用重放框架"
  - "不改写生产账本、不发送真实通知、不停止服务、不提交或交付源码、不发布或升级"
scope: "本地 trades lifecycle/resolver/intake/Inbox、既有 ledger writer 合同、相关 CLI 受控恢复及测试和 canonical 设计文档"
success_signals:
  - "S1: 77.5P 三个 500 股 lot 匹配一笔 1500 股成交，75P 既有结果保持幂等"
  - "S2: 当日 80P 不被三月同价旧终态争抢，成交时间与 writer 冻结截止一致"
  - "S3: 到期与提前指派均支持股票先到和期权先到，不以价内为硬门槛；唯一证据后才产生一次经济效果与 Outbox"
  - "S4: 普通已指派股票卖出与期权交收双候选待核实，不因分支顺序误消费"
  - "S5: 已 handled/skipped/not_option_deal 的精确 broker 成交可预览、审计和安全恢复，重复及崩溃重试不重复记账或通知"
authorized_slices:
  - {slice: "matching", design_doc_ref: "docs/FUTU_TRADE_HOLDINGS_SYNC.md#股票成交先到与提前指派配对2026-09-29-设计", success_signal: "S1,S2,S3", depends_on: []}
  - {slice: "stock-ownership", design_doc_ref: "docs/FUTU_TRADE_HOLDINGS_SYNC.md#股票成交先到与提前指派配对2026-09-29-设计", success_signal: "S4", depends_on: ["matching"]}
  - {slice: "skipped-recovery", design_doc_ref: "docs/FUTU_TRADE_HOLDINGS_SYNC.md#股票成交先到与提前指派配对2026-09-29-设计", success_signal: "S5", depends_on: ["matching", "stock-ownership"]}
slice_checkpoints:
  - {slice: "matching", diff_fingerprint: "170bcfe11dc4c0bcd348ffbef5aaa24d973ec62a31d5352f9a512952d7d7b3ef", validation: "historical checkpoint 259 passed; final 9-file 440 passed covers competing sources, partial settlement and idempotent replay", done: true}
  - {slice: "stock-ownership", diff_fingerprint: "d5c8d9402fffd47fa08b7961760315eaa0b77bcaaf1a8b973d75ee1f89bab500", validation: "historical checkpoint 259 passed; final 9-file 440 passed covers transaction-time dual candidate exclusion", done: true}
  - {slice: "skipped-recovery", diff_fingerprint: "a73a00dc704de95e825269251c54e69b6fca8b047bdd6c3cf7b31bccb29d7491", validation: "historical checkpoint 259 passed; final 9-file 440 passed covers ordinary claim/resume isolation", done: true}
user_confirmation:
  - "用devflow 修复问题，先定位问题的原因，再确定验收方案，最后实现方案"
  - "先读整个期权交易处理+股票交易处理的代码，看看两者是不是有冲突和逻辑矛盾"
  - "好的，认可你的方案"
  - "选 full"
prd_doc: "not-applicable"
prd_doc_ref: "not-applicable"
design_doc: "docs/FUTU_TRADE_HOLDINGS_SYNC.md"
design_ref: "docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:c2a4ebf33cceed1a4653b3725849b07cb55192730fbeb233c3668a85c82b9590"
implementation_workspace: "<task-worktree>/options-monitor"
review_base: "origin/main@975351a4e50781aa5dd3483d3805e4270705e839"
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: Astra findings repaired and second Deepreview complete
status: completed
next_action: "本地修复和 440 项相关测试已完成；远端生产恢复须重新核证并单独授权"
approved_scope_ref: "本对话用户消息：用devflow 修复问题；好的，认可你的方案"
path_approval_ref: "本对话用户消息：选 full"
implementation_baseline:
  design_doc: "docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:cbf667f0d33499370a21eb4ba2b45a3b4cb6840886a20e23761e2917d1daa43d"
  implementation_workspace: "<task-worktree>/options-monitor"
  review_base: "origin/main@975351a4e50781aa5dd3483d3805e4270705e839"
  head: "975351a4e50781aa5dd3483d3805e4270705e839"
  git_status: " M .devflow/scope.md; M docs/FUTU_TRADE_HOLDINGS_SYNC.md"
  staged: []
  unstaged:
    - {path: ".devflow/scope.md", sha256: "3018fb56dcc280ebadd048916c7041459df3cd0022d5b054a5f2f07bf26b4fd9", size: 3989}
    - {path: "docs/FUTU_TRADE_HOLDINGS_SYNC.md", sha256: "cbf667f0d33499370a21eb4ba2b45a3b4cb6840886a20e23761e2917d1daa43d", size: 143588}
  untracked: []
inventory:
  - {path: ".devflow/scope.md", status: "M", hash: "self-referential", size: 0, type: "scope", mode: "100644", classification: "planned", evidence_ref: "process contract; self-hash cannot be embedded"}
  - {path: "docs/FUTU_TRADE_HOLDINGS_SYNC.md", status: "M", hash: "c2a4ebf33cceed1a4653b3725849b07cb55192730fbeb233c3668a85c82b9590", size: 144428, type: "design", mode: "100644", classification: "planned", evidence_ref: "design review and remote readback"}
  - {path: "src/application/ledger/commands.py", status: "M", hash: "a0069e33f0f3e627a00fe5d6e005e4b6becdfa2ac9fb647120fa513ec23d9226", size: 89624, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/application/ledger/writer_lifecycle_allocation.py", status: "M", hash: "e3b006d433b2ccfa923d4d905b0b2be6a4d0641e9e1e013d63d6fd980d091f80", size: 35544, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff; Deepreview resolved P1"}
  - {path: "src/application/ledger/writer_lifecycle_evidence.py", status: "M", hash: "499f07df06d501017f24e445d23eabd6204fd1e52734b60c9c529612b995818d", size: 74830, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff; Deepreview resolved P1"}
  - {path: "src/application/positions/workflows.py", status: "M", hash: "885c081132bac6b6a14a650e3743b79889aff24d3cd93ef4301c47719e082683", size: 56081, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/application/trades/auto_intake.py", status: "M", hash: "dc6bb78bf5c86e1659478530d16c858a19537e359682b39a204b330e2c3d82e6", size: 170213, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/application/trades/inbox.py", status: "M", hash: "7d182b62e68c7d217f2efaa8cf2dd414f36162f84e07044d7c9c102a0236ef26", size: 170148, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/application/trades/lifecycle.py", status: "M", hash: "a4716c2409562a0feb8812d0d33330ba3836ac64598aa3f6e84586072ee80a32", size: 72909, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/application/trades/lifecycle_reconciliation.py", status: "M", hash: "c1b603a87204c8a9686fdf1dd91455e3c3aca24b4c15cf59bcf20cbb589617d7", size: 60099, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/application/trades/resolver.py", status: "M", hash: "1bb131a8b3776add3edec7f6912e92d77735a538806d6d0570ee6b4e0deda41e", size: 38806, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "src/interfaces/cli/run_ops.py", status: "M", hash: "af5080b644ae3eefe22d60c98a7d737745a2189adf166c948680bbbf8fd491e1", size: 11747, type: "source", mode: "100644", classification: "planned", evidence_ref: "259 passed; Ruff"}
  - {path: "tests/test_assigned_stock_sale_intake.py", status: "M", hash: "da22b856cc86ff94fd0ed19c8f6908a61b3ab8c50fa0a80a5a462d6d5a91b83c", size: 52062, type: "test", mode: "100644", classification: "planned", evidence_ref: "structured-source red-before-green; 259 passed"}
  - {path: "tests/test_trades_auto_intake_cli.py", status: "M", hash: "f1b4cae838258b7bff6c7e32e56ffc048b0db507a75af21c8aa03c3971fb351c", size: 84242, type: "test", mode: "100644", classification: "planned", evidence_ref: "259 passed"}
  - {path: "tests/test_trades_resolver_close.py", status: "M", hash: "fd2f009c4362474d8bf912b22f6b331e6e41736ab1fdd31b63f2ff9816332a2e", size: 84602, type: "test", mode: "100644", classification: "planned", evidence_ref: "259 passed"}
content_revision: "sha256:c2a4ebf33cceed1a4653b3725849b07cb55192730fbeb233c3668a85c82b9590"
planreview_round: 2
deepreview_round: 2
in_flight: []
evidence_paths:
  - "docs/reviews/plan-review-20260929-215140.md"
  - "docs/reviews/plan-review-20260929-215443.md"
  - "docs/reviews/code-review-20260929-232446.md"
  - "docs/reviews/code-review-20260929-233845.md"
  - "docs/reviews/code-review-20260930-003122.md"
  - "Improve Design Panel: four native-subagent fallback reviews of initial sha256:34197151e920171dfa6511bd15346a2c81cf967cf523d2dd69899a53c81b527c; reviewer_model unknown; cross-family independence unverified"
blocking_findings: []
resolved_findings:
  - {item: "结构化 Futu 成交在 stock-sale 与 lifecycle writer 的排他检查中使用不同 source key", severity: "P1", evidence: "新增反例先红后绿；docs/reviews/code-review-20260929-232446.md", resolution: "两种 writer 共用 futu_compatibility_source_key"}
  - {item: "两个股票来源竞争时股票重试仍可落账", severity: "P1", evidence: "docs/reviews/code-review-20260929-233845.md；Inbox 重试和直接 writer 回归先红后绿", resolution: "事务快照核对未消费的股票来源，已消费来源不妨碍部分交收和幂等读回"}
  - {item: "期权后到未重查已指派股票卖出候选", severity: "P1", evidence: "docs/reviews/code-review-20260929-233845.md；股票先到后补库存的反例先红后绿", resolution: "最终 ledger 事务调用 trades 所有权校验，双候选拒绝落账"}
  - {item: "普通重放领取恢复专属 pending Inbox", severity: "P2", evidence: "docs/reviews/code-review-20260929-233845.md；push/backfill/CLI 回归先红后绿", resolution: "claim 和普通 resume 依据恢复标记隔离，只允许显式恢复领取"}
residual_risks:
  - {item: "富途结构化股票成交缺少已验证的指派因果标记", classification: "needs-new-issue-or-user-decision", owner: "交易录入操作者", destination: "双归属及不完整来源保持人工核实"}
  - {item: "目标远端恢复需重新核证并获生产写入授权", classification: "assigned-to-later-work-unit", owner: "目标环境操作者", destination: "本地研发通过后单独安排受控远端恢复"}
  - {item: "旧手工指派 lot 无物理账户证据，既有股票卖出路径仍按内部账户处理", classification: "needs-new-issue-or-user-decision", owner: "账本/交易录入负责人", destination: "增加手工指派物理账户来源或单独复核旧 lot"}
  - {item: "本地缓存 origin/main 已前进至 569c2fe6，远端 DNS 暂不可用，包含同一设计文档的改动", classification: "assigned-to-later-work-unit", owner: "交付操作者", destination: "交付授权时检查最新远端 main、设计文档重叠与本次完整 diff"}
