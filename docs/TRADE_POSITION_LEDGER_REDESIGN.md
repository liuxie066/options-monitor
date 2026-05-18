# Trade And Position Ledger Redesign

这份文档锁定下一轮重构目标：重新设计交易与持仓两个核心模块，废弃当前 v1/v2 混合读写模型，让风控只依赖一个可解释、可回放、可验证的 canonical ledger。

## Problem

重构前 `option_positions` 模块同时存在几套语义：

- v1 `trade_events -> projection -> position_lots`
- v2 snapshot / event / projection compatibility layer
- facade 曾经优先读取 v2 compat records，再回退 v1 lots
- manual close / auto-close / sync / context builder 对 `record_id` 的来源不完全一致

这会造成核心风险：

- 同一逻辑仓位在不同路径解析成不同 `record_id`
- `position_key` 聚合身份被误用为 lot 身份
- same strike / same expiry / different expiry 多 lot 场景下，close target 不稳定
- 风控上下文可能读取到和写路径不一致的持仓状态

结论：这不是单点 bug，而是账本边界不清。继续修补 v1/v2 兼容层会增加风险。

## Design Goal

新账本必须满足一个底线：

> 风控、报表、自动平仓、手工平仓只能从同一个 canonical ledger 派生状态；任何 close 都必须能解释为关闭了哪一个确定的 lot。

核心目标：

- 单一事实源：交易事件是唯一事实源。
- lot 级身份：`lot_id` 是唯一可写目标。
- 聚合视图只读：`position_key` 只能用于展示、汇总和风控聚合，不能作为写入目标。
- close 显式目标：manual close、auto-close、assignment、exercise 都必须解析到确定 `lot_id`。
- fail closed：身份不唯一、投影异常、对账不一致时，阻断写入和风控建议。
- 可回放：任何时刻都可以从事件流重放出当前持仓。
- 可迁移：旧 SQLite / Feishu / v2 只作为迁移输入或对账输入，不再参与 canonical target resolution。

## Non Goals

- 不在第一阶段删除旧 SQLite 文件或运行时状态。
- 不直接改写生产仓位数据。
- 不把 Feishu 作为本地事实源。
- 不为一次迁移做大规模人工 SQL 修复。
- 不把 v2 compatibility projection 继续演进为 canonical；新账本独立建模。

## Current Cutover Status

当前默认运行边界：

