# 交易监听整体设计 — Devflow scope

## 原始授权

goal: 设计全局交易监听、策略自动归属和 Wheel 双向轮转通知的整体方案。
non_goals: 不实现代码，不交易，不修改生产账本或配置，不发送通知，不提交、推送、发布或部署。
scope: 全局成交接收与恢复契约；普通 CSP/CC、Wheel、Combo 归属；部分成交与部分指派；CSP 转 CC 的覆盖状态；CC 指派后卖出 CSP 重新接股；候选建议价格。
success_signals:
  - S1：多来源重复、乱序、重启与关联失败均不重复经济事实，且可恢复。
  - S2：账户、策略、订单分腿与容量隔离明确，冲突不会静默绑定。
  - S3：Wheel 双向轮转、部分转换及下一轮确认语义完整。
  - S4：通知区分入账、归属、覆盖及送达，候选建议价格含口径与时间。
  - S5：复用现有 owner，交付不超过三个可验证实现切片及四份独立优化建议。
  - S6（后续授权，见 OM Bot 补充）：渠道无关的 OM Bot 查询、预览确认、写入与回执闭环，模型不能确认。
authorized_slices:
  - {slice: A, success_signal: [S1, S2, S4], depends_on: []}
  - {slice: B, success_signal: [S1, S2, S3, S6], depends_on: [A]}
  - {slice: C, success_signal: [S3, S4, S5], depends_on: [B]}
slice_checkpoints:
  - {slice: A, diff_fingerprint: eca8bf95f02a0b0ff0e672fa310da48f52463d48e5571d0ea07944541ea7333f, validation: "220 passed in 18.57s", done: true}
  - {slice: B, diff_fingerprint: 37c584289dc500c171ac98205d9c12c392568805b1b49719153f6acdb4d6a210, validation: "304 passed; corrected Bot scene 8 passed", done: true}
  - {slice: C, diff_fingerprint: 9b144c0efe36e3c61c1ce64fe216fbe549e232a12fd8ab63418c32ec164b94b7, validation: "7618 passed + 4 loopback passed; 2 skipped; lint/guards/smoke/spec passed", done: true}
user_confirmation:
  - 用 devflow 先设计交易监听的整体方案
  - 完整设计：全局契约、失败恢复、验收和四路设计优化（推荐）

## 绑定与进度

prd_doc: not-applicable（本次没有单独批准的 PRD；既有 PRD 仅作事实输入）
prd_doc_ref: not-applicable
design_doc: docs/FUTU_TRADE_HOLDINGS_SYNC.md
design_ref: docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:71c3d4626a88468b62451323f098c85d0e2ac2776b54d06923dc5e22f5276304
implementation_workspace: /private/tmp/om-trade-attribution
review_base: 248d9c9334274fa70d02383850c028d81c136120（git fetch origin main 已刷新；仅升级器与依赖图增量）
authorization_diffs:
  - 用户“进入impl”授权冻结设计 A/B/C 实现及 workflow 必需 Review；详见 Impl 授权与冻结基线。
  - 用户确认待归属成交统一通过 OM Bot，不绑定飞书；本次“用 devflow 继续设计”授权补齐该设计。
