goal: "Wheel 已开启时，身份与交割验证通过的外部 Combo 卖腿指派自动进入 Wheel；Wheel 内部指派仍待决策"
non_goals:
  - "自动交易、通知发送、生产数据写入或升级"
  - "增加 Wheel 开关或启用 CC+LP 开仓扫描"
  - "自动回填启用前或旧版本漏建的历史分支"
scope: "SP+LC funding Put 与 CC+LP short Call 的外部指派、组合身份证明及 Wheel 生命周期接入"
success_signals:
  - "S1: SP+LC 精确身份和真实接货成立时只创建一个 active Wheel Call 分支，长 Call 独立"
  - "S2: CC+LP 精确身份和真实交股成立时只创建一个 active Wheel Put 分支，长 Put 独立"
  - "S3: Wheel 内部指派仅转换父分支并创建 pending_decision 子分支，不重复 bootstrap"
  - "S4: 身份、交割、账户、市场、历史窗口、void、覆盖或数量冲突时不误建；原因可审阅"
  - "S5: 重放与事务失败不重复或半写；分支可见与候选可推荐分别验证"
authorized_slices:
  - slice: "组合身份与结构证明"
    design_doc_ref: "docs/WHEEL_STRATEGY_PRD.md@sha256:f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5"
    success_signal: ["S1", "S2", "S4"]
    depends_on: []
  - slice: "外部指派接入与生命周期验收"
    design_doc_ref: "docs/WHEEL_STRATEGY_PRD.md@sha256:f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5"
    success_signal: ["S1", "S2", "S3", "S4", "S5"]
    depends_on: ["组合身份与结构证明"]
slice_checkpoints:
  - {slice: "组合身份与结构证明", diff_fingerprint: "sha256:6bf2acf9fd8e0251d82e997ab56bd2167b13fbc592bf4748c82c1bfce4123ce1", validation: "tests/test_combo_membership.py in 82-pass target suite", done: true}
  - {slice: "外部指派接入与生命周期验收", diff_fingerprint: "sha256:b9932aac494d4d293f6826dd1cc014a30861c556095afbfd00e62d6e83caedf9", validation: "199 focused and adjacent tests passed; ruff, diff check and guardrails passed; review 1 void regression repaired", done: true}
  - {slice: "CC+LP lifecycle allocation 入口补测", diff_fingerprint: "sha256:2ecbcdf9f87c7069eb0a6bf7ffd34f7546fce39eaf93e07e736a923e972dcf13", validation: "79 touched-file tests passed; stale observation rejected without duplicate Wheel branch", done: true}
  - {slice: "Astra Review F1-F3 返工", diff_fingerprint: "sha256:47861042efa18683f9ede9cc2374f5a45d2c1b0e5a8207ba3fa331e5e5d3dfde+ab76a8e129f2e8644acdfd4d3711902c6d5c4d49af318f5613d7065886204935", validation: "red-before-green: 5 failed then 5 passed; 148 touched tests and 168 adjacent tests passed; ruff, diff check, guardrails passed", done: true}
  - {slice: "Review 4 F4 生命周期更正", diff_fingerprint: "sha256:8cd2d8d3550e61e8e02395559ba56f3f795f3667db8faa5c728b28a89bed0ecc+f85efd2ae60c28bcdf85359f619824a59d84bd01145130b09e9bdb5bbd7f20b7", validation: "real lifecycle writer test red then green; 244 related tests passed; ruff, diff check, guardrails passed", done: true}
user_confirmation:
  - "Wheel 已开启时，就代表身份和交割均验证通过的外部 Combo 卖腿指派可以自动进入 Wheel"
  - "完成后续环节；选择完整链路 Save Design → Improve Design → Impl → Review"
  - "本次同时实现 SP+LC 与 CC+LP"
prd_doc: "docs/WHEEL_STRATEGY_PRD.md"
prd_doc_ref: "docs/WHEEL_STRATEGY_PRD.md@sha256:f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5"
design_doc: "docs/WHEEL_STRATEGY_PRD.md"
design_ref: "docs/WHEEL_STRATEGY_PRD.md@sha256:f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5"
implementation_workspace: "/Users/<user>/.codex/worktrees/combo-wheel-assignment/options-monitor"
review_base: "b199beab282a0b1460e769213703945dec1c2e6c"
authorization_diffs:
  - {when: "2026-09-30T00:21:57+0800", what: "Devflow 简单模式补齐 CC+LP lifecycle allocation 业务入口端到端测试", ref: "本次用户明确请求"}
  - {when: "2026-09-30", what: "Devflow 修复独立 Astra Review F1-F3，随后重跑 DeepReview", ref: "本轮用户明确请求"}
