goal: 为 Portfolio Exposure 增加可受控开启和关闭的 Holdings 来源配置
non_goals:
- 不改变现有全部未平仓卖出期权被指派的计算及查询持仓来源
- 不迁移 external_holdings 账户，不写 Futu、PM Holdings、OM 账本或生产配置
- 不提交、推送、建 PR、合并、发布或部署
scope: 全局 portfolio.holdings.enabled 默认关闭、YAML/runtime 校验与生成、开启预检、预览/确认/读回、CLI/文档及针对性测试
success_signals:
- 'H1: 缺省关闭，默认与生成快照可见；只接受全局布尔值'
- 'H2: 可预览和受控开启/关闭；写入具备 SHA 确认、备份、构建和读回；关闭不依赖 PM 可用'
- 'H3: 开启需 PM Holdings 新鲜可信证据；失败不写，仅报告已观测范围'
- 'H4: 旧指派情景计算和查询不变；新配置尚未改变其结果，公开说明此边界'
authorized_slices:
- slice: default-validation
  design_doc_ref: CONFIGS.md#Portfolio Exposure 的 Holdings 来源配置
  success_signal: H1
  depends_on: []
- slice: controlled-toggle
  design_doc_ref: CONFIGS.md#Portfolio Exposure 的 Holdings 来源配置
  success_signal: H2,H3
  depends_on:
  - default-validation
- slice: cli-doc-regression
  design_doc_ref: CONFIGS.md#Portfolio Exposure 的 Holdings 来源配置
  success_signal: H4
  depends_on:
  - controlled-toggle
slice_checkpoints:
- slice: default-validation
  diff_fingerprint: sha256:7aebb9da0dfdac47e7b74208f7ab70ecf435d17b16d8875219ba4b1153d29ff8
  validation: red KeyError on DEFAULT_CONFIG; green 1 passed with pytest -p no:cacheprovider
  done: true
- slice: controlled-toggle
  diff_fingerprint: sha256:056dac03712e0a9ebc41dc59a187cf26aca45e33cc691b3ff93f458f5ccc1d8e
  validation: 'H2/H3 red first; tests/test_config_yaml_holdings.py: 10 passed'
  done: true
- slice: cli-doc-regression
  diff_fingerprint: sha256:c2a6dfa7d1821f5cd50836d59972644f2d4a026da2dd20c7963acf2e6f12378f
  validation: 279 targeted tests passed; CLI temp preview/apply passed; Ruff and guardrails
    OK; scenario files unchanged
  done: true
user_confirmation:
- 本次只是增加了 holdings 的配置
- 是，沿用指派后情景
- 仅标示已观测范围，不宣称完整
- 用 /devflow 完成holdings 配置改造
- 完整链路
prd_doc: not-applicable
prd_doc_ref: not-applicable
design_doc: CONFIGS.md
design_ref: CONFIGS.md sha256:e7923b334c32605f3f2e54c70d0b65661fe5b3c62da3b14021f9fe9919e275b7
implementation_workspace: '. (managed worktree: portfolio-exposure-config-current/options-monitor)'
review_base: origin/main@ef75a799af4ea1e921a4a1be88cecb7a4a849b77
authorization_diffs: []
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence:
- Brainstorm
- Save Design
- Improve Design
- Impl
- Review
current_node: Review
internal_step: deepreview
status: completed
next_action: 研发工作流已完成；交付阶段待独立授权
approved_scope_ref: 本对话用户确认 Portfolio Exposure 仅做 Holdings 配置改造
path_approval_ref: 本对话用户选择完整链路
implementation_baseline:
  design_doc: CONFIGS.md sha256:f0a3caa6d84a4214e1f6f581b8a9c7518fb6f41561c64298242183d6ec092a2c
  implementation_workspace: '. (managed worktree: portfolio-exposure-config-current/options-monitor)'
  review_base: origin/main@ef75a799af4ea1e921a4a1be88cecb7a4a849b77
  head: ef75a799af4ea1e921a4a1be88cecb7a4a849b77
  git_status: "M .devflow/scope.md\n M CONFIGS.md\n M CONFIGURATION_GUIDE.md\n M configs/examples/config.yaml.example\n\
    \ M src/application/config_validator.py\n M src/application/config_yaml.py\n M\
    \ src/interfaces/cli/config_ops.py\n?? src/application/config_yaml_holdings.py\n\
    ?? tests/test_config_yaml_holdings.py"
  staged: []
  unstaged:
  - path: .devflow/scope.md
    hash: d28e1e3ddf9974d13eaa1eefca165f0c0834b8fc39d9f031c6e5ad94224178ff
    size: 2788
  - path: CONFIGS.md
    hash: f0a3caa6d84a4214e1f6f581b8a9c7518fb6f41561c64298242183d6ec092a2c
    size: 33271
  - path: CONFIGURATION_GUIDE.md
    hash: 7da91c688b3550f307fb6077b6f0a9ae131176cdaf525cbd356644f52d67eb67
    size: 12081
  - path: configs/examples/config.yaml.example
    hash: 0d630e1f4bfef0efe05a2cf4956fd69bd05ed5d8c404a8ca99099f78e3df65d6
    size: 1940
  - path: src/application/config_validator.py
    hash: 559a9168785beeb1e532b6fda64176247d066149d84746d9ec481a2585389559
    size: 77047
  - path: src/application/config_yaml.py
    hash: ed9fdb073dd22d346e5a90bae8bb478e6f300d76b21dc48739bfbae5a7e60103
    size: 49599
  - path: src/interfaces/cli/config_ops.py
    hash: 207335225b3caab4e3dd4be3e0355af28a0f25dc7a5bda0ce807186291da70db
    size: 16120
  untracked:
  - path: src/application/config_yaml_holdings.py
    hash: 5588fe2165d7363b738c11c64c26521d323cea2892cf5763f16a60c9d035bd50
    size: 7853
  - path: tests/test_config_yaml_holdings.py
    hash: aa77b6958ca8dd787c05fcd7102e6ad285caeb9c3ba9fb1b72d526c4d7d91758
    size: 5083
