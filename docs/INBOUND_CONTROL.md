# Inbound Control And Transport

`./om assistant handle` is the controlled entry point for remote messages from Feishu, WeChat, Hermes, or other gateways.

It is not a shell bridge. Gateways should pass one message into OM and let OM parse, authorize, audit, and execute the request through the existing agent-tool contract.
The current CLI namespace is `./om assistant ...`, and the current implementation
path is `src/application/assistant/...`; the product/module name is Inbound.

## Bot Channel Model

OM treats a messaging integration as one bot channel with three operations:

- `receive`: user sends a message into OM.
- `reply`: OM responds to the original inbound message.
- `send`: OM proactively sends notifications, receipts, and alerts.

Feishu is the first concrete bot channel. Its receive/reply/send paths use the same `OM_FEISHU_BOT_*` configuration, so user messages, automatic replies, and proactive notifications stay in the same Feishu Bot identity. The implementation registers these directions as channel capabilities under `src.application.channels`; Feishu WS receives events through `ChannelService.handle_inbound`, while proactive notifications use the same channel registry for outbound delivery.

WeChat ClawBot is a separate bot channel. Its adapter supports proactive `send`, target `bind`, one-batch `receive/reply` through `./om channel wechat-clawbot poll-once`, and long-running receive/reply through `./om channel wechat-clawbot serve`. The daemon wraps the same poll-once service path instead of adding another notification-only or assistant-only path.

## Boundary

Allowed architecture:

```text
Feishu / WeChat / Hermes
  -> thin channel adapter
  -> ChannelService inbound dispatch
  -> AgentSession (conceptual session boundary)
  -> AgentLoop
       -> Perceive
       -> Understand
       -> Decide
       -> Act
       -> Observe
  -> channel reply
```

`AgentSession` is a conceptual boundary, not a separate runtime layer or a
required class name. In the current implementation it is carried by
`AssistantRequest`, audit identity, conversation context, and pending-operation
state before control enters `AgentLoop`.

`src.application.channels` owns channel capability registration and service
dispatch. `src.application.inbound` owns transport details only: Feishu payload
extraction, Feishu long-connection receive/reply/reaction behavior, and the
transport-facing request contract. Inbound parsing, command catalog, LLM
routing, operation store, audit, policy, and renderer ownership live in
`src.application.assistant`.

The current code still uses compatibility names such as `AssistantRequest`,
`PerceptionResult`, `ReasoningResolution`, `ActionResult`, and
`ObservationResponse`. Treat them as implementation handles inside the same
Agent loop, not as separate runtime layers. `assistant.mode` is a legacy
compatibility field only; the active product controls are `assistant.enabled`
and `assistant.planner.enabled`.

The authority split is:

- `Understand` may parse commands, use deterministic parsing as a safety
  component, or ask the AgentLoop Planner for a bounded plan.
- `Decide` owns sender checks, capability support, policy, risk class, and
  whether the next action is `allow`, `preview`, `ask`, `deny`, or `defer`.
- `Act` may execute read/local tools or create a `PendingOperation`.
- LLM-originated plans may never confirm, cancel, apply, send notifications,
  write config, write ledger/trade state, operate services, or bypass pending
  operation gates.

Disallowed architecture:

```text
Feishu / WeChat / Hermes
  -> arbitrary shell
  -> arbitrary ./om command
```

## Supported Read Commands And Planner Reads

Slash commands are the deterministic read surface. They do not call LLM:

| Pattern | Tool |
|---|---|
| `/status` | `runtime_status` |
| `/health` | `healthcheck` |
| `/config-check` | `config_validate` |
| `/positions [lx|sy|all] [到期月份/到期日/标的/类型/方向]` | `option_positions_read` |
| `/income [lx|sy] [YYYY-MM|6月|本月|上月]` | `monthly_income_report` |
| `/runs [limit]` | `runtime_runs` |
| `/logs <run_id>` | `runtime_logs` |
| `/symbols` | monitored symbols |

