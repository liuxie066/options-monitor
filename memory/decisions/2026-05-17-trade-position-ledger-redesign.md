# Context

auto-close-expired missed a same-expiry position and manual close resolved to a different `record_id`. Investigation showed the risk is structural: v1 `position_lots`, v2 compatibility projection, and facade-based read paths can expose different position identities.

# Decision

交易与持仓模块进入新账本重构路线，不继续把 v1/v2 混合模型修成 canonical。

新设计以独立 trade / position ledger 为目标：

- `TradeEvent` 是 append-only 事实源。
- `PositionLot` 是 lot 级可写状态投影。
- `RiskPositionView` 是风控只读聚合视图。
- `position_key` 只能用于聚合展示，不能作为写入目标。
- close / auto-close / manual-close 必须解析到确定 `lot_id`。
- 身份不唯一或投影不一致时，风控和写路径 fail closed。

# Pattern

先做低风险计划和 shadow replay，再切读路径，最后切写路径。旧 v1/v2 模块只作为 migration adapter 和历史对账输入，不能继续向写路径提供可写 `record_id`。

设计文档：`docs/TRADE_POSITION_LEDGER_REDESIGN.md`。

# 2026-05-17 Progress

已完成 Phase 0-3 的安全边界：

- auto-close 写路径候选改为当前 writable `position_lots`，不再经由 v2/facade compat records 取可写 `record_id`。
- legacy close matcher 增加完整 contract identity 校验：broker/account/symbol/option_type/side/strike/expiration。
- 新增纯 domain `domain/domain/ledger/`，包含 `ContractKey`、新 `TradeEvent`、`PositionLot`、`RiskPositionView`、projection 和 invariant diagnostics。
- 新增 `src/application/ledger/` shadow replay adapter，可只读导入旧 `trade_events` / `position_lots` 并生成 reconciliation report。
- `option_positions_context_builder` 增加 ledger shadow 校验；可投影身份冲突时清空 `open_positions_min` 并标记 `ledger.fail_closed=true`。
- `runtime_status` 暴露 `option_positions_context.ledger` 和 summary 级 `ledger_status` / `ledger_fail_closed`。

仍未完成：

- manual close / trade intake / auto-close 全量切到新 ledger service。
- Feishu mirror 改为新 ledger read model 的外部镜像。
- 删除 v2 compat target resolver 和旧 facade 写前候选能力。

## Phase 4 Partial Progress

manual close 已切到 `src.application.ledger.service` 过渡层：

- `preflight_manual_close` 从当前 writable `position_lots` 构建 shadow ledger projection。
- 非重复写入前校验 target `lot_id` 存在、仍 open、contract identity 与当前 record fields 一致、close 数量不超过 open 数量。
- projected close event 会先在新 ledger domain projection 中回放，失败则 fail closed。
- idempotent retry 先用旧 stable event_id 识别 duplicate，避免要求已关闭 lot 仍出现在 open projection。
- 实际持久化仍暂时委托 `persist_manual_close_event`，v2 append / refresh 仍作为兼容后置步骤。

剩余 Phase 4：

- manual open、auto-close expired、trade intake open / close、adjust / void / repair 继续切到同一个 service。
- 写入成功后的 canonical projection publish 仍未替换旧 `position_lots` rebuild。
- v2 compat 和 Feishu mirror 还不能删除。

## Phase 4 Additional Progress

auto-close expired 和 trade intake close 已继续收口：

- `auto_close_expired_positions` 在 `persist_expire_auto_close_event` 前调用 `preflight_expire_auto_close`。
- auto-close 的 ledger preflight 会校验 projected `expire_close` 事件只能关闭当前 target lot，不能跨 strike / expiry / account。
- trade intake close 候选源优先使用当前 writable `position_lots`，不再优先走 facade compat records。
- 真实 repo 下 broker close 会按 FIFO match 拆成逐 lot close events，并在 raw_payload 写入 `record_id` / `target_lot_id` / `close_target_source_event_id` / `source_deal_id`。
- 不支持 `position_lots` 的 legacy/fake repo 仍走旧 aggregate close fallback，便于兼容测试和非 canonical adapter。

剩余 Phase 4 更新：

- manual open、trade intake open、adjust / void / repair 仍待切到 ledger service。
- close 写入仍通过旧 `_persist_trade_event_object` 发布 `position_lots`，不是最终独立 canonical store。
- v2 compat、Feishu mirror 和旧 facade 仍未退休。

## Phase 4 Open Cutover Progress

manual open 和 trade intake open 已接入 `src.application.ledger.service` 过渡层：