inventory:
- path: .devflow/scope.md
  status: ' M'
  hash: sha256:self-reference
  size: 5242
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: CONFIGS.md
  status: ' M'
  hash: sha256:e7923b334c32605f3f2e54c70d0b65661fe5b3c62da3b14021f9fe9919e275b7
  size: 33407
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: CONFIGURATION_GUIDE.md
  status: ' M'
  hash: sha256:8ea867efc63587c136f980bdd78351485ff80cc9f2fc5c6270844cb751870262
  size: 12829
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: configs/examples/config.yaml.example
  status: ' M'
  hash: sha256:0d630e1f4bfef0efe05a2cf4956fd69bd05ed5d8c404a8ca99099f78e3df65d6
  size: 1940
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: configs/system.json
  status: ' M'
  hash: sha256:b62c3b1dbf294e72a230c1f430d8374543645a428d81fcd5cc5ab90c99d2f7a9
  size: 8311
  type: file
  mode: 0o644
  classification: required-correctness/safety
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: src/application/config_defaults.py
  status: ' M'
  hash: sha256:ac7d480d9ac4e4fa65ee3fae602d1a181d7059f3acb793fa0dcfec9adf4e2b19
  size: 10619
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: src/application/config_validator.py
  status: ' M'
  hash: sha256:61fdbf6cb1c39f062db70a63d36649b2141afa7570d8a81ebb9e2d0f3a8b7653
  size: 77050
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: src/application/config_yaml.py
  status: ' M'
  hash: sha256:ed9fdb073dd22d346e5a90bae8bb478e6f300d76b21dc48739bfbae5a7e60103
  size: 49599
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: src/interfaces/cli/config_ops.py
  status: ' M'
  hash: sha256:e31a7773a9f0c08659a197dc67b9b71bb19c0c20bab628d5512083be3fada97f
  size: 16259
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: src/application/config_yaml_holdings.py
  status: ??
  hash: sha256:12bde7e65b26eaf360dbf0c9398bc2814c2850bedde70ca335409db029d9b915
  size: 11069
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
- path: tests/test_config_yaml_holdings.py
  status: ??
  hash: sha256:ee907e73d76a704753caf67526d7b520468cd9e53f8583313b9aeab22438b051
  size: 10117
  type: file
  mode: 0o644
  classification: planned
  evidence_ref: 279 tests, Ruff, guardrails, git diff --check
content_revision: e7923b334c32605f3f2e54c70d0b65661fe5b3c62da3b14021f9fe9919e275b7
planreview_round: 4
deepreview_round: 2
in_flight: []
evidence_paths:
- docs/reviews/plan-review-20260929-232624.md
- docs/reviews/plan-review-20260929-232714.md
- docs/reviews/plan-review-20260929-233053.md
- docs/reviews/plan-review-20260929-233539.md
- docs/reviews/code-review-20260929-234649.md
- docs/reviews/code-review-20260929-234855.md
blocking_findings: []
residual_risks:
- item: 真正按开关改变情景持仓来源
  classification: assigned-to-later-work-unit
  owner: Portfolio Exposure 后续任务
  destination: 后续独立设计与授权
- item: origin/main 在本轮审查后前进至 ebe02c17；与本轮文件无直接交集，交付前需重核基线
  classification: assigned-to-later-work-unit
  owner: 后续 Delivery
  destination: 获交付授权时核对最新基线与门禁