workflow_version: 2
mode: workflow
workflow_path: full
node_sequence: [Brainstorm, Save Design, Improve Design, Impl, Review]
current_node: null
internal_step: null
status: completed
next_action: none（已完成授权的 Impl 和 Review；交付与生产操作待另行授权）
approved_scope_ref: 本文件原始授权
path_approval_ref: 用户选择完整设计的回复
implementation_baseline: {"design_ref": "docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:71c3d4626a88468b62451323f098c85d0e2ac2776b54d06923dc5e22f5276304", "implementation_workspace": "/private/tmp/om-trade-attribution", "review_base": "248d9c9334274fa70d02383850c028d81c136120", "head": "248d9c9334274fa70d02383850c028d81c136120", "git_status": "M docs/FUTU_TRADE_HOLDINGS_SYNC.md\n?? .devflow/", "staged": [], "unstaged": [{"path": "docs/FUTU_TRADE_HOLDINGS_SYNC.md", "hash": "71c3d4626a88468b62451323f098c85d0e2ac2776b54d06923dc5e22f5276304", "size": 117029, "type": "file", "mode": "0o666", "layer": "unstaged"}], "untracked": [{"path": ".devflow/scope.md", "hash": "192e7802617f49bd680ea48696e5330e19b0cd459e08c3ce0fc099f069420f40", "size": 8302, "type": "file", "mode": "0o644", "layer": "untracked"}]}
inventory: [{"path": "docs/DEPENDENCY_GRAPH.md", "hash": "21552fa522ab9a796e0e3f8e7514fa866674c9b4a77d1c5fdf7c3445c6fb82bc", "size": 8918, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "docs/FUTU_TRADE_HOLDINGS_SYNC.md", "hash": "71c3d4626a88468b62451323f098c85d0e2ac2776b54d06923dc5e22f5276304", "size": 117029, "type": "file", "mode": "0o666", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "docs/INBOUND_CONTROL.md", "hash": "299f97e28051b88c9c85d2ae7f341519d5744008fc10111e792804cde1cd6cd6", "size": 5821, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "docs/TOOL_REFERENCE.md", "hash": "87b86b2256759e52694cbf1e464186560c9c8f9e8aa7a26daf2bcc84cdd77d9c", "size": 13698, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "docs/WHEEL_STRATEGY_PRD.md", "hash": "512701edd643e17eee36116899f0f58633dc10defc1d9f5a7e009002c4af61fc", "size": 108646, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "docs/dependency_graph.mmd", "hash": "cac68e8499d31bf094d95d1d4227eb6d86c76ff126ad0c20bdbd088cfe697382", "size": 6714, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/combo_reconciliation.py", "hash": "c2e2f67fe746d39086cbc01fecaec8b66c57cf5946bf4698ed5b30006b60984b", "size": 27065, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/strategy_membership.py", "hash": "141c11e7a1dfe0818c2536a9a767eedaeb60ae223daa25783e6a5f4ccf28a7af", "size": 11934, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/wheel/__init__.py", "hash": "1a1d36c83b7b799162088710c0a241a0a361d4cd15d8f0d875392328df0125ec", "size": 3844, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/wheel/_common.py", "hash": "49be42d8181dedd98211c2d2c02f9b07fc9e6612e0fd0baaa3a2b53b73aba108", "size": 2319, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/wheel/events.py", "hash": "be2673372b57f23621123f124d01ec3fbc9985095e5afc5cc5c6f3552e29405a", "size": 12086, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/wheel/intents.py", "hash": "565d05010f76e923fe9d05cbfa845d49f19cdce4d4740c17720d7e41ec30adf0", "size": 39945, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "domain/domain/wheel/projection.py", "hash": "4aece0e9877ac284a89a5afd3f5686aedaa69deeb63af691dad40b3b66a0778f", "size": 74029, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/agent_tools/positions.py", "hash": "181a3baec78aac72aade7864cf35f1d4266080be31dde442ab779313ad2fa2a8", "size": 72912, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/attribution_operations.py", "hash": "4d717201ce71e2f279e62613730e817089b285473356373c3430edecb73b8ba7", "size": 17310, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/capability_catalog.py", "hash": "bf1f612773892ff81c996c6317d8ab42c4ae8554e8db20d330c60b04e836e2f2", "size": 25126, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/command_parser.py", "hash": "1829bcb5647903f36a3cc9982efdfac023a97065efe9e60e48263cbd3ecf4520", "size": 22183, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/inbound_control.py", "hash": "a2a5226cd8d68d96fe88d67eaa87973d34f19e4589ac2468a15cb7d01877a28e", "size": 15211, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/inbound_service.py", "hash": "0ac7939a6fb7ceb33ea20da602173d3d8b758cfe2f437fed266c4f58b44d038e", "size": 22666, "type": "file", "mode": "0o644", "classification": "required-correctness/safety", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/operation_diagnostics.py", "hash": "f47b2607c3bd7b24ae5749186b25b3a41be89715df74f193410cfb522531dcb2", "size": 37099, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/operation_lifecycle.py", "hash": "b2917e4a564ea2f41182e70f896bcfea1582193c67d3cbcd1371e04335e793b3", "size": 19648, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/operation_store.py", "hash": "4bf883449e624c69ecf24d2f8e862d97f61bf66c26affeba9c9a27efec193674", "size": 43294, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/permission_request.py", "hash": "afecd4e631846e3f2534d87adf27932f76e5cc2c78f883abe4e62dc251bdbd0b", "size": 3439, "type": "file", "mode": "0o644", "classification": "required-correctness/safety", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/permission_response.py", "hash": "96f2c93dd05cb37fdc5f46fcdfca01ab58029b9a47986e30840b14c554582762", "size": 10478, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/assistant/renderer.py", "hash": "7b692a5489b4ee50193bdda9b99706c67a87622a0f349d7b3b60affcda31299c", "size": 51253, "type": "file", "mode": "0o644", "classification": "required-correctness/safety", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/bot/host.py", "hash": "6d69802111f9e2c10b9c2c5cbbfbe9d49a8fbdc9698a6c87f1c6f43fd85f2a91", "size": 15802, "type": "file", "mode": "0o644", "classification": "required-correctness/safety", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/bot/om_chat.scene.json", "hash": "8ba27277b788182f9b962f581a9768ae8c2f10f21cc3a7427c873af86320130e", "size": 1532, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/candidate_models.py", "hash": "2509b0528952b4d0a78c6015e381eec7915847d5b047ab0e18770aaeccac039f", "size": 8856, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/daily_decision_brief_renderer.py", "hash": "62a9aeb1dff1cefd14d3121fe31c099826fe88e90f8eef2f04f4304f4064aac4", "size": 98010, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/daily_decision_brief_repository.py", "hash": "747fdfdbe730f20a939b707b1b058a51d0c2dceda69dda5b6b6db08fca664bd7", "size": 95220, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/daily_decision_brief_service.py", "hash": "a5c6e6ce9a9c7ce24d098bdaa69b7a433bbf13948e4cb512575ba7f648dbcf5c", "size": 103182, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/futu_portfolio_context.py", "hash": "a45b1f5053223f1f319b6610e490c600143c2323bef03b3d183a927e3d4b4bd8", "size": 37475, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/api.py", "hash": "fa2e091c9eecd4907fd4c6025a0e4fc0e261e931de6e2798cc2ebd8b7c336c91", "size": 20918, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/combo_reconciliation.py", "hash": "435e4ee92750cf2f9160772a2b3285e1d907c614bef54e0950ecaa9ebeea4bbc", "size": 49508, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/projector_implementation.py", "hash": "39c117efbe1bd09ac6f53f9a37f4657d0dd8500924c5a6a88023ec540fb66404", "size": 12617, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/repository_core.py", "hash": "dfc8fb4b48e7c8644aacf314d7e88f5469cbaee5340c04af61a9ee8c70f39df3", "size": 33577, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/repository_schema.py", "hash": "fbf88fffb13291f3523c50ce9b6285eefb7d045faeb36f091dbe7a1c08d6ebb3", "size": 6858, "type": "file", "mode": "0o644", "classification": "required-correctness/safety", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/trade_attribution.py", "hash": "292264589e4cb6d981a741fa254b7126715ca4aba602f9599c2bf0dc61ab6a1c", "size": 18137, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/trade_attribution_migration.py", "hash": "858d7d464b25c2fb88f6a0afa5055f864450d362eff9c83c35df0908ad155077", "size": 6851, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/ledger/wheel_trade_companions.py", "hash": "d762e0533626044840f97b69fb9b4e742079881ccf1bf7b3423c1ecbb027417b", "size": 30163, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/required_data_prefetch_planning.py", "hash": "3638a1fd14af68d80675925fd27fa38d60bc5cede52d7b6b15fd7a3bbade8913", "size": 37712, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/trades/attribution.py", "hash": "ba745272bfc11c0c9ed28e20115dbebe6ebbb9b3825b1204379c8c93280eb89d", "size": 42641, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/trades/auto_intake.py", "hash": "4f141bf89a80d93b86647403c3c6a975f637538616c3a6345e00661150f2c47f", "size": 155558, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/trades/combo_reconciliation.py", "hash": "8c8dcb6e6818a4695d49abbf443cd9cd286ad2d043172b7d9365028bf0a5def2", "size": 6407, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/trades/inbox.py", "hash": "27cd1a96cffdf7d2e3c5c865a84e59ad005447cfa1391f5b3efb18d77474c0f4", "size": 150644, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/trades/receipt.py", "hash": "68ddb665e5ce7f38b4077ea289f3413eae4bbf5a0010e3ffa2ee29e16eabc8d4", "size": 43263, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/trades/resolver.py", "hash": "2737258360715e38d143bccfdbb9ecbcb5180d007a783d5b363214cd9618312d", "size": 36376, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/wheel/capacity.py", "hash": "229994ef895864fc6a71204cb09b9ecfae1192a4e664d093c1a4ff3a5893aa84", "size": 52517, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/wheel/read_model.py", "hash": "a1b0453c003dbe9408267aecaefddbe37df26ba8a11671305f9a3aadbf09f838", "size": 11172, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/wheel/scanning.py", "hash": "d9b2b8e702c8448cdd6ebc4dd330200488a4b2decce6fc8df6f0e9da22673f07", "size": 31349, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/application/wheel/workflows.py", "hash": "af8cef100128fdd017ef458ec758e910370454eac71f57f29da37db3996b703b", "size": 129474, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "src/interfaces/cli/run_ops.py", "hash": "f3852a6a89f575b58580ad554d02ae302bb1133fb6b5eec223a2f29bb6c97664", "size": 10100, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/ledger_sqlite_test_support.py", "hash": "eab1b2187d67aadce341801b35c76b4668e8d6210472f256eeeb48a048457531", "size": 281, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_architecture_guards.py", "hash": "5da5919a19f095ceacb65d1acb329b64d6296013752c2cf12a849e46feb15dc2", "size": 22031, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_attribution_operations.py", "hash": "3445d84b111f83f6192bf7e9d08c9318aeb7d6d1676d683a59518d7f73a54a48", "size": 25601, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_bot_output_contract.py", "hash": "958df38fe0045fd34e6dae492d5c542cef391a352b32396a0592af890a1f2c05", "size": 5260, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_bot_phase1.py", "hash": "c9374d6f8ff7bb382f215ca03732a77364ea7bd23d0efae6bdad763d4b36559f", "size": 31298, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_cash_conversion_backfill.py", "hash": "99a4bb17238dd25c1df35e6e51648ffa7584f623fc962cf1080091a4c4eb8344", "size": 21647, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_cli_operator_commands.py", "hash": "fef606d9d62eff55bf0638a6e03a45d428f8dc3350606f801657b9a5a36e6b80", "size": 50838, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_combo_reconciliation_application.py", "hash": "af396145999954317792aaa053cd62c644c49deae6e974a146219e9790565d7a", "size": 18758, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_combo_reconciliation_domain.py", "hash": "e68728bac0aecc7a419a5206b3e0c96174d31a6f6c2547186fef708166bd89b6", "size": 9288, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_combo_reconciliation_repository.py", "hash": "710d7ec4ae8d4df170d7b3ee0f9a66009e9768459440d0fe1f25167f38e7b0e0", "size": 3861, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_daily_decision_brief_renderer.py", "hash": "fa82a8c6e7969f62a8f0f4262e69a604dbf6eb9d1ccfa2f4f6e5bf249423ac3f", "size": 57660, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_daily_decision_brief_service.py", "hash": "f3ddc635088b59c5fba3ac3c755b1a4aaa1f1bb90afc513f056c4278a4aa2d9d", "size": 101761, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_futu_portfolio_context.py", "hash": "7bd315276604b7d38103d8f5675b3de4009ab5b2a1538abbbbae8b01348e08ca", "size": 29164, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_inbound_control.py", "hash": "68f14c0a0563dac7aa4961acd0017d487a86dff60a10305c4eb0f459c3104c85", "size": 170649, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_lifecycle_redesign_contracts.py", "hash": "eaea218c5379677a8f5c864f30323f7eb8cc0b3070d427765c1defb8ab6929f9", "size": 68380, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_lot_identity_migration.py", "hash": "daf76b66ab48b22549e0533d086762b9722c6eb37d6b310c6a29b1681cbef085", "size": 110037, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_lot_parity_probe.py", "hash": "4e1aea974ee56f0ef59b27ba273e6c32043bd4419202135b5e17d4e1d8c96c6a", "size": 99356, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_position_projection_facade_inventory.py", "hash": "07e6ae5a46424de45590d4af8f4ddbde6b80c7d0b88fd0dcb1147c5233c65258", "size": 11081, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_projection_verify.py", "hash": "7cef396912f072d883e076a6ee907f04f7f1e6f93c1b8c1ca9f49a5a472280e5", "size": 15425, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_required_data_fetch_planning.py", "hash": "f59581e6b0648a90b6afac28468039f15aed6a4c85e1c5a1b82b822eaefe12c4", "size": 56424, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_attribution.py", "hash": "d0b1cfa1050b14b84bb388ed825992670613e23181d2216e0df85588aa30f386", "size": 7875, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_attribution_migration.py", "hash": "8f1d662d13dd987b0829b292c370a46fde8ee1a962cec91ea760f2cef9705029", "size": 7958, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_attribution_view.py", "hash": "cb143d9676c1bf016e347ebdd253e0afba7f38b956509ba9b9d271c1dfd689ee", "size": 34161, "type": "file", "mode": "0o644", "classification": "planned", "status": "untracked", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_contract_identity.py", "hash": "05f5a6482be19684d27b8daf959c8b8ee15a7bc9c3b64ab2a43c9829d029b0a6", "size": 62905, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_event_pagination.py", "hash": "7ff3f29609b0b96846d92820c4a77ceb9b3f1b82fcdfb7d395d78155687768a8", "size": 45682, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_events_cli.py", "hash": "fda4337e8b3d79a7b3a1883582238c6fa5fbedf9a165a33dd5c4284f6f723889", "size": 60589, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_intake_recovery.py", "hash": "77ee6c77e4d468969b65238964d30abb40c8e26c247903cdb4ec995bb98097a9", "size": 90344, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trade_receipt_readback.py", "hash": "74f540de1994b60047b850e72e7a62add1cc9872ee6bb5177612771746d4ba3f", "size": 9487, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trades_combo_reconciliation.py", "hash": "8d8495ea46f8a732557898934212f94f26fd9c6db38e0af0f965a5ecc081a2c1", "size": 10792, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trades_receipt.py", "hash": "f3ccb047d6309c951f27211ed232e45d8b8bfcf121af2fd6e96d2dbab15746db", "size": 24994, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trades_resolver_close.py", "hash": "8906d9d9381aa87456dc29bf72a474bbdee3c9a910b018feab85131992e2866a", "size": 66479, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trades_resolver_open.py", "hash": "0fb21261bc8bbab6bb48a92531e4c047409b1c402a37c5512b3e0fdcdc7f2297", "size": 22196, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_trades_state_reconcile.py", "hash": "804c820d91e1759cf95f14a35ab22451a6bf4f355d3d5f18d80443572620d8eb", "size": 81012, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_wheel_intent_policy_binding.py", "hash": "f8bf010a62ed118b7d7b5e392229dbec04ca3c2b45069d2b6a1751b0d1069408", "size": 18076, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_wheel_scanning.py", "hash": "f2f8e54025375b2758ebc5ef55e13648b1379bd239436c343fea9969ddb248af", "size": 33903, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_wheel_strategy.py", "hash": "526c443cb92d64226376e48e049076e327745b57dd5e6a3563f92651a3f0a782", "size": 32007, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}, {"path": "tests/test_wheel_tick_integration.py", "hash": "a124307d3ea77a2885620e06e7810b1a0cb706bdaf433779e23f4baea07a5428", "size": 8075, "type": "file", "mode": "0o644", "classification": "planned", "status": "modified", "evidence_ref": ".devflow/scope.md#review-3-correction-checkpoint"}]
content_revision: 8
planreview_round: 3
deepreview_round: 4
in_flight: []
evidence_paths:
  - docs/reviews/code-review-20260922-235504.md
  - src/application/trades/auto_intake.py
  - src/application/trades/inbox.py
  - src/application/trades/resolver.py
  - src/application/trades/combo_reconciliation.py
  - src/application/ledger/combo_reconciliation.py
  - src/application/ledger/wheel_trade_companions.py
  - src/application/wheel/workflows.py
  - src/application/wheel/capacity.py
  - domain/domain/strategy_membership.py