- canonical state 固定为 `trade_events -> position_lots`。
- manual open / close / adjust、trade intake、auto-close、void / repair 的默认写后状态都从 canonical projection 发布。
- `rebuild` / `reconcile` / `inspect` / `history` 默认读写面不再调用 v2 service。
- 旧 `option_positions_facade` 已删除；canonical `position_lots` 为空时不会用 v2 compat records 补数据。
- canonical read model 已迁到 `src/application/ledger/read_model.py`；旧 `option_positions_facade` re-export 兼容层已删除。
- `src/application/ledger/api.py` 是非 ledger runtime 进入账本核心的唯一公共应用边界；`positions` / `trades`、agent tools、CLI、web UI、pipeline context、cash headroom 都不再直接 import ledger 内部 service / preflight / resolver / publisher / repository / reconciliation / read-model 模块。
- `ledger.api` 已收敛为薄公共 facade；命令写入/维护动作落在 `src/application/ledger/commands.py`，查询/读模型动作落在 `src/application/ledger/queries.py`，避免公共 API 自身演变成新的 god service。
- runtime 调用方只使用语义账本动作（manual position record、broker trade record、expired-close plan/record、projection refresh、position snapshot read、mirror sync metadata、event review/repair、reconciliation），而不是自行组合 `persist_*` / `preflight_*` / `require_*` / `load_*` 内部原语。
- 风控读取路径开始使用显式 read DTO：`PositionLotSnapshot` / `RiskPositionView`。`src/application/positions/context_builder.py` 内部消费 `RiskPositionView`，CLI / JSON 输出边界再转换为 dict。
- repository/config、stored event codec、bootstrap、event write/projection publish、manual open/close/adjust、manual void/repair、auto-close maintenance、ledger preflight、close lot resolver、target identity guard 已分别迁到 `src/application/ledger/repository.py`、`event_codec.py`、`bootstrap.py`、`writer.py`、`manual_trades.py`、`interventions.py`、`maintenance.py`、`preflight.py`、`lot_resolver.py`、`targets.py`。
- close target 解析已统一为 `CloseTargetResolution` 读写契约：manual close 使用唯一 strict match，broker close 使用严格 exact FIFO target set，auto-close 使用显式 current `record_id` target；解析结果会写入 preview / diagnostics / operation / raw_payload。
- lot record / field 语义已迁到 `domain/domain/ledger/position_fields.py`；旧 `domain/domain/option_position_lots.py` 仅作为兼容 re-export，ledger / positions / trades 核心路径不再从旧模块取写入字段模型。
- open lot fields 与 open/close/adjust patch 已收敛到显式 `PositionLotFields` / `PositionLotPatch` contract；`src/application/ledger` 写路径内部使用 contract builder，旧 dict helper 只作为兼容输出边界保留。
- projection 应用 adjust event 时会先用 `decode_position_lot_patch` 将 stored `raw_payload.patch` 解码成 `PositionLotPatch`；`PositionLot` 不再直接解释自由 patch dict。
- Feishu mirror 回写已收敛为显式 `PositionLotSyncMetadataPatch`，仓储层只接受 typed patch 并只暴露 `update_position_lot_sync_metadata`，不再提供看起来能更新任意 lot fields 的通用接口。
- projection 发布到 `position_lots` 仓储替换时使用显式 `PositionLotRecord`；`repository.replace_position_lots` 不再接受自由 `list[dict]` 写入。
- manual open/close/adjust 的中心结果边界已收敛到 `src/application/ledger/results.py`：preflight / write / preview 在应用内部使用 `LedgerPreflightResult`、`LedgerWriteResult` 和 manual preview/result contracts，只有 CLI / workflow 输出边界调用 `to_payload()` 转 dict。
- event writer / projection refresh 的底层写入结果也已收敛到 `LedgerWriteResult` / `ProjectionRefreshResult`；`writer.py` 不再把 trade-event 写入和 rebuild 结果作为自由 dict 返回，调用方在 CLI / review / maintenance 输出边界转换。
- auto-close expired 的核心结果边界已收敛到 `ExpiredCloseDecision` / `ExpiredCloseApplyResult` / `ExpiredCloseRunResult`；`ledger.maintenance` 内部不再用自由 dict 表达待关闭决策和应用结果，`positions.maintenance` 只在 report/receipt/JSON 输出边界转换为 payload。
- broker trade intake 的 open preview / open write / close preview / close write 已收敛到 `BrokerTradeOpenPreviewResult` / `BrokerTradeOperation`；`trades.resolver.IntakeResolution.operations` 不再承载自由 operation dict。
- manual void / repair preview 已收敛到 `TradeEventInterventionPreview`，repair 写入结果用 `LedgerWriteResult` 表达；CLI / review 边界只消费 `to_payload()`。
- SQLite `trade_events.event_json` 新写入走 canonical `domain.domain.ledger.TradeEvent` schema；旧 event_json 只在 `event_codec` / migration 边界被解析。
- manual open/close/adjust、auto-close、bootstrap、broker normalized deal writer、manual void/repair 默认构造 canonical event；缺少 target lot 的 close 在写入前解析或 fail closed。
- `src/application/option_positions_service.py` 已物理删除；持仓/交易写入不再保留旧 service 兼容出口。
- v2 domain / repo / service 已物理删除；旧 `output_shared/state/option_positions_v2/` 只作为历史文件留存，不再有运行时代码读取。
- `tests/test_option_positions_legacy_retirement.py` 防止 v2 文件、底层 ledger public export、非 `ledger.api` runtime import 回归。

## New Domain Model

### ContractKey

合约语义身份，用于聚合和筛选，不是 lot 身份。

字段：

- `broker`
- `account`
- `underlying_symbol`
- `option_type`
- `position_side`
- `strike`
- `expiration_ymd`

约束：

- symbol 在边界 canonicalize，例如 `0700.HK`。
- expiration 必须是 `YYYY-MM-DD`。
- strike 用数值规范化后比较。
- account 必须是小写账户标签。

### TradeEvent

不可变交易事实。

事件类型：

- `open`
- `close`
- `expire_close`
- `assignment`
- `exercise`
- `adjust`
- `void`
- `repair`
- `verification`

公共字段：

- `event_id`
- `event_type`
- `event_time_ms`
- `broker`
- `account`
- `contract_key`
- `contracts`
- `price`
- `fees`
- `currency`
- `source`
- `raw_payload`

目标字段：

