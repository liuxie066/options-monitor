# Bot receipt market argument correction — Devflow scope

goal: 修复 Bot 把成交回执查询参数冲突误报为成交实际市场的问题。
non_goals:
  - 修改回执数据、成交入账、行情或 OpenD 监听行为
  - 扩大渠道授权或自动切换市场配置
  - 修复券商成交时间解释问题
  - 提交、推送、发布或升级运行环境
scope: Bot receipt_read 参数构造、对应提示词、隔离回归测试及现有 Bot 设计文档
success_signals:
  - S1: US 固定范围下模型误传 HK 时不读回执，错误说明只是参数冲突；省略 market 重试能读 US 回执。
  - S2: 显式跨范围工具参数仍被拒绝，不能自动切换配置或声称回执实际属于请求市场。
  - S3: 省略形式 deal_id 被拒绝，完整 ID 能用于精确查询。
  - S4: 提示词明确区分香港时间/券商地点与标的市场，模型不能把 SCOPE_DENIED 当作资源归属证据。
authorized_slices:
  - {slice: receipt-input-boundary, design_doc_ref: 'docs/BOT_DESIGN.md sha256:3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d', success_signal: [S1, S2, S3], depends_on: []}
  - {slice: receipt-model-guidance, design_doc_ref: 'docs/BOT_DESIGN.md sha256:3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d', success_signal: [S4], depends_on: [receipt-input-boundary]}
slice_checkpoints:
  - {slice: receipt-input-boundary, diff_fingerprint: 'git-diff sha256:7cda8154855c3c5cb428d5af05f6d55270cfbebc1747b39919a00ce770f81987', validation: '28 focused tests passed; git diff --check passed', done: true}
  - {slice: receipt-model-guidance, diff_fingerprint: 'git-diff sha256:360b74a76f278c92400b36e003020c32564d30a9c19b8e21f6206bf274619fb5', validation: '55 Bot/receipt tests plus 132 adjacent caller tests passed; guardrails, ruff and diff check passed', done: true}
user_confirmation:
  - '用户：[$devflow] 按建议优化实施'
  - '用户选择：simple（推荐）'
prd_doc: not-applicable
prd_doc_ref: not-applicable
design_doc: docs/BOT_DESIGN.md
design_ref: 'docs/BOT_DESIGN.md sha256:3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d'
implementation_workspace: /private/tmp/om-bot-receipt-scope
review_base: origin/main@5670eb384c8c7c521bfdcf0dfa79da2ade873bb3
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: simple
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: complete
status: completed
next_action: None within authorized Devflow scope; Delivery or live-model validation requires a separate instruction.
approved_scope_ref: '用户：[$devflow] 按建议优化实施'
path_approval_ref: '用户选择：simple（推荐）'
implementation_baseline:
  design_doc: 'docs/BOT_DESIGN.md sha256:3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d'
  implementation_workspace: /private/tmp/om-bot-receipt-scope
  review_base: origin/main@5670eb384c8c7c521bfdcf0dfa79da2ade873bb3
  head: 5670eb384c8c7c521bfdcf0dfa79da2ade873bb3
  git_status: ' M docs/BOT_DESIGN.md'
  staged: []
  unstaged:
    - {path: docs/BOT_DESIGN.md, hash: 3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d, size: 8577}
  untracked: []
inventory:
  - {path: .devflow/scope.md, status: M, hash: self-referential-tracker, size: self-referential-tracker, type: file, mode: '0o644', classification: planned, evidence_ref: Devflow scope contract}
  - {path: docs/BOT_DESIGN.md, status: M, hash: 3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d, size: 8577, type: file, mode: '0o644', classification: planned, evidence_ref: design_ref}
  - {path: docs/DEPENDENCY_GRAPH.md, status: M, hash: 579ea0f6a2334f7286bb86450a832b629c57d7ae15b8782b2730cac92a703af3, size: 8918, type: file, mode: '0o644', classification: required-correctness/safety, evidence_ref: pre-push dependency graph --check}
  - {path: src/application/bot/prompts/tool_rules.md, status: M, hash: f866c7c0823174baaecdaf7b6832461d32d52b6e48441f2a4a2056bb36f6fc7e, size: 2673, type: file, mode: '0o644', classification: planned, evidence_ref: receipt-model-guidance}
  - {path: src/application/bot/tools.py, status: M, hash: 960737d599b1d2edd60dc916d59a121a47723241ff16fa26dd5072eca3eef3df, size: 33617, type: file, mode: '0o644', classification: planned, evidence_ref: receipt-input-boundary}
  - {path: tests/test_bot_receipt_host.py, status: M, hash: 9ede3f99c1377e067cff097632d0fda3be764b2195ceb4065697c75a171daf8d, size: 11533, type: file, mode: '0o644', classification: planned, evidence_ref: receipt-input-boundary and receipt-model-guidance}
content_revision: 'docs/BOT_DESIGN.md sha256:3327f01112a84e29ea08084b69b42c64c1ebfcb4c24a19ed3135da41ae985b5d'
planreview_round: 1
deepreview_round: 1
in_flight: []
evidence_paths:
  - docs/BOT_DESIGN.md
  - docs/reviews/plan-review-20260924-115230.md
  - docs/reviews/code-review-20260924-115920.md (pass-with-risks)
  - tests/test_bot_receipt_host.py
  - tests/test_bot_receipt_scope.py
  - docs/DEPENDENCY_GRAPH.md (generated; --check passed)
panel_reviews:
  snapshot: 'docs/BOT_DESIGN.md sha256:6c30752a93e873bf207115eb2416ef721dcd12d3153f69f5f5aae5e3d0a96380'
  reviewer_backend: native-subagent
  reviewer_model: unknown
  independence: unverified
  usable_results: 4
  accepted:
    - 参数冲突不等于资源归属；区分工具参数级与用户意图级保证
    - 零读取断言、市场大小写等价
    - 遮盖 ID 时请求补全，精确查询不得退化为列表查询
  rejected: []
  deferred:
    - 'Host 级自然语言市场意图判定；owner: Bot scope design；destination: 后续获授权的设计切片'
blocking_findings: []
residual_risks:
  - {item: 真实模型是否按参数冲突提示重试尚未实测, classification: assigned-to-later-work-unit, owner: Bot 真实模型验收, destination: 后续只读渠道复验}
