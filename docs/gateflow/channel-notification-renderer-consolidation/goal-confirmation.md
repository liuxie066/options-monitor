# Gateflow Goal Confirmation — Channel Notification Renderer Consolidation

- Gate: `goal confirmation`
- Work unit: `channel-notification-renderer-consolidation`
- Branch: `plan/channel-notification-renderer-consolidation`
- Baseline: `feat/channel-aware-notification-rendering@cd3d6c3d38d7250017d152822d664950d42a578b`
- User confirmation: 2026-07-21 — “用gateflow 完成改造”
- Decision: `pass`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/goal-confirmation.md`

## Goal and motivation

收敛重复的通知 renderer，使 Daily Decision Brief 成为 scheduled automatic ordinary notification 的唯一主 renderer；Compact 仅保留兼容用途；Legacy 进入废弃/删除状态；无候选成为 Decision Brief 状态；System Notice 与 Receipt 各自共享窄 presentation shell。

## Success signals

- scheduled ordinary delivery 不再读取 Compact/Legacy sender switch；
- manual/force 只生成扫描 artifacts，不自动发送普通通知；
- old config keys 在 Phase A/B accepted-with-warning 且无控制权；
- multi-market unsupported 有确定性 terminal failure；
- runtime status/analysis 将 `symbols_notification.txt` 标为 compatibility-only；
- System Notice/Receipt shell 不吸收 caller-owned state machine；
- P0 Feishu Post / WeChat Markdown transport identity 不变。

## Scope boundary

本轮实施 approved Phase A slices 1-4 及其 validation/review。Phase C 的 Legacy 物理删除、strict config rejection 和 legacy read alias removal 仍受 compatibility release、生产证据与 CEO hard-pause gate 约束，不得提前执行。

不修改生产 config，不发送真实通知，不 release/deploy，不 merge，不把 PR 标记 ready。

## Direct code evidence

- `src/application/tick_notification_flow.py` 当前仍由 `daily_brief.enabled` / `render_style` 选择 sender renderer。
- `src/application/tick_run_context.py` 的 tick key 未包含 trigger kind，completion 固定 `ok=true`。
- `src/application/agent_tools/runtime_status_impl.py` 与 `analysis.py` 仍把 `notification` artifact 作为 notification existence/health signal。
- OpenD/system delivery failure 与 trade/maintenance receipt 各自存在重复 presentation shell。

## No overdesign

不新增 registry、DSL、继承体系、通用 message DTO、第二 sender、第二 finalizer或新存储；优先删除旧 authority，并窄扩现有 finalization/idempotency/read-model contracts。

## Blocking open questions

无。生产 launcher 事实与 Phase C consumer inventory 已被定义为后续 evidence gate，缺失时停止推进而不猜测。

## Residual risks

- account+market 多消息 delivery：`assigned to later work unit` — `option-notification-experience`。
- Phase C compatibility evidence：`covered by later approved slice` — P1 Slice 6 hard-pause gate。
- 真实客户端视觉差异：`covered by later approved slice` — 独立授权 canary/release gate。

## Gate state

- Current gate: `goal confirmation pass`
- Next entry point: `plan`
