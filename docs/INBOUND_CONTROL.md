# Inbound Control

`./om assistant handle` is the common application entry for local, Feishu, and
WeChat messages. It has two mutually exclusive paths:

```text
explicit protocol -> deterministic Control
all other text    -> read-first Bot
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

## Bot Boundary

Messages that are not explicit Control protocol enter Bot when both
`assistant.enabled` and `assistant.bot.enabled` are true.
Portfolio-management access is a separate fail-closed projection: `portfolio_query`
is available to Bot only when `assistant.bot.toolsets.portfolio` is also
true. Missing values mean disabled.

Bot uses:

```text
Channel UI -> Bot Service -> Host -> om_chat Agent
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
Bot session history. Before every later channel turn it injects the current conversation's
pending-operation summaries from the operation store. The operation store, not
chat history, remains authoritative for confirmation and cancellation.

## Reply Contract

Channel adapters render the returned `AssistantTurnResult.response_text`.

- Control replies may include deterministic results, preview requests, or
  permission errors.
- Bot replies contain the model's final answer or an explicit runtime/data
  failure.
- Unauthorized-sender behavior remains channel-policy dependent.

Channel adapters must not import command parsers, tool implementations, or
Bot internals directly.

## Configuration

```yaml
assistant:
  enabled: true
  bot:
    enabled: true
    toolsets:
      portfolio: false
  active_model: deepseek-default
```

Change `portfolio` to `true` to share the portfolio-management pure-read toolset
with Bot. This does not start the portfolio-management API service and does
not change the external `./om-agent` Tool Gateway contract.

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

Bot Host persists real sessions, runs, and model/tool events. Control audit
rows must not be repackaged as synthetic Agent plans, evidence bundles, or
verifier traces.

Durable Host diagnostics are available through:

```bash
./om bot runs --host-db <audit-db>
./om bot events --host-db <audit-db> --run-id <run-id>
./om bot cancel --host-db <audit-db> --run-id <run-id>
./om bot replies --host-db <audit-db>
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

### 已入账成交的策略归属

`trade_attribution_read` 只读当前配置账户内的成交归属。自然语言选择由 Bot 的
`request_control_preview` 交给 Control；也可输入：

```text
/attribute lx <execution_key> ordinary
/attribute lx <execution_key> wheel <wheel_branch_id>
/attribute lx <execution_key> combo <strategy_group_id>
/confirm attribution <operation_id>
/cancel attribution <operation_id>
```

`execution_key` 使用查询返回的规范成交身份，不是 broker 原始订单编号。`/pending` 只列当前对话的预览。
确认要求同一已鉴权渠道、sender、非空 conversation、当前写权限和签名，并重新检查账本资源与账户映射。
模型没有 confirm/apply 权限；归属操作不下单、不重复记经济成交、不重发原成交回执。
普通单腿确认不依赖 provider 容量；Wheel/Combo 依赖当前完整竞争证据和新鲜容量。
预览后实质变化需重新预览，重复确认按既有事实读回；取消已认领的操作不撤销账本。