- 新增 open preflight：从当前 writable `position_lots` 导入 shadow ledger projection，再追加 projected open event 验证 `lot_id` / `position_key` / contract identity。
- `execute_manual_open` 在真实 repo 下通过 `persist_manual_open_event_with_ledger` 写入，并返回 `ledger_preflight`；duplicate retry 先按 stable open event_id 识别，不重复写 lot。
- `apply_trade_open_with` 在支持 `position_lots` 的 repo 下通过 `persist_trade_open_event_with_ledger` 写入；不支持 canonical read model 的 fake/legacy repo 继续走旧 fallback。
- open 写入仍暂时委托旧 `persist_manual_open_event` / `persist_trade_event`，但写入入口已经统一到 canonical ledger preflight。

剩余 Phase 4：

- adjust / void / repair 仍待切到 ledger service。
- 写入后的 canonical projection publish 仍未替换旧 `_persist_trade_event_object`。
- v2 compat、Feishu mirror 和旧 facade 写前候选能力仍未退休。

## Phase 4 Intervention Cutover Progress

adjust / void / repair 已接入 `src.application.ledger.service` 过渡层：

- `execute_manual_adjust` 通过 `persist_manual_adjust_event_with_ledger` 写入，先校验 target lot 仍 open、`record_id` 与当前 fields / shadow projection 身份一致，再投影 adjust event。
- trade-event `void` / `repair` 的 dry-run 和 apply 都会先追加预览事件到新 ledger shadow projection；projection 有 error 时 fail closed，不先落库。
- `src.application.trade_event_review` 的 `preview/apply_void_trade_event`、`preview/apply_repair_trade_event` 已返回 `ledger_preflight`。
- `om option-positions void-event` 也改为走 ledger wrapper。
- `src.application.ledger.migration` 补齐 legacy `adjust` / `void` 事件导入，避免事件干预预检无法回放已有调整/作废历史。

剩余 Phase 4：

- 写入后的 canonical projection publish 仍未替换旧 `_persist_trade_event_object`。
- v2 compat、Feishu mirror 和旧 facade 写前候选能力仍未退休。

## Phase 4 Publisher Cutover Progress

写入后的 `position_lots` 发布路径已切到新 ledger projection：

- `domain/domain/ledger/PositionLot` 补齐 adjust patch 语义：contracts / strike / expiration / premium / multiplier / opened_at 会进入 canonical lot state，不再只是记录 `last_event_id`。
- 新增 `src.application.ledger.publisher`：使用新 `TradeEvent -> PositionLot` replay 生成 legacy-compatible `position_lots` records，供现有 SQLite / report / sync 表面继续读取。
- `rebuild_position_lots_from_trade_events`、`_persist_trade_event_object`、manual repair publish、bootstrap materialization 和 `trade_event_review` dry-run 已改用新 publisher。
- legacy 无 target close 只在 publisher migration adapter 内做 FIFO 拆分，并生成 `close_unmatched_contracts` 诊断；新写路径仍通过 ledger service 生成显式 `target_lot_id`。
- Feishu / legacy option_positions bootstrap 的弱字段快照只作为外部迁移输入透传；本地 `sqlite_position_lots` 快照无法 canonical import 时继续失败并回滚。
- `option_positions_facade.load_option_position_records` 默认优先读取 canonical `position_lots`，只有没有可用 lots 时才回退 v2 compat records，避免 v2 抢占普通读路径。

验证：

- `python3 -m pytest tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：332 passed。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_facade.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/interfaces/cli/trade_events.py src/application/option_positions_context_builder.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py`：passed。
- `git diff --check`：passed。

剩余 Phase 4：

- v2 append / refresh 仍作为 post-write compatibility receipt 存在，下一步要改成只读/显式诊断入口，不能继续作为默认写后物化链路。
- Feishu mirror 仍需明确标注为外部镜像，并从 canonical publisher/read model 同步。

## Phase 4 V2 Post-Write Retirement

v2 已从默认写后链路中移除：

- `position_workflows` 的 manual open / close / adjust 不再 append v2 native event、不再自动写 verification snapshot、不再刷新 v2 projection。
- `om option-positions void-event` 不再在 apply 后写 v2 verification snapshot / refresh v2 projection。
- 写操作结果保留 `v2_result={"mode":"retired","reason":"post_write_v2_projection_disabled"}`，明确说明该副作用已关闭。
- 显式 `option-positions rebuild` / `reconcile` 和 `option_positions_v2_service` 仍保留为诊断/迁移入口，不再参与默认写入后的 canonical 状态物化。