blocking_findings: []
residual_risks:
  - item: 生产 activation 与 provider 容量字段口径未验证
    classification: assigned-to-later-work-unit
    owner: Wheel lifecycle 与 capacity owner
    destination: 后续另行授权的生产迁移、启用预览与读回；B 源码和隔离验收已完成

## 设计阶段工作区保护（历史记录）

当前在 main，有用户既有文档、技能及 codex 研究目录改动；不覆盖、不清理。
本次只修改 scope、唯一设计 owner 和 workflow 过程证据。
实现阶段若获授权，需重新核验远端基线及隔离工作区；本次不创建实现 worktree。

## 后续澄清与授权差异（保留原始记录）

- 用户澄清：现在是自动启动的；订单不会带策略信息。原每轮确认建议撤回，不新增确认步骤。
- 用户要求：把现在代码的问题都找出来，设计优化方案。范围按本对话的完整交易链路理解，不扩展为无关全仓重构。
- 设计采用 active 分支规则匹配，并记录该方案为拟实施规则；不扩大 internal 分支激活权限，不实施。
- 原 Brainstorm 待答项已被上述流程澄清取代；原残余风险转交设计中的迟到证据与生产 activation 边界。
- 本轮源码检查纠正了此前平仓无分配规则的说法：已有 strict_exact_fifo，保留。
- 验证：197 passed in 20.26s；五个相关基线测试文件见设计正文。

