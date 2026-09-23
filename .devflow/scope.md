# Close Advice 剩余收益止盈 — Devflow scope

goal: 按已讨论的简化策略优化 Close Advice，使提前买回建议由净兑现比例和剩余最高年化决定。
non_goals: 不因指派风险建议平仓；不扫描替代仓、换仓、roll 或自动下单；不修改生产配置、报告、账本或服务；不提交、推送、发布或部署。
scope: active short Put/Call 的领域判断、版本化报告、只读消费和 Daily Brief 提示；保留 sealed quote/fee fail-closed。
success_signals:
  - S1: 每个数据完整且适用的 short lot 有可解释的 close/hold 行，80%/10% 边界按全成本正确计算。
  - S2: 缺失/非法数据 not_evaluable，旧策略报告不得成为新策略的 close 通知。
  - S3: Put/Call 分母和文案准确，接受指派的前提不被误写为风险触发器。
  - S4: 相关领域、runner、reader、Daily Brief 和文档检查通过；历史回放能做则报告结果，不能做则报告缺口。
authorized_slices:
  - {slice: domain_policy, design_doc_ref: 'docs/CLOSE_ADVICE_CONTRACT.md sha256:a6ed802823c36663cb74664753f3cb64a8a2237eb302c83512bf7abe41a80693', success_signal: [S1, S2, S3], depends_on: []}
  - {slice: report_consumers, design_doc_ref: 'docs/CLOSE_ADVICE_CONTRACT.md sha256:a6ed802823c36663cb74664753f3cb64a8a2237eb302c83512bf7abe41a80693', success_signal: [S2, S3, S4], depends_on: [domain_policy]}
slice_checkpoints:
  - {slice: domain_policy, diff_fingerprint: 4b5799da7ac2009d757df4f32413a7c097852729d28eec82324841009c8e79c7, validation: 'domain checkpoint: 14 passed; final integrated: 304 passed', done: true}
  - {slice: report_consumers, diff_fingerprint: 4bf6c9511d21a789ced76b8826c288841f86722bc02063f593e85e55d4473f66, validation: '304 targeted passed; 4 sandbox-constrained Feishu tests passed with local port permission; Ruff and guardrails passed', done: true}
user_confirmation:
  - 用户要求“/devflow 按这个策略优化close advice”；最近一版策略为 OTM、已兑现至少80%、剩余最高年化至多10%，并沿用接受指派前提。
  - 用户选择“简单流程”。
prd_doc: not-applicable
prd_doc_ref: not-applicable
design_doc: docs/CLOSE_ADVICE_CONTRACT.md
design_ref: docs/CLOSE_ADVICE_CONTRACT.md sha256:9200c68d10a3399139022502f37a087e7e166f9f1f4515423e38c3c4b6f3a815
implementation_workspace: /private/tmp/om-close-advice-yield
review_base: origin/main@43109d5d1d1a9951c3e9e0a4f260701196fed4be
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: simple
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: completed
status: completed
next_action: 等待后续单独授权的提交、发布或生产升级；历史阈值校准需待远端报告可读取。
approved_scope_ref: 本文件原始授权
path_approval_ref: 用户回复“简单流程”
implementation_baseline:
  design_doc: docs/CLOSE_ADVICE_CONTRACT.md sha256:a6ed802823c36663cb74664753f3cb64a8a2237eb302c83512bf7abe41a80693
  implementation_workspace: /private/tmp/om-close-advice-yield
  review_base: origin/main@43109d5d1d1a9951c3e9e0a4f260701196fed4be
  head: 43109d5d1d1a9951c3e9e0a4f260701196fed4be
  git_status: ' M .devflow/scope.md; M docs/CLOSE_ADVICE_CONTRACT.md'
  staged: []
  unstaged:
    - {path: .devflow/scope.md, hash: 803b5775f1715da57e2f4d97b69b4ce46c502d33403d000d0af4c3298d556447, size: 3543}
    - {path: docs/CLOSE_ADVICE_CONTRACT.md, hash: a6ed802823c36663cb74664753f3cb64a8a2237eb302c83512bf7abe41a80693, size: 12834}
  untracked: []
