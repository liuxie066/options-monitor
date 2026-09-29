goal: "让已通过渠道鉴权的 Bot 用户在受控只读权限内查询 US/HK，并按实际市场与账户解释 3690.HK 等记录"
non_goals:
  - "不扩大交易、通知、Control 或账本写权限"
  - "不修改实时 config.yaml、生成的 config.us.json/config.hk.json、服务或生产状态"
  - "不提交、推送、建 PR、合并、发布或升级"
scope: "Bot 渠道可信市场授权、Host 工具选择、会话/记忆隔离、只读持仓事件工具、提示词和相关文档测试"
success_signals:
  - "S1: 已配置双市场授权时 3690.HK 可明确选择 HK 只读工具；未授权、冲突或缺失配置均在 Host 拒绝"
  - "S2: 每次读取按所选市场核验账户，不能借 US 的 lx 身份读取未授权 HK 数据；结果与回复标注实际市场、账户、数据来源"
  - "S3: assignment 与 trade attribution 两类记录能分别查询；无法证明的事实明确为未知，不将空结果或参数冲突推断成事实"
  - "S4: 授权集变化不复用旧会话与个人记忆；旧单市场部署默认保持原行为"
authorized_slices:
  - {slice: "trusted-market-routing", design_doc_ref: "docs/BOT_DESIGN.md#跨市场只读查询设计2026-09-29", success_signal: "S1,S2", depends_on: []}
  - {slice: "records-and-isolation", design_doc_ref: "docs/BOT_DESIGN.md#跨市场只读查询设计2026-09-29", success_signal: "S3,S4", depends_on: ["trusted-market-routing"]}
user_confirmation:
  - "/devflow 按这个方向优化"
  - "full"
  - "确认：当前 allowlist 用户可读 US/HK；本轮仅源代码只读能力"
prd_doc: "not-applicable"
prd_doc_ref: "not-applicable"
design_doc: "docs/BOT_DESIGN.md"
design_ref: "docs/BOT_DESIGN.md sha256:ab715ab44cba86181c921e34b3460b4ae5cca090a7aba257fde6ada19eea8fcd"
implementation_workspace: "<task-worktree>/options-monitor"
review_base: "origin/main"
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
progress: "completed"
review_counts: {panel: 4, planreview: 2, deepreview: 1}
review_artifact: "docs/reviews/code-review-20260929-222907.md"
review_verdict: "pass; no material findings; residual risks assigned in artifact"
panel_backend: "native-subagent"
panel_model: "unknown (default Codex; DeepSeek backend unavailable)"
panel_independence: "unverified"
panel_snapshot: "docs/BOT_DESIGN.md sha256:c5a822bc1dfc34728a0866163fb38ef8572422c747a59c8ced2db1a4dcab6019"
panel_decisions: "accepted: explicit assistant source, tool routing, market/account evidence, event type, attribution filter, grant generation, same-account memory; rejected: exposing unfiltered position list; deferred: manual byte-and-metadata restore epoch hardening"
planreview_artifact: "docs/reviews/plan-review-20260929-215242.md"
implementation_baseline:
  design_doc: "docs/BOT_DESIGN.md sha256:ab715ab44cba86181c921e34b3460b4ae5cca090a7aba257fde6ada19eea8fcd"
  workspace: "<task-worktree>/options-monitor"
  review_base: "origin/main@975351a4e50781aa5dd3483d3805e4270705e839"
  head: "975351a4e50781aa5dd3483d3805e4270705e839"
  status_short: [" M .devflow/scope.md", " M docs/BOT_DESIGN.md"]
  staged: []
  unstaged:
    - {path: ".devflow/scope.md", sha256: "ea81433eee0e30eb7196f88fa78f63a9187aa3e3995b8f77f73c2f8e573d87a1", size: 2682}
    - {path: "docs/BOT_DESIGN.md", sha256: "ab715ab44cba86181c921e34b3460b4ae5cca090a7aba257fde6ada19eea8fcd", size: 17080}
  untracked: []
slice_checkpoints:
  - {slice: "trusted-market-routing", diff_fingerprint: "final combined code diff sha256:16e12fef054cfbd9c5c1ac6eb6ca461a2b05318589407811d7e4ea1bc63feb09 + tests/test_bot_cross_market_read.py sha256:6a036e2f963775a897be097acfa9be4c65f4240f91a2d409269a7b72206fab60", validation: "360 passed; ruff --no-cache and git diff --check passed", done: true}
  - {slice: "records-and-isolation", diff_fingerprint: "final combined code diff sha256:16e12fef054cfbd9c5c1ac6eb6ca461a2b05318589407811d7e4ea1bc63feb09 + tests/test_bot_cross_market_read.py sha256:6a036e2f963775a897be097acfa9be4c65f4240f91a2d409269a7b72206fab60", validation: "360 passed; ruff --no-cache and git diff --check passed", done: true}
