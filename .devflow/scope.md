goal: 删除不可达的扫描全局 Feishu Holdings 风险读取、全局期权上下文及缓存分支，保留 CSP/Combo Put 账户级候选风险计算和排序
non_goals:
- 不改变账户富途、账本、CC、PM/Portfolio Exposure、独立 Feishu 读取命令或缺失证据判定
- 不修改真实配置、运行缓存、服务或外部数据
- 不提交、推送、建 PR、合并、发布或升级
scope: origin/main 基线上的本地源码、测试、当前文档及公共面退役登记
success_signals:
- 'S1: 直接扫描和预备运行不再读取 Feishu 作为全局扫描风险，不再读写两类 .global.json 缓存'
- 'S2: CSP/Combo Put 保留账户级持仓与期权风险字段及跨标的排序；CC 行为不变'
- 'S3: 测试、导入调用和现行操作者文档不再宣称全局扫描风险可用'
authorized_slices:
- slice: A-global-scan-retirement
  design_doc_ref: docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md#全局-holdings-扫描分支退役2026-09-30devflow-simple
  success_signal: S1,S2,S3
  depends_on: []
slice_checkpoints:
- slice: A-global-scan-retirement
  diff_fingerprint: sha256:5a3f712b7531a61ecfa7c7e35bb20747f4ece94ecd8e2e2badde59ab6770e5a5
  validation: 73 focused passed; full pytest 8002 passed, 3 skipped; Ruff, dependency graph, public surface, doc guardrails
    and diff check passed
  done: true
user_confirmation:
- 用 devflow 删除已不可达的全局 Feishu Holdings 风险读取及配套的全局期权上下文、缓存分支；保留仍用于候选数据和排序的账户级风险计算。
- simple（推荐）
prd_doc: not-applicable
prd_doc_ref: not-applicable
design_doc: docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md
design_ref: docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md sha256:f6e6444382cc9f8aef3f30c836c2e01c0e52c45f9247d3fdf60c79923bd641be
implementation_workspace: <workspace>/global-holdings-risk-retirement/options-monitor
review_base: origin/main@7f5e7e9467bc5318fc6c5ed1d61ba1c4b2d97c54
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: simple
node_sequence:
- Brainstorm
- Save Design
- Improve Design
- Impl
- Review
current_node: Review
internal_step: null
status: completed
next_action: Await a separately authorized delivery stage, if requested
approved_scope_ref: current user request
path_approval_ref: current user selection of simple
implementation_baseline:
  design_doc: docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md sha256:f6e6444382cc9f8aef3f30c836c2e01c0e52c45f9247d3fdf60c79923bd641be
  implementation_workspace: <workspace>/global-holdings-risk-retirement/options-monitor
  review_base: origin/main@7f5e7e9467bc5318fc6c5ed1d61ba1c4b2d97c54
  head: 7f5e7e9467bc5318fc6c5ed1d61ba1c4b2d97c54
  git_status: ' M docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md'
  staged: []
  unstaged:
  - path: docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md
    hash: f6e6444382cc9f8aef3f30c836c2e01c0e52c45f9247d3fdf60c79923bd641be
    size: 21228
  untracked: []
inventory:
- path: .devflow/scope.md
  status: modified
  hash: self-referential-control-file
  size: self-referential-control-file
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: CONFIGS.md
  status: modified
  hash: f61d989d1bdfbc7c755d8a8a8dba1d0f510cff49ace5a57ff07814cdb3e23e38
  size: 33648
  type: file
  mode: '100644'
  classification: required-correctness/safety
  evidence_ref: 'S3: current config contract must not claim scan-global Holdings'