inventory:
  - {path: .devflow/scope.md, status: M, hash: self-referential, size: self-referential, type: text, mode: 100644, classification: task-scope, evidence_ref: current-file}
  - {path: CONFIGS.md, status: M, hash: 45d0d5f9026dd810349a4acd6035f5a6e2c1cd7fefbab5326cbdc1021f04acc8, size: 18493, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/AGENT_WIKI.md, status: M, hash: e923432e581bf208682c792a21e4c439b715cf4e4377e55f7b1e275ead36e5e2, size: 59434, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/ARCHITECTURE.md, status: M, hash: 1bc35c100fdb6096af105bec67cd31deae2d001941eb1a0dd6bdafbbd3f3cbf1, size: 18193, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/CLOSE_ADVICE_CONTRACT.md, status: M, hash: 9200c68d10a3399139022502f37a087e7e166f9f1f4515423e38c3c4b6f3a815, size: 13637, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/DEPENDENCY_GRAPH.md, status: M, hash: 9abca0b3d5d61f42d0977b2f6bd8f829506f5711413ce9501f2d1767355e51a2, size: 8918, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/PRODUCT_ARCHITECTURE.md, status: M, hash: fc1abbffd3e11faccda3d62bcec2d647e617efd2eeca7776e40fabf01226da01, size: 10648, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/STRATEGY_ARCHITECTURE.md, status: M, hash: d1b42875be5892376e7826c515e50527daf0e73d334e01912ba917462625af36, size: 15302, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: docs/public_surface_retirements.json, status: M, hash: 0b7a94f8811dea31b2afc378b6b5194874d7bd34f14038f16263dee5fd032455, size: 1726, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: domain/domain/close_advice.py, status: M, hash: e0622fe69f404c0a3c90df346f9b07d9f6790217e71b55f505725d580f72c81a, size: 14505, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/agent_tools/close_advice_read_impl.py, status: M, hash: be6f0dbb532831569415c6a74ef06effc5eeda734a60370c9656caf6752d53c3, size: 34243, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/agent_tools/materialization_impl.py, status: M, hash: 7e24129f09ff505705460acc79e7dfa9e2115d9f68d1cbc0623257ac5322aaf1, size: 53275, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/assistant/renderer.py, status: M, hash: 106f55f9624fa624a6443df907faf9b0e83f704b03f8b690897765b1e64b58ed, size: 51395, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/assistant/tool_bindings.py, status: M, hash: 60946d697bc684a1a76469af92756c38e997f0a2e67d6014ae1a128a44f13f71, size: 11419, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/close_advice_runner.py, status: M, hash: ced4915c4edf60244dc22bc468bcbab707f5f0f79133716ded7d5d7574db19b2, size: 85959, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/config_validator.py, status: M, hash: 060e7bcaeb6ca9978e9ce41bf258bc2c15652a0278eddf76d807e1cee6a39982, size: 75588, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/daily_decision_brief_renderer.py, status: M, hash: ad19e9c64773a5f6a3f454db518c7d2d0581709efe23bb0c74c29f89338373c1, size: 98233, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: src/application/daily_decision_brief_service.py, status: M, hash: a7c2eaa87be67a3e1f63780e677ea606eca9fab1da5ae30c99f83b75d03c7dd8, size: 103342, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_agent_plugin_smoke.py, status: M, hash: 7c2c02793b7fc5385ee22f7ed2fb0cb24fd1e08f50976646d8aa5abd5ea2f564, size: 205421, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_assistant_runtime.py, status: M, hash: 97cecf630de094a64d6d10d578277405ea2588b23232e2152a0eaf5c16426ab3, size: 9446, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_close_advice_required_data.py, status: M, hash: 2e727ac6208b53aed40fe324d210f641c419c72136142d40d4e9d25bef21e9e2, size: 43861, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_close_advice_runner.py, status: M, hash: 4f284de8e85d88223c16d148de7266eb34d2113e2fdebcb523e94d4c9e06424b, size: 19034, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_daily_decision_brief_renderer.py, status: M, hash: 5fcb2872bc0b2e4b72d8df84d9e95544a39028446fd9fbe64d876f7ad99e8429, size: 57684, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_daily_decision_brief_service.py, status: M, hash: e01ce6a7f23f9a329d2f13160a8f913702d46e4d25ff868c99449eaa498721da, size: 102311, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
  - {path: tests/test_strict_close_advice.py, status: M, hash: 700697508eee3136a03daac1f3a8d97ea01e9cb4c6756c14af92743ddbb7b9d7, size: 8865, type: text, mode: 0644, classification: authorized-implementation, evidence_ref: git-diff}
content_revision: 4
planreview_round: 0
deepreview_round: 1
in_flight: []
evidence_paths:
  - docs/CLOSE_ADVICE_CONTRACT.md 原始设计快照 sha256:b0508db641fea41e79b4cb69c36c29fcf65ff73c67588fd854e42db45a4716cc；四位原生 subagent 均核对通过。
  - Impl: targeted 304 passed; full sandbox run 7699 passed, 2 skipped, 5 failures (1 stale generated dependency graph, 4 local port bind denied); graph regenerated and dependency test included in 304; four port tests separately passed with permission.
  - Review: docs/reviews/code-review-20260924-000027.md；完整当前差异审查，无实质性 finding，verdict=passed；历史回放仍未完成。
  - Improve Design Panel: 4/4 可用；reviewer_backend=native-subagent；reviewer_model=unknown；independence=unverified；建议裁决见设计文档末段；Planreview=not-applicable（无剩余需要对抗检查的重要设计缺口）。
  - Workspace isolation precheck: git fetch origin main 成功，HEAD=origin/main=43109d5d1d1a9951c3e9e0a4f260701196fed4be，ahead=0，behind=0；当前 main 有其他未跟踪文件，须隔离。
blocking_findings: []
residual_risks:
  - {item: 80%/10% 尚无完整历史结果校准, classification: assigned-to-later-work-unit, owner: Close Advice 离线回放, destination: 实现验证与上线前评估}
  - {item: 远端历史回放连接当前受跳板机解析失败影响, classification: assigned-to-later-work-unit, owner: Close Advice 离线回放, destination: 连接恢复后重试只读回放}