Panel 输入快照：docs/reviews/trade-attribution-design/panel-input.md sha256:3447339db6473f7b07a5a93967127a65c83ce91829f23afac7a1397e2e5f1a16

Panel 第四路：已完成；与前三路使用同一原始快照。

## 前序回合修订与验证记录（历史状态）

- 原快照四路要求未改变；3 份可用结果已合批处理，未冒充完整通过。
- 第四路未创建；本轮无 in-flight 任务，不重复派发相同失败。
- Planreview 尚未开始，不把主 agent 修订视为 adversarial gate。
- 设计中的 F10 已用隔离 SQLite 复现 partial exposure 仍会自动采用，补入 A。
- 新治理事件与最小启用记录已补充 owner、schema、迁移和验收；修改业务代码仍未授权。
- evidence: docs/reviews/trade-attribution-design/evidence.md
- adjudication: docs/reviews/trade-attribution-design/panel-adjudication.md
- 文档 git diff --check 通过；文档措辞与敏感产物 guardrails 结果见最终执行记录。
- 本 scope 与设计均未暂存、未提交。原用户工作区改动保留。

## 本次继续

- 第四路 `/root/design_panel_4` 已创建，读取同一不可变原始快照；旧线程限额阻塞已解除。
- 仍等待第四份结果；Planreview 调用次数尚未增加。