workflow_version: 2
mode: workflow
workflow_path: simple
node_sequence: ["Save Design", "Improve Design", "Impl", "Review"]
current_node: "Review"
internal_step: null
status: completed
next_action: "本轮源码修复与 Review 完成；交付阶段仅在用户另行明确要求后执行"
approved_scope_ref: "authorization_diffs latest entry and user_confirmation above"
path_approval_ref: "simple-mode follow-up request in authorization_diffs"
implementation_baseline:
  design_doc: "docs/WHEEL_STRATEGY_PRD.md@sha256:f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5"
  implementation_workspace: "/Users/<user>/.codex/worktrees/combo-wheel-assignment/options-monitor"
  review_base: "b199beab282a0b1460e769213703945dec1c2e6c"
  head: "b199beab282a0b1460e769213703945dec1c2e6c"
  git_status: [" M .devflow/scope.md", " M docs/WHEEL_STRATEGY_PRD.md", " M src/application/ledger/wheel_trade_companions.py", " M tests/test_wheel_assignment_recovery.py", " M tests/test_wheel_workflows.py"]
  staged: []
  unstaged:
    - {path: ".devflow/scope.md", hash: "107d5503a19fb6fc27669def03b1d220ddcada03f491971e43690d1bb5afa13f", size: 3537}
    - {path: "docs/WHEEL_STRATEGY_PRD.md", hash: "f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5", size: 116821}
    - {path: "src/application/ledger/wheel_trade_companions.py", hash: "f11c7a267a3377d63a5ef7034e6041d3700268fe0eee7b14528ab2e3725484f8", size: 32424}
    - {path: "tests/test_wheel_assignment_recovery.py", hash: "fdb80d479ef171039f723c6cc6648de9813bb89eba3777c24f9647695c9a9d74", size: 18903}
    - {path: "tests/test_wheel_workflows.py", hash: "7a25a08cdbcf6370f8e8ad9f8c72e4c316b4c60d02be5727b907a0c3716b5416", size: 48642}
  untracked: []
inventory:
  - {path: "docs/WHEEL_STRATEGY_PRD.md", status: modified, hash: "f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5", size: 116821, type: file, mode: "100644", classification: design, evidence_ref: "design_ref"}
  - {path: "src/application/ledger/combo_membership.py", status: modified, hash: "47861042efa18683f9ede9cc2374f5a45d2c1b0e5a8207ba3fa331e5e5d3dfde", size: 32486, type: file, mode: "100644", classification: implementation, evidence_ref: "F2/F3 tests passed"}
  - {path: "src/application/ledger/wheel_trade_companions.py", status: modified, hash: "8cd2d8d3550e61e8e02395559ba56f3f795f3667db8faa5c728b28a89bed0ecc", size: 35539, type: file, mode: "100644", classification: implementation, evidence_ref: "F1/F4 tests passed"}
  - {path: "src/application/ledger/writer_lifecycle_allocation.py", status: modified, hash: "f85efd2ae60c28bcdf85359f619824a59d84bd01145130b09e9bdb5bbd7f20b7", size: 34316, type: file, mode: "100644", classification: implementation, evidence_ref: "F4 lifecycle correction test passed"}
  - {path: "tests/test_combo_membership.py", status: modified, hash: "9d9f14ae905d01d67933d12864bee2f22adf1dec4248d03723a0a514ae5bd64e", size: 13808, type: file, mode: "100644", classification: tests, evidence_ref: "red then green; 148 and 168 passed"}
  - {path: "tests/test_wheel_assignment_companions.py", status: modified, hash: "dccb1bacbbc6aaac136375cbd3df65b990f135f352402fffa4ffda41ed48279f", size: 35620, type: file, mode: "100644", classification: tests, evidence_ref: "F4 red then green; 244 passed"}
  - {path: "tests/test_settlement_observation.py", status: modified, hash: "86884715848eae28dbf6318ed07ec6ac5305a933c41ce272934d0e0ad0634ae4", size: 125544, type: file, mode: "100644", classification: tests, evidence_ref: "79 touched-file tests passed"}
  - {path: "tests/test_wheel_assignment_recovery.py", status: modified, hash: "5dee8619fb888ca61c6b7675114b32e09120f09c486cc387beb3d2885dbcdbd2", size: 19570, type: file, mode: "100644", classification: tests, evidence_ref: "real writer invalid pair tests passed"}
  - {path: "tests/test_wheel_workflows.py", status: modified, hash: "7a25a08cdbcf6370f8e8ad9f8c72e4c316b4c60d02be5727b907a0c3716b5416", size: 48642, type: file, mode: "100644", classification: tests, evidence_ref: "199 passed"}
  - {path: ".devflow/scope.md", status: modified, hash: "self-referential", type: file, mode: "100644", classification: workflow, evidence_ref: "Review 5"}
content_revision: "sha256:f4de943e5cccb91ccf04d52ffcc96c05950c7ac9b14181dca19b10778d9930d5"
planreview_round: 2
deepreview_round: 5
in_flight: []
evidence_paths: ["docs/WHEEL_STRATEGY_PRD.md", "docs/reviews/design-panel-20260929-234005.md", "docs/reviews/plan-review-20260929-234112.md", "docs/reviews/plan-review-20260929-234330.md", "docs/reviews/code-review-20260930-000312.md", "docs/reviews/code-review-20260930-000637.md", "docs/reviews/code-review-20260930-001658.md", "docs/reviews/code-review-20260930-002446.md", "docs/reviews/code-review-20260930-003312.md", "docs/reviews/code-review-20260930-004658.md"]
blocking_findings: []
residual_risks:
  - {item: "CC+LP post-open adoption has no controlled writer", classification: "assigned-to-later-work-unit", owner: "Combo reconciliation", destination: "separate controlled adoption design"}
  - {item: "historical CC+LP missing branches remain unrecovered", classification: "assigned-to-later-work-unit", owner: "Wheel recovery", destination: "separate explicit recovery design"}
