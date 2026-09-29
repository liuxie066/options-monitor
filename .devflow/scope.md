goal: "修复 OM 首次配置的离线就绪判断、跨终端目录定位、预览与失败恢复"
non_goals:
  - "不迁移或修改已有生产配置、服务或凭证"
  - "不运行扫描、不连接 OpenD、不发送通知"
  - "不提交、推送、建 PR、合并、发布或部署"
scope: "om setup init/check、公共 runtime root 解析、相关 CLI/Tool Gateway 只读消费及文档"
success_signals:
  - "S1: 占位账户 ID 或所选市场快照缺失阻断离线配置就绪；Bot 单独报告"
  - "S2: 无高优先级覆盖时新终端定位新建目录，显式环境与服务路径优先"
  - "S3: dry-run/确认前展示完整目标与关键设置，不写持久目标、不覆盖并发目标"
  - "S4: 预备内容绑定最终 YAML；写入失败只恢复本次未变化的文件；成功回读可用"
authorized_slices:
  - {slice: "offline-readiness", design_doc_ref: "CONFIGS.md#首次初始化与运行目录", success_signal: "S1", depends_on: []}
  - {slice: "durable-runtime-root", design_doc_ref: "CONFIGS.md#首次初始化与运行目录", success_signal: "S2", depends_on: []}
  - {slice: "preview-and-recovery", design_doc_ref: "CONFIGS.md#首次初始化与运行目录", success_signal: "S3,S4", depends_on: ["durable-runtime-root"]}
slice_checkpoints:
  - {slice: "offline-readiness", diff_fingerprint: "sha256:e6a124b902ef3f25d376e456011b45056b01fb6d1ad014cc39c74ad4471855da", validation: "tests/test_setup_check.py: 12 passed", done: true}
  - {slice: "durable-runtime-root", diff_fingerprint: "sha256:9c85a198f3678637de6ad7e0e3bfc8cc3c3e72896d417aa547497b0ddf818dd2", validation: "new-process resolver/tool scope and tick-cron: passed", done: true}
  - {slice: "preview-and-recovery", diff_fingerprint: "sha256:8ce75051b073c9d3d0ef2c4b293da6b25fa3befc900a960dc3e17f44b1f037ef", validation: "setup-init faults, new-process setup check, config suite: passed", done: true}
user_confirmation:
  - "先修2、3项"
  - "再处理预览与恢复"
  - "full"
prd_doc: "not-applicable"
prd_doc_ref: "not-applicable"
design_doc: "CONFIGS.md"
design_ref: "CONFIGS.md sha256:1f6942c383a72235c5b7f409164e8e15229243bd254bed3a1573d3842b4b8ff0"
implementation_workspace: ". (managed worktree: first-run-readiness/options-monitor)"
review_base: "origin/main@18f7ff06364c663cacf9ac84eef591171131b687"
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: Review
internal_step: deepreview
status: completed
next_action: "研发已完成；如需源码交付，另行授权提交或推送"
approved_scope_ref: "本对话用户消息: 先修2、3项; 再处理预览与恢复"
path_approval_ref: "本对话用户消息: full"
implementation_baseline:
  design_doc: "CONFIGS.md sha256:1f6942c383a72235c5b7f409164e8e15229243bd254bed3a1573d3842b4b8ff0"
  implementation_workspace: ". (managed worktree: first-run-readiness/options-monitor)"
  review_base: "origin/main@18f7ff06364c663cacf9ac84eef591171131b687"
  head: "18f7ff06364c663cacf9ac84eef591171131b687"
  git_status: " M CONFIGS.md"
  staged: []
  unstaged:
    - {path: "CONFIGS.md", hash: "1f6942c383a72235c5b7f409164e8e15229243bd254bed3a1573d3842b4b8ff0", size: 27786}
  untracked: []