Natural-language read requests are planner territory when
`assistant.planner.enabled=true`. For example, `状态`, `持仓 sy`, `这个月赚了多少`,
`分析 long call 是不是应该平仓`, and `现在泡泡玛特 sell put 的 max strike 是多少`
must be translated into bounded read-only capabilities such as
`runtime_status`, `option_positions_read`, `monthly_income_report`,
`close_advice_read`, or `symbol_config_read`. If a needed read tool or required
slot is missing, Inbound should ask for the missing capability/field instead of
falling back to a weakly related query.

Read tools use the pure-read whitelist. Admin write operations are separate and must pass sender allowlist, operation gates, preview storage, and explicit confirmation before applying.

## Inbound Command Facade

Inbound is the default command facade above the same allowlist, audit, preview/confirm, and tool execution path. The current CLI namespace remains `./om assistant ...`. It adds slash commands for users who do not want to remember natural-language phrases:

| Command | Intent |
|---|---|
| `/status` | `runtime_status` |
| `/health` | `healthcheck` |
| `/positions [lx|sy|all]` | open/all option positions |
| `分析 long call 是不是应该平仓` | close-advice analysis for matching open option positions |
| `/income [lx|sy] [YYYY-MM|6月|本月|上月]` | income report |
| `/runs [limit]` | recent runs |
| `/logs <run_id>` | runtime logs |
| `/symbols` | monitored symbols |
| `/pending` | pending preview operations |
| `/record-open ...` | preview a manual opening trade record |
| `/record-close ...` | preview a manual closing trade record |
| `/confirm trade|symbol|upgrade [operation_id]` | confirm a pending write preview |
| `/cancel trade|symbol|upgrade [operation_id]` | cancel a pending write preview |

Local one-shot testing uses the same command facade:

```bash
./om assistant handle --text '/positions sy' --format text
```

For long-running Feishu WS, `config.assistant.json` currently controls the Inbound facade:

```yaml
assistant:
  enabled: true
  planner:
    enabled: false
  context_window_messages: 8
```

This keeps OM Copilot enabled while disabling model planning. Unknown slash
commands return clarification; non-slash messages are limited to local helper
aliases, pending-operation aliases, and explicit preview/confirm/cancel
phrases. Natural-language read requests return clarification unless planner
routing is enabled. Supported active states are `assistant.enabled=true|false`
and `assistant.planner.enabled=true|false`.

By default, `default_market_scope` is intentionally unset. Feishu WS should receive an explicit `--config-key us|hk` or `--config-path` when it is bound to one market. Only set `assistant.default_market_scope: us|hk|all` when that default is an explicit product decision for Inbound.

LLM translation is disabled by default:

```yaml
assistant:
  enabled: true
  planner:
    enabled: false
  context_window_messages: 8
  active_model: deepseek-default
  models:
    deepseek-default:
      provider: deepseek
      base_url: "https://api.deepseek.com"
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
      confidence_min: 0.75
      timeout_seconds: 20
      max_output_tokens: 512
```

When `assistant.planner.enabled` is true, non-slash natural language enters the
AgentLoop Planner. Slash commands remain command-first and never call LLM. The
Planner may plan pure-read tools directly, or exactly one preview-write
capability such as `manual_trade_open`, `manual_trade_close`,
`manual_trade_update`, `symbol_edit`, `model_use`, or `upgrade_now`.
Preview-write plans create pending previews through the existing operation
handlers only; they cannot confirm, cancel, apply, notify externally, write the
ledger, write config directly, operate services, or send proactive messages.
Deterministic command aliases stay as fallback/shadow evidence for local helper
messages, pending operations, and preview/confirm/cancel phrases. Slash commands
remain command-first. Natural-language read queries should be handled by the
Planner or rejected with clarification; they should not be recovered through
keyword fallback.

The command surface authority is `src/application/assistant/commands.py`. Slash command metadata, the LLM intent surface, and inbound help text should use that catalog instead of maintaining separate command lists.

After an inbound message is parsed, the execution router follows one chain:
`PerceptionResult -> ReasoningResolution -> ActionResult -> ObservationResponse`.
Pure-read tools still pass the read allowlist before execution. Write, confirm,
and admin operations use their existing preview/confirm handlers, but the audited
`ReasoningResolution` records the selected action, payload, safety class, and
confirmation requirement before those handlers run.