剩余 Phase 4：

- Feishu mirror 仍需从 canonical read model 明确同步，并在文档/接口上标注为外部镜像而非事实源。

## Phase 4 Feishu Boundary Cutover

Feishu `option_positions` 已从默认事实源候选中移除：

- `load_option_positions_repo` 在空本地库时不再因为配置了 Feishu table 就自动读取远端 `option_positions`。
- 只有 data config 显式设置 `option_positions.bootstrap_from_feishu.enabled=true` 时，才允许执行 Feishu bootstrap。
- 默认空库会返回 `sqlite_only_feishu_bootstrap_disabled` 或 `sqlite_only_no_feishu_bootstrap`，明确本地 `trade_events` 才是事实源。
- Feishu bootstrap 仍保留为一次性迁移入口；启用后配置缺失 / 读取失败仍会 fail fast 或 degraded。
- `option_positions_feishu_sync` 的 last-run 结果增加 `source_of_truth="canonical_position_lots"`、`mirror_kind="feishu_option_positions"`、`mirror_only=true`。
- 文档和 portfolio data-config 示例同步改为“显式 bootstrap / mirror”语义。

验证：

- `python3 -m pytest tests/test_option_positions_sqlite_service.py tests/test_sync_option_positions_to_feishu.py tests/test_agent_plugin_smoke.py tests/test_webui_symbol_strategy_cleanup.py tests/test_position_workflows_auto_sync.py`：159 passed。

剩余 Phase 4：

- 显式 `option-positions rebuild` / `reconcile` 仍保留 v2 诊断语义，后续需要切成新 ledger native diagnostics 或降级为 legacy migration command。

## Phase 4 Canonical Rebuild/Reconcile Cutover

默认维护命令已继续脱离 v2：

- `om option-positions rebuild` 不再刷新 `option_positions_v2`，改为直接从 canonical `trade_events` 重放并刷新 SQLite `position_lots`。
- rebuild 输出标记 `mode="canonical_position_lots_rebuild"`、`source_of_truth="trade_events"`、`projection="position_lots"`。
- 新增 `src.application.ledger.reconciliation`：用 verification snapshot 对当前 canonical `position_lots` 做对账。
- `om option-positions reconcile` 不再调用 `option_positions_v2_service.reconcile_option_positions_snapshot`，也不再让 verification snapshot 覆盖 projection。
- canonical reconciliation report 持久化到 `output_shared/state/option_positions/current/reconciliation.latest.json`。
- `inspect` 会读取新的 canonical latest reconciliation report；如果 mismatch 存在，只作为诊断证据，后续必须通过 explicit repair / adjust / void 进入账本。

验证：

- `python3 -m pytest tests/test_option_positions_cli.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_legacy_v2.py tests/test_ledger_*.py`：98 passed。

剩余 Phase 4：

- `src.application.option_positions_inspection` 的 projected/history 仍依赖 v2 compat records，下一步应改成直接从 canonical `position_lots` + ledger events 生成 inspect/history。

## Phase 4 Canonical Inspection Cutover

inspect/history 读面已从 v2 compat records 切到 canonical ledger：

- `build_lot_event_history` 直接读取 SQLite `position_lots` 和 `trade_events`，按 explicit record/source refs 与 canonical contract identity 汇总相关 open / close / adjust / void 事件。
- `inspect_projection_state` 不再调用 `load_option_positions_v2_records`，当前状态来自 `repo.list_position_lots()`，projected 状态来自 `project_legacy_trade_events_to_position_lots(events)`。
- baseline snapshot 字段保留为兼容输出，但固定为 `None`；verification / reconciliation 诊断改读 `output_shared/state/option_positions/current/reconciliation.latest.json`。
- 孤儿 close 事件诊断切到新 ledger 的 explicit target 语义，返回 `target_lot_not_found`，不再用 v2 的 `close_without_open_position`。
- `src/application/option_positions_inspection.py` 已无 `option_positions_v2` / `load_option_positions_v2_records` 引用。

验证：

- `python3 -m compileall src/application/option_positions_inspection.py`：passed。
- `python3 -m pytest tests/test_option_positions_cli.py`：16 passed。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/interfaces/cli/trade_events.py src/application/option_positions_context_builder.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py`：passed。
- `python3 -m pytest tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：338 passed。

剩余 Phase 4：

- v2 compat service / tests 仍作为 legacy migration 诊断表面存在，后续需要继续降级或删除默认入口。
- `option_positions_facade` 仍有无 canonical lots 时的 v2 fallback，后续要明确是否只保留为显式 legacy migration 命令。

