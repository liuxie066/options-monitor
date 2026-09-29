goal: "退役 external_holdings 账户及账户级 Holdings 回退，保留全局持仓风险读取"
non_goals:
  - "不实现 om holdings configure、Bot 命名或标的初始化改动"
  - "不迁移或改写历史期权账本、实际运行配置、凭据或服务"
  - "不提交、推送、发布或部署；不修改实际运行配置或服务"
scope: "Devflow full：基于已保存设计优化、实施并审查账户退役方案"
success_signals:
  - "S1: 新建与增改入口只提供富途账户；PRD 验收 3"
  - "S2: 旧账户字段和映射不再成为有效来源，走现有配置错误；PRD 验收 5 与用户简化取舍"
  - "S3: 富途失败及错误缓存/预备来源不回填 Holdings；PRD 验收 4"
  - "S4: 全局风险保留 Holdings，缺失和失败明确不可用；PRD 验收 4"
  - "S5: 旧配置用现有流程预览、备份、构建、回读并核对账本；PRD 验收 5"
authorized_slices:
  - {id: A, behavior: "账户配置只认富途", covers: [S1, S2], depends_on: []}
  - {id: B, behavior: "账户与全局风险分流", covers: [S3, S4], depends_on: [A]}
  - {id: C, behavior: "迁移及公共契约核对", covers: [S5], depends_on: [A, B]}
slice_checkpoints:
  - {slice: A, diff_fingerprint: "sha256:bee03f1674e8e2293466b0f979a8cc4b53b6e86d331c84b56a63630faa3d81bc (final source subset)", validation: "配置、账户 CLI、Tool Gateway 与 Tick 启动回归", done: true}
  - {slice: B, diff_fingerprint: "sha256:50a95cf7aa19c366c1e4177095a8a3edbb484c0104076d4c4cd8b307de05ed05 (final source subset)", validation: "账户富途、预备上下文与全局风险回归", done: true}
  - {slice: C, diff_fingerprint: "sha256:f542227d7b0b6487910888c7c612ebffbe561b6e8e0cf03cb5b9fb0bfa5beff4 (final source subset)", validation: "隔离迁移 fixture、文档与公共契约门禁", done: true}
user_confirmation:
  - "为什么需要做拒绝功能，是不是直接把配置，绑定的入口删除掉就可以了，搞一个拒绝有点画蛇添足"
  - "进入 devflow 的下一阶段"
  - "用/devflow 的 full 模式完成方案实施"
