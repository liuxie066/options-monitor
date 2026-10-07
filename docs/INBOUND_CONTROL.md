# Inbound Control

`./om bot handle` is the common application entry for local, Feishu, and
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

`bot.enabled` is the single activation switch for new inbound Control commands and
model conversations. Non-Control messages enter the model path only when enabled.
Authenticated cancellation of an already admitted Feishu analysis remains available
so disabling Bot does not strand active work; replacement work still requires Bot enabled.
The Bot scene selects canonical read tools. Portfolio queries follow the
`portfolio_management.enabled` integration gate; there is no extra Bot toolset switch.
Bot reads the channel market by default. A validated
`bot.read_markets: [us, hk]` grant can add the other market for
authenticated senders. The Host resolves each requested market to a fresh
runtime config in the same runtime root and checks the account in that market;
the model cannot supply a config path or expand this grant. Dual-market reads
without a market or recognizable symbol require clarification. Revoking or
changing the grant stops an active answer before it is persisted.
Each controlled rebuild of `config.bot.json`, including a version upgrade,
creates a new read generation even if `read_markets` is unchanged. Channel
sessions and personal memory start in that new generation; old records remain
stored but are not automatically carried into it.

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

`read_markets` limits business reads, not Control preview requests. A supported
change with a clear target goes to deterministic Control, which checks the
sender's operation permissions and resolves the target independently. For
example, a US-bound channel can request a preview for disabling
`3690.HK`'s `combo_yield.enabled`; Control still requires authorization and a
separate confirmation before applying it. This does not grant HK read access
or disable the symbol's CSP/CC monitoring.

After Control returns, the inbound service writes a structured receipt to
Bot session history. Before every later channel turn it injects the current conversation's
pending-operation summaries from the operation store. The operation store, not
chat history, remains authoritative for confirmation and cancellation.

## Reply Contract

Channel adapters render the returned `BotTurnResult.response_text`.

- Control replies may include deterministic results, preview requests, or
  permission errors.
- Bot replies contain the model's final answer or an explicit runtime/data
  failure.
- Unauthorized-sender behavior remains channel-policy dependent.

Channel adapters must not import command parsers, tool implementations, or
Bot internals directly.

## Configuration

```yaml
bot:
  enabled: true
  active_model: deepseek-default
```

Bot loads tools by scene; there is no portfolio toolset switch. PM-backed reads
use the canonical `portfolio_management.enabled` integration boundary.
`om bot configure` changes `bot.enabled`. The default is false. Disabling Bot
stops inbound processing and real model execution; local configuration, status,
and diagnostic commands remain available. Saving configuration does not start a service.

`bot.models` defines model profiles. Generated
`resolved/config.bot.json` must be rebuilt after authoring changes.
Planner flags, task profiles, per-business Scene allowlists, and
`assistant.agent_loop` are not supported runtime controls.

## Diagnostics

```bash
./om bot commands --format text
./om bot capabilities
./om bot llm-check
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

`trade_attribution_read` 只读所选市场配置账户内的成交归属；
`option_positions_read.events` 提供独立的本地交易事件证据，`assignment`
事件不等于券商确认。自然语言选择由 Bot 的
`request_control_preview` 交给 Control；也可输入：

```text
/attribute lx <execution_key> ordinary
/attribute lx <execution_key> wheel <wheel_branch_id>
/attribute lx <execution_key> wheel <branch_id_1>,<branch_id_2>,<branch_id_3>
/attribute lx <execution_key> combo <strategy_group_id>
/confirm attribution <operation_id>
/cancel attribution <operation_id>
```

`wheel` 的逗号列表按每张合约列一个完整分支 ID：上例把同一笔 3 张成交分别分给三个分支。
部分平仓或待确认指派不会自动释放这三个分支的占用。

`execution_key` 使用查询返回的规范成交身份，不是 broker 原始订单编号。`/pending` 只列当前对话的预览。
确认要求同一已鉴权渠道、sender、非空 conversation、当前写权限和签名，并重新检查账本资源与账户映射。
模型没有 confirm/apply 权限；归属操作不下单、不重复记经济成交、不重发原成交回执。
普通单腿确认不依赖 provider 容量；Wheel/Combo 依赖当前完整竞争证据和新鲜容量。
预览后实质变化需重新预览，重复确认按既有事实读回；取消已认领的操作不撤销账本。