inventory:
  - {path: ".devflow/scope.md", status: " M", hash: "sha256:self-reference", size: 3143, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "CONFIGS.md", status: " M", hash: "sha256:1f6942c383a72235c5b7f409164e8e15229243bd254bed3a1573d3842b4b8ff0", size: 27786, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "CONFIGURATION_GUIDE.md", status: " M", hash: "sha256:e5ab0fdb2d6ee9e11719ba76327f3b5d433c78c344d54319a007f11f8d300aa6", size: 11145, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "README.md", status: " M", hash: "sha256:652f4fc8493bbe2656a81f175c9adc9cd248425045465e07d88473d2ce002295", size: 19898, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "docs/DEPENDENCY_GRAPH.md", status: " M", hash: "sha256:11c3a6282ce7ecf32f4b43c8708afcdf2952042146a514675100b7727e061fe7", size: 8918, type: file, mode: "0644", classification: "required-correctness/safety", evidence_ref: "origin/main diff and focused checks"}
  - {path: "docs/GETTING_STARTED.md", status: " M", hash: "sha256:62fa3a59fa549e1e6fdffbbcb33a3dea730e5bf13fb4a4f1ecfe845c644761a3", size: 7147, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "docs/dependency_graph.mmd", status: " M", hash: "sha256:2de75e2f11184c6dc90f64b05363437e95582c3a2dbc8ce14cc830eb8974d68c", size: 6805, type: file, mode: "0644", classification: "required-correctness/safety", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/agent_tool_config.py", status: " M", hash: "sha256:e0a12fc02c4ed2556709f756162ff7fb0ff15119aab6c8ab03e686e769bb63de", size: 4441, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/agent_tools/project.py", status: " M", hash: "sha256:15bc0a76e41b9066108c7a304631c869c9aab5dab19342c38923df4d77aefcb7", size: 11808, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/agent_tools/project_runs.py", status: " M", hash: "sha256:619ca98ee71e4b371fad031aa1fa92711ee36c10ceb90bd3457b4a65cb4fa39a", size: 32406, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/agent_tools/runtime.py", status: " M", hash: "sha256:8e89a265f2aa48e90612899bf6c7fea43685b14334f1f90c417b0f7f8acbcde5", size: 26533, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/config_yaml_init.py", status: " M", hash: "sha256:382ebbcf74db4c684469f98a61e252a6643fe23712e405f380d609cd4e0bb6ca", size: 16955, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/runtime_paths.py", status: " M", hash: "sha256:bb7257c0a1530ca8c2db06affe36c441370d0636ac5751ac84231fdb61087b13", size: 3415, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/setup/check.py", status: " M", hash: "sha256:49838c11a2f35f496ec21b7eb6468e6f100afec0fe3ca078bacfe8596d6c30cb", size: 16690, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/application/tick_cron.py", status: " M", hash: "sha256:5f018be587e1cf712753d0f3c6bb5a52a70f804c4e6768fbfafbba7225491c34", size: 17959, type: file, mode: "0644", classification: "required-correctness/safety", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/interfaces/cli/home.py", status: " M", hash: "sha256:8ae7ae6ba76620aa5667e4ea6d7c56904a967ee61b9984437a4c460ddb08d851", size: 4682, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "src/interfaces/cli/setup_ops.py", status: " M", hash: "sha256:e6a0d4bcedda97f865f5975d9d5f00036bb8326a6d011b6614678a7b6df23dcb", size: 10298, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "tests/test_runtime_paths.py", status: " M", hash: "sha256:cbde4a7942a76c38a658ab0c17b2e4e1c36502415d29af0157917a567d573042", size: 4254, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "tests/test_setup_check.py", status: " M", hash: "sha256:f24292359c44442055a798fa7464265afdf07c7b4b3f0283cb0b034eed8ab487", size: 14057, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
  - {path: "tests/test_setup_init_cli.py", status: " M", hash: "sha256:2407e2918029c1542ff058c0c4c534909d23227f4513b09c1337914c8760741c", size: 8651, type: file, mode: "0644", classification: "planned", evidence_ref: "origin/main diff and focused checks"}
content_revision: "1f6942c383a72235c5b7f409164e8e15229243bd254bed3a1573d3842b4b8ff0"
planreview_round: 2
deepreview_round: 1
in_flight: []
evidence_paths:
  - "docs/reviews/plan-review-20260929-005839.md"
  - "docs/reviews/plan-review-20260929-010001.md"
  - "docs/reviews/code-review-20260929-012144.md"
blocking_findings: []
residual_risks:
  - {item: "强杀后可能留下新建文件", classification: "needs-new-issue-or-user-decision", owner: "首次配置操作者", destination: "当前手动核对见 Getting Started；若需自动恢复再定需求"}