Supported providers:

- `openai`: uses OpenAI Responses API. Leave `base_url` empty for `https://api.openai.com/v1/responses`, or set a full Responses-compatible base URL.
- `deepseek`: uses DeepSeek's OpenAI-compatible Chat Completions API. Leave `base_url` empty or set `https://api.deepseek.com`; OM calls `/chat/completions` and requests `response_format: {"type":"json_object"}`.

To enable it, set the API key in the local env file or deployment env file, then
enable `assistant.planner.enabled` and choose `assistant.active_model` in
`config.yaml`:

```bash
OM_LLM_API_KEY='sk-...'
```

```yaml
assistant:
  enabled: true
  planner:
    enabled: true
  context_window_messages: 8
  active_model: openai-default
  models:
    openai-default:
      provider: openai
      base_url: ""
      model: gpt-5.2
      api_key_env: OM_LLM_API_KEY
      confidence_min: 0.75
      timeout_seconds: 20
      max_output_tokens: 512
```

DeepSeek example:

```bash
DEEPSEEK_API_KEY='sk-...'
```

```yaml
assistant:
  enabled: true
  planner:
    enabled: true
  context_window_messages: 8
  active_model: deepseek-default
  models:
    deepseek-default:
      provider: deepseek
      base_url: "https://api.deepseek.com"
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
      confidence_min: 0.75
      timeout_seconds: 20
      max_output_tokens: 512
```

Run `./om config build-assistant --source yaml` after editing `config.yaml`. The generated `config.assistant.json` contains only the resolved active `assistant.llm`; runtime/router/arbitrator do not see `assistant.models` or `assistant.active_model`.

The API key stays in environment settings; assistant config only names which env var to read. Model management commands never accept API key values:

```bash
./om assistant model catalog
./om assistant model list
./om assistant model add deepseek-default --provider deepseek --model deepseek-chat --api-key-env DEEPSEEK_API_KEY --apply
./om assistant model use deepseek-default --apply
./om assistant model current
./om assistant model check --active --live
```

The same model surface is available in chat through one slash command namespace:

```text
/model
/model list
/model use deepseek-default
```

`/model` and `/model list` are read-only. `/model use <name>` only creates a preview; it writes `config.yaml` and rebuilds `config.assistant.json` after `确认模型` or `/confirm model <operation_id>`.

When LLM translation runs, OM sends a bounded same-conversation context window to the translator: recent inbound audit rows plus current pending operation summaries. Sender and conversation identifiers are used locally to select the window, but are not sent to the provider. `assistant.context_window_messages` controls the recent-message window and is capped at 20; this context is only used for intent translation, not execution.

Check the translator control plane before enabling it in Feishu:

```bash
./om assistant commands --format text
./om assistant capabilities
./om assistant llm-check
./om assistant llm-check --live
```

`assistant commands` renders the slash-command help surface. `assistant
capabilities` renders the full assistant capability catalog used by the
AgentLoop Planner manifest: read-only capabilities are executable, approved
preview-write capabilities may create pending previews, and confirm/cancel/apply
capabilities are intentionally absent from the Planner manifest. The default
LLM check validates `config.assistant.json`, the effective env file, redacted
API-key presence, the resolved provider endpoint URL, and the current capability
routing surface. `--live` sends one read-only structured planning probe to the
configured provider.

## Sender Allowlist

Remote channels require an explicit sender allowlist:

```bash
export OM_FEISHU_BOT_USER_OPEN_ID='ou_xxx'
```

Multiple Feishu users can be comma-separated. If this is empty, OM defaults the allowlist to `OM_FEISHU_BOT_USER_OPEN_ID`:

```bash
export OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx,ou_yyy'
```

Future non-Feishu channels should expose the same `(channel, user_id)` allowlist semantics instead of bypassing this policy.

`local` channel is allowed by default for local CLI testing. Set this to force allowlist checks for local invocations too:

```bash
export OM_INBOUND_REQUIRE_ALLOWLIST=true
```

## Audit And Idempotency

Every handled message is written to SQLite. The default audit DB is:

```text
output_shared/state/inbound_control.sqlite3
```

Override it with:

```bash
export OM_INBOUND_AUDIT_DB=/var/lib/options-monitor/state/inbound_control.sqlite3
```

For a repo-local runtime root, keep the env value relative to the runtime root:

```bash
export OM_INBOUND_AUDIT_DB=output_shared/state/inbound_control.sqlite3
```

Use the absolute `/var/lib/options-monitor/...` form only when that host's
runtime root really is `/var/lib/options-monitor`.

When `--message-id` is supplied, assistant control treats `(channel, message_id)` as idempotent. A repeated message returns the stored response and does not execute the tool again.

The audit table records:

- `command_id`
- `channel`
- `sender_id`
- `message_id`
- `raw_text`
- `intent_name`
- `tool_name`
- `tool_payload_json`
- `perception_json`
- `reasoning_json`
- `action_json`
- `observation_json`
- `decision`
- `result_ok`
- `error_code`
- `response_json`
- duplicate replay counters

## Examples

Local test:

```bash
./om assistant handle --text '/positions sy' --sender local --channel local --message-id local-1
```

Feishu message call:

```bash
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx' \
./om assistant handle \
  --text '/income <account> <YYYY-MM>' \
  --sender ou_xxx \
  --channel feishu \
  --message-id '${FEISHU_MESSAGE_ID}'
```

Thin Feishu event-payload adapter:

```bash
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx' \
./om inbound feishu --input-file feishu_event.json --format text
```

This adapter only extracts Feishu `im.message.receive_v1` text-message fields and delegates to `./om assistant handle`.

Text output for chat replies:

```bash
./om assistant handle --text '/status' --format text
```

## Feishu Long Connection

`./om inbound feishu-ws` is the long-running Feishu App long-connection client for the full Feishu loop:

```text
Feishu Event Subscription long connection
  -> ./om inbound feishu-ws
  -> ./om inbound feishu
  -> Inbound command facade
  -> Inbound allowlist/audit/pure-read tools
  -> Feishu message reply API
```

It still does not expose arbitrary shell execution. It only forwards Feishu text messages received through the authenticated SDK connection into the same Inbound control path. OM no longer supports the HTTPS callback receiver as the production Feishu inbound path.

Required environment values:

```bash
export OM_FEISHU_BOT_APP_ID='<Feishu app_id>'
export OM_FEISHU_BOT_APP_SECRET='<Feishu app_secret>'
export OM_FEISHU_BOT_USER_OPEN_ID='ou_xxx'
export OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx'
```

The same Feishu Bot credentials are used for long-connection event receiving, same-message replies, and proactive OM notifications. There is no fallback to a separate notification app.

Reaction and reply behavior is configured in the current assistant config under `inbound.feishu_ws`, not in the secret env file. Set `inbound.feishu_ws.ack_reaction` to a Feishu `emoji_type` such as `SMILE` to enable message reactions; leave it empty to disable reaction acknowledgements. Reaction failures are reported in the JSON status for that event but do not fail the inbound command or block the text reply.

Local config check:

```bash
./om inbound feishu-ws --check
```

Run the long-connection client directly on the server:

```bash
./om inbound feishu-ws \
  --config-key us \
  --config-path /var/lib/options-monitor/config.us.json \
  --audit-db /var/lib/options-monitor/output_shared/state/inbound_control.sqlite3 \
  --lock-path /var/lib/options-monitor/locks/feishu-ws.lock
```

For Linux systemd rendering:

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --include-feishu-ws \
  --output-dir /tmp/options-monitor-service
```

Install the rendered `options-monitor-feishu-ws.service`, reload systemd, and enable it. It does not bind a local HTTP port and does not require a public callback URL, reverse proxy, TLS certificate, or tunnel. The rendered service passes `--lock-path` so only one long-connection client should run per Feishu App.

For Mac launchd, pass the same local env file through `--env-file`; the rendered plist stores it as `OM_ENV_FILE` because launchd does not inherit your interactive shell environment.

Supported Feishu events:

- `im.message.receive_v1` with text content

Only subscribe this event for the OM Bot in Feishu Open Platform. Install `requirements/server.txt` on hosts that run `feishu-ws`, because long connection uses the `lark-oapi` server dependency set.

## WeChat ClawBot Polling

`./om channel wechat-clawbot serve` is the long-running WeChat ClawBot polling client for the full WeChat loop:

```text
WeChat message
  -> ClawBot iLink get_updates
  -> ./om channel wechat-clawbot serve
  -> ChannelService inbound dispatch
  -> Inbound command facade
  -> Inbound allowlist/audit/pure-read tools
  -> ClawBot same-message reply