- 本次第四份结果已裁决；新增 F11 和 mode=off 竞争语义，content_revision=3。

- Planreview 1: docs/reviews/plan-review-20260922-184245.md，结论 fail；P1 迁移顺序、P2 提交前 freshness/取消，均待合批修订。

- P1/P2 由 Improve Design 合批采纳修订；增加 drain 顺序及等锁/取消反例，待完整重审。

## 前序 revision 4 冻结（本轮补充前）

- content_revision: 4；四份 Panel 建议全部裁决；改后设计完成第 2 次完整 Planreview，pass-with-risks。
- reviewer_backend=native-subagent；reviewer_model=unknown；independence=unverified。
- 最终 Planreview: docs/reviews/plan-review-20260922-184436.md
- 无未决 blocking finding；生产 activation/provider 口径等风险已指定 owner 和 B 验证入口。
- 文档 diff 检查与措辞/敏感产物 guardrails 通过；业务源码未变，复用 197 passed。
- 未创建实现 worktree，未修改业务源码/生产状态，未暂存或提交；用户既有改动保留。
- 本授权序列 Brainstorm → Save Design → Improve Design 已完成，不自动进入 Impl。

## OM Bot 补充授权与继续

- S6：统一 OM Bot 查看待归属、选择、预览确认、校验/写入/回执，不绑定渠道；仍由 Control 执行，模型不获写权限。
- 沿用 full 路径及原始非目标，不新增实现授权；planreview_round 保持 2，不清零。
- 新完整 Panel 输入：docs/reviews/trade-attribution-design/bot-panel-input.md sha256:9f14f5339a620454c42cd3c2eaadb27f76d2c724f606bcc032d99cdce1ad08cb。
- 复用既有 Control/operation store；无新增审批数据库、渠道专属 UI 或跨渠道身份合并。