## Phase 4 V2 Default Read Surface Retirement

v2 已从默认读面和 facade fallback 中移除：

- `src.application.option_positions_facade.load_option_position_records` 不再导入或调用 `load_option_positions_v2_records`。
- 默认 records 读取顺序现在只保留 canonical `position_lots`，以及显式 repo adapter 暴露的 `list_records`；canonical lots 为空时不会再偷偷用 v2 projection 补数据。
- `src.application.option_positions_v2_service` 增加 legacy-only 模块说明：只作为旧 v2 migration diagnostics，不应被默认交易/持仓读写路径 import。
- `tests/test_option_positions_legacy_v2.py` 增加结构性防回归：`src/application` 和 `src/interfaces` 除 v2 service 自身外不得引用 `option_positions_v2_service` / `load_option_positions_v2_records`。
- 新增 facade 回归用例：即使 v2 service 可以从孤立 `trade_events` 投影出 compat records，只要 canonical `position_lots` 为空，默认 facade 也返回空，而不是回退 v2。

验证：

- `rg -n "option_positions_v2_service|load_option_positions_v2_records" src/application src/interfaces`：只剩 `option_positions_v2_service.py` 自身内部引用。
- `python3 -m pytest tests/test_option_positions_legacy_v2.py tests/test_option_positions_cli.py`：26 passed。
- `python3 -m pytest tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：340 passed。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/option_positions_v2_service.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/interfaces/cli/trade_events.py src/application/option_positions_context_builder.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py`：passed。
- `git diff --check`：passed。

剩余 Phase 4：

- v2 domain / repository / service 代码仍存在，但已经没有默认应用路径依赖；下一步可决定保留为短期 legacy migration 包，或在迁移窗口结束后整体删除。

## Phase 4 Legacy V2 Quarantine

v2 进入隔离废弃状态，不再作为默认系统的一部分：

- `tests/test_option_positions_v2.py` 重命名为 `tests/test_option_positions_legacy_v2.py`，测试语义从“v2 正常路径”改为“legacy v2 quarantine diagnostics”。
- `domain/domain/option_positions_v2.py`、`domain/storage/repositories/option_positions_v2_repo.py`、`src/application/option_positions_v2_service.py` 均标注为 legacy quarantine / migration diagnostics。
- `docs/OPTION_POSITIONS_MIGRATION.md` 增加 `Legacy v2 quarantine` 边界：`output_shared/state/option_positions_v2/` 只能用于历史对照和迁移核对，不能用于风控、target resolver、facade fallback 或默认维护命令。
- `docs/TRADE_POSITION_LEDGER_REDESIGN.md` 更新当前状态：canonical state 固定为 `trade_events -> position_lots`，v2 只保留为短期 legacy quarantine，物理删除等待迁移窗口结束。
- 结构性测试继续约束默认 `src/application` / `src/interfaces` 不得 import `option_positions_v2_service` 或 `load_option_positions_v2_records`。

验证：

- `python3 -m pytest tests/test_option_positions_legacy_v2.py tests/test_option_positions_cli.py`：26 passed。
- `python3 -m pytest tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：340 passed。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_positions_v2.py domain/storage/repositories/option_positions_v2_repo.py domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/option_positions_v2_service.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/interfaces/cli/trade_events.py src/application/option_positions_context_builder.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py`：passed。
- `git diff --check`：passed。

剩余：

- v2 代码已可进入物理删除；旧 `output_shared/state/option_positions_v2/` 不应再被代码读取。

## Phase 4 V2 Physical Retirement

v2 quarantine 代码已物理删除：

- 删除 `domain/domain/option_positions_v2.py`。
- 删除 `domain/storage/repositories/option_positions_v2_repo.py`。
- 删除 `src/application/option_positions_v2_service.py`。
- 删除 `tests/test_option_positions_legacy_v2.py`。
- `domain/domain/__init__.py` 不再导出 `option_positions_v2`。
- `domain/storage/repositories/__init__.py` 不再导出 `option_positions_v2_repo`。
- 新增 `tests/test_option_positions_legacy_retirement.py`，防止 v2 文件、runtime import、lazy package export 回归。
- 文档改为“legacy v2 state”：旧 `output_shared/state/option_positions_v2/` 只作为历史文件留存，没有代码级读取入口。