- open：生成新的 `lot_id`
- close / expire_close / assignment / exercise：必须携带 `target_lot_id`
- adjust：必须携带 `target_lot_id`
- void / repair：必须携带 `target_event_id`
- verification：只生成对账结果，不直接覆盖 canonical state

### PositionLot

lot 级当前状态，由事件流投影生成。

字段：

- `lot_id`
- `open_event_id`
- `contract_key`
- `opened_at_ms`
- `contracts_opened`
- `contracts_open`
- `contracts_closed`
- `status`
- `premium_open`
- `realized_pnl`
- `last_event_id`
- `close_event_ids`

约束：

- `lot_id` 全局唯一且稳定。
- `contracts_open >= 0`。
- `contracts_closed <= contracts_opened`。
- close 不能跨 lot、跨 expiry、跨 strike、跨 account。
- close 数量超过 open 数量时必须报错，不允许自动截断。

### PositionView

只读聚合视图。

用途：

- 风控上下文
- 报表
- 通知展示
- close advice 输入

字段：

- `position_key`
- `contract_key`
- `total_contracts_open`
- `lot_ids`
- `cash_secured_amount`
- `underlying_share_locked`
- `earliest_expiration_ymd`
- `diagnostics`

约束：

- 不能作为写入目标。
- 必须保留组成该聚合视图的 `lot_ids`。
- 如果聚合结果来自多个 lot，manual close 必须要求显式 `lot_id` 或显式策略。

## Proposed Module Boundaries

新模块建议使用独立 package，避免继续污染 `option_positions_*` 语义：

```text
domain/domain/ledger/
  identity.py          # ContractKey / symbol / expiration / strike canonicalization
  events.py            # TradeEvent schemas and validation
  lots.py              # PositionLot model and patches
  position_fields.py   # canonical lot record fields and open/close patch helpers
  projection.py        # event replay -> lots + views
  invariants.py        # fail-closed checks
  reconciliation.py    # broker/Feishu/manual snapshot comparison

domain/storage/repositories/
  ledger_repo.py       # append-only event store + projection snapshot repository

src/application/ledger/
  repository.py        # SQLite/config repository boundary and capability guards
  bootstrap.py         # explicit bootstrap/migration materialization into trade_events
  service.py           # open/close/auto-close/adjust use cases
  migration.py         # legacy v1/v2 replay import, read-only first
  risk_context.py      # risk-safe read model
  repair.py            # void/repair orchestration
  reports.py           # reporting adapter

src/interfaces/cli/
  ledger.py            # new human CLI surface
```

已删除的 legacy v2 quarantine：

- `domain/domain/option_position_ledger.py`
- `domain/domain/option_positions_v2.py`
- `domain/storage/repositories/option_positions_v2_repo.py`
- `src/application/option_positions_v2_service.py`
- `src/application/option_positions_service.py`
- `src/application/option_positions_facade.py`

旧模块不能再作为新写路径的 target resolver；v2 和旧 service 已没有默认读路径 fallback 或代码级迁移诊断入口。`src/application` / `src/interfaces` 的默认 runtime 代码不得直接 import `option_positions_service`。

## Write Path Rules

所有写路径统一进入 `src.application.ledger.service`：

```text
CLI / agent / intake / auto-close
-> ledger service
-> validate selector
-> resolve exact lot_id
-> append TradeEvent
-> replay projection
-> run invariants
-> publish read models
-> optional mirror sync / receipt
```

Close target resolution：

- `CloseTargetResolution` 是 close 写路径唯一的目标解析契约，包含 source、strategy、selector、target record_ids、contracts_to_close 和逐 lot candidate。
- manual close 的 selector 自动解析只能在唯一 open lot 命中时通过；多 lot 同 strike / 同 expiry 必须 fail closed 或要求显式 lot。
- broker close 按严格 contract identity 选择候选，并把 FIFO 拆分成逐 lot target close events；不同 expiry 不允许跨配。
- auto-close 不再重新用 contract selector 猜 lot，而是对当前待关闭 `record_id` 执行 explicit target resolution，并校验传入字段与当前 lot 身份一致。
- `position_key`、聚合 view、Feishu mirror id 都不能作为 close 写目标；写目标只能是当前 canonical `record_id` / lot id。

Manual close：

- 传入 `lot_id`：直接校验 lot 与 close payload 一致。
- 传入 contract selector：只有唯一 open lot 时才允许自动解析。
- 多 lot 命中：拒绝，要求显式 `lot_id`。

