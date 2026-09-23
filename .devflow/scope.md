# Runtime failure evidence and retention — Devflow scope

goal: Planned Tick/OpenD failures fail quickly with durable, readable cause; audit evidence remains readable at large size; cleanup has reviewable preview.
non_goals:
  - Re-login Futu accounts or place trades.
  - Send live test notifications.
  - Delete production data, change production services, release or deploy.
  - Commit, push, or create a PR without separate delivery authorization.
scope: Local source, tests, deployment templates, docs, and read-only cleanup preview; remote inspection only.
success_signals:
  - S1: Capturable Tick failures have run evidence with stage/code; external kill or disk failure is shown as an evidence gap using systemd state.
  - S2: OpenD login/phone/picture verification fails within a bounded attempt, alerts at most once per incident attempt, and runtime/quality state agrees through recovery.
  - S3: Tick unit has finite timeout and ERROR journal priority; healthcheck degrades per missing credential instead of failing wholesale.
  - S4: A 500 MB audit does not defeat recent evidence reads; historical reads are bounded and report incomplete coverage; service drift details can be exported.
  - S5: Rotation and retention preview preserve evidence; one-time cleanup remains preview-only with fixed candidate and protection checks.
authorized_slices:
  - {slice: A, design_doc_ref: 'docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md sha256:3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4', success_signal: [S1, S2, S3], depends_on: []}
  - {slice: B, design_doc_ref: 'docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md sha256:3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4', success_signal: [S3, S4], depends_on: [A]}
  - {slice: C, design_doc_ref: 'docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md sha256:3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4', success_signal: [S4, S5], depends_on: [B]}
slice_checkpoints:
  - {slice: A, diff_fingerprint: 'git-diff sha256:48b3d917b08b8862806743abc49806226cd3c4f155612b25de1e7885dae77cad + tests/test_tick_health_status.py sha256:b23cc4e9fb059fe10fb6e5985b4d54469b52f40b99ef6b664cf0fbe115417395', validation: '235 targeted passed; git diff --check passed', done: true}
  - {slice: B, diff_fingerprint: 'git-diff sha256:94bff84b2cce86c3df2b7a6f40842470b0f798d478601bdd6888c810937a80cb', validation: '41 targeted passed; window and facade tests passed', done: true}
  - {slice: C, diff_fingerprint: 'git-diff sha256:94bff84b2cce86c3df2b7a6f40842470b0f798d478601bdd6888c810937a80cb', validation: '68 targeted passed; rotation and preview tests passed; git diff --check passed', done: true}
user_confirmation:
  - 用户要求“用 devflow 执行任务”，承接 2026-09-23 OM 运维修复建议。
  - 用户选择“完整路径（推荐）”；问题文字明确本地设计、实现与审查，远端仅只读、不执行删除或部署。
prd_doc: not-applicable
prd_doc_ref: not-applicable
design_doc: docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md
design_ref: docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md sha256:3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4
implementation_workspace: /private/tmp/om-runtime-failure-evidence
review_base: origin/main@bbb7521f24bb361a3ecfd5fb01d3a62879480655
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: complete
status: completed
next_action: None within authorized Devflow scope; Delivery or remote operations require a separate instruction.
approved_scope_ref: This file original contract; design_ref above
path_approval_ref: 用户回复“完整路径（推荐）”
implementation_baseline:
  design_doc: docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md sha256:3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4
  implementation_workspace: /private/tmp/om-runtime-failure-evidence
  review_base: origin/main@bbb7521f24bb361a3ecfd5fb01d3a62879480655
  head: bbb7521f24bb361a3ecfd5fb01d3a62879480655
  git_status: ' M docs/INDEX.md; ?? docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md'
  staged: []
  unstaged:
    - {path: docs/INDEX.md, hash: 7c47a9e2ca829841613e95a8f0d62fcd8264b6966615589fc556a02195d3ec3a, size: 8230}
  untracked:
    - {path: docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md, hash: 3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4, size: 17218}
