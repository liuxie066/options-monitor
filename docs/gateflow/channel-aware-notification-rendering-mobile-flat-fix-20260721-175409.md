# Gateflow Fix Artifact — Mobile-flat Notification Rendering

## Scope and Decision

- Work unit: `channel-aware-notification-rendering`
- Reopened gate: `Slice 5 — visual acceptance`
- Trigger: later operator feedback that two-level Markdown indentation is difficult to read on Feishu mobile
- Fix status: implementation, aggregate review/re-review, and deterministic validation complete; pending new canary authorization
- Safety boundary: no runtime config change, no real notification send, no merge, no release, and no deployment

The provider contract remains unchanged:

- Feishu proactive notifications use `post` with exactly one `md` node.
- WeChat ClawBot receives the same canonical Markdown string.
- Feishu request-size preflight, idempotency, retry, delivery confirmation, and controlled text rollback semantics are unchanged.

## P0 Presentation Contract

All proactive notification families now follow the same mobile-first rules:

1. Use one `# OM · <message family> · <account/component>` primary title when the message is standalone.
2. Present status, market, time, conclusion, diagnostics, and business facts as flat `字段｜值` lines.
3. Use only limited `##` sections for major groups; do not use Markdown tables.
4. Do not emit nested Markdown lists. Candidate/position detail is flattened into adjacent field lines rather than indented child bullets.
5. Display user-facing ISO timestamps in Beijing time without changing stored or audited timestamps.
6. Preserve business values and safety statements, including assigned-stock confirmation-before-write and Combo Yield relation-pending behavior.

## Implemented Families

- Daily Brief: flat title/status/market/data shell; flat candidate, position, omission, change, and capacity lines.
- Compact Tick and Legacy Tick: common Decision Brief title/status/time/conclusion shell; flat candidate detail, close-advice, missing-data, and cash lines.
- No-candidate heartbeat: represented as a Decision Brief state rather than a special sentence-only template.
- Delivery failure: flat System Alert shell and one line per failed account.
- OpenD failure/recovery: flat System Alert/System Notice shell with Beijing time, impact/result, reason, diagnostic code, and optional detail.
- Trade receipt: flat Receipt shell, explicit status, transaction fields, confirmation-safe assigned-stock choices, and preserved Combo Yield relation warning.
- Position maintenance receipt: flat Receipt shell, result summary, Beijing time, completed rows, and failure rows.

## Deterministic Validation

Final validation after aggregate review fixes:

```text
Focused renderer/delivery suite: 178 passed
Broad daily-brief/multi-tick/notification suite: 348 passed
Ruff: passed
compileall: passed
Git diff whitespace check: passed
```

Representative tests enforce:

- one primary H1 for standalone messages;
- no `###` headings in final messages;
- no Markdown blockquotes or nested ordered/unordered lists;
- no Markdown tables;
- real no-candidate pipeline fragments wrap into Legacy/Compact scheduled shells without a second H1;
- real cash footer rows become flat `账户｜...` and `数据｜...` fields;
- preserved business values and delivery identity projection into the Feishu single `md` node.

## Aggregate Review Closure

- Initial aggregate review: `docs/reviews/code-review-20260721-180557.md`
- Re-review: `docs/reviews/code-review-20260721-181035.md`
- `DR-MF-01` — accepted — fixed: no-candidate pipeline output is a body fragment; standalone heartbeat retains the Decision Brief shell.
- `DR-MF-02` — accepted — fixed: actual cash footer Markdown is flattened in Legacy, Compact, and heartbeat paths.
- Blocking open questions: none.
- Aggregate review status: pass.

## Canary State

- The previous API/readback evidence remains valid.
- The previous visual qualification is superseded and marked `needs-fix` in `docs/gateflow/channel-aware-notification-rendering-canary-20260721-164120.md`.
- No new canary was sent during this fix.
- A new five-category canary requires separate explicit operator authorization.
- If the corrected flat Post presentation still fails product acceptance, follow the existing controlled code/version rollback to text; do not auto-fallback after an ambiguous or confirmed Post send.

## Deferred P1 Work Unit — Renderer Consolidation

P1 is intentionally excluded from this P0 fix and must run as a separate work unit with its own plan and reviews:

1. Daily Brief becomes the only primary renderer for scheduled notifications.
2. Compact Tick enters a compatibility period.
3. Legacy Tick is marked deprecated and then removed.
4. No-candidate becomes a Decision Brief state owned by the primary renderer.
5. OpenD and delivery-failure messages share one System Notice shell.
6. Trade and maintenance messages share one Receipt shell.

This separation avoids combining a presentation correction with renderer ownership migration and deletion risk.
