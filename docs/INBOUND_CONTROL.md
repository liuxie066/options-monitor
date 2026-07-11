# Inbound Control

`./om assistant handle` is the common application entry for local, Feishu, and
WeChat messages. It has two mutually exclusive paths:

```text
explicit protocol -> deterministic Control
all other text    -> read-first Copilot
                     -> optional validated Control preview request
```

## Control Boundary

Control owns:

- sender allowlist and message-id idempotency;
- slash commands and unambiguous pending-operation replies;
- deterministic read command payloads;
- write previews, confirm/cancel, apply, and readback receipts;
- `control_json` audit records and operation timelines.

Control does not infer business intent from free text, select tools for natural
language questions, or synthesize analytical conclusions.

Read commands execute canonical tools from `agent_tool_registry`. Write-capable
commands use:

```text
explicit command
-> preview receipt
-> pending operation
-> explicit confirmation
-> apply path
-> readback receipt
```

No model can enter the apply path.

When one expiry notice contains multiple contracts, `/record-expiry` creates one
pending operation per contract. Confirm the whole notice with the plain reply
`确认` (the conversation resolves its unique `command_id`), use
`/confirm trade <command_id>` as an explicit fallback, or confirm individual
contracts with `/confirm trade <operation_id>`.

## Copilot Boundary

Messages that are not explicit Control protocol enter Copilot when both
`assistant.enabled` and `assistant.copilot.enabled` are true.

Copilot uses:

```text
Channel UI -> Copilot Service -> Host -> om_chat Agent
                                      -> pure-read tools
                                      -> request_control_preview
                                         -> deterministic Control preview
```

There is one generic Scene. Service does not classify income, positions,
diagnostics, symbols, strategies, or monthly reviews. Host projects only
canonical pure-read tools and owns run/session/event lifecycle. The model never
receives write, confirm, cancel, or apply tools. Its only state-change surface
is a generic preview request projected from the Control capability catalog.

After Control returns, the inbound service writes a structured receipt to
Copilot session history. Before every later channel turn it injects the current conversation's
pending-operation summaries from the operation store. The operation store, not
chat history, remains authoritative for confirmation and cancellation.

## Reply Contract

Channel adapters render the returned `AssistantTurnResult.response_text`.

- Control replies may include deterministic results, preview requests, or
  permission errors.
- Copilot replies contain the model's final answer or an explicit runtime/data
  failure.
- Unauthorized-sender behavior remains channel-policy dependent.

Channel adapters must not import command parsers, tool implementations, or
Copilot internals directly.

## Configuration

```yaml
assistant:
  enabled: true
  copilot:
    enabled: true
  active_model: deepseek-default
```

`assistant.models` defines model profiles. Generated
`resolved/config.assistant.json` must be rebuilt after authoring changes.
Planner flags, task profiles, per-business Scene allowlists, and
`assistant.agent_loop` are not supported runtime controls.

## Diagnostics

```bash
./om assistant commands --format text
./om assistant capabilities
./om assistant llm-check
./om-agent run --tool operation_timeline --input-json '{"limit":10}'
```

Copilot Host persists real sessions, runs, and model/tool events. Control audit
rows must not be repackaged as synthetic Agent plans, evidence bundles, or
verifier traces.

Durable Host diagnostics are available through:

```bash
./om copilot runs --host-db <audit-db>
./om copilot events --host-db <audit-db> --run-id <run-id>
./om copilot cancel --host-db <audit-db> --run-id <run-id>
./om copilot resume --host-db <audit-db> --run-id <run-id> --assistant-config <path>
./om copilot replies --host-db <audit-db>
```

`cancel` above cancels an active analysis run. It is distinct from cancelling a
pending deterministic Control operation. Channel replies use the Host outbox so
temporary delivery failure is retryable and the same delivery key is not sent
twice.

## Non-Goals

Do not add:

- hardcoded business-question routing;
- task-specific answer templates;
- a second tool registry;
- ordinary LLM fallback without tools;
- natural-language writes.
