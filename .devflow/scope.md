# Setup CLI Devflow scope

```yaml
goal: "Provide Mac/Linux terminal first-run setup and later basic/advanced configuration without Agent chat secret entry."
non_goals:
  - "No real deployment, service install/start/restart, broker or Feishu call, notification, ledger or runtime data mutation."
  - "No new configuration store, secret backend, TUI dependency, or auto-trade capability."
  - "No commit, push, PR, merge, release, or upgrade."
scope: "Source, documentation, and isolated tests for om setup init, om config edit, minimal config starter, feature-aware setup diagnostics, and Mac/Linux terminal guidance."
success_signals:
  - "S1: Interactive first-run creates a minimal selected-market config; cancel, non-TTY, conflict and permission failures do not silently overwrite."
  - "S2: Basic and all supported advanced features remain discoverable and editable through CLI terminal paths with validation, preview and confirmation."
  - "S3: Ordinary env and credential steps use correct platform and privilege boundaries without secret disclosure or implicit service changes."
  - "S4: Setup readiness is feature-aware and distinguishes config, credential storage, broker and service evidence; placeholder Futu IDs are not ready."
  - "S5: Isolated Mac/Linux CLI, failure and security tests pass; user docs explain interactive and manual paths."
authorized_slices:
  - {slice: "A-starter-readiness", design_doc_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69", success_signal: ["S1", "S4"], depends_on: []}
  - {slice: "B-interaction", design_doc_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69", success_signal: ["S1", "S2", "S3"], depends_on: ["A-starter-readiness"]}
  - {slice: "C-platform-docs", design_doc_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69", success_signal: ["S3", "S4", "S5"], depends_on: ["B-interaction"]}
slice_checkpoints:
  - {slice: "A-starter-readiness", diff_fingerprint: "164f44c6ee3ffae5665fa10b2ed44c2555fb201fbf925caa37888c90d67f4a0d", validation: "207 related tests passed and final combined 315 tests passed; full ruff passed", done: true}
  - {slice: "B-interaction", diff_fingerprint: "3e38ba858a45efe8c40b371ce3d1d55688b714588ed56f088bc20ba4a0d2563a", validation: "319 combined tests passed, including symlink and transaction regressions; 119 earlier adjacent tests passed with sandbox exclusions", done: true}
  - {slice: "C-platform-docs", diff_fingerprint: "e8b470f03f75d9b08b503ae8f1378f3f48841474216eefae7cd90a4a40ba5483", validation: "Mac/Linux isolated and installer tests passed; docs fact/link review and git diff --check passed", done: true}
user_confirmation:
  - "想先做一个cli交互，现在还缺什么"
  - "高级功能还是需要保留，不然无法配置"
  - "Linux也能通用吗？"
  - "[$devflow] 开始写代码了"
  - "full"
prd_doc: "not-applicable"
prd_doc_ref: "not-applicable"
design_doc: "docs/SETUP_CLI_DESIGN.md"
design_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"
implementation_workspace: "<task-worktree>/options-monitor"
review_base: "origin/main@15a0348591dbe352ffa7403f36836246b0f24050"
authorization_diffs: []
workflow_version: 2
mode: "workflow"
workflow_path: "full"
node_sequence: ["Brainstorm", "Save Design", "Improve Design", "Impl", "Review"]
current_node: "Review"
internal_step: "Deepreview-round-3-complete"
status: "completed"
next_action: "Devflow source work complete; delivery, release and deployment require separate authorization."
approved_scope_ref: "Current conversation confirms terminal CLI first-run and later advanced configuration for Mac/Linux without real deployment."
path_approval_ref: "User: full"
implementation_baseline:
  design_doc: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"
  implementation_workspace: "<task-worktree>/options-monitor"
  review_base: "origin/main@15a0348591dbe352ffa7403f36836246b0f24050"
  head: "15a0348591dbe352ffa7403f36836246b0f24050"
  git_status: "20 staged integration files; .devflow/scope.md and docs/INDEX.md unstaged; docs/SETUP_CLI_DESIGN.md untracked"
  staged:
    - {path: "CHANGELOG.md", hash: "3461327055548dd6789d16598958d52b9659eda525c7f633a724d67d69c24283", size: 388374}
    - {path: "README.md", hash: "cf69a0a8aec93524c226d992a087be73ae8b40fa73ce72336b87ae309bca8ced", size: 7035}
    - {path: "docs/DEPENDENCY_GRAPH.md", hash: "a99151f7ae5fc7db68ce17b49d1f349bc7b47e050cb566a56550aacf31fb9d6e", size: 8919}
    - {path: "docs/DEPLOY_LINUX_MAC.md", hash: "3eb990b975542301b1e37061766633e973f8c5b266069ace72daf5c67ae289ee", size: 35266}
    - {path: "docs/GETTING_STARTED.md", hash: "90469874632ae36f1a04ba1230f404c67b06bb453b69d96269d344266d1adeca", size: 8132}
    - {path: "docs/INDEX.md", hash: "1b6bf652487588ea1bad573bf8053413ae1d8b143a509337395892e2057fd53e", size: 8366}
    - {path: "docs/INSTALL.md", hash: "6bae9760001493dedcca3fffedac83e0b54e1cfac897084e9419333efb58238e", size: 8269}
    - {path: "docs/dependency_graph.mmd", hash: "e6381b4a1725eec2ba5f7f4c60fb2e4fe02ece3be52dc628ab07948777fb4ad6", size: 6893}
    - {path: "scripts/install.sh", hash: "ee7411a31a54ccf10ff7782ecf26749ba923a619195ed4bbad7de7c037647940", size: 17926}
    - {path: "src/application/assistant/operation_policy.py", hash: "9d36728daa3335ff047872609ec783b7a7d3b7bf89bd8b94a053d740dccd8aeb", size: 7306}
    - {path: "src/application/service_drift.py", hash: "37247caad17c57a913c5e8742f07c43eddb416d53ab3f7e682acf432c4aec1f3", size: 114057}
    - {path: "src/application/service_upgrade.py", hash: "65ed7eae1ed12066ce32a9b501bdf204d48f19ed6818656a09df63470fd5387e", size: 147854}
    - {path: "src/application/setup/check.py", hash: "d0ff5521282ab1d9d6dfef18ec9047077d82aeaaa84e1e3e0ff45478f747dc55", size: 24309}
    - {path: "src/infrastructure/secret_store/macos_keychain.py", hash: "4809ca6f7d112714bb3e819f95f3f1bd766284ae400bd4ea809fa678d3922cb4", size: 8975}
    - {path: "src/infrastructure/secret_store/systemd_credentials.py", hash: "eda396b1a8483f72310cd429a452add067b5f18aef4a72ff85996a1ccdd37a69", size: 9035}
    - {path: "src/interfaces/cli/service_ops.py", hash: "79db5e44c7bb1f6ffff51eeaacb4bbdb727b686b81b726c4493f85d91207e1ce", size: 25888}
    - {path: "tests/test_install_script.py", hash: "ae001eeff2467955c2ec99d5459d43adc8eba4fabc9679f771330f4816826310", size: 23477}
    - {path: "tests/test_launchd_service_deploy.py", hash: "62c66c83152436d3a2f8723205b19a9d55075dd25f2352c41d828ec9045f9095", size: 24844}
    - {path: "tests/test_secret_providers.py", hash: "c9fc86e5fcc55626ff9e9ae064638ef688e958795a9010ef72329fbe33840b7a", size: 7638}
    - {path: "tests/test_setup_check.py", hash: "aecfc1a2dbcfb0b19efdb456bc85ab5e87f1f40ba636f7e4c9f60f3b8ba15dad", size: 25642}
  unstaged:
    - {path: ".devflow/scope.md", hash: "e69955a753bfec425df7e0b5945036656b85b231a7f10c53fcf7c19d9099305e", size: 3552}
    - {path: "docs/INDEX.md", hash: "0cd30748ec549f20aa40538d3c5da1946b1e9343be5b67633665a5417acf659c", size: 8472}
  untracked:
    - {path: "docs/SETUP_CLI_DESIGN.md", hash: "ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69", size: 12810}
inventory:
  - {path: "CHANGELOG.md", status: "M ", hash: "3461327055548dd6789d16598958d52b9659eda525c7f633a724d67d69c24283", size: 388374, type: "markdown", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "CONFIGURATION_GUIDE.md", status: " M", hash: "c8d29ddcd715f7c4419ab46e716c67cf556162910ce973313799b38b1c6a871e", size: 11878, type: "markdown", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "README.md", status: "MM", hash: "5e10e6f7e73b291d64c8f63622d437641456e91f62bf1885c4b7befbf8b056a9", size: 7315, type: "markdown", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "docs/DEPENDENCY_GRAPH.md", status: "MM", hash: "3e69013c33b01a3f7a4e8935a337d1f6e02bc617b2490f78fded1493d038de97", size: 8927, type: "markdown", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "implementation_baseline.staged"}
  - {path: "docs/DEPLOY_LINUX_MAC.md", status: "MM", hash: "b0c5a34d06806ad310403f843071ea05c97f3f92c6975635ed5b12a383fab1ba", size: 35509, type: "markdown", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "docs/GETTING_STARTED.md", status: "MM", hash: "28dafa1d066587be423ae38ada40d0a4818ecf457f226d709b41ef3bd49c0844", size: 10603, type: "markdown", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "docs/INDEX.md", status: "MM", hash: "0cd30748ec549f20aa40538d3c5da1946b1e9343be5b67633665a5417acf659c", size: 8472, type: "markdown", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "docs/INSTALL.md", status: "MM", hash: "4b2ffc16eafb6be93f54dc49cbad932598d4c7822baf862afe3af429bbde1e6b", size: 8941, type: "markdown", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "docs/dependency_graph.mmd", status: "MM", hash: "0ba9427cea44c48eda541c09d3ce1bb522f1b965d13fd5945c48d3a7c240377a", size: 6893, type: "mermaid", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "implementation_baseline.staged"}
  - {path: "scripts/install.sh", status: "MM", hash: "fae435ab05168fcd84453040bf6bda370f57697ed9217db77a0e66b202fb6011", size: 17958, type: "shell", mode: "0o755", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/application/assistant/operation_policy.py", status: "M ", hash: "9d36728daa3335ff047872609ec783b7a7d3b7bf89bd8b94a053d740dccd8aeb", size: 7306, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "src/application/config_authoring_transaction.py", status: " M", hash: "c002a1ac157acf9719673595f5fca020eb05ab29500a7a7054f8b717e0ae4b47", size: 44361, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/application/config_yaml_init.py", status: " M", hash: "e0aacf5cd177638e22e6ce59994988a0023c99498c8bf5209d45c84ff611138d", size: 11442, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/application/runtime_config_readiness.py", status: " M", hash: "2b408ad770287506fb77a69a3f890012896163a486c3e2e9e08a22c60794fddd", size: 5559, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/application/service_drift.py", status: "M ", hash: "37247caad17c57a913c5e8742f07c43eddb416d53ab3f7e682acf432c4aec1f3", size: 114057, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "src/application/service_upgrade.py", status: "M ", hash: "65ed7eae1ed12066ce32a9b501bdf204d48f19ed6818656a09df63470fd5387e", size: 147854, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "src/application/setup/check.py", status: "MM", hash: "3839b93fc7d835e2e22a85f1d2d84492b97ef0bc0f09ce63b4a10a6317771f2b", size: 27777, type: "python", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/infrastructure/secret_store/macos_keychain.py", status: "M ", hash: "4809ca6f7d112714bb3e819f95f3f1bd766284ae400bd4ea809fa678d3922cb4", size: 8975, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "src/infrastructure/secret_store/systemd_credentials.py", status: "M ", hash: "eda396b1a8483f72310cd429a452add067b5f18aef4a72ff85996a1ccdd37a69", size: 9035, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "src/interfaces/cli/config_ops.py", status: " M", hash: "c7abddf251eaccdda76f7212b6fd5461ac8f077042d859b70c9cd0ae9fcb3472", size: 15876, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/interfaces/cli/service_ops.py", status: "M ", hash: "79db5e44c7bb1f6ffff51eeaacb4bbdb727b686b81b726c4493f85d91207e1ce", size: 25888, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "src/interfaces/cli/setup_ops.py", status: " M", hash: "0cc87e6a632cef8ee121c972c6941332c9776df54911de11444df3cfa300ff91", size: 3919, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_cli_operator_commands.py", status: " M", hash: "fd6edfb2645b519fb8461523de4032c797e2f455c0ee0338b6d1dc8bf9443390", size: 50852, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_config_authoring_transaction.py", status: " M", hash: "a631e310ea7c8fde5a62ad72d05e4d7c8d2f024f2f29f00a3fa940c0185b62e8", size: 56573, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_config_yaml.py", status: " M", hash: "2a2d52b0aad6eddf32fd9db60e5a66860135e7ac80464032834df84953f2bdca", size: 69247, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_install_script.py", status: "MM", hash: "7ffb8cb2e2348d27e147ccf46b4987e44311bb126b0867dcf9cdd1abad984475", size: 23430, type: "python", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_launchd_service_deploy.py", status: "A ", hash: "62c66c83152436d3a2f8723205b19a9d55075dd25f2352c41d828ec9045f9095", size: 24844, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "tests/test_secret_providers.py", status: "M ", hash: "c9fc86e5fcc55626ff9e9ae064638ef688e958795a9010ef72329fbe33840b7a", size: 7638, type: "python", mode: "0o644", classification: "preexisting-staged-preserved", evidence_ref: "implementation_baseline.staged"}
  - {path: "tests/test_setup_check.py", status: "MM", hash: "08203ac66524079dfa1d1f97642b7f7fbeb4e30dfadedea440cb35a4d75c3a32", size: 28967, type: "python", mode: "0o644", classification: "preexisting-staged-plus-current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "docs/SETUP_CLI_DESIGN.md", status: "??", hash: "ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69", size: 12810, type: "markdown", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/interfaces/cli/config_interactive.py", status: "??", hash: "af3e9e5854ce6aefb87f68a4b299d442590c16a4c3759b67bde431bfd216cb72", size: 13484, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "src/interfaces/cli/setup_interactive.py", status: "??", hash: "7a34d60bbad65d1771158101ea88cb6202bcd592563bf1d274d658b05c915801", size: 8281, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_config_interactive.py", status: "??", hash: "9cf111adc601c73431b5cf68c37888edf7daf6c2866b097f2026f2f804f95cb5", size: 6440, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
  - {path: "tests/test_setup_interactive.py", status: "??", hash: "eada783e53eeea75e98eb903248f03f1e3c527a70b52169c9321969b39d29ae5", size: 4501, type: "python", mode: "0o644", classification: "current-task", evidence_ref: "docs/SETUP_CLI_DESIGN.md@ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"}
content_revision: "ff153e56ca9ed29affbff4f7288b51e3ac05bd890e14d845da712b07e185dd69"
planreview_round: 2
deepreview_round: 3
in_flight: []
evidence_paths: ["docs/SETUP_CLI_DESIGN.md", "CONFIGURATION_GUIDE.md", "CONFIGS.md", "docs/INDEX.md", "docs/reviews/plan-review-20260928-193529.md", "docs/reviews/plan-review-20260928-193628.md", "docs/reviews/code-review-20260928-200946.md", "docs/reviews/code-review-20260928-202408.md", "docs/reviews/code-review-20260928-202925.md"]
blocking_findings:
  - {id: "review-1", severity: "medium", artifact: "docs/reviews/code-review-20260928-200946.md", status: "repaired-and-validated"}
  - {id: "review-2", severity: "high", artifact: "docs/reviews/code-review-20260928-202408.md", status: "repaired-and-validated"}
residual_risks:
  - {item: "Linux manual shell cannot consume systemd credentials without a credential-bearing runtime", classification: "assigned-to-later-work-unit", owner: "deployment flow", destination: "docs/DEPLOY_LINUX_MAC.md"}
  - {item: "Removing a market leaves old runtime snapshot and possibly active service", classification: "assigned-to-later-work-unit", owner: "controlled service retirement", destination: "docs/DEPLOY_LINUX_MAC.md"}
design_panel:
  reviewer_backend: "native-subagent"
  reviewers: ["cli_panel_1_fallback", "cli_panel_2", "cli_panel_3", "cli_panel_4"]
  reviewer_model: "unknown"
  independence: "unverified"
  snapshot: "docs/SETUP_CLI_DESIGN.md@e44d15717e50072bbfa7eebb69d546bfd334f3541ce2bfc3065be4aaeb2af262"
  adjudication: "accepted first-create transaction, path binding, offline evidence limits, market retirement pending, Linux manual credential pending and redacted preview; rejected a new env editing subsystem in favor of existing settings diagnostics and explicit sudoedit handoff."
```
