# Gateflow Plan — Daily Decision Brief 与后续增量监控

- **Gate**: plan
- **Work unit**: `daily-decision-brief`
- **Date**: 2026-07-19
- **Base**: `main` / `origin/main` / `v1.2.420` at `5aecee73b3e4ace39b0c38ce9a98d18180020d1b`
- **Branch**: `codex/v1.3.0-daily-decision-brief`
- **Status**: accepted after plan review/re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-plan-20260719.md`

## Goal / motivation / success signal

建立 `daily_decision_brief.v1` 这一唯一、结构化、账号隔离的日内决策基线。首个成功 scheduled scan 生成完整日报；后续 scheduled scan 使用同一 read model 重算，并只在 material change 时发送增量。CLI、Agent Tool 与通知 renderer 都读取同一 canonical payload。

成功信号：

1. 每个 `market + market_trading_date + account` 有稳定 identity、revision、data as-of、valid-until、actionability 和 actions。
2. 首份可用基线只发送一次；失败或 `--no-send` 不会错误标记为已送达，下一轮仍可发送最新完整基线。
3. 后续 diff 始终相对“最后成功送达 revision”，而不是仅相对上一轮；发送失败不会吞掉 material change。
4. `live_actionable / planning_only / blocked`、P0/P1/P2、blocked/recovered、P0 变化、行动失效和整手容量变化都有 deterministic tests。
5. 默认配置关闭该功能；关闭时现有通知、scheduler、candidate ranking、Close Advice 和 agent contract 行为不变。
6. focused tests、notification/tick/agent/config regressions、architecture checks、`git diff --check` 和完整 pytest 通过。

## Non-goals / scope boundary

本 work unit 不做自动交易、订单草稿、消息按钮、LLM 排名、第二套 candidate optimizer、完整绩效日报、新闻聚合、历史 run 迁移、任务完成状态、生产配置修改、真实通知、部署、merge、tag 或发布。

不会重新引入或扩展 Strategy Lab。候选排序继续调用 `domain.domain.engine.rank_candidate_rows()`；Combo Yield 只消费当前结构化产物并保留已有 group/leg identity。

## Goal confirmation alignment

用户已于 2026-07-19 明确确认：

- 接受目标、成功信号、scope 与 non-goals；
- 接受现有 10 分钟 timer 的 09:40 成功/明确 blocked、进程级失败由 10:00 下一轮恢复的语义，不要求 09:45 精确 SLA；
- 接受 `default-off`，Draft PR 不改变生产通知，发布/启用/远端升级另行授权。

## First-principles judgment and direct code evidence

1. `src/application/scan_scheduler.py` 已拥有 start+10、hourly、end-10 与 account-level run/notify state；不创建第二套 scheduler。
2. `src/application/account_run.py` 已将 candidate CSV、`candidate_filter_trace.jsonl`、Close Advice、account metrics 和 config override 写入 run account workspace；日报从这些结构化 artifact 组装，绝不反解析 `symbols_notification.txt`。
3. `domain/domain/engine/candidate_engine.py` 已拥有 canonical candidate ranking；日报只做行动编排和 diff。
4. `domain/domain/close_advice.py` 与 `close_advice.csv` 已拥有平仓建议 tier、reason、position/group identity；日报不复制其政策。
5. `domain/storage/repositories/state_repo.py` 已提供 account/run/shared JSON 与 idempotency records；不新增数据库。
6. `src/application/tick_notification_flow.py`、`scheduled_notification.py` 与 channel adapters 已拥有 routing、quiet hours、retry、delivery confirmation 和 provider idempotency；日报只提供替代的 account messages 与稳定 delivery key。
7. `src/application/tick_cron.py` 已使用 market-level process lock，`tick_execution` 也做分钟级 claim；日报 revision 写入仍使用 account-level file lock，防止手工并发入口产生 revision 冲突。
8. `src/application/tick_scheduler_context.py` 的 trading-day guard 在整日休市时跳过 scan；本 work unit 不制造伪扫描。已存在 brief 的 read surface 会在 `valid_until` 后把 effective actionability 降为 `planning_only`，但不会把旧数据重新包装成 LIVE 建议。

## Canonical contract

### Daily brief identity and schema

新增纯 domain 模块 `domain/domain/daily_decision_brief.py`：

- `DAILY_DECISION_BRIEF_SCHEMA_VERSION = "daily_decision_brief.v1"`
- `DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION = "daily_decision_brief_diff.v1"`
- actionability：`live_actionable | planning_only | blocked`
- priority：`P0 | P1 | P2`
- identity：`market + market_trading_date + account`
- revision：同 identity 下从 0 单调递增
- action ID：对稳定业务字段做 canonical JSON + SHA-256；允许字段仅为 `action_type, strategy_family, account, symbol, option_type, side, expiration, strike, contract_symbol, position_lot_id, strategy_group_id, leg_role`。price、rank、message、return 不进入 ID。

Payload 顶层字段：

- `schema_version, brief_id, market, market_trading_date, account, revision, run_id`
- `generated_at_utc, data_as_of_utc, valid_until_utc`
- `status, actionability, strategy_summary`
- `actions[]`
- `positions[]`
- `capacity`
- `candidates`（`sell_put / covered_call / combo_yield`）
- `rejections`
- `events[]`
- `data_gaps[]`
- `source_artifacts[]`

Action 字段：

- `action_id, priority, state, action_type, strategy_family`
- stable identity fields；`title, reason, metrics, source`
- `state` 只允许 `active | invalidated | blocked | observe`

### Priority and actionability policy

不新增策略评分：

- Close Advice `strong -> P0`，`medium -> P1`，其它可展示 tier -> `P2`。
- 已通过现有候选过滤的 candidate 使用其现有 `priority/alert_level/tier`（若有）；缺失显式 tier 时统一为 `P1`，不根据排名擅自提升为 P0。
- critical data/run failure -> blocked P0 action。
- `live_actionable` 仅当 account scan 成功、至少一种可信 account-level decision source（Close Advice、candidate、capacity/position context）可读、当前市场 session 尚未超过 `valid_until_utc`。
- forced/out-of-session snapshot 或读取时已过 `valid_until_utc` -> effective `planning_only`。
- `blocked` 仅用于 account pipeline/no-scan failure、所有结构化 decision sources 都不可读，或对所有行动都必需的 account-level cash/position canonical source不可用。单 symbol/单 strategy prefetch 或报价失败只写 `data_gaps` 并抑制受影响 action，不得阻断其它有效行动。

### Material change diff

`diff_daily_decision_briefs(last_delivered, current)` 返回 changes 与 `material`：

必须 material：

- blocked / recovered；
- 新增 P0、任何 action 升级到 P0；
- 上次已送达的 P0/P1 active action 消失或变为 invalidated/blocked；
- `live_actionable <-> planning_only` 且存在 P0/P1 active action；
- Sell Put 可开整手合约数或 Covered Call 可覆盖整手合约数变化；
- 新出现的 P1/P0 candidate action（其存在即表示已经跨过现有 canonical filter threshold）。

非 material：

- 同一 action ID 的价格/收益数字变化；
- 同 tier 内 rank 互换；
- 不能改变整手容量的现金/股份变化；
- P2 observe 重排；
- heartbeat 或完全相同状态。

Diff 必须相对最后成功送达 revision。若首份完整 brief 尚未成功送达，始终重发“当前最新完整 brief”，不发送 delta。

## Persistence and state transitions

新增 application repository/service `src/application/daily_decision_brief_repository.py`，仅使用 JSON + existing state paths：

- run-scoped：`output_runs/<run_id>/accounts/<account>/state/daily_decision_brief.json`
- run-scoped diff：`.../daily_decision_brief_diff.json`
- account current：`output_accounts/<account>/state/daily_decision_brief.<market>.current.json`
- account revision：`output_accounts/<account>/state/daily_decision_brief.<market>.<date>.rNNNN.json`
- account delivery pointer：`output_accounts/<account>/state/daily_decision_brief.<market>.delivery.json`
- shared current index：`output_shared/state/current/daily_decision_briefs.current.json`，键为 `<MARKET>/<account>`

每个 `account + market` 用 `daily_decision_brief.<market>.lock` + `fcntl.flock` 序列化 prepare/revision writes；shared current index 另用 `daily_decision_briefs.current.lock` 序列化 read-modify-write，避免 HK/US 独立 cron 并发丢条目。状态转换：

1. assemble candidate payload；
2. lock；读取 same-day current，分配 revision；写 revision/current/run/shared；
3. 读取 delivery pointer 指向的最后成功送达 revision；
4. 无 full delivery -> `delivery_kind=full`；否则 diff(last delivered, current)；
5. 无 material -> 静默但 revision 已持久化；
6. delivery 成功后原子更新 delivery pointer；失败/quiet/no-send 不推进 pointer；
7. 下一轮重新相对 last delivered 计算，因此 failed delta 不会丢失。

稳定 provider idempotency key：

- full：`daily-brief:<market>:<date>:<account>:full`
- delta：`daily-brief:<market>:<date>:<account>:from:<last-delivered-brief-digest>:<material-diff-digest>`；current revision 只进 audit，不进入 provider key，确保 post-send/pre-pointer crash 后相同 material diff 仍使用同一 key

`scheduled_notification.send_account_message_with_retry()` 增加可选 idempotency override；默认调用行为不变。

## Structured assembler

新增 `src/application/daily_decision_brief_service.py`：

- 输入：`base, run_id, account, markets_to_run, scheduler_decision, AccountResult, config, now_utc`
- 只读 run account 结构化文件：candidate CSV、`candidate_filter_trace.jsonl`、`close_advice.csv`、account metrics、required-data/event summaries；不读取 notification Markdown。
- CSV 使用 pandas（项目现有依赖）。Sell Put 调用 `rank_candidate_rows(mode="put")`，Covered Call 调用 `rank_candidate_rows(mode="call")`；Combo Yield 保留 pipeline 已输出的 canonical pair/group 顺序并以 `strategy_group_id + leg contracts` 去重，不在日报层重新排名。每个策略最多 `max_candidates_per_strategy`。
- Sell Put capacity 使用已有 `compute_sell_put_cash_capacity()` 的 basis/free/required，再计算该 candidate 的整手容量；账户 capacity 以 canonical row 中的现金 facts 和 top actionable contract 表示。
- Covered Call capacity 使用 row 中 `shares_total/shares_locked/shares_available_for_cover/multiplier/call_covered_contracts_available`，不解析文本。
- Close Advice 保留 `position_lot_id / strategy_group_id / leg_role`。
- rejection 摘要从 trace/reject summary 的结构化 rule/category 聚合。
- `source_artifacts` 只记录相对 run 路径与 row count，不暴露秘密或任意外部路径。
- 缺字段显式写入 `data_gaps`；不得合成不存在的 upstream 数据。

## Renderer and notification integration

新增 `src/application/daily_decision_brief_renderer.py`：

- `render_full_brief()`：状态/时间、一句话策略、P0/P1/P2、已有仓位/Close Advice、容量、三类候选、拒绝/事件/数据缺口；中文 Markdown；每节和总条数有上限。
- `render_blocked_brief()`：blocker、已知数据、下一轮恢复语义。
- `render_delta_brief()`：只渲染 diff changes，不重复完整日报。
- `render_recovery_brief()`：blocked -> recovered 的 delta 变体。

修改 `TickNotificationRequest` 增加 `markets_to_run`、`scheduler_markets` 与 `scheduler_decision`。`run_tick_notification_flow()` 在 `notifications.daily_brief.enabled=true` 时：

1. 对每个 `account + scheduler market` 独立 partition/assemble/persist lifecycle；production scheduled runtime 必须是单 market。
2. 手工 `--market-config all` 时仍按 market 分别生成 state，但 Daily Brief 主动发送 fail closed：不合并跨市场消息、不推进 delivery pointer，并记录 `daily_brief_multi_market_delivery_skipped`。CLI/Tool 可分别读取已生成的 market briefs。
3. 单 market 时用 lifecycle 产生的 `messages_by_account` 取代 legacy full-message preparation。
4. 继续使用现有 route、quiet-hours、per-account retry、delivery confirmation、failure summary 与 finalization。
5. account delivery confirmed 后推进该单 market lifecycle 的 delivery pointer；失败/quiet/no-send 都不推进。
6. audit event 增加 brief identity/revision/delivery kind/diff digest，不记录完整敏感内容。

关闭时完全走原路径。

## Public read surfaces

### CLI

新增 `src/interfaces/cli/daily_brief_ops.py` 并在 `./om` 注册：

```text
./om daily-brief latest --account lx [--market US] [--json]
./om daily-brief day --account lx --date YYYY-MM-DD [--market US] [--revision N] [--json]
```

默认输出 renderer Markdown；`--json` 输出 canonical payload。无 artifact 返回结构化 unavailable/error，不回退解析旧通知。

### Agent Tool

新增 `src/application/agent_tools/daily_brief.py`，工具名 `daily_decision_brief_read`：

- pure read / read-only；
- input：`account, market?, date?, revision?`；
- output：brief、effective actionability、coverage/source/freshness；
- path 通过 `mask_path`；
- 加入 registry 与 agent contract tests。

## Config/public contract

新增默认关闭配置：

```json
"notifications": {
  "daily_brief": {
    "enabled": false,
    "max_candidates_per_strategy": 3,
    "max_actions_per_priority": 5,
    "max_rejection_reasons": 5
  }
}
```

修改：

- `src/application/config_defaults.py`
- `src/application/config_validator.py`
- `configs/examples/user.common.example.json`
- 如生成一致性测试要求，更新 `configs/system.json`

Validator 拒绝非 object、非 bool `enabled` 和非正整数 limits；本 work unit 不提供 actionability/tier/策略 threshold override。

## Implementation slices

### S1 — Domain contract, stable identity, diff

**Objective**：建立纯 domain schema normalization、action identity、actionability expiry 和 material diff。

**Allowed files**：

- `domain/domain/daily_decision_brief.py`
- `domain/domain/__init__.py`
- `tests/test_daily_decision_brief_domain.py`
- S1 Gateflow artifacts

**Exact changes**：实现 schema constants、normalizers、stable action ID、brief ID、effective actionability、diff/change sorting/digest。无 I/O、无 pandas、无 `src/` import。

**Validation**：稳定 ID 不受 rank/price 变化影响；P0 add/upgrade、P0/P1 invalidation、blocked/recovered、capacity whole-contract change、P1 threshold crossing为 material；rank/price/P2 reorder 非 material；expired live brief read as planning-only。

**Stop condition**：domain tests 通过且 deepreview 无未裁决 finding。

### S2 — Structured assembler and persistence lifecycle

**Objective**：从 run artifacts 组装 brief，并实现 revision/current/history/delivery-pointer/read service。

**Allowed files**：

- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_repository.py`
- `domain/storage/repositories/state_repo.py`（仅必要 shared/account helpers）
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_repository.py`
- S2 Gateflow artifacts

**Prerequisite**：S1 accepted commit。

**Validation**：候选/Close Advice/trace/capacity/缺失 artifact；account + market isolation；partial symbol gap 不阻断有效行动；revision monotonic；full unsent retry；delta 对 last delivered；no-material revision；account-market lock/shared-index lock/atomic write；旧 state/unavailable fail closed。

### S3 — Renderer and tick delivery integration

**Objective**：default-off 集成现有 notification flow，复用 route/retry/confirmation，并稳定 delivery idempotency。

**Allowed files**：

- `src/application/daily_decision_brief_renderer.py`
- `src/application/tick_notification_flow.py`
- `src/application/multi_account_tick.py`
- `src/application/scheduled_notification.py`
- `src/application/notification_delivery_adapter.py`（仅测试需要时）
- focused notification/tick tests
- S3 Gateflow artifacts

**Prerequisite**：S2 accepted commit。

**Validation**：disabled legacy parity；single-market first full；multi-market state partition + outbound fail-closed/no pointer advance；no-send/quiet/failure 不推进 pointer；confirmed send 推进 single-market pointer；no material silent；failed delta next run retained；post-send/pre-pointer equivalent diff reuses stable key；blocked/recovery；per-account isolation；message bounds；stable idempotency override。

### S4 — CLI, Agent Tool, config and docs

**Objective**：提供 canonical read surfaces 与 default-off public config/documentation。

**Allowed files**：

- `src/interfaces/cli/daily_brief_ops.py`
- `src/interfaces/cli/main.py`
- `src/application/agent_tools/daily_brief.py`
- `src/application/agent_tool_registry.py`
- `src/application/config_defaults.py`
- `src/application/config_validator.py`
- `configs/examples/user.common.example.json`
- `configs/system.json`（若由默认生成合同要求）
- `README.md`, `docs/AGENT_WIKI.md`
- focused CLI/agent/config/docs tests
- S4 Gateflow artifacts

**Prerequisite**：S3 accepted commit。

**Validation**：latest/day/revision/JSON/Markdown/unavailable；tool pure-read manifest/output contract；invalid config fail fast；examples default false；public docs 写明 09:40/10:00、advisory-only、休市 planning-only、生产启用另行授权。

### S5 — Scenario/regression closure

**Objective**：补足跨模块场景与回归证明，不新增产品行为。

**Allowed files**：

- `tests/test_daily_decision_brief_scenarios.py`
- 必要的现有 test expectations（只因新增默认字段/工具）
- S5 Gateflow artifacts

**Scenarios**：

- 09:40 first full for lx/sy；
- same-day unchanged silent；
- new/upgrade P0；
- main action invalidated；
- blocked -> recovery；
- capacity changes by whole contract vs cash-only noise；
- delivered revision vs previous revision failure recovery；
- post-close effective planning-only；
- all-day no-run does not create fake live brief；
- old runtime with no artifacts reports unavailable；
- disabled config exact legacy notification behavior。

## Validation ladder

每 slice focused pytest + `python3 -m compileall` for touched modules + `git diff --check`。

Aggregate：

```bash
python3 -m pytest -q tests/test_daily_decision_brief_*.py
python3 -m pytest -q tests/test_tick_notification_flow.py tests/test_tick_notification_perception_flow.py tests/test_scheduled_notification_application.py tests/test_multi_account_tick.py
python3 -m pytest -q tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py
python3 -m pytest -q tests/test_layered_config.py tests/test_config_validator.py
python3 scripts/check_dependency_graph.py
python3 scripts/release_check.py
python3 -m pytest -q
python3 -m compileall -q domain/domain src/application src/interfaces