- 本轮复用四个彼此未共享结论的既有 reviewer；前三路已重新派发，第四路等待并发槽。原命名 DeepSeek 不支持当前账户的证据仍有效，不重复失败派发。

- Bot 扩展四路结果已全部回收并裁决：docs/reviews/trade-attribution-design/bot-panel-adjudication.md；content_revision=6。
- S6 在设计 B 验收落点明确，C 承接提示；F12/F13 隔离 SQLite 复现，文档检查通过。

## OM Bot 补充最终冻结

- content_revision=6，design_ref=docs/FUTU_TRADE_HOLDINGS_SYNC.md sha256:71c3d4626a88468b62451323f098c85d0e2ac2776b54d06923dc5e22f5276304。
- Bot Panel 4/4，全部建议裁决；最终完整 Planreview 第 3 次：docs/reviews/plan-review-20260922-201918.md，pass-with-risks。
- reviewer_backend=native-subagent；reviewer_model=unknown；independence=unverified；最终 Planreview 由主 agent 执行。
- 无未决 blocker；全部 residual risk 具备 owner/destination，见设计剩余边界和本轮 review。
- S6 属后续明确授权，未改原始 S1–S5；六项成功信号均落在三切片，authorized_slices 仍为空。
- 仅设计与过程文件修改，未改业务代码、生产状态、工作树或交付状态；既有用户改动保留。

## Impl 授权与冻结基线

- 用户原话：“进入impl”；覆盖已冻结设计 A/B/C 的实现、测试及 workflow 必需 Review，不包含提交、推送、发布、部署、真实通知或生产改账。
- 原设计阶段 non_goals 中“不实现代码”由本次明确授权解除，其余边界保持；原授权记录保留不改。
- 实现基线相对设计仅新增升级器/依赖图提交，交易与 Bot owners 未变化；无其它任务提交混入。
- S1–S6 与三片授权映射闭合，无 orphan。
- A owners: trades/resolver.py、combo_reconciliation.py、receipt.py 及对应 tests；验证相关 pytest 与 intake facade。
- 原 main 的用户改动不迁入，仅复制本任务设计与过程证据；scope owner 已迁到本 worktree。

## Slice A checkpoint

- done: true
- validation: 220 passed in 18.57s；resolver open/close、combo reconciliation、receipt、intake recovery、trade execution input。
- 旧“无 effect 猜 Buy Call 开仓”测试改为显式 open；新增公开 intake 无 effect 不写经济事件，以及未来 lot 不被平仓的反例，保留原 crash/recovery 数量和发送断言。
- inventory: [{"path": "docs/FUTU_TRADE_HOLDINGS_SYNC.md", "hash": "71c3d4626a88468b62451323f098c85d0e2ac2776b54d06923dc5e22f5276304", "size": 117029, "type": "file", "mode": "0o666", "classification": "planned"}, {"path": "src/application/trades/combo_reconciliation.py", "hash": "f882bec37db32749f0cf3648ca7c4f5908b723db0ce0ff8ea8c36273cce43da3", "size": 5545, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "src/application/trades/receipt.py", "hash": "70f12916438535da3721ba451050b81836f2c07dfebdbd5679ab62401683a784", "size": 41436, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "src/application/trades/resolver.py", "hash": "2737258360715e38d143bccfdbb9ecbcb5180d007a783d5b363214cd9618312d", "size": 36376, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "tests/test_trade_intake_recovery.py", "hash": "77ee6c77e4d468969b65238964d30abb40c8e26c247903cdb4ec995bb98097a9", "size": 90344, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "tests/test_trades_combo_reconciliation.py", "hash": "c81fb3a860003cda5e198ffcb955ee78b55fa6fed63d9b4063e67284ec063421", "size": 9069, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "tests/test_trades_receipt.py", "hash": "b6ba682606869077888f1332b7ec93633d243cd9d43afa2c26b59dba84afaf08", "size": 24112, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "tests/test_trades_resolver_close.py", "hash": "8906d9d9381aa87456dc29bf72a474bbdee3c9a910b018feab85131992e2866a", "size": 66479, "type": "file", "mode": "0o644", "classification": "planned"}, {"path": "tests/test_trades_resolver_open.py", "hash": "0fb21261bc8bbab6bb48a92531e4c047409b1c402a37c5512b3e0fdcdc7f2297", "size": 22196, "type": "file", "mode": "0o644", "classification": "planned"}]
- diff_fingerprint: eca8bf95f02a0b0ff0e672fa310da48f52463d48e5571d0ea07944541ea7333f
- B owners: domain strategy_membership、Wheel projection/capacity/workflows、ledger 与 trades attribution、assistant Control/Bot；按冻结设计实现。