inventory:
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "docs/DEPENDENCY_GRAPH.md", "sha256": "718c188cb1207ae16b5c6266c5a5027e8eba0385db03496c920a364ac1db7aaf", "size": 8918, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "docs/INDEX.md", "sha256": "7cd9b62f179c32f6854b909cd2de4e47f8980b96eb34ca4f4c987b1e353bb3e1", "size": 8369, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "docs/dependency_graph.mmd", "sha256": "cbbae2b4ebff59f46c38048c7a7b4d412ca7e08b23cf7910efc049af9efc6c9e", "size": 6805, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "domain/storage/json_io.py", "sha256": "e36a787fde0b98660cc97ce29a9c6b78c89b8481258e2819530abb4a8c5fc6b5", "size": 5986, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "domain/storage/repositories/state_repo.py", "sha256": "b7c8cc694b013c0ada1fa76749e296b753eb277c092eba112361c37e53b026ac", "size": 13592, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/agent_tools/healthcheck_impl.py", "sha256": "80d3eec157174960e022118a9b75a779c1b8ba3a2799d960ebb53fb50e01f62e", "size": 49243, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/agent_tools/notification_perception.py", "sha256": "c1a1cb27d9d212934801782606df7f9b85f7cda80fcfa3c2f383b576dcab990c", "size": 8364, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/agent_tools/runtime.py", "sha256": "6a3d8b37237df87ce5be314426d147a1b41cb97b9e3963f824639b054b482aba", "size": 26491, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/agent_tools/runtime_status_impl.py", "sha256": "fcf2c225238008a8c427f659dc4f1db9b38a1c98960dc5bdf0d0fc1f629a1836", "size": 140037, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/multi_account_tick.py", "sha256": "29ac68cb55c0bd5ab3819d166623c7106538cb0d43e66d2556a26f4cebd87720", "size": 35973, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/multi_tick/opend_guard.py", "sha256": "18ba078283b008b6ccead532b11f5b9a73eea957b43b0a8a763910d700f5d43a", "size": 14278, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/multi_tick_watchdog.py", "sha256": "bdc798e2801e9baadbc50fdd69bd41fc34bad2f4bee25ec01c6dc7eaae41bc74", "size": 11004, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/notification_perception_read.py", "sha256": "e28d7c52cf4fed2498100d2493aa75815bfa1993b740f787208f2bfd23c482ca", "size": 27514, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/quality/runtime_checks.py", "sha256": "dc103953c40afd01d82a5e5207033d6b0aa846d83fb36cca19684dbae5874b89", "size": 9435, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/runtime_logs_cli.py", "sha256": "889feb3c3952c85c7da09cf3f4fd061d78344f9a751c98c428ae1e0df467da49", "size": 12626, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/secret_resolver.py", "sha256": "565251dbee1844b2670dac338806582d998c7eaf9130be4ec2f7a60a08ba90d9", "size": 8646, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/service_deploy.py", "sha256": "864aefcd4c0106b85a99c515ec75c41e3e13f245243c14dbcfbb165e599fa9ea", "size": 100652, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/service_drift.py", "sha256": "f3ad39b0251f1ff07b99fc5ed836ac065cbfa1a412d2402543ac3ee32b993e63", "size": 100960, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/tick_cron.py", "sha256": "d217567727621221d45d583fccc584a5700579efaf1c2cfa159e7341c85c2f6d", "size": 16077, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/tick_guard_flow.py", "sha256": "91ec10a22fffe2086f4203ea83c8788862c1140851fdd6caa86ae07984eb83fe", "size": 7944, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/trades/state.py", "sha256": "a5affe56fe91f784a79b35a1ce131f62444922f1466fec8802553a82c4bb0bfb", "size": 11910, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/infrastructure/futu_gateway.py", "sha256": "a438136032255f6c4a2a2c312cc5a69b4a2fc86fb675d941f15f5181289a0ebf", "size": 43182, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/infrastructure/futu_trade_push.py", "sha256": "1abac6b47b06531f2663db309bbf824c0c0c9e2433a2facb2622222b421e522b", "size": 13159, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/infrastructure/opend_retcodes.py", "sha256": "402923c2c3547746ae3d8ab60f3bc377f88c03948c326dca5e238de6de4b0f74", "size": 3229, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/infrastructure/opend_watchdog.py", "sha256": "328b2629163f088f44a684ac7b684fffd4e243a9a18f5365e5314c8bdf2066ef", "size": 14580, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/interfaces/cli/service_ops.py", "sha256": "c43ed47ee4e0d7b64a589ffccf83cb0e6261098ead133663b440b424a76f0ff0", "size": 25645, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_agent_plugin_smoke.py", "sha256": "d62336a4f610b9e1d672e8dc844d836940ecd20ce775b0fb586d3c8e8375de5c", "size": 207169, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_multi_tick_watchdog_timeout.py", "sha256": "d7ce9b8038175f0ad371b17824cada304aa33f1b2090d66727523b1d300f4243", "size": 12241, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_notification_perception_read_tool.py", "sha256": "0c24b26416c90c5d33882a641a01771dabcfd416ce8be7c840a1e042901c55c7", "size": 18662, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_opend_retcodes.py", "sha256": "3db7c437872aa50752cac54fca76385295d736f3251158435e9c4f087f0d5360", "size": 2376, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_opend_watchdog_alerts.py", "sha256": "63806d29e409b85aeb8e3fb6101ab6c2e72b49bd65c15ba2d8c5da58076f469a", "size": 19030, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_tick_cron.py", "sha256": "fcfb75e1831cd0732893810cb97794e44b17a0584079524a065603ab2b456386", "size": 13183, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_trades_state.py", "sha256": "167293d958683614f4c7ccd5b409eec4c4cef8f6ecd488024539cb43c2bd23f1", "size": 11765, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/unit/test_service_deploy_unit.py", "sha256": "4011679771168b1c4ba176b9f42bf4b06f5eeaafb3f3337edae26ccff5dc9ce2", "size": 91099, "status": "M", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "docs/RUNTIME_FAILURE_EVIDENCE_DESIGN.md", "sha256": "3082a94dbfefe8bfb137087ff9d5d705a51d477a9f50723ab2897f480cb17cc4", "size": 17218, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "docs/RUNTIME_FAILURE_OPERATIONS.md", "sha256": "db8f0acdde311e8921ebdbbb7cfa336dfca7133ec6b8247d4dca8858fada10fb", "size": 3082, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "scripts/incident_cleanup_preview.py", "sha256": "0d233e4c303f73e09f9fa65b4f443082c5fc18c7b74cf97ef5d1924228fb811e", "size": 953, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "src/application/incident_cleanup_preview.py", "sha256": "a0b615b64c3d36cc042ac5244420219bba185f9d9f9e37c6b76935bd5e89c6d1", "size": 9851, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_audit_rotation.py", "sha256": "d0e6933c6ac78c80b3483b81a45b20049572fde7e11c9e7a08c062eccb0e339c", "size": 1131, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_incident_cleanup_preview.py", "sha256": "71ea56caefb98aa166df1f362678e89afc55970d342533e5ad5f5f8a62e92027", "size": 2705, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_runtime_logs_journal.py", "sha256": "107256465a8eaad106686d3baf25e6076360d30a03b942a059de453ed4fdfdf9", "size": 513, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_service_drift_export.py", "sha256": "742c3f8d0117a371a5fb651ba8153d5726dd0ba1ec3442374f2e38a7516708f7", "size": 864, "status": "??", "type": "file"}
  - {"classification": "planned", "evidence_ref": "docs/reviews/code-review-20260924-023155.md", "mode": "0o644", "path": "tests/test_tick_health_status.py", "sha256": "f00714b9fe7d4a3b9ebfc1c6c8ff99d592fa3521cf0023eb50887aadb609ec10", "size": 2548, "status": "??", "type": "file"}
