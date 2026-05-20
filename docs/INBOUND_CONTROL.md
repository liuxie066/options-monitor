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
| `持仓 sy` | `option_positions_read` |
| `收益 sy` | `monthly_income_report` |
| `收益 sy 2026-05` | `monthly_income_report` with month filter |
| `最近运行` | `runtime_runs` |
| `日志 <run_id>` | `runtime_logs` |

Only this pure-read whitelist is enabled. Tools that write local reports or cache files, send notifications, mutate config, or touch broker state are not available through inbound control.

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

LLM translation is intentionally not part of the first implementation.

If it is added later, it must only translate natural language into a structured intent. The translated intent must still go through the same sender allowlist, pure-read whitelist, audit, and idempotency checks. Low-confidence, incomplete, or write-like intents must return clarification or preview only.

## Write Actions

Write actions are out of scope for the first inbound version.

Future write actions must use:

```text
request -> preview -> command_id -> explicit confirmation -> re-validate -> execute -> receipt
```

Broker writes and real trade actions should be the last capability enabled, after preview and confirmation flows are proven.