- path: CONFIGURATION_GUIDE.md
  status: modified
  hash: 7fbc4bc884dd43d8dc9020df9eb1a335c01291d78e9bdc6db4df9992017af0f9
  size: 13388
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: README.md
  status: modified
  hash: 7d162bcac2f772c9155a762688aa95d0a57d4e2084e50a67198e8a5dec463178
  size: 20281
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: docs/AGENT_GETTING_STARTED.md
  status: modified
  hash: 964a6f93734e33f27f04024d059d5608116b77126e7cdc84cf7c3f612dc57896
  size: 4459
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: docs/DEPENDENCY_GRAPH.md
  status: modified
  hash: 698dbe581e6a4458862d39685825a1abd5638805f9c22d9e9a53858097aefc97
  size: 8918
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md
  status: modified
  hash: f6e6444382cc9f8aef3f30c836c2e01c0e52c45f9247d3fdf60c79923bd641be
  size: 21228
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: docs/INDEX.md
  status: modified
  hash: 80ed65bae0b61c6724ad7e318dbdae3baa7db10d9f143afb354d0ae13c5e52d1
  size: 8579
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: docs/STRATEGY_ARCHITECTURE.md
  status: modified
  hash: ae48591bea22616ff5a62a4fe8136a1ef678ce15bd4c5e8bbb578fe42b55d42b
  size: 15293
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: docs/public_surface_retirements.json
  status: modified
  hash: 6ee4d6b3e6264a94dc3772ca6efa88d22e39e2001db08956f47d4ef1fd669442
  size: 8217
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: src/application/pipeline_context.py
  status: modified
  hash: c53db4e58a86f633afb8f30570d6ca08aa124e01770073b2f84ffdab20181be9
  size: 19704
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: src/application/portfolio_context_builder.py
  status: modified
  hash: 606260db2cd81e52f05ada6e90a13625e0cf3762342c3fd1cb72a40f7d2e3c81
  size: 18209
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: src/application/portfolio_context_service.py
  status: modified
  hash: 08291a677ccabffcff65426c8e925780dcba71f5d7ef3b3771b11055d7404b11
  size: 4827
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: src/application/prepared_portfolio_context.py
  status: modified
  hash: 6a08baf395f0ef16ae10f757b87296ed02136a988a82f6a8809e800a64c1c2dd
  size: 38859
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: src/application/short_vol_risk_context.py
  status: modified
  hash: d5fe1ed8bf898de4f784afa55478edfde5209d8ac8c106236dde3cd82d15307e
  size: 9065
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: src/application/strategy_policy.py
  status: modified
  hash: 0f4806c2e10cea138a9680a96911b6200203d201abc59b0cce8fcb6b6ed603c9
  size: 18984
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: tests/test_pipeline_context_exchange_rates.py
  status: modified
  hash: 88241a39d68216da24bd4436e26c4b8e28d7460688e2229cbb637d604bc6535d
  size: 7129
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: tests/test_pipeline_context_shared_context.py
  status: modified
  hash: 741dc585110d47dfde87e8716a9f6dd1edc241980c4634d8d892243ab59e7ebf
  size: 33386
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: tests/test_prepared_portfolio_context.py
  status: modified
  hash: 28c46286ea3e4cfa5098f7a69da6ad1fa439df625ddc4c746639886881725eb8
  size: 27488
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: tests/test_sell_put_strategy_risk.py
  status: modified
  hash: 115bd0cd935b6907154eeb5c9c59817abafe18ec2368b5faf6595e80f5172154
  size: 13799
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
- path: tests/test_strategy_policy.py
  status: modified
  hash: 905a9343c748b7ffa49f6ed492632ec7a2f7ece281103e2005326aebebec8fcd
  size: 10749
  type: file
  mode: '100644'
  classification: planned
  evidence_ref: frozen design slice A
content_revision: sha256:f6e6444382cc9f8aef3f30c836c2e01c0e52c45f9247d3fdf60c79923bd641be
panel:
  original_design_sha256: 2ab3bcc4337838c648d604d5f95633a1d226c58a009352dd3156b10c2c23d60f
  reviewer_backend: native-subagent
  reviewer_model: unknown
  independence: unverified; cross-family reviewer unavailable on this account
  reports:
  - design_panel_a_fallback
  - design_panel_b_fallback
  - design_panel_c
  - design_panel_d
  accepted:
  - account option shared cache preservation
  - account-level CSP ranking fixture
  - actual public surface retirement records
  - current operator docs
  - narrow existing missing-option semantics and CC scope
  rejected: []
planreview_round: 0
deepreview_round: 1
in_flight: []
evidence_paths:
- full pytest exit 0 (8002 passed, 3 skipped)
- dependency graph --check cycles=0
- public surface and doc guardrails OK
- docs/reviews/code-review-20260930-112911.md (Review passed; no findings)
blocking_findings: []
residual_risks:
- item: Old runtime global cache files may remain
  classification: assigned-to-later-work-unit
  owner: target-environment operator
  destination: separately authorized inventory and cleanup if needed
- item: Missing option context can appear as zero short-put collateral
  classification: needs-new-issue-or-user-decision
  owner: CSP risk owner
  destination: separate evidence and behavior decision