prd_doc: "../../setup-symbol-selection/options-monitor/docs/CLI_REFACTOR_PRD.md"
prd_doc_ref: "../../setup-symbol-selection/options-monitor/docs/CLI_REFACTOR_PRD.md sha256:1020b936b1efc0f740d39ac10f182f1eb7320dbbda7c303b1b44f739e77f3a97 (draft; original Save Design hash 576a7a21254a2b15354639024d4918918bfd2d6a8fc85c47b3e4301226eb400d)"
design_doc: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md"
design_ref: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md sha256:a6ddb95580400b204a9314216b2f5b341e37ff5728aa3b20e9f6296aec8c85ab (status update only; frozen implementation baseline below)"
implementation_workspace: "."
review_base: "origin/main@975351a4e50781aa5dd3483d3805e4270705e839"
authorization_diffs:
  - {when: "本对话", what: "将专门拒绝功能改为删除旧配置和绑定，保留现有配置错误", ref: "用户对上一方案的纠正"}
  - {when: "本对话", what: "授权进入 Save Design，不含实现或运行配置写入", ref: "进入 devflow 的下一阶段"}
  - {when: "本对话", what: "授权 Devflow full 的 Improve Design、Impl、Review；不扩展运行配置和交付阶段", ref: "用/devflow 的 full 模式完成方案实施"}
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Improve Design, Impl, Review]
current_node: Review
internal_step: complete
status: completed
next_action: "后续源码交付或目标环境配置切换须按各自授权范围执行"
approved_scope_ref: "本对话关于退役 external_holdings 的原始任务及用户简化取舍"
path_approval_ref: "本对话: 用/devflow 的 full 模式完成方案实施"
implementation_baseline:
  design_doc: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md sha256:f44011f37080184f7c75f96c83cb58233676a7d34b4fbf91aa2d6c6af7398c97"
  implementation_workspace: "."
  review_base: "origin/main@975351a4e50781aa5dd3483d3805e4270705e839; git fetch origin main succeeded; ahead/behind 0/0"
  head: "975351a4e50781aa5dd3483d3805e4270705e839"
  git_status: "M .devflow/scope.md; M docs/INDEX.md; ?? docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md"
  staged: []
  unstaged:
    - {path: ".devflow/scope.md", hash: "fdaa5deaf62184f193432b23227ff0b458951927dd21e2d07fcb5da9063bb75d", size: 3364}
    - {path: "docs/INDEX.md", hash: "291d254ba2f7da9229ca7682a67a7ac5e4437e1befaf32867ff297652cc0b16a", size: 8534}
  untracked:
    - {path: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md", hash: "f44011f37080184f7c75f96c83cb58233676a7d34b4fbf91aa2d6c6af7398c97", size: 10891}
inventory:
  - {path: "CONFIGURATION_GUIDE.md", status: "unstaged", sha256: "8174d088e16f6b01c4db7405a2cc491267e0fc3eb9f4e690f32847652173bdc4", size: 11070, mode: "0644"}
  - {path: "README.md", status: "unstaged", sha256: "5cc9b5577e16662a70821fd5517b98bc3bcc33a76c636f14594e839aa0154432", size: 19898, mode: "0644"}
  - {path: "configs/examples/config.yaml.example", status: "unstaged", sha256: "5b6e1b74b4053ee04dad57c80e0c21623ad81608928e01c05dcf2e1cee2e5532", size: 1897, mode: "0644"}
  - {path: "docs/AGENT_GETTING_STARTED.md", status: "unstaged", sha256: "2ae0c36ecf6f9af0ad22f961a21c66b932c00f9fbb228c3282b9a53bcf08cb24", size: 4399, mode: "0644"}
  - {path: "docs/AGENT_INTEGRATION.md", status: "unstaged", sha256: "c37d2735f604ff4e631cf5d836029efd555f71539e3a093de87e0f00c3b298a6", size: 21992, mode: "0644"}
  - {path: "docs/AGENT_WIKI.md", status: "unstaged", sha256: "e3fbfe74c3e70c19702ab4fe259d01ce97158e2cd635e6b23ce91dd7a62322be", size: 60523, mode: "0644"}
  - {path: "docs/DEPENDENCY_GRAPH.md", status: "unstaged", sha256: "ad370f0d463e8b36696ed0a3d4d2b471c8d9b7c7f6b48f91ce367580b96e899f", size: 8918, mode: "0644"}
  - {path: "docs/INDEX.md", status: "unstaged", sha256: "be6deef6599d82a7ea91b6bb6935a07f75f664886c98c58ad95fb50db00fb036", size: 8537, mode: "0644"}
  - {path: "docs/LEDGER_ARCHITECTURE.md", status: "unstaged", sha256: "b37f7798f6ece6127128e34211d13b055ce3d3ab98abbf3b30671bdff6d517f5", size: 105252, mode: "0644"}
  - {path: "docs/public_surface_retirements.json", status: "unstaged", sha256: "6d445c9087f82c5ae1fa8f309ca1451b8fb8351f157fcf40ec20b95d499834d0", size: 6543, mode: "0644"}
  - {path: "src/application/account_config.py", status: "unstaged", sha256: "c405c8cbba585140ae2a4295949a58eb3cebfd2b1085186f902c44d8fb3e0e71", size: 24836, mode: "0644"}
  - {path: "src/application/account_management.py", status: "unstaged", sha256: "03fb896c822e108b54b7ae4c8ee217b86c3bf04032ca6fbf709588369aba95c0", size: 3323, mode: "0644"}
  - {path: "src/application/agent_tool_config.py", status: "unstaged", sha256: "36c6397c6790ac06dbd5b4c2b4f791bebea5b15239ecc0ace0c60e82ad3a4661", size: 4806, mode: "0644"}
  - {path: "src/application/agent_tool_registry.py", status: "unstaged", sha256: "5c60f541729e72d2a1a3a47666f6316d4371e172e97ad98609f2f8249890068c", size: 4538, mode: "0644"}
  - {path: "src/application/agent_tools/healthcheck_impl.py", status: "unstaged", sha256: "53c55454be4a6d59f15edd6dc07d7d7cd7c9e61f7e9082dac35e053ad9cc48e7", size: 47688, mode: "0644"}
  - {path: "src/application/cash_headroom_query.py", status: "unstaged", sha256: "de99c3093071508648598ff4daafda8b255e24a5126cb345bffe7c60440e6c95", size: 20508, mode: "0755"}
  - {path: "src/application/config_validator.py", status: "unstaged", sha256: "3aa5a3cfde5446ff74f71df9fcb11e9be90cb6facd9143982650e3ba956dd9e4", size: 76274, mode: "0644"}
  - {path: "src/application/config_yaml.py", status: "unstaged", sha256: "7bd0fb16e0ce5364dd835c8596346fccd3a1b48f51cbcbcd01809a17d41fbb9b", size: 49298, mode: "0644"}
  - {path: "src/application/config_yaml_accounts.py", status: "unstaged", sha256: "aef0bc02145855b1d0576dbe2ce094a149a6155b1e0741d90fb04c17b27c2a03", size: 15775, mode: "0644"}
  - {path: "src/application/config_yaml_init.py", status: "unstaged", sha256: "20920c53221d8e35d1defc83d8039e407c40c665230d90aec381bed846722985", size: 15961, mode: "0644"}
  - {path: "src/application/layered_config.py", status: "unstaged", sha256: "65b9dea136b35d1427889500812f7a2d05edd70d4e5d2fc31860d905842c8a32", size: 12100, mode: "0644"}
  - {path: "src/application/multi_account_tick.py", status: "unstaged", sha256: "8c2fd651da353da972c043de91e5c0c99b32341d5ad37f5c1fa8c953b57aa55b", size: 36267, mode: "0644"}
  - {path: "src/application/pipeline_context.py", status: "unstaged", sha256: "47e0e1f2a9f42b8ff4b3a70f2bc51dda3b1f1f9a54cb2abf5568cb8ac12c6531", size: 25119, mode: "0644"}
  - {path: "src/application/portfolio_context_builder.py", status: "unstaged", sha256: "a375c30374dd70ff8d00d100172be2ca03ff281896349701ce8aec7b0c94dcbc", size: 19565, mode: "0644"}
  - {path: "src/application/portfolio_context_service.py", status: "unstaged", sha256: "b2855e3c5d9be259645edc44c6350226106dcf02a30483f89c873e2ce21ddd82", size: 4993, mode: "0644"}
  - {path: "src/application/positions/maintenance.py", status: "unstaged", sha256: "e2e409dbe1d39324d1a5f87c4af3ecc2c212ba781bd8d4a6a087351a18c6b4e2", size: 28331, mode: "0644"}
  - {path: "src/application/prepared_portfolio_context.py", status: "unstaged", sha256: "a06073dfda38e541addb41aad89c6f26257c63aa5e220a36e27b3a91e4380d3e", size: 40203, mode: "0644"}
  - {path: "src/application/short_vol_risk_context.py", status: "unstaged", sha256: "ea9bcc318b7fc7103d1657fd99a45c5da81bcead270a3c0d8d729a3787591c1d", size: 9944, mode: "0644"}
  - {path: "src/interfaces/agent/cli.py", status: "unstaged", sha256: "dec39632d241253eb237850c0772f8744e843374def63076529b1a493bb0b5cc", size: 8522, mode: "0644"}
  - {path: "src/interfaces/cli/account_ops.py", status: "unstaged", sha256: "51f5cdcd67997c7329ddc04096cc3f9d6c5831ec439b713af899338c9bb7952c", size: 4738, mode: "0644"}
  - {path: "src/interfaces/cli/config_ops.py", status: "unstaged", sha256: "c3b72bb48b51405139ab4fb7865ae7bf51dc350f4ae722e7b9f3ac85936da1da", size: 14908, mode: "0644"}
  - {path: "src/interfaces/cli/setup_ops.py", status: "unstaged", sha256: "1725b7769a1fc8c9d91bd36192e58b2deadab00819fd144a495052ee961d6297", size: 10156, mode: "0644"}
  - {path: "tests/run_smoke.py", status: "unstaged", sha256: "580c604b4f70bbd4a523d9c6202d643e98870a4b27e1b76e70a792a925e0c615", size: 23882, mode: "0755"}
  - {path: "tests/test_account_config.py", status: "unstaged", sha256: "a7636d6300afd336858c6a09e44e97f3bb7929451772452d8f6ffa727e70225e", size: 9761, mode: "0644"}
  - {path: "tests/test_account_scope_authority.py", status: "unstaged", sha256: "72f8d58d97bbfa56158295a7681f9fe7cb9002311d557121d982416401a17294", size: 11920, mode: "0644"}
  - {path: "tests/test_agent_plugin_smoke.py", status: "unstaged", sha256: "d3b151eaccc486b87e53b32e941c2aa145a040c158365be1ec8eabc022a47ad9", size: 211830, mode: "0644"}
  - {path: "tests/test_cli_operator_commands.py", status: "unstaged", sha256: "e385e5b89c6ad8c87132294073626d18e0e1a260aa0d456d215916baae26f4e8", size: 50620, mode: "0644"}
  - {path: "tests/test_config_yaml.py", status: "unstaged", sha256: "a860aa29f40418a8677cfb3a11c4f9a462ed353dba8ce3d5448cd3e0ad7b4bf0", size: 72003, mode: "0644"}
  - {path: "tests/test_day8_config_regressions.py", status: "unstaged", sha256: "e176aacee3f469c272c18906e6721b1394498a8d1d1b393b3f6dc960d0459b0c", size: 9928, mode: "0644"}
  - {path: "tests/test_global_liquidity_filters.py", status: "unstaged", sha256: "0a323fed36468267cf8c889466139c9386c85f72558e384af591aa7972130eea", size: 19809, mode: "0644"}
  - {path: "tests/test_inbound_control.py", status: "unstaged", sha256: "4ad5e20c72ce8b12d5f152deb04d58710da5fa5c56232b95ddeae9252d81f85c", size: 170647, mode: "0644"}
  - {path: "tests/test_multi_account_tick.py", status: "unstaged", sha256: "1a756ddc494133fab137f0269b7db5ac65433d0756693b41b526d6a6bd6691a6", size: 41898, mode: "0644"}
  - {path: "tests/test_pipeline_context_contract_validation.py", status: "unstaged", sha256: "ecc5f44b5f41ff029535689502e5c5bd536fba98f0bfd0dc14c4fea1c2036e19", size: 4982, mode: "0644"}
  - {path: "tests/test_pipeline_context_shared_context.py", status: "unstaged", sha256: "c913bb44c0b985b1f93cbf80d24ca65a41146d083c8abd7f95ce17ec994da765", size: 36439, mode: "0644"}
  - {path: "tests/test_positions_maintenance.py", status: "unstaged", sha256: "86e04cb3720c7becf2d004ed1f1993115c78e9a2623b0461b55b07502b7ca48b", size: 27683, mode: "0644"}
  - {path: "tests/test_prepared_portfolio_context.py", status: "unstaged", sha256: "9adf7363df85ee80c061d01d6d5957b08da776e6932249cba65d259dc6de5267", size: 27932, mode: "0644"}
  - {path: "tests/test_query_sell_put_cash_futu.py", status: "unstaged", sha256: "c731d3789dcc93e8543adbc69e716677ae9ca8320ef5f027e84430ad55e6165e", size: 8879, mode: "0644"}
  - {path: "tests/test_sell_put_strategy_risk.py", status: "unstaged", sha256: "8aaf5a5ca7a07b5091c5e5a2a064353968acb10bd44cb35776e20ef004f3a4be", size: 15771, mode: "0644"}
  - {path: "tests/test_trades_account_mapping.py", status: "unstaged", sha256: "f1e7f8fe039ac36071249a1fbfebe285298972a8ac159020d2d7d56745c207bb", size: 9169, mode: "0644"}
  - {path: "tests/test_wheel_cli.py", status: "unstaged", sha256: "414816b8a22ad1c6333d46b263edff1930c385761026a198b253c79a85573a73", size: 40862, mode: "0644"}
  - {path: "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md", status: "untracked", sha256: "a6ddb95580400b204a9314216b2f5b341e37ff5728aa3b20e9f6296aec8c85ab", size: 10925, mode: "0644"}
content_revision: "a6ddb95580400b204a9314216b2f5b341e37ff5728aa3b20e9f6296aec8c85ab"
planreview_round: 1
deepreview_round: 2
in_flight: []
evidence_paths:
  - "docs/EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md"
  - "docs/INDEX.md"
  - "docs/reviews/plan-review-20260929-212753.md"
  - "docs/reviews/code-review-20260929-222158.md"
  - "docs/reviews/code-review-20260929-222816.md"
  - "/private/tmp/om-holdings-pytest-final.log"
blocking_findings: []
residual_risks:
  - {item: "目标环境有效配置与未结 lot 尚未清点", classification: "needs-new-issue-or-user-decision", owner: "目标环境操作者", destination: "获授权的配置切换前只读清单与归属决定"}
  - {item: "最终 Tick 启动改动采用聚焦回归和 smoke，未重跑全量套件", classification: "accepted-validation-scope", owner: "本任务", destination: "第二轮 code review artifact 的验证限制；后续 Delivery 若要求 exact-final 全量门禁再跑"}