验证：

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_option_positions_cli.py`：passed。
- `python3 -m pytest tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：333 passed。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/interfaces/cli/trade_events.py src/application/option_positions_context_builder.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py domain/domain/__init__.py domain/storage/repositories/__init__.py`：passed。
- `git diff --check`：passed。

## Phase 5 Legacy SQLite Runtime Cut

旧 SQLite `option_positions` 表和通用 records fallback 已从默认运行路径继续切出：

- `load_option_positions_repo` 不再在空 `trade_events` / `position_lots` 时默认迁移旧 SQLite `option_positions` 表。
- 旧表迁移改为显式 opt-in：`option_positions.bootstrap_from_legacy_sqlite.enabled=true`。
- 默认发现旧表时返回 `sqlite_only_legacy_option_positions_bootstrap_disabled`，强调本地 `trade_events` 才是事实源。
- `option_positions_facade.load_option_position_records` 只读取 canonical `position_lots`；空投影不再 fallback 到 repo `list_records`。
- manual close auto matcher 和 trade intake close matcher 只从 canonical read repo 取候选，不再用 legacy `list_records` 解析可写 target。
- agent `monthly_income_report` 改为通过 `load_canonical_option_position_records` 读报表输入。
- 结构性测试防止 `option_positions_facade`、`position_workflows`、`trade_intake_resolver`、`agent_tool_scan` 重新出现 `list_records(page_size=500)` fallback。

剩余：

- 继续把旧 `option_positions_*` 命名表面整理为新 ledger service / read-model adapter，避免长期保留 facade 语义债。

## Phase 5 Ledger Read Model Cut

canonical read model 已从旧 facade 命名表面迁出：

- 新增 `src/application/ledger/read_model.py`，承载 canonical `position_lots` 读取、字段规范化、lot view、列表行和月度收益报表入口。
- `src/application/option_positions_facade.py` 缩减为 compatibility re-export；保留旧函数名给测试和外部旧调用方。
- 风控上下文、cash headroom、pipeline context、Feishu sync、agent tools、manual close matcher、option intake、auto trade intake、CLI option/trade events 已改为直接 import `ledger.read_model`。
- 新增结构性测试：默认 runtime code 不再 import `option_positions_facade`；旧 facade 不能重新成为核心读模型。

剩余：

- 旧 `option_positions_*` 存储兼容层继续收敛成 migration-only。

## Phase 5 Ledger Service Runtime Routing Cut

runtime 调用入口继续从旧 `option_positions_service` 命名表面迁出：

- `src/application/ledger/service.py` 增加稳定服务接口，覆盖 repo 解析、repo capability guard、Feishu sync gate、rebuild、manual/trade persist、auto-close decision/apply、repair preview 等当前 runtime 需要的入口。
- `src/application` / `src/interfaces` 的 runtime 代码已改为通过 `ledger.service` 调用持仓/交易写服务；`option_positions_service` 暂时只作为旧实现兼容层和测试入口。
- `ledger.read_model` 也改为通过 `ledger.service` 取得 repo，避免 read model 重新依赖旧 service 命名表面。
- `LedgerPreflightError` 独立到 `src/application/ledger/errors.py`，避免新 service 与旧 service 之间形成静态 import 环。
- import ownership 测试从旧 facade 调整到 `ledger.read_model`，匹配“facade 只做兼容 re-export”的新边界。
- 新增结构性测试：除 `option_positions_service.py` 自身和 `ledger/service.py` 桥接层外，runtime 代码不得直接 import `src.application.option_positions_service`。

已验证：

- `python3 -m pytest tests/test_config_import_ownership_application.py tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：372 passed。
- `basedpyright --level error src/application/ledger/errors.py src/application/ledger/service.py src/application/ledger/read_model.py src/application/position_workflows.py src/application/position_maintenance.py src/application/trade_event_review.py src/application/option_positions_feishu_sync.py src/application/agent_tool_handlers.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/application/option_positions_sync_config.py`：0 errors。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/option_positions_context_builder.py src/application/cash_headroom_query.py src/application/pipeline_context.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/application/option_intake.py src/application/auto_trade_intake.py src/interfaces/cli/option_positions.py src/interfaces/cli/option_positions_report.py src/interfaces/cli/trade_events.py src/interfaces/webui/server.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py src/application/agent_tool_scan.py src/application/agent_tool_handlers.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py tests/test_config_import_ownership_application.py`：passed。
- `git diff --check`：passed。

剩余：

- 后续可把 `option_positions_service.py` 内部实现继续拆到 `ledger.repository` / `ledger.bootstrap` / `ledger.maintenance`，最后让旧 service 退化为 compatibility re-export。

## Phase 5 Repository Boundary Cut

仓储/配置公共边界已从旧 service 中拆出第一层：

- 新增 `src/application/ledger/repository.py`，承载 option position SQLite path 解析、Feishu table ref 解析、bootstrap/sync 开关解析、repo protocol/capability guard。
- `SQLiteOptionPositionsRepository`、position_lot contract-column helpers、sync-meta patching、SQLite transaction helper 已迁入 `ledger.repository`。
- `src/application/option_positions_service.py` 不再定义这些公共配置/guard 函数和 SQLite repository class；它从 `ledger.repository` 导入后继续作为兼容导出，避免一次性打断旧测试和外部调用。
- `src/application/ledger/service.py` 的 repo capability guard、`load_table_ref`、`option_positions_sync_to_feishu_enabled` 已直接走 `ledger.repository`，不再回旧 service。
- 新增结构性测试，防止配置/guard 定义回流到 `option_positions_service.py`。

已验证：

- `python3 -m pytest tests/test_option_positions_service.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_legacy_retirement.py tests/test_ledger_service.py`：84 passed。
- `basedpyright --level error src/application/ledger/repository.py src/application/ledger/service.py src/application/option_positions_service.py`：0 errors。
- `python3 -m compileall src/application/ledger/repository.py src/application/ledger/service.py src/application/option_positions_service.py`：passed。

剩余：

- SQLite repository class、bootstrap materialization 和 auto-close maintenance 仍在旧 service 内部；下一步继续拆到 `ledger.repository` / `ledger.bootstrap` / `ledger.maintenance`。

## Phase 5 Bootstrap Boundary Cut

bootstrap / migration materialization 已从旧 service 中拆出：

- 新增 `src/application/ledger/bootstrap.py`，承载 Feishu / legacy SQLite / existing `position_lots` bootstrap records 规范化、bootstrap trade event 生成、projection 物化、`load_option_positions_repo`。
- `src/application/ledger/service.py` 的 `load_option_positions_repo` 已直接委托 `ledger.bootstrap`，不再回旧 `option_positions_service`。
- `src/application/option_positions_service.py` 只保留兼容导出 `load_option_positions_repo` 和 auto-close maintenance 对 `apply_bootstrap_snapshot` 的调用，不再定义 bootstrap 主流程。
- import ownership 测试改为检查 `ledger.bootstrap` 持有 Feishu bitable adapter 引用。
- 新增结构性测试，防止 bootstrap 主流程定义回流到旧 service。

已验证：

- `python3 -m pytest tests/test_option_positions_service.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_legacy_retirement.py tests/test_ledger_service.py`：84 passed。
- `basedpyright --level error src/application/ledger/repository.py src/application/option_positions_service.py tests/test_option_positions_legacy_retirement.py`：0 errors。
- `python3 -m compileall src/application/ledger/repository.py src/application/option_positions_service.py tests/test_option_positions_legacy_retirement.py`：passed。

剩余：

- auto-close maintenance 和 trade-event publish helpers 已可拆出；下一步继续缩小旧 service 的 manual intervention/open/close/adjust 实现。

## Phase 5 Writer / Maintenance Boundary Cut

事件写入和过期自动平仓维护已从旧 service 中拆出：

- 新增 `src/application/ledger/writer.py`，承载 trade event append 后的 canonical projection 重放、`position_lots` 刷新、projection diagnostics summary、Feishu sync meta 保留。
- 新增 `src/application/ledger/targets.py`，承载当前 lot 目标身份一致性校验，供 manual close/adjust 和 auto-close 共用。
- 新增 `src/application/ledger/maintenance.py`，承载 `build_expired_close_decisions`、`persist_expire_auto_close_event`、`auto_close_expired_positions`。
- `src/application/ledger/service.py` 的 rebuild / persist_trade_event / auto-close wrappers 已直接委托新 ledger owner。
- `src/application/option_positions_service.py` 不再定义 repository、bootstrap、writer、target matching、auto-close maintenance 主流程；保留旧函数名作为兼容导出。
- 新增结构性测试，防止 writer / targets / maintenance 定义回流到旧 service。

已验证：

- `python3 -m pytest tests/test_config_import_ownership_application.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_legacy_retirement.py tests/test_option_positions_service.py tests/test_ledger_service.py`：119 passed。
- `python3 -m pytest tests/test_config_import_ownership_application.py tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：377 passed。
- `basedpyright --level error src/application/ledger/errors.py src/application/ledger/repository.py src/application/ledger/bootstrap.py src/application/ledger/writer.py src/application/ledger/targets.py src/application/ledger/maintenance.py src/application/ledger/service.py src/application/ledger/read_model.py src/application/option_positions_service.py src/application/position_workflows.py src/application/position_maintenance.py src/application/trade_event_review.py src/application/option_positions_feishu_sync.py src/application/agent_tool_handlers.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py`：0 errors。
- `python3 -m compileall src/application/option_positions_service.py src/application/ledger/maintenance.py src/application/ledger/targets.py src/application/ledger/writer.py src/application/ledger/service.py tests/test_option_positions_legacy_retirement.py`：passed。
- `git diff --check`：passed。

