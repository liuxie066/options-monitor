# Gateflow Goal Confirmation — Channel-aware Notification Rendering

## Gate

- Work unit: `channel-aware-notification-rendering`
- Gate: `goal confirmation`
- Decision: `pass`
- Baseline: `main@9b1e200ed313407c3f20708a33549ba0d5e46cf0`
- Branch: `feat/channel-aware-notification-rendering`

## Goal and Motivation

Keep one canonical Markdown business rendering while projecting it by delivery channel: proactive Feishu App notifications use `post` with exactly one `md` node, and WeChat ClawBot continues to receive the same Markdown unchanged. The change must preserve delivery confirmation, idempotency, retry, and ambiguity semantics.

## Success Signals

- Feishu proactive payload is `msg_type=post` with `zh_cn.content=[[{"tag":"md","text": markdown}]]` and no `title`.
- WeChat receives byte-for-byte identical canonical Markdown through its existing `text` path.
- Oversized Feishu post requests fail before token acquisition or message HTTP, with structured non-content diagnostics and no retry/fallback.
- Timeout, transient, confirmed, or ambiguous post attempts never auto-fallback to text.
- Focused and broader regression suites, static checks, compileall, and diff checks pass.

## Scope Boundary

In scope: Feishu proactive sender payload/preflight, notification adapter normalization, scheduled-notification diagnostic propagation, tests, and Agent Wiki documentation.

Out of scope: business-renderer rewrites, Feishu inbound replies/outbox, WeChat protocol changes, runtime format switches, fragmentation/truncation, production config, live notifications, release, deployment, and remote upgrade.

## Direct Evidence and First-principles Judgment

- `src/infrastructure/feishu_bot.py` owns Feishu message payload and transport retry behavior.
- `src/application/notification_delivery_adapter.py` currently routes proactive Feishu notifications through `send_text_message()` while WeChat has an independent adapter.
- `src/application/scheduled_notification.py` owns per-attempt audit/retry state and must retain provider-local deterministic failure details without learning Feishu payload shape.
- Existing business renderers already produce the canonical Markdown required by both channels; adding channel-specific renderers or a registry would duplicate ownership without a current need.

## Product Decisions Already Confirmed

The user selected Feishu方案 B (`post` + single `md` node), kept WeChat Markdown, required controlled code/version rollback to text if visual acceptance fails, accepted the revised size/preflight contract, and explicitly instructed Gateflow execution using the revised plan and second review.

## Blocking Open Questions

None for local implementation. Real Feishu canaries remain separately authorization-gated.

## Residual Risks

- Feishu desktop/mobile rendering and exact live API acceptance remain external risks assigned to the separately approved canary gate.
- The deliberate fail-closed size policy can leave an oversized notification unsent; operator replay is only allowed after controlled rollback and explicit decision.

## Next Entry Point

`accepted plan commit`