## Slice B 中间检查点（未完成）

- 已实现：普通单腿确认及 claim 恢复；只读账户快照；显式启用与受控迁移；旧 writer 写入屏障；交易类型/策略归属区分；Wheel/Combo 同快照候选裁决、历史分支与账户隔离、竞争成交容量检查；共享事务内绑定及提交前取消；分钟恢复与 Inbox 结果缓存入口。
- 新增 provider 容量观察使用可终止子进程，单账户 10 秒预算；仍待超时/取消专项测试。
- 已验证：134 passed in 16.05s（intake recovery / Combo / Bot ordinary / view / portfolio）；随后物理账户配置校验新增，view 4 passed in 0.60s。此前 Wheel/Combo owner 73 passed in 3.40s。
- 仍待完成：exact intent 历史消费与 reservation 转换；Bot Wheel/Combo 的完整预览/确认/恢复；迟到冲突持久化与解除读回；新旧自动路径切换和 Inbox 并发反例；迁移后 projection fingerprint/readiness 验收；CLI 管理入口测试；语义 hash 时间稳定性与完整隔离证据。
- C 尚未开始；Review 尚未开始，deepreview_round=0。当前改动为未完成实现，不可作为发布或生产启用依据。

## Slice B checkpoint

- done: true；304 个账户/归属/迁移/并发/意图/Control/交易入口测试通过；新增真实 Bot Scene 最终 8 passed in 1.05s（先前失败为测试 tool_call ID 复用及错误文本断言，已修正）。
- 共享原子 writer、精确意图部分消费、过期后的历史有效成交、双 writer 排他、provider 超时/取消、CLI 启用、迁移强制重投影与旧 writer fence 已验证。首次回执允许 pending，分钟恢复更新归属且不重发原回执。
- required-correctness/safety: bot/host.py 原本忽略 control_preview_specs，S6 无法从真实模型入口请求预览；现在只允许 catalogue 的 preview capability，模型不能 confirm，tests/test_attribution_operations.py 真实 Scene 验证。
- 生产 provider 字段/activation 状态仍须受控上线验收；本次只使用隔离 fixtures，不启用生产规则。复杂人工纠错按冻结设计停在受控人工修复，不新增自动撤销后继事件命令。
- C owners: domain Wheel projection、wheel/read_model/scanning/capacity、Daily Brief service/renderer；验收：双向数量状态、部分覆盖剩余扫描、零张余股、价格缺失阻断及同源报价。

## 最终回归修复记录

- 旧隔离测试的裸 SQLite 连接显式参与当前 writer 协议，不削弱生产写屏障；复用 tests/ledger_sqlite_test_support.py，归属 planned migration 验收。旧 migration 必须保留新增三条屏障，校验完整 guard 集合而非豁免。
- schema helpers 移回 repository_schema，移除 repository_core→trade_attribution→repository 循环；归属 required-correctness/safety，dependency graph cycle 检查是直接证据。
- 更新真实新表面的 Bot/CLI/tool/projection inventories 与部分覆盖 fixtures；旧无证据买 Call 不再开仓，历史 enrichment fixture 显式构造已入账 open。
- 文档与依赖图派生产物同步；生产 provider 字段和 activation 状态仍留部署受控验收，owner: Wheel/capacity 维护者，destination: 后续明确授权的升级/enable 预览与读回。

## Final Impl checkpoint / Review input

- C done=true: focused coverage/price 243 passed; final full suite 7618 passed, 2 skipped, 1 warning in 158.94s; isolated loopback tests 4 passed in 2.97s. Total 7622 passed.
- Lint, guardrails (wording/runtime config/sensitive/public surface), dependency graph zero cycles, standalone smoke, om-agent spec and diff whitespace checks passed.
- Signal closure: A -> S1/S2/S4; B -> S1/S2/S3/S6; C -> S3/S4/S5. No orphan.
- Inventory: 84 files excluding this scope; design SHA and HEAD/base unchanged. No commit or production changes.
- Review round 1: parent covers Wheel quantities/scans/notifications/docs; scoped reviewers cover ledger/schema/domain ownership, intake/recovery/capacity/intents, and OM Bot/Control/permissions. All untracked files included.

## Review 1 result

- Artifact: docs/reviews/code-review-20260922-224632.md; fail. R1-R6 accepted blocking correctness/safety findings, R7 public-doc correction.
- Additional owner required: assistant/inbound_service.py (R1/R2 actual facade), Bot host downgrade, coverage unknowns, scoped Combo evidence/rejections; each maps to S1/S2/S4/S6. No new product scope or side effects.
- Migration populated history/rollback/persistent old connection acceptance is not yet proven; add isolated tests.

## Review 1 correction checkpoint

