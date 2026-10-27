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

Every delivery checks the current sender allowlist before consuming an audited
response, including duplicates and expired deliveries. A denied duplicate records
the current denial without replaying or replacing the original response; an
authorized retry continues to reuse that response without rerunning the command.

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

Manual-trade labeled integer fields accept whole numeric values such as `1` and
`1.0` without rounding. Fractional values return `INPUT_ERROR` before a trade
preview or pending operation is created; correct the value in a new message.

Confirmed `/monitor-run` operations use the guarded `tick-cron` wrapper. When
its return code is 0 and its entire stdout, after stripping surrounding whitespace,
is `SKIP_LOCKED`, Control records terminal `skipped` with reason `tick_cron_locked`.
This attempt did not start a tick and has no `applied_at`. The response and operation
timeline use `ok=True` for successful request handling; that flag is not evidence
of tick execution or notification delivery. The receipt explicitly reports the
occupied market lock and skipped attempt. Other successful output and nonzero
exit codes retain their normal success/failure handling. Skipped operations are
not retried automatically: repeated confirmation cannot run them again, and a
later attempt requires a new preview and confirmation.

When one expiry notice contains multiple contracts, `/record-expiry` creates one
pending operation per contract. Confirm the whole notice with the plain reply
`确认` (the conversation resolves its unique `command_id`), use
`/confirm trade <command_id>` as an explicit fallback, or confirm individual
contracts with `/confirm trade <operation_id>`.

If confirmation loses its receipt after a possible ledger commit, the operation
remains unverified. Confirm the original operation ID to check its signed request
against effective ledger evidence; this recovery never repeats the write. A close
or legacy expiry without a stable operation identity remains unverified. Ordinary
completed single-operation confirmations retain their existing rejection behavior.
Batch retries inspect every child, including expired or cancelled children; an
unfinished child prevents whole-batch success. `applied_count` counts children
completed or recovered by this call, while `already_applied_count` counts earlier
completions whose ledger evidence was checked again.

## Bot Boundary

`bot.enabled` is the single activation switch for new inbound Control commands and
model conversations. Non-Control messages enter the model path only when enabled.
Authenticated cancellation of an already admitted Feishu analysis remains available
so disabling Bot does not strand active work; replacement work still requires Bot enabled.
The Bot scene projects a fixed list of canonical read tools; it currently excludes
the portfolio tools. `portfolio_management.enabled` does not add tools to the
Scene. See the [capability boundary](OM_AGENT_CAPABILITY_MAP.md#surfaces) for the
separate Tool Gateway PM contract.
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

Use the complete top-level `bot` authoring example in the
[capability map](OM_AGENT_CAPABILITY_MAP.md#configuration), including the model
profile selected by `bot.active_model`.

Bot loads the fixed Scene tool list; there is no portfolio toolset switch.
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

`operation_timeline` 的 `statuses` 为字符串列表；非空规范化列表与 `operation_id` 及认证会话范围同时过滤。省略、空列表或仅含空白项时，精确 ID 查询不限制状态，无非空 ID 的查询使用既有生命周期默认状态。历史 audit-only 证据仍按原规则重建。

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

Feishu WebSocket worker 在启动及空闲期间恢复到期回复，自主恢复每秒最多处理一条，
沿用 Host outbox 的 backoff、delivery lease、24 小时 capability TTL 与冻结正文/UUID。
`--no-reply` 和缺少 app 凭据均阻止旧回复领取；不需要新入站消息触发恢复。
只有整数 `code=0` 且 `data.message_id` 为非空字符串才确认送达并清除 payload；
缺失或模糊回执保留原回复与发送身份，不使用 client UUID 代替 provider 回执。
worker 停止后不再启动恢复或处理排队事件；已经进入处理/transport 的调用按现有结果结算，
停止无法撤回已发请求。过期 lease 回收的回复也必须通过 capability TTL。

Feishu 官方回复接口的 UUID 去重承诺为一小时，24 小时 capability TTL 不能代替该保证。
Host 保存 `replay_until`：明确拒绝后的尝试可建立新窗口；网络/5xx/模糊回执、失效发送
lease 表示可能已有外部效果，原窗口不得延长，后续明确拒绝也不能抹掉较早的未知事实。
未知回复在窗口截止（含截止时刻）、缺失可证明窗口或 capability 到期后进入
`delivery_unknown`，保留正文与 UUID，不自动领取、清空或盲重发。
升级前已有尝试但没有窗口的记录同样保留为未知；首次尚未尝试的 pending 不受该迁移限制。
用 `./om bot replies --host-db <audit-db>` 查看 `delivery_unknown` 与
`last_error=delivery_outcome_unknown`，人工核对 provider 的实际消息后再决定后续操作；
本次没有新增强制重发或人工结算命令。

Feishu 结算必须匹配 claim 的 `attempt_count`；旧 sender 或缺失 attempt 的旧调用不能改变
新领取的状态。回复 POST 每次只尝试一次，由 outbox 持久结算与调度负责重试；实际 POST 前
（包括等待 token 后）再次检查冻结窗口。已选择的 text fallback 在发送前持久化，后续重试
只使用原 fallback UUID/正文；较早 card 结果未知时不另建 fallback 发送身份。

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

## Concurrent message execution and unknown outcomes

Within one inbound audit SQLite database, the normalized `(channel, message_id)`
obtains one durable execution claim before Control, Bot, or analysis cancellation
runs. A concurrent authorized duplicate receives the stored completed result or a
non-success observation; it never acquires a second execution right. Requests
without a message ID retain independent local invocation semantics. Every replay
still checks the current sender allowlist and trusted conversation/config scope.

`INBOUND_PROCESSING` (`status=processing`, `ok=false`) means the original request
is still within its observation budget. `INBOUND_INCOMPLETE` (`status=incomplete`,
`ok=false`) means its business outcome is not confirmed. Both remain visible in
JSON/text CLI output with a nonzero exit code. The shared Feishu/WeChat reply
decider does not enqueue these observations into the stable final-result outbox
identity; completed known business failures still follow normal reply rules.

The budget remains 180 seconds from the original received monotonic time,
including database waits. Observation expiry is not a takeover lease: an expired
processing claim is displayed as incomplete without mutation, and only its owner
may still save a late final result. Interrupted or unsaved results cannot be
reset, reclaimed, or automatically executed again. A failed final-result save
must not be reported as business success. Existing completed audit rows remain
completed after schema extension; unprovable legacy scope still blocks replay.

For an incomplete result, use its command ID to inspect existing operation
history, the relevant tool's readback, and any delivery receipt. Do not assume
that an external effect failed or replay it with a new ID while the outcome is
unknown. Only an operator who has established that no effect occurred, or has
explicitly resolved the effect, may decide on a new request. This local claim does
not guarantee provider exactly-once delivery or repair historical state.

Analysis cancellation retains its original preflight target binding and completed
message guard. Only the current service claim owner can pass its private identity
proof into that same cancellation transaction; the proof never becomes a public
message field or provider key. Queue redelivery reuses the original preflight
target and cannot cancel a later run or start replacement analysis again.