剩余：

- 旧 `option_positions_service.py` 还剩 manual open / close / adjust / void / repair 的兼容实现；下一步应切到 `ledger.manual_trades` / `ledger.interventions` 或等价 owner，最后让旧 service 只 re-export。

## Phase 5 Manual Trade / Intervention Boundary Cut

旧 service 剩余实现已切出：

- 新增 `src/application/ledger/manual_trades.py`，承载 manual open / close / adjust 写入、manual close 幂等查询和 manual event id 生成。
- 新增 `src/application/ledger/interventions.py`，承载 manual void / repair preview 和 apply。
- `src/application/ledger/service.py` 已不再 import `src.application.option_positions_service`，runtime 路径直接走新的 ledger owner。
- `src/application/option_positions_service.py` 缩减为 40 行 compatibility export，不再持有持仓/交易实现。
- 结构性测试收紧：runtime 不得直接 import 旧 service，manual write / intervention 定义不得回流旧 service。

已验证：

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_ledger_service.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_service.py`：90 passed。
- `python3 -m pytest tests/test_config_import_ownership_application.py tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py`：379 passed。
- `basedpyright --level error src/application/ledger/errors.py src/application/ledger/repository.py src/application/ledger/bootstrap.py src/application/ledger/writer.py src/application/ledger/targets.py src/application/ledger/maintenance.py src/application/ledger/manual_trades.py src/application/ledger/interventions.py src/application/ledger/service.py src/application/ledger/read_model.py src/application/option_positions_service.py src/application/position_workflows.py src/application/position_maintenance.py src/application/trade_event_review.py src/application/option_positions_feishu_sync.py src/application/agent_tool_handlers.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py`：0 errors。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_service.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/option_positions_context_builder.py src/application/cash_headroom_query.py src/application/pipeline_context.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/application/option_intake.py src/application/auto_trade_intake.py src/interfaces/cli/option_positions.py src/interfaces/cli/option_positions_report.py src/interfaces/cli/trade_events.py src/interfaces/webui/server.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py src/application/agent_tool_scan.py src/application/agent_tool_handlers.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py tests/test_config_import_ownership_application.py`：passed。
- `git diff --check`：passed。