- R1–R7 corrected in the authorized Impl scope. Regression: 62 targeted passed; full suite 7633 passed + 4 isolated loopback passed, 2 skipped.
- After full-suite collection, strengthened only the migration fixture to remove all new writer fences before upgrade and prove the same old connection succeeds before / fails after migration; final 6 migration tests passed. Production source was unchanged during full validation.
- Migration tests preserve populated trade/Wheel/activation facts, inject after table rebuild/reprojection/readback, verify pre-commit rollback and post-commit durable state with verified private backup.
- Lint, guardrails, dependency graph (zero production module cycles), standalone smoke and whitespace passed. Agent spec contract unchanged from the valid slice C check.
- Validation artifacts: /private/tmp/om-full-corrected.log, /private/tmp/om-loopback-corrected.log, /private/tmp/om-corrections-targeted.log, /private/tmp/om-migration-corrected.log, /private/tmp/om-smoke-corrected.log.
- Additional files assistant/inbound_service.py and ledger/repository_schema.py are required-correctness/safety fixes at existing owning boundaries; no delivery or production action.

## Review 2 result

- Artifact: docs/reviews/code-review-20260922-231319.md; fail. Five accepted blockers, all within S1/S2/S4/S6 and original owners. No scope or permission expansion.

## Review 2 correction checkpoint

- R2-1: capacity uses all physical-account market obligations and intent reservations; pooled Put cash requires complete US/HK snapshot coverage; unknown active reservations fail closed. Candidate matching stays market-scoped. Same facts/model used in precommit verification.
- R2-2: only the call winning previewed-to-confirmed CAS may apply. Already claimed and CAS-loser calls read back or recover without gaining write permission.
- R2-3: rejected Combo members suppress only their exact already-rejected counterparts; new unequal-quantity counterparts retain competition.
- R2-4: legacy Combo auto adoption gates evidence by each inference market/date.
- R2-5: V2 historical invalid intent multiplier preserves unknown reserved shares at projection owner; display cannot infer a known value from branch multiplier.
- Focused checks: 93 passed plus final cross-market view/writer suite 15 passed. Full suite 7644 passed + 4 isolated loopback passed; 2 skipped. Lint, guardrails, graph (zero production module cycles), smoke and whitespace passed. Agent spec metadata unchanged from valid prior check.
- Evidence: /private/tmp/om-full-r2fix.log, /private/tmp/om-loopback-r2fix.log, /private/tmp/om-r2-added.log, /private/tmp/om-r2-crossmarket.log, /private/tmp/om-smoke-r2fix.log.
- No design, scope, delivery, production, or external-notification expansion.

## Review 3 result

- Artifact: docs/reviews/code-review-20260922-233720.md; fail. R3-1 through R3-4 accepted. Existing assistant renderer/permission-request owners required for S6 usable confirmation; no product or authority expansion.

## Review 3 correction checkpoint

- R3-1: every active relevant capacity branch must have trusted integrity, even with empty active intent IDs. Canonical projection keeps conflicted intent reservations unknown.
- R3-2: residual delivered Combo exposure survives partial close; full-pair adoption eligibility remains strict.
- R3-3: pending and structured permission hints generate the attribution command family; exact commands roundtrip through the public facade for confirm and cancel.
- R3-4: actual preview text renders every affected leg, contract, side, quantity, price, gross premium, current/target membership, warnings and canonical identities. Existing assistant renderer owns formatting.
- Focused 182 passed; full 7649 passed plus 4 isolated loopback passed, 2 skipped. Lint, guardrails, graph (633 modules, zero cycles), smoke, whitespace passed. Existing agent spec metadata check remains valid.
- Evidence: /private/tmp/om-r3-focused.log, /private/tmp/om-full-r3fix.log, /private/tmp/om-loopback-r3fix.log, /private/tmp/om-smoke-r3fix.log.
- Existing renderer.py and permission_request.py added as required-correctness/safety owners for S6; no design, product scope, delivery, production or notification expansion.

## Impl / Review completion

- Approved A/B/C and S1–S6 complete; Deepreview 4/5 pass-with-risks, no remaining blocking finding or in-flight work. Counters and previous review evidence retained.
- Final artifact: docs/reviews/code-review-20260922-235504.md. Reviewed source inventory: 89 files plus this workflow owner. All reviewed source hashes remain unchanged; only this completion record differs from the review input.
- Validation: 7653 passed total, 2 skipped; lint/guardrails/zero-cycle dependency graph/smoke/whitespace passed. Evidence details in the review artifact.
- Frozen design SHA and base/HEAD unchanged. Existing design-stage non-goal against implementation is superseded only by the recorded Impl authorization.
- Implementation workspace remains /private/tmp/om-trade-attribution on codex/trade-attribution. No source delivery or production action; preserve this dirty unmerged worktree for future authorized delivery.
- Remaining production/provider/channel acceptance belongs to separate rollout; future conflict-resolution recurrence belongs to any future formal resolver write surface. No unfinished authorized implementation work.