```

Check the server configuration before starting the daemon:

```bash
./om channel wechat-clawbot serve --check \
  --label default \
  --state-dir /var/lib/options-monitor/output_shared/state/channels/wechat_clawbot/default \
  --config-key us \
  --config-path /var/lib/options-monitor/config.us.json \
  --assistant-config /var/lib/options-monitor/resolved/config.assistant.json \
  --audit-db /var/lib/options-monitor/output_shared/state/inbound_control.sqlite3 \
  --allowed-senders "wechat:<from_user_id>"
```

Run it directly on the server:

```bash
./om channel wechat-clawbot serve \
  --label default \
  --state-dir /var/lib/options-monitor/output_shared/state/channels/wechat_clawbot/default \
  --config-key us \
  --config-path /var/lib/options-monitor/config.us.json \
  --assistant-config /var/lib/options-monitor/resolved/config.assistant.json \
  --audit-db /var/lib/options-monitor/output_shared/state/inbound_control.sqlite3 \
  --allowed-senders "wechat:<from_user_id>" \
  --lock-path /var/lib/options-monitor/locks/wechat-clawbot.lock
```

For Linux systemd rendering:

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --config-us /var/lib/options-monitor/config.us.json \
  --config-hk /var/lib/options-monitor/config.hk.json \
  --include-wechat-clawbot \
  --output-dir /tmp/options-monitor-service
```

The rendered `options-monitor-wechat-clawbot.service` passes `--lock-path` so only one poller consumes a ClawBot state directory. Configure `inbound.wechat_clawbot.allowed_senders` in `config.yaml`, or pass `--wechat-clawbot-allowed-senders` as an explicit render-time override. There is no wildcard default. Use `./om channel status --runtime-root <runtime> --profile-path <runtime>/service.profile.json` for the unified Feishu + WeChat channel read surface; it reports configured/available booleans and redacted paths, not ClawBot tokens or allowlist text. When the allowlist comes from `config.yaml`, `service.profile.json` records only `allowed_senders_configured/source`.

## LLM Translator

LLM planning is opt-in and inactive unless `assistant.enabled` and
`assistant.planner.enabled` are both true. In that state, non-slash natural
language is routed through the AgentLoop Planner first; deterministic parsing is
fallback/shadow evidence and the authority for command/confirm/cancel/apply
messages. Slash commands are resolved by the Inbound command catalog before LLM
and do not call the Planner.

The current provider adapters use OpenAI Responses API for `openai` and Chat
Completions JSON output for `deepseek`. They produce an `om-tool-plan-v1`
capability plan for the AgentLoop. Planner read steps still go through the read
whitelist before execution. Planner preview-write steps are converted back into
the same operation preview path as deterministic commands, so sender allowlist,
operation gates, preview storage, audit, idempotency, and later explicit
confirmation still apply. Low-confidence or incomplete requests must return
clarification.

`agent_loop` is the current bounded Planner lane inside the Agent loop. It may plan read tools or one preview-write operation, but deterministic execution, factual rendering, preview storage, confirm/apply, and audit ownership remain outside model authority. If a write-like request such as a Futu fill alert is planned as a read query, OM rejects the plan instead of silently returning nearby holdings or income data.

### Data Analysis-style Analytical Answers

For analytical questions such as `6月收益的组成`, `分析 lx 6月净现金流明细`, or `历史以来总的净现金流`, Inbound should follow a Data Analysis-style contract:

```text
user question
-> LLM plans the analysis and required OM tools
-> deterministic OM tools fetch ledger/runtime evidence
-> a controlled analyzer computes an analysis artifact
-> deterministic renderer writes factual tables and rows from the artifact
-> LLM may add a concise interpretation based on artifact references only
```