Auto-close expired：

- 从 canonical `PositionLot` 列表逐 lot 选择过期 lot。
- 每个 close event 必须写入 `target_lot_id`。
- same expiry 多 lot 要逐条关闭，不允许按聚合数量一次性扣减。

Trade intake：

- broker close 成交如无法解析唯一 `target_lot_id`，进入 pending/unmatched，不写 close。
- 不允许用同一 contract_key 静默扣最早 lot，除非明确配置 FIFO，并在事件里记录策略。

Repair：

- repair/void 必须 append-only。
- 有下游 close/adjust 依赖的 open event，修复前必须先处理依赖。

## Read Path Rules

风控只读 `RiskPositionView`：

```text
TradeEvent store
-> projection
-> invariant check
-> PositionLot current state
-> RiskPositionView
-> scan / close advice / notifications
```

如果 invariant check 失败：

- 不生成新的交易建议。
- 不执行 auto-close。
- runtime_status 暴露失败原因和阻断状态。
- operator 需要先 reconcile 或 repair。

## Reconciliation Rules

外部数据只做对账，不直接改 canonical state：

- broker current positions
- Feishu mirror
- manual verification snapshot

对账结果分类：

- `matched`
- `missing_in_ledger`
- `missing_in_broker`
- `quantity_mismatch`
- `identity_mismatch`
- `duplicate_lot_identity`
- `unmatched_close_event`

修正方式：

- 生成建议事件草稿。
- operator dry-run 审阅。
- apply 后 append 事件并重放。

## Migration Plan

### Phase 0: Stop Bleeding

目标：先阻断当前已知混读混写风险。

任务：

- auto-close 写路径不再从 v2/facade compat records 取 `record_id`。
- manual close、auto-close、adjust、repair 使用同一 lot resolver。
- close matcher 加完整 contract identity 校验。
- v2 compat records 标记为 read-only display，不可用于写入 target。

验收：

- same expiry 多 lot 不误合并。
- same strike 不同 expiry 不跨期关闭。
- manual close 与 auto-close 对同一 lot 得到同一 target。

### Phase 1: New Domain Kernel

目标：先写纯 domain，不接生产存储。

任务：

- 建 `domain/domain/ledger/`。
- 实现 `ContractKey`、`TradeEvent`、`PositionLot`。
- 实现纯函数 projection。
- 实现 invariant diagnostics。

验收：

- 不导入 `src/`。
- 纯 pytest fixture 可回放。
- 所有 identity 冲突能返回确定 diagnostics。

### Phase 2: Shadow Replay

目标：用历史 v1/v2 数据生成新账本 shadow projection，不影响线上读写。

任务：

- 从旧 `trade_events` 导入新 `TradeEvent`。
- 从旧 `position_lots` / v2 snapshots 只读构建 baseline import events。
- 生成 shadow `PositionLot` 和 `RiskPositionView`。
- 输出 reconciliation report。

验收：

- shadow projection 和当前可解释仓位差异全部列出。
- 不能静默吞掉差异。
- 支持当前 `sy/0700` 这类 same expiry / same strike / different expiry fixture。

### Phase 3: Risk Read Cutover

目标：风控先读新 read model，但旧写路径暂不删除。

任务：

- `src/application/positions/context_builder.py` 改为读取新 `RiskPositionView`。
- close advice 输入改为读取新 `PositionLot` / view。
- runtime_status 暴露 ledger invariant 状态。

验收：

- 如果新账本 invariant 失败，scan/close advice fail closed。
- dual-run 对比旧上下文和新上下文。
- 报表差异可解释。

### Phase 4: Write Cutover

目标：把核心写路径切到新 ledger service。

任务：

- manual open / close
- auto-close expired
- trade intake open / close
- adjust / void / repair
- Feishu sync mirror

验收：

- 所有写入 append-only。
- 每次写入后 replay + invariant check。
- 旧 `option_positions_*` 只能通过 adapter 读新状态或迁移历史。

当前进展：