剩余：

- 运行完整核心回归和静态检查后，可考虑把旧 `option_positions_service` 测试引用逐步改到 `ledger.*` owner，最后删除 compatibility export。

## Phase 5 Compatibility Service Retirement

旧 service 兼容出口已彻底删除：

- `src/application/option_positions_service.py` 已物理删除，不再保留旧函数名 re-export。
- 测试里的旧 `svc` 引用已改到真实 owner：`ledger.repository`、`ledger.bootstrap`、`ledger.writer`、`ledger.manual_trades`、`ledger.interventions`、`ledger.maintenance`。
- `tests/test_option_positions_legacy_retirement.py` 改为要求旧 service 文件不存在，并继续校验 repository / bootstrap / writer / target / maintenance / manual trade / intervention 定义都留在对应 owner。
- `docs/AGENT_WIKI.md` 和 `docs/TRADE_POSITION_LEDGER_REDESIGN.md` 已更新为旧 service 已删除，不再把它描述为兼容层。

已验证：

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_ledger_service.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_service.py tests/test_option_positions_cli.py tests/test_trade_events_cli.py tests/test_option_intake_command.py tests/test_trade_intake_resolver_open.py tests/test_trade_intake_resolver_close.py`：150 passed。
- `python3 -m pytest tests/test_config_import_ownership_application.py tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py tests/test_option_intake_command.py`：394 passed。
- `basedpyright --level error src/application/ledger/errors.py src/application/ledger/repository.py src/application/ledger/bootstrap.py src/application/ledger/writer.py src/application/ledger/targets.py src/application/ledger/maintenance.py src/application/ledger/manual_trades.py src/application/ledger/interventions.py src/application/ledger/service.py src/application/ledger/read_model.py src/application/position_workflows.py src/application/position_maintenance.py src/application/trade_event_review.py src/application/option_positions_feishu_sync.py src/application/agent_tool_handlers.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py tests/test_option_positions_cli.py tests/test_option_positions_sqlite_service.py tests/test_option_positions_service.py tests/test_trade_events_cli.py tests/test_option_intake_command.py`：0 errors。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_feishu_sync.py src/application/option_positions_facade.py src/application/option_positions_inspection.py src/application/option_positions_context_builder.py src/application/cash_headroom_query.py src/application/pipeline_context.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/application/option_intake.py src/application/auto_trade_intake.py src/interfaces/cli/option_positions.py src/interfaces/cli/option_positions_report.py src/interfaces/cli/trade_events.py src/interfaces/webui/server.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py src/application/agent_tool_scan.py src/application/agent_tool_handlers.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py tests/test_config_import_ownership_application.py tests/test_option_positions_cli.py tests/test_option_positions_sqlite_service.py tests/test_trade_events_cli.py tests/test_option_intake_command.py`：passed。
- `git diff --check`：passed。

