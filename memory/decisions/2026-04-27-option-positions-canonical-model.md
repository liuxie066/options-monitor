## Context

最近几轮 option positions 重构同时涉及 SQLite 主存储、Feishu bootstrap、手工写入后的同步、cross-account sync collision 修复，以及 bootstrap snapshot 在后续事件投影中的保留问题。

当前代码已经表现出明确方向：本地事件流负责定义仓位事实，`position_lots` 只是由事件流投影出的当前视图，Feishu 只承担 bootstrap 输入和远端镜像输出。

为了停止继续靠补丁修语义边界，需要先把 option positions 的 canonical model 固定下来。

## Decision

option positions 采用以下 canonical model：

1. `trade_events` 是唯一 canonical truth。
2. `position_lots` 是由 `trade_events` 全量投影生成的当前仓位视图，不是第一真相源。
3. Feishu、legacy 表、以及历史 `position_lots` snapshot 只允许作为 bootstrap 输入；目标都是转换成 `trade_events -> projection -> position_lots`。
4. Feishu sync 是后置副作用。它可以回写 `feishu_record_id` / `feishu_sync_hash` / `feishu_last_synced_at_ms`，但这些字段不定义业务身份，也不能改变投影结果。
5. 身份职责固定如下：
   - `event_id`: 事件身份，仅属于 `trade_events`
   - `record_id`: 本地 lot 身份，仅属于 `position_lots`
   - `source_event_id`: lot 回溯到哪个事件生成，用于投影追踪
   - `position_id`: 业务语义标签，不承担跨账户唯一性
   - `feishu_record_id`: 远端镜像锚点，只服务同步
6. cross-account matching 不能只靠 `position_id`，必须至少同时满足 `account`，并继续受 `broker + symbol + option_type + side` 约束。

## Rationale

- 当前 `scripts/option_positions_core/service.py` 已经在每次事件写入后重新投影 `position_lots`，说明事件流才是真正驱动状态的层。
- 当前 `scripts/option_positions_core/ledger.py` 负责 open/close 匹配与 lot 投影，天然适合作为仓位状态的唯一推导路径。
- bootstrap snapshot 虽然会被包装成 `TradeEvent(source_type="bootstrap_snapshot")`，但它的职责应该是把历史状态导入 canonical event flow，而不是长期与事件流并列。
- Feishu sync metadata 回写到本地 lot 是工程上的必要妥协，但这些字段必须被视为集成状态，不能反向决定本地业务语义。
- `position_id` 当前不包含 account，也不保证全局唯一，因此不能承担跨账户 identity；最近的 collision 修复说明这一点必须显式固定。

## Locked Rules

- 任何本地状态变更都应先变成 `TradeEvent`，再重投影 lot。
- manual open / manual close 的真实动作是追加事件，不是直接 patch lot。
- 重投影后的 lot 结果只由 `trade_events` 决定，不由 `feishu_*` metadata 决定。
- sync 失败不能破坏本地 canonical state。
- bootstrap seed lot 在后续事件投影中必须保留，除非被同账户同业务键的 close event 合法消费。
- 远端匹配遇到 duplicate remote rows 时，应优先显式报 conflict，而不是模糊吞并。

## Known Ambiguities To Keep Watching

- `record_id` 目前存在两种来源：普通事件投影生成的 `lot_{event_id}`，以及 bootstrap snapshot 继承的历史 `lot_record_id`。本轮先锁边界，不强行统一。
- close matching 仍然依赖 `note.exp` 辅助判断 expiration，这属于脆弱点，需要在后续最小实现修复阶段继续收紧。
- `position_lots` 同时承载业务字段和 sync metadata，是当前现实下的混合视图；本轮先保证 metadata 不影响 canonical reprojection。