git diff --check
```

若实际 test file 名不同，使用仓库存在的等价 focused suite 并在 artifact 记录精确命令。

## Docs decision

Public config、通知生命周期、CLI/Tool contract 和休市语义均改变，因此必须更新 README、`docs/AGENT_WIKI.md` 与 config example。不会修改 VERSION/CHANGELOG，因为本 work unit 只到 Draft PR，发布另行授权。

## Why this is not over-engineered

- 不新增 scheduler、daemon、queue、数据库、任务系统或第二发送栈；
- 一个 domain contract、一个 assembler/lifecycle service、一个 repository、一个 renderer；
- 复用现有 canonical candidate/close policy、run artifacts、state paths、delivery route 和 idempotency；
- 配置只有 enable + 展示限额，没有 speculative strategy thresholds；
- history 用 flat JSON revision，满足审计/read surface，避免 schema migration；
- default-off 避免在 Draft PR 中改变生产行为。

## Risks / open questions

无 blocking open question。需要在 review 中重点攻击：

1. revision/delivery pointer 并发与 crash window；
2. candidate CSV 的 mixed schema 与空文件；
3. full send 失败后新 revision 是否仍能重发最新 full；
4. delta 必须相对 last delivered，而不是 previous current；
5. market date/timezone 与 post-close effective actionability；
6. legacy disabled path 是否 byte-for-byte 保持语义；
7. message size / missing data / per-account isolation；
8. WeChat 不支持 provider-side idempotency 时的 residual duplicate window。

## Residual risks classification at plan gate

| Risk / uncovered area | Classification |
|---|---|
| provider 确认成功后、本地 delivery pointer 写入前崩溃，WeChat 可能重复 | covered by S3 stable key + local pointer；provider 不支持 key 的极小 crash window assigned to later production observation，不扩大为新 outbox DB |
| 历史 run 没有新 schema | assigned to later work unit；read surface 显式 unavailable |
| 真实消息噪声和长度 | covered by S3 renderer limits、S5 scenarios；production canary 是后续 rollout |
| 09:45 精确 SLA | fixed at goal gate；采用 09:40/10:00 语义 |
| 自动交易/任务完成状态 | assigned to later work unit；本轮 advisory-only |

## Completion report format

Final closeout 必须列出：changed contract/modules、验证命令/结果、docs、所有 plan/code/deep/PR findings 状态、remaining risks/owner、Draft PR URL、issue link status（本 work unit 无 issue 时明确说明）、merge 后 next entry point（产品 review -> 单独授权 release/production config/remote upgrade/canary）。

## Gate transition

- **Current gate**: implementation S1
- **Next entry point**: 实现 S1 domain contract、stable identity、effective actionability 和 material diff。