The design goal is to preserve LLM intelligence without making the LLM a factual source. The LLM may choose what to inspect, which dimensions to compare, and what explanation angle is useful. It must not be the component that writes accounting facts such as amount, currency, contract count, account, symbol, expiration, close type, or date.

Canonical factual rendering is declared by the tool definition through `output_contract.canonical_renderer`. `agent_loop` may use this contract to select the deterministic renderer, but it should not own tool-name special cases for ledger/runtime fact rows. When a tool has payload-dependent factual output, use an `output_contract_resolver` so the concrete contract travels with the observation.

For income and cashflow analysis, the controlled artifact is the authority. It should contain the evidence needed for the final answer, for example:

- `data_scope`: source, account scope, month scope, coverage, warnings.
- `facts_table`: user-facing rows derived from `monthly_income_report(include_rows=true)`.
- `totals_by_account`: net cashflow, realized PnL, premium, and cash-secured denominator when available.
- `totals_by_source`: open premium, close/exercise/assignment/expiry realized PnL, long-option recovery, and other cashflow buckets when available.
- `reconciliation_notes`: why net cashflow differs from realized PnL, which data is missing, and which conclusions are unsupported.

Final answer rules:

- No natural-language character matching should be used to validate amounts or row facts after the LLM has written them.
- User-visible fact rows, amounts, contract counts, accounts, symbols, dates, currencies, and close types must be rendered from the artifact, not from free-form LLM text.
- LLM interpretation may reference artifact row ids, bucket names, and computed metrics, but it must not restate or recalculate detailed ledger facts.
- If the artifact cannot support the requested answer, the response should say which capability or evidence is missing instead of returning a nearby summary.
- Existing grounded rendering is only a safety baseline. Future implementation work for income analysis should move toward `tool evidence -> analysis artifact -> deterministic factual rendering -> optional LLM interpretation`, not toward more parser or regex guards.

Acceptance criteria for this design:

- A detail/composition question returns a factual table before any interpretation.
- A known multi-contract row cannot be shown as one contract because the renderer reads the artifact quantity.
- A known amount cannot drift because the LLM never owns amount rendering.
- If LLM synthesis is unavailable, the artifact-backed factual answer is still useful.
- If LLM synthesis is available, it improves analysis selection and explanation, not accounting fact generation.

## Write Actions

Inbound write actions are opt-in. Set `OM_INBOUND_OPERATIONS_ENABLED=1`, configure `OM_INBOUND_ADMIN_OPEN_IDS`, then enable only the required operation family:

```bash
export OM_INBOUND_TRADE_WRITE_ENABLED=1
export OM_INBOUND_SYMBOL_WRITE_ENABLED=1
export OM_INBOUND_UPGRADE_WRITE_ENABLED=1
export OM_INBOUND_MODEL_WRITE_ENABLED=1
```

Write actions use:

```text
request -> preview -> command_id -> explicit confirmation -> re-validate -> execute -> receipt
```

The preview may be initiated by a deterministic slash/natural-language command
or by an approved `agent_loop` Planner preview capability. Confirmation is not
planner-visible: it must come from a deterministic confirm command, match an
existing pending operation, and pass the configured sender, env, HMAC, TTL, and
operation-family gates.

Supported write commands:

| Text | Preview intent | Confirm | Cancel |
| --- | --- | --- | --- |
| `记录开仓 ...` / `记录平仓 ...` | `manual_trade_*` | `确认记录 [operation_id]` | `取消记录 [operation_id]` |
| `增加/修改/删除监控标的 ...` | `symbol_*` | `确认监控 [operation_id]` | `取消监控 [operation_id]` |
| `立即升级` / `立即升级到 v<version>` | `upgrade_now` | `确认升级 [operation_id]` | `取消升级 [operation_id]` |
| `/model use <name>` | `model_use` | `确认模型 [operation_id]` | `取消模型 [operation_id]` |

`立即升级` delegates to the same service upgrade path as `./om update apply --auto --confirm`. The preview does not switch releases. Confirmation only records the confirmation and starts an independent `assistant upgrade-worker`; the worker runs the upgrade, writes the final applied/failed result, and sends the final Feishu receipt after service restarts.
