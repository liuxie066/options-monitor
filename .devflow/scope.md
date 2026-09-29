goal: "修复 PR #387 F2 的全局 Holdings 观察时间误判与硬门"
non_goals:
  - "不建立新鲜度 SLA、PM 表 schema 或持仓逐行完整性契约"
  - "不修改生产配置、服务、Feishu 持仓或账本"
  - "不提交、推送、建 PR、合并、发布或升级"
scope: "本地 PR #387 复审修复工作树中的 F2；此前 F1/F3-F8 改动作为既有基线保留并随 Review 完整审查"
success_signals:
  - "S1: 全局 Holdings 观察时间未知时仍可计算风险，风险输出有可见 warning"
  - "S2: Feishu 编辑时间及旧缓存不能冒充可信持仓观察时间"
  - "S3: 错误来源/范围、空风险内容和未完成分页仍不可用"
authorized_slices:
  - {slice: "F2-observation-contract", design_doc_ref: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md#选定行为与失败语义", success_signal: "S1,S2,S3", depends_on: []}
slice_checkpoints:
  - {slice: "F2-observation-contract", diff_fingerprint: "sha256:84609eb17bce25d75739f4a3cd8d4f4d6d06fb6227145356114abc8597e735e8", validation: "9 initial F2 red, 2 missing-has-more red, 2 missing-items red; 93 focused green; rebased full suite 7988 passed, 3 skipped; Ruff, diff, dependency graph, guardrails green", done: true}
user_confirmation:
  - "用/devflow 的 full 模式完成方案实施（此前同一账户退役任务）"
  - "基于poc 的结果，上面这个问题要怎么修复？"
  - "/devflow 按你的方案实施"
prd_doc: "not-applicable"
prd_doc_ref: "not-applicable"
design_doc: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md"
design_ref: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md sha256:a0504cb223cde0e3f44563ec6bfaef82df834f302712187a9ae80557bfc5061a"
implementation_workspace: "<workspace>/pr-387-review-fixes/options-monitor"
review_base: "rebased patch base origin/main@1bb08e4f4ae720e8034c8018ccc281294475d3b4; local implementation baseline remains HEAD@ef75a799af4ea1e921a4a1be88cecb7a4a849b77; rebase conflicts limited to scope and generated dependency graph"
authorization_diffs:
  - {when: "2026-09-30", what: "Apply the recommended F2 observation-contract correction through Devflow", ref: "current user request"}
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: null
status: completed
next_action: "Handoff local design, implementation, tests, and review evidence; delivery and target-environment cutover require separate authorization"
approved_scope_ref: "current user request and preceding recommended F2 fix"
path_approval_ref: "prior user selection of Devflow full"
implementation_baseline:
  design_doc: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md sha256:a0504cb223cde0e3f44563ec6bfaef82df834f302712187a9ae80557bfc5061a"
  implementation_workspace: "<workspace>/pr-387-review-fixes/options-monitor"
  review_base: "HEAD@ef75a799af4ea1e921a4a1be88cecb7a4a849b77"
  head: "ef75a799af4ea1e921a4a1be88cecb7a4a849b77"
  git_status: |
    M CONFIGURATION_GUIDE.md
     M docs/DEPENDENCY_GRAPH.md
     M docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md
     M docs/LEDGER_ARCHITECTURE.md
     M src/application/account_config.py
     M src/application/agent_tool_config.py
     M src/application/cash_headroom_query.py
     M src/application/config_yaml_accounts.py
     M src/application/pipeline_context.py
     M src/application/portfolio_context_service.py
     M src/application/prepared_portfolio_context.py
     M src/infrastructure/feishu_bitable.py
     M src/interfaces/cli/main.py
     M tests/test_account_config.py
     M tests/test_agent_plugin_smoke.py
     M tests/test_cli_runtime_paths.py
     M tests/test_config_yaml.py
     M tests/test_feishu_bitable.py
     M tests/test_pipeline_context_shared_context.py
  staged: []
  unstaged:
    - {path: "CONFIGURATION_GUIDE.md", hash: "e270e6de7dbc78a357dbfc2d6691d3cdebb70f79952918edbe6cb4d113e8a408", size: 11307}
    - {path: "docs/DEPENDENCY_GRAPH.md", hash: "7a1a9fab523589bdccda367e754e6de593bcc9ce3ebddb9637fe10cfa552e86a", size: 8918}
    - {path: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md", hash: "a0504cb223cde0e3f44563ec6bfaef82df834f302712187a9ae80557bfc5061a", size: 14928}
    - {path: "docs/LEDGER_ARCHITECTURE.md", hash: "3e2ee09454215418e84550f9ca61806afdf6f5698cac7cc689665e461a695da6", size: 105394}
    - {path: "src/application/account_config.py", hash: "7bae539f02fd0700f42a6b326b8be6f2303ff3766b4a972b3c023c840fec022c", size: 24538}
    - {path: "src/application/agent_tool_config.py", hash: "f7507462b33bcd4777a92feb566965a10771e59b140b45fde24e55ddfa9ac5b5", size: 4878}
    - {path: "src/application/cash_headroom_query.py", hash: "98dd21a4a640069a90db54502d2653da78f023062460564e6a8d3b145fe4893e", size: 20415}
    - {path: "src/application/config_yaml_accounts.py", hash: "d5f59e1320f0ba0723cafe9301605348a49875ee5513307b3df00c3aa1d8bbeb", size: 16005}
    - {path: "src/application/pipeline_context.py", hash: "5c0700941c8fbb8b3f4ed72f475e36e3672eb0d2a23ac8c3e9fdd55191ce18c8", size: 25012}
    - {path: "src/application/portfolio_context_service.py", hash: "c3eaa2771751a9e6b636a329cbc7839ab071fee2a5fdca42b9ee73f26a145c47", size: 4920}
    - {path: "src/application/prepared_portfolio_context.py", hash: "21a07248d71aee5e8b4e49df5c0ae11b801204d1c02d2b81ca5602cd8b504040", size: 39988}
    - {path: "src/infrastructure/feishu_bitable.py", hash: "109bff3d380700ac5e981516306a44c94a43a39017f06bc6f71e3374ef7338f6", size: 16809}
    - {path: "src/interfaces/cli/main.py", hash: "bac9dae16acd994b9d88b86ff63271c4b53094b8360a1c92b46c598d70ea6a0e", size: 15145}
    - {path: "tests/test_account_config.py", hash: "19beb5c7bcc85b85c1cf027817a5db84a786fc2144527aa23023aacd2c8fafac", size: 10508}
    - {path: "tests/test_agent_plugin_smoke.py", hash: "8d2110681288787955233d49cd927ba036cc73a9a8b419cdf4a5811cc45d16eb", size: 209872}
    - {path: "tests/test_cli_runtime_paths.py", hash: "ebb965ab972572e185a22482b29f62e26338ad63c2a301189c0e7309aedcae1b", size: 4199}
    - {path: "tests/test_config_yaml.py", hash: "0d3f07ec7b6d91e6ce3adbadf1ecb2fff2a063ffa4567b11cebd44e093352cab", size: 73054}
    - {path: "tests/test_feishu_bitable.py", hash: "2d3fbf1ed0bd08292007bba1563defb483c711f257612cb057c38a454bb797bf", size: 12077}
    - {path: "tests/test_pipeline_context_shared_context.py", hash: "446b45268e17dfdcb01f0d409a699b67e6c6808394623373af3eb56b3e66af08", size: 38549}
  untracked: []
inventory:
  - {path: "CONFIGURATION_GUIDE.md", status: modified, hash: "e270e6de7dbc78a357dbfc2d6691d3cdebb70f79952918edbe6cb4d113e8a408", size: 11307, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "docs/DEPENDENCY_GRAPH.md", status: modified, hash: "17a88a24631d9ef34a125aeb0e3d92f457a2046cba88136ea4bc705075aaa918", size: 8918, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md", status: modified, hash: "a0504cb223cde0e3f44563ec6bfaef82df834f302712187a9ae80557bfc5061a", size: 14928, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "docs/LEDGER_ARCHITECTURE.md", status: modified, hash: "3e2ee09454215418e84550f9ca61806afdf6f5698cac7cc689665e461a695da6", size: 105394, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "docs/public_surface_retirements.json", status: modified, hash: "1a359844166909a4880a4c371104df19a49d7da851676ea402e13999f7c86720", size: 6885, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/account_config.py", status: modified, hash: "7bae539f02fd0700f42a6b326b8be6f2303ff3766b4a972b3c023c840fec022c", size: 24538, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/agent_tool_config.py", status: modified, hash: "f7507462b33bcd4777a92feb566965a10771e59b140b45fde24e55ddfa9ac5b5", size: 4878, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/cash_headroom_query.py", status: modified, hash: "98dd21a4a640069a90db54502d2653da78f023062460564e6a8d3b145fe4893e", size: 20415, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/config_yaml_accounts.py", status: modified, hash: "d5f59e1320f0ba0723cafe9301605348a49875ee5513307b3df00c3aa1d8bbeb", size: 16005, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/pipeline_context.py", status: modified, hash: "4f90cbb5ec172d39bb66826ee057ad44116defd7eb0f810a79c4d51caabe92df", size: 25012, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/portfolio_context_builder.py", status: modified, hash: "46be42341b14959895f738ca88a5e4ac5ae2182f25eb7971134835c017928136", size: 19758, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/portfolio_context_service.py", status: modified, hash: "c3eaa2771751a9e6b636a329cbc7839ab071fee2a5fdca42b9ee73f26a145c47", size: 4920, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/prepared_portfolio_context.py", status: modified, hash: "4f2c6a5cd6fe2213dc16addcc6d43f74fbdde4905e92b225fff05150b09994d2", size: 39984, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/application/short_vol_risk_context.py", status: modified, hash: "231b81ed5875c21f9aecd903801ed7771d2a37f6422cc61a44fbeb1602bc94d3", size: 10083, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/infrastructure/feishu_bitable.py", status: modified, hash: "90c48626c1be2da942458b247d655619cffd689b6b471752922a916f8331d1de", size: 17665, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "src/interfaces/cli/main.py", status: modified, hash: "bac9dae16acd994b9d88b86ff63271c4b53094b8360a1c92b46c598d70ea6a0e", size: 15145, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_account_config.py", status: modified, hash: "19beb5c7bcc85b85c1cf027817a5db84a786fc2144527aa23023aacd2c8fafac", size: 10508, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_agent_plugin_smoke.py", status: modified, hash: "8d2110681288787955233d49cd927ba036cc73a9a8b419cdf4a5811cc45d16eb", size: 209872, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_cli_runtime_paths.py", status: modified, hash: "ebb965ab972572e185a22482b29f62e26338ad63c2a301189c0e7309aedcae1b", size: 4199, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_config_yaml.py", status: modified, hash: "0d3f07ec7b6d91e6ce3adbadf1ecb2fff2a063ffa4567b11cebd44e093352cab", size: 73054, type: file, mode: "100644", classification: "preexisting-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_feishu_bitable.py", status: modified, hash: "78d9837957b3c59128db3cbd6016094a43499cc6caacc9f822be4c3b0f1f15a0", size: 12499, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_fetch_portfolio_context_richtext.py", status: modified, hash: "50f5cb26175719f627d184c8b236e92f146229c2ab7e33471d4c64ec0455d2a7", size: 6684, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_pipeline_context_shared_context.py", status: modified, hash: "1f3dd4df65be0e63da672db2415c3ac26d56fd1bb4cd7b11a6b75cc377149872", size: 39997, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_prepared_portfolio_context.py", status: modified, hash: "4ad23b5d9954aca427661656f6ba3a22d16d5af9e8504367e6d32d9262c903a0", size: 29940, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
  - {path: "tests/test_sell_put_strategy_risk.py", status: modified, hash: "b766eb867d4066278e97def6a461b9c7e46b7b37cdc712c3b64bd5ede31575c2", size: 16382, type: file, mode: "100644", classification: "F2-or-shared-review-fix", evidence_ref: "git diff HEAD"}
content_revision: "sha256:a0504cb223cde0e3f44563ec6bfaef82df834f302712187a9ae80557bfc5061a"
panel:
  original_design_sha256: "2c825323619a4e783356ee00229da1ca39e838c2772d1cf21fc68f6eaf7d3c95"
  reports: [f2_design_panel_a, f2_design_panel_b, f2_design_panel_c, f2_design_panel_d]
  accepted: ["old-cache false trusted must refetch", "unknown must be visible in risk warning", "pagination must terminate", "clarify syntactic scope and retrieval-time meaning"]
  deferred: ["row-level holdings completeness requires separate source contract and target data sample"]
planreview_round: 1
deepreview_round: 1
in_flight: []
evidence_paths:
  - "docs/reviews/plan-review-20260930-005117.md"
  - "docs/reviews/code-review-20260930-011119.md"
  - "Feishu metadata POC: 65/65 last_modified_time; 0/65 explicit observation fields; no remote writes"
blocking_findings: []
residual_risks:
  - {item: "Holdings invalid or unsupported asset rows may be skipped", classification: "needs-new-issue-or-user-decision", owner: "Holdings projection and portfolio-risk owners", destination: "separate observed-data contract and environment sample"}
  - {item: "Unfiltered Feishu query does not prove expected account coverage", classification: "assigned-to-later-work-unit", owner: "target-environment migration operator", destination: "independent account inventory at authorized cutover"}
  - {item: "origin/main advanced after implementation baseline", classification: "assigned-to-later-work-unit", owner: "delivery owner", destination: "reconcile exact outgoing patch against current main and rerun affected gates after delivery authorization"}