checkpoint_limit: "两片实现交错，只有最终合并快照，未记录独立可 bisect 的片边界；不把相同指纹称为两个独立版本。"
scope_closure: "S1,S2->trusted-market-routing; S3,S4->records-and-isolation; no uncovered signal or orphan slice"
scope_guard_extra: "取消入口和 Host 存储修复属于 S4 必要正确性：原飞书预检未传 assistant_config_path，授权代际会使活跃会话取消失效；tests/test_feishu_analysis_cancellation.py 覆盖。"
inventory_self: ".devflow/scope.md is this tracked workflow record; self hash omitted to avoid recursive mismatch"
inventory:
  - {path: "docs/BOT_DESIGN.md", status: " M", sha256: "ab715ab44cba86181c921e34b3460b4ae5cca090a7aba257fde6ada19eea8fcd", size: 17080, classification: "planned"}
  - {path: "src/application/agent_tools/operations_impl.py", status: " M", sha256: "0e2cecd2e5d454c836eb9f3dcf63951cf8360372b20fdfd227d86228676a2634", size: 49087, classification: "planned"}
  - {path: "src/application/agent_tools/positions.py", status: " M", sha256: "34edb182e33bca4599dec3ca828f3abace4c87b7a8a45a4ede1777c1c9139874", size: 73000, classification: "planned"}
  - {path: "src/application/agent_tools/runtime.py", status: " M", sha256: "329f31d4a04f2aa92256e0a277ab17eec77a0179bc43c38ad54aec47fb5fc83a", size: 26561, classification: "planned"}
  - {path: "src/application/bot/channel_facade.py", status: " M", sha256: "64eb204eb7dfec54a128a287cf3720ba91e44b348c4c36e6f8ce9346d7041645", size: 16708, classification: "planned"}
  - {path: "src/application/bot/host.py", status: " M", sha256: "b0376d69eef81d94cf05d7d41b9792054a3c4e06cfbd51870d6d85ea2b7586b6", size: 17287, classification: "planned"}
  - {path: "src/application/bot/host_store.py", status: " M", sha256: "ea122440037c0f307ee32486ef4e3fa72ce27dc8c2d7154bcdec431299d68727", size: 38531, classification: "required-correctness/safety"}
  - {path: "src/application/bot/memory.py", status: " M", sha256: "2749b04fc4bd7d5bb25648b8472fdf814ccec54e14a98f8ec6ea699342da6930", size: 23260, classification: "planned"}
  - {path: "src/application/bot/memory_worker.py", status: " M", sha256: "4a55f0368c3c8f4ae7e3ad2762c0469192e32184600698aab9a9de23be366fee", size: 5584, classification: "planned"}
  - {path: "src/application/bot/model_config.py", status: " M", sha256: "b9da0c15d71f3e0f374755ef09a0b98c279e1b96ed25eeaf552871e4fb31865a", size: 10267, classification: "planned"}
  - {path: "src/application/bot/om_chat.scene.json", status: " M", sha256: "03729d61edd136e8223067403898e06e460b7fdcd93695bc3c265bf6df1eaebf", size: 1826, classification: "planned"}
  - {path: "src/application/bot/prompts/tool_rules.md", status: " M", sha256: "635934ff60bfd02a48c02186782e8e07c41df95003eb1b4ce6e0a2db33d77dce", size: 2985, classification: "planned"}
  - {path: "src/application/bot/scene.py", status: " M", sha256: "eabba5bf754c736d6c5c5a3e971c96eb3dfec87e94e131fbc6d1761502dcc09a", size: 8574, classification: "planned"}
  - {path: "src/application/bot/service.py", status: " M", sha256: "87e5837460ea858e7b789da1cd36878328ed92c11ce4c5cfbbf72a26395a708a", size: 4479, classification: "planned"}
  - {path: "src/application/bot/session.py", status: " M", sha256: "9b410bbcdcf5d26243ed3ea8cb16d501a0ebeab9425990bc6eca5f096d04967f", size: 1539, classification: "planned"}
  - {path: "src/application/bot/tools.py", status: " M", sha256: "699a93bc82a71387591ff038efeae8e46ec943641ada17071aed24ae3a3fc95f", size: 38080, classification: "planned"}
  - {path: "src/application/config_validator.py", status: " M", sha256: "aaa31e928730ddede8b3b2fe29495d06844df70ad27b108d5b747813261fa1f9", size: 76484, classification: "planned"}
  - {path: "src/application/inbound/feishu.py", status: " M", sha256: "f5f12c37a2de997068967cbf474f82e5b0b1bc8e652af9e615572a356033f131", size: 9752, classification: "required-correctness/safety"}
  - {path: "src/application/inbound/feishu_ws.py", status: " M", sha256: "ac038cd7de3bce4518d548d9ce1aedd2ccd3694124a9556b8897223b725264c5", size: 47368, classification: "required-correctness/safety"}
  - {path: "src/application/trades/attribution.py", status: " M", sha256: "743c803df319761f4ec93fc9ec5d2583928d629a6612a8cf711e74044c75b899", size: 43091, classification: "planned"}
  - {path: "tests/test_bot_phase1.py", status: " M", sha256: "576fc4a1d101ccfe3d4cb0f398092fb92ecb81bb3da64cabe95de8b0b88a26d6", size: 31423, classification: "planned"}
  - {path: "tests/test_feishu_analysis_cancellation.py", status: " M", sha256: "f79a524d71933007fab1829d063da4ddbf5078cbf1998b00639fd29007dece41", size: 22487, classification: "required-correctness/safety"}
  - {path: "tests/test_trade_event_pagination.py", status: " M", sha256: "720f5e52f20316250fca0618db6e9ef02fc953127edf9973cb0ba0939458c3d1", size: 45951, classification: "planned"}
  - {path: "tests/test_bot_cross_market_read.py", status: "??", sha256: "6a036e2f963775a897be097acfa9be4c65f4240f91a2d409269a7b72206fab60", size: 13471, classification: "planned"}