- manual open / close / adjust、auto-close expired、trade intake open / close、void / repair 已切到 ledger preflight / service 语义。
- service 会在非重复写入前运行 `position_lots` replay、reconciliation、target lot identity 校验和 projected close 校验。
- idempotent retry 先按旧 event_id 识别为 duplicate，不要求已关闭 lot 仍出现在 open projection。
- 写后 `position_lots` 发布从 canonical ledger replay 生成；v2 append / refresh 已从默认写后链路退休。
- auto-close expired 已接入同一个 lot close preflight，写入前校验 `expire_close` 事件不会跨 lot / strike / expiry。
- manual close、trade intake close、auto-close expired 已统一输出 `CloseTargetResolution`，并把同一解析 payload 贯穿 preview / diagnostics / write operation / stored raw_payload。
- trade intake close 已优先从当前 writable `position_lots` 取候选，并在真实 repo 下按 strict exact FIFO match 拆成逐 lot target close events。
- Feishu `option_positions` 已降级为 mirror/sync surface，不再是 bootstrap 输入或默认事实源。
- 旧 SQLite `option_positions` 表已降级为显式 bootstrap 输入；默认运行路径不会在空投影时自动迁移旧表。
- facade、manual close matcher、trade intake close matcher、agent monthly income report 已收敛到 canonical `position_lots` 读面，不再 fallback 到通用 `list_records`。
- `domain.domain.option_position_lots` 已降级为兼容 re-export，核心 runtime 使用 `domain.domain.ledger.position_fields`。

### Phase 5: Legacy Retirement

目标：删除旧 v1/v2 混合模型。v2 代码已完成物理删除，剩余 `option_positions_*` 名称只允许作为用户入口、历史配置键、legacy local bootstrap 或 mirror 语义，不允许作为核心写模型 owner。

任务：

- 保持 v2 文件、导出和 runtime import 不回归的结构性测试。
- 保持 facade、close target resolver、agent report 不 fallback 到 legacy compat records。
- 默认 runtime code 直接依赖 `src.application.ledger.read_model`；旧 `option_positions_facade` 已删除并由结构性测试防回归。
- legacy SQLite `option_positions` table 只能通过显式 `bootstrap_from_legacy_sqlite` 迁移入口读取。
- 文档更新：`OPTION_POSITIONS_MIGRATION.md` 和 `OPTION_POSITIONS_REPAIR.md` 改为 legacy-only。
- CLI 命令给出迁移后的新入口。

验收：

- 代码里没有“v2 compat record -> writable record_id”的路径。
- runtime code 不存在 `option_positions_v2_service`、`option_positions_v2_repo`、`domain.domain.option_positions_v2`。
- 风控、报表、通知、同步都读同一个 projection。

## Regression Fixtures

必须覆盖这些场景：

- `sy/0700`：
  - short call strike `510` exp `2026-05-28` qty `2`
  - short put strike `450` exp `2026-05-28` qty `6`
  - short put strike `450` exp `2026-06-29` qty `3`
- same underlying + same strike + same expiry + multiple lots
- same underlying + same strike + different expiry
- same underlying + same expiry + put/call 同时存在
- manual close selector 命中多个 lot 时 fail
- auto-close expired 逐 lot 关闭
- manual close / broker close / auto-close 对同一 logical close 输出可审计的 `CloseTargetResolution`
- close target lot_id 不存在时报错
- close target lot_id 与 payload contract_key 冲突时报错
- broker close 成交无法唯一匹配时进入 unmatched
- replay 后 lot_id 稳定
- Feishu sync metadata 不影响 projection，且 metadata 回写不能携带业务 lot 字段
- repair/void append-only，且有下游依赖时阻断

## Quality Gates

每个阶段至少运行：

```bash
python3 -m pytest tests/test_positions_*.py
python3 -m pytest tests/test_trades_*.py
python3 -m pytest tests/test_ledger_*.py
git diff --check
```

新账本 domain 阶段应增加独立测试：

```bash
python3 -m pytest tests/test_ledger_*.py
```

上线前需要 shadow replay 真实本地状态，并保存：

- event count
- lot count
- open lot count
- invariant diagnostics
- old/new risk context diff
- reconciliation summary

## Operator Safety

以下动作必须先 dry-run：

- migration import
- reconciliation apply
- broker snapshot repair
- Feishu prune
- legacy retirement cleanup

任何删除旧数据的动作都不属于前四个阶段。

## Success Criteria

重构完成时必须满足：

- 系统只有一个 canonical trade ledger。
- 系统只有一个 canonical position projection。
- 风控输入只能来自新 projection。
- close/auto-close/manual-close 必须 target 到确定 `lot_id`。
- `position_key` 不再作为写入身份。
- v1/v2 旧模块不再参与 target resolution。
- 任意一条 open lot 可以解释它的完整事件链。
- 任意一条 close event 可以解释它关闭的具体 lot。