content_revision: 3
panel_results: {usable: 4, reviewer_backend: native-subagent, reviewer_model: unknown, independence: unverified, original_design_sha256: 8ebf9fe468077bcf17cb96a4de504e26149f6ddf6c283569d964d05be7924ce7}
planreview_round: 2
deepreview_round: 3
in_flight: []
evidence_paths:
  - docs/reviews/plan-review-20260924-013711.md (fail; three contract gaps)
  - docs/reviews/plan-review-20260924-014031.md (pass-with-risks)
  - Workspace precheck: git fetch origin main succeeded; HEAD=origin/main=bbb7521f24bb361a3ecfd5fb01d3a62879480655; ahead=0, behind=0; main unrelated untracked contents preserved.
  - Slice A: wrapper failure audit and completion receipt, OpenD manual-action codes/alert latch, runtime/quality status, finite tick unit, healthcheck credential degradation; 235 targeted tests passed.
  - Slice B: bounded audit window, journal-only hint and explicit drift export; 41 targeted tests passed.
  - Slice C: coordinated JSONL rotation, cross-segment seal read and fixed read-only cleanup preview; 68 targeted tests passed.
  - All slices combined: 298 targeted tests passed before refinements; 46 focused post-review tests and 24 latest audit/dependency tests passed.
  - Final full pytest excluding loopback fixture: 7720 passed, 3 skipped. New historical-window/dependency tests: 24 passed.
  - Loopback fixture: 4 passed under approved local-loopback test permissions. ruff, diff check, generated dependency graph, docs/sensitive guardrails passed.
  - docs/reviews/code-review-20260924-022405.md (two findings fixed in Impl)
  - docs/reviews/code-review-20260924-023155.md (second round; later source boundary refinement requires final re-review)
  - docs/reviews/code-review-20260924-023550.md (third round; pass-with-risks, no blocking finding)
blocking_findings: []
residual_risks:
  - {item: Application audit cannot survive host loss or unwritable disk, classification: assigned-to-later-work-unit, owner: Operations, destination: systemd/journal evidence reconciliation in slice A and remote deployment verification later}
  - {item: Production audit automatic deletion retention period is not authorized, classification: assigned-to-later-work-unit, owner: Operations, destination: separate production retention approval}
  - {item: Remote cleanup apply is outside authorized scope, classification: assigned-to-later-work-unit, owner: Operations, destination: separate reviewed production execution}