## Phase 5 Test Naming And Read Facade Retirement

旧 service 命名和 read facade 继续收敛：

- `tests/test_option_positions_service.py` 重命名为 `tests/test_ledger_maintenance.py`。
- `tests/test_option_positions_sqlite_service.py` 重命名为 `tests/test_ledger_sqlite_workflows.py`。
- `src/application/option_positions_facade.py` 已删除；剩余测试改为直接依赖 `src.application.ledger.read_model`。
- `tests/test_option_positions_legacy_retirement.py` 扩展为防止旧 service 测试文件名和旧 read facade 文件回归。
- `docs/AGENT_WIKI.md`、`docs/TRADE_POSITION_LEDGER_REDESIGN.md` 不再把 `option_positions_facade.py` 描述为兼容出口。

已验证：

- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_ledger_maintenance.py tests/test_ledger_sqlite_workflows.py tests/test_ledger_service.py`：90 passed。
- `python3 -m pytest tests/test_option_positions_legacy_retirement.py tests/test_option_positions_context_partial_close.py tests/test_option_positions_reporting.py tests/test_ledger_maintenance.py tests/test_ledger_sqlite_workflows.py`：119 passed。
- `python3 -m pytest tests/test_config_import_ownership_application.py tests/test_ledger_*.py tests/test_option_positions_*.py tests/test_trade_events_cli.py tests/test_close_advice_runner.py tests/test_agent_plugin_smoke.py tests/test_position_maintenance.py tests/test_position_workflows_auto_sync.py tests/test_trade_intake_*.py tests/test_auto_trade_intake_audit.py tests/test_sync_option_positions_to_feishu.py tests/test_option_intake_command.py`：394 passed。
- `basedpyright --level error src/application/ledger/errors.py src/application/ledger/repository.py src/application/ledger/bootstrap.py src/application/ledger/writer.py src/application/ledger/targets.py src/application/ledger/maintenance.py src/application/ledger/manual_trades.py src/application/ledger/interventions.py src/application/ledger/service.py src/application/ledger/read_model.py src/application/position_workflows.py src/application/position_maintenance.py src/application/trade_event_review.py src/application/option_positions_feishu_sync.py src/application/agent_tool_handlers.py src/application/trade_intake_resolver.py src/interfaces/cli/option_positions.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py tests/test_option_positions_cli.py tests/test_ledger_sqlite_workflows.py tests/test_ledger_maintenance.py tests/test_option_positions_context_partial_close.py tests/test_option_positions_reporting.py tests/test_trade_events_cli.py tests/test_option_intake_command.py`：0 errors。
- `python3 -m compileall domain/domain/ledger src/application/ledger domain/domain/option_position_ledger.py src/application/option_positions_feishu_sync.py src/application/option_positions_inspection.py src/application/option_positions_context_builder.py src/application/cash_headroom_query.py src/application/pipeline_context.py src/application/position_workflows.py src/application/trade_event_review.py src/application/trade_intake_resolver.py src/application/option_intake.py src/application/auto_trade_intake.py src/interfaces/cli/option_positions.py src/interfaces/cli/option_positions_report.py src/interfaces/cli/trade_events.py src/interfaces/webui/server.py src/application/position_maintenance.py src/application/agent_tool_openclaw.py src/application/agent_tool_scan.py src/application/agent_tool_handlers.py src/application/option_positions_sync_config.py tests/test_option_positions_legacy_retirement.py tests/test_config_import_ownership_application.py tests/test_option_positions_cli.py tests/test_ledger_sqlite_workflows.py tests/test_ledger_maintenance.py tests/test_option_positions_context_partial_close.py tests/test_option_positions_reporting.py tests/test_trade_events_cli.py tests/test_option_intake_command.py`：passed。
- `git diff --check`：passed。
