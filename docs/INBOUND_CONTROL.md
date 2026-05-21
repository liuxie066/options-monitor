# Inbound Control

`./om inbound handle` is the controlled entry point for remote messages from Feishu, WeChat, Hermes, or other gateways.

It is not a shell bridge. Gateways should pass one message into OM and let OM parse, authorize, audit, and execute the request through the existing agent-tool contract.

## Bot Channel Model

OM treats a messaging integration as one bot channel with three operations:

- `receive`: user sends a message into OM.
- `reply`: OM responds to the original inbound message.
- `send`: OM proactively sends notifications, receipts, and alerts.

Feishu is the first concrete bot channel. Its receive/reply/send paths use the same `OM_FEISHU_BOT_*` configuration, so user messages, automatic replies, and proactive notifications stay in the same Feishu Bot identity. Future WeChat support should add a separate adapter with the same channel semantics instead of adding another notification-only path.

## Boundary

Allowed architecture:

```text
Feishu / WeChat / Hermes
  -> ./om inbound handle --text ... --sender ... --channel ...
  -> OM inbound parser / policy / audit
  -> existing pure-read om-agent tools
```

Disallowed architecture:

```text
Feishu / WeChat / Hermes
  -> arbitrary shell
  -> arbitrary ./om command
```

## First Supported Commands

The first implementation is read-only and deterministic. It supports:

| Message | Tool |
|---|---|
| `状态` | `runtime_status` |
| `健康检查` | `healthcheck` |
| `配置检查` | `config_validate` |
| `持仓` | `option_positions_read` for all accounts, open positions |
| `持仓 sy` | `option_positions_read` for one account |
| `收益` | `monthly_income_report` for all accounts/months |
| `收益 sy` | `monthly_income_report` for one account |
| `收益 sy 2026-05` | `monthly_income_report` with month filter |
| `最近运行` | `runtime_runs` |
| `日志 <run_id>` | `runtime_logs` |

Read commands use the pure-read whitelist. Admin write operations are separate and must pass sender allowlist, operation gates, preview storage, and explicit confirmation before applying.

## AgentRuntime Command Facade

`AgentRuntime` is the default command facade above the same inbound parser, policy, audit, and tool execution path. It adds slash commands for users who do not want to remember natural-language phrases:

| Command | Intent |
|---|---|
| `/status` | `runtime_status` |
| `/health` | `healthcheck` |
| `/positions [lx|sy|all]` | open/all option positions |
| `/income [lx|sy] [YYYY-MM|本月|上月]` | income report |
| `/runs [limit]` | recent runs |
| `/logs <run_id>` | runtime logs |
| `/symbols` | monitored symbols |
| `/pending` | pending preview operations |
| `/confirm trade|symbol|upgrade [operation_id]` | confirm a pending write preview |
| `/cancel trade|symbol|upgrade [operation_id]` | cancel a pending write preview |

Local one-shot testing uses the same command facade by default when `agent.runtime.enabled` is true:

```bash
./om inbound handle --text '/positions sy' --format text
```

For parser diagnostics, bypass the facade explicitly:

```bash
./om inbound handle --no-agent-runtime --text '持仓 sy' --format text
```

For long-running Feishu WS, the same config controls the facade:

```yaml
agent:
  runtime:
    enabled: true
    context_window_messages: 8
```

This still does not enable LLM. Unknown slash commands return clarification; non-slash messages continue through the deterministic inbound parser.

LLM translation is disabled by default:

```yaml
agent:
  llm:
    enabled: false
    provider: ""
    base_url: ""
    model: ""
    api_key_env: OM_LLM_API_KEY
    confidence_min: 0.75
    timeout_seconds: 20
    max_output_tokens: 512
```

When enabled, LLM translation only runs after command and deterministic parsing fail. It must return an `om-llm-intent-v1` JSON intent into the same inbound router; it must not execute tools or rewrite canonical OM responses. The current intent schema is read-only and only allows help/status/health/config/positions/income/runs/logs/symbols/pending operations.

The command surface authority is `src/application/agent_runtime/command_catalog.py`. Slash command metadata, the read-only LLM intent surface, and inbound help text should use that catalog instead of maintaining separate command lists.

Supported providers:

- `openai`: uses OpenAI Responses API. Leave `base_url` empty for `https://api.openai.com/v1/responses`, or set a full Responses-compatible base URL.
- `deepseek`: uses DeepSeek's OpenAI-compatible Chat Completions API. Leave `base_url` empty or set `https://api.deepseek.com`; OM calls `/chat/completions` and requests `response_format: {"type":"json_object"}`.

To enable it, set the API key in the local env file or deployment env file, then set `agent.llm` in runtime config:

```bash
OM_LLM_API_KEY='sk-...'
```

```yaml
agent:
  llm:
    enabled: true
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
agent:
  llm:
    enabled: true
    provider: deepseek
    base_url: "https://api.deepseek.com"
    model: deepseek-v4-flash
    api_key_env: DEEPSEEK_API_KEY
    confidence_min: 0.75
    timeout_seconds: 20
    max_output_tokens: 512
```

The API key stays in environment settings; runtime config only names which env var to read. When LLM translation runs, OM sends a bounded same-conversation context window to the translator: recent inbound audit rows plus current pending operation summaries. Sender and conversation identifiers are used locally to select the window, but are not sent to the provider. `agent.runtime.context_window_messages` controls the recent-message window and is capped at 20; this context is only used for intent translation, not execution.

Check the translator control plane before enabling it in Feishu:

```bash
./om agent commands --format text
./om agent llm-check --config-key us
./om agent llm-check --config-key us --live
```

`agent commands` renders the same command catalog used by slash commands, inbound help, and the LLM intent schema. The default LLM check only validates runtime config, the effective env file, redacted API-key presence, and the resolved provider endpoint URL. `--live` sends one read-only structured translation probe to the configured provider.

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

When `--message-id` is supplied, inbound control treats `(channel, message_id)` as idempotent. A repeated message returns the stored response and does not execute the tool again.

The audit table records:

- `command_id`
- `channel`
- `sender_id`
- `message_id`
- `raw_text`
- `intent_name`
- `tool_name`
- `tool_payload_json`
- `decision`
- `result_ok`
- `error_code`
- `response_json`
- duplicate replay counters

## Examples

Local test:

```bash
./om inbound handle --text '持仓 sy' --sender local --channel local --message-id local-1
```

Feishu message call:

```bash
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx' \
./om inbound handle \
  --text '收益 sy 2026-05' \
  --sender ou_xxx \
  --channel feishu \
  --message-id '${FEISHU_MESSAGE_ID}'
```

Thin Feishu event-payload adapter:

```bash
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx' \
./om inbound feishu --input-file feishu_event.json --format text
```

This adapter only extracts Feishu `im.message.receive_v1` text-message fields and delegates to `./om inbound handle`.

Text output for chat replies:

```bash
./om inbound handle --text '状态' --format text
```

## Feishu Long Connection

`./om inbound feishu-ws` is the long-running Feishu App long-connection client for the full Feishu loop:

```text
Feishu Event Subscription long connection
  -> ./om inbound feishu-ws
  -> ./om inbound feishu
  -> AgentRuntime command facade
  -> OM inbound allowlist/audit/pure-read tools
  -> Feishu message reply API
```

It still does not expose arbitrary shell execution. It only forwards Feishu text messages received through the authenticated SDK connection into the same inbound control path. OM no longer supports the HTTPS callback receiver as the production Feishu inbound path.

Required environment values:

```bash
export OM_FEISHU_BOT_APP_ID='<Feishu app_id>'
export OM_FEISHU_BOT_APP_SECRET='<Feishu app_secret>'
export OM_FEISHU_BOT_USER_OPEN_ID='ou_xxx'
export OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx'
```

The same Feishu Bot credentials are used for long-connection event receiving, same-message replies, and proactive OM notifications. There is no fallback to a separate notification app.

Reaction and reply behavior is configured in runtime config under `inbound.feishu_ws`, not in the secret env file. Set `inbound.feishu_ws.ack_reaction` to a Feishu `emoji_type` such as `SMILE` to enable message reactions; leave it empty to disable reaction acknowledgements. Reaction failures are reported in the JSON status for that event but do not fail the inbound command or block the text reply.

Local config check:

```bash
./om inbound feishu-ws --check
```

Run the long-connection client directly on the server:

```bash
./om inbound feishu-ws \
  --config-key us \
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

## LLM Translator

LLM translation is opt-in and inactive unless `agent.llm.enabled` is true.

The current provider adapters use OpenAI Responses API for `openai` and Chat Completions JSON output for `deepseek`. They must only translate natural language into an `om-llm-intent-v1` structured intent. The translated intent must still go through the same sender allowlist, pure-read whitelist, audit, and idempotency checks. Low-confidence, incomplete, or write-like intents must return clarification or preview only.

LangGraph is intentionally not part of the production runtime yet. The current workflow is a one-shot command facade plus an optional LLM translator; OM keeps deterministic execution, factual rendering, preview/confirm/apply, and audit ownership. Add LangGraph only when there is a real stateful workflow that cannot stay clear in this simpler runtime.

## Write Actions

Inbound write actions are opt-in. Set `OM_INBOUND_OPERATIONS_ENABLED=1`, configure `OM_INBOUND_ADMIN_OPEN_IDS`, then enable only the required operation family:

```bash
export OM_INBOUND_TRADE_WRITE_ENABLED=1
export OM_INBOUND_SYMBOL_WRITE_ENABLED=1
export OM_INBOUND_UPGRADE_WRITE_ENABLED=1
```

Write actions use:

```text
request -> preview -> command_id -> explicit confirmation -> re-validate -> execute -> receipt
```

Supported write commands:

| Text | Preview intent | Confirm | Cancel |
| --- | --- | --- | --- |
| `记录开仓 ...` / `记录平仓 ...` | `manual_trade_*` | `确认记录 [operation_id]` | `取消记录 [operation_id]` |
| `增加/修改/删除监控标的 ...` | `symbol_*` | `确认监控 [operation_id]` | `取消监控 [operation_id]` |
| `立即升级` / `立即升级到 v1.2.111` | `upgrade_now` | `确认升级 [operation_id]` | `取消升级 [operation_id]` |

`立即升级` delegates to the same service upgrade path as `./om update apply --auto --confirm`. The preview does not switch releases. Confirmation only records the confirmation and starts an independent `inbound upgrade-worker`; the worker runs the upgrade, writes the final applied/failed result, and sends the final Feishu receipt after service restarts.
