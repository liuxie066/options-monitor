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
export OM_FEISHU_BOT_USER_OPEN_ID='***REMOVED-OPEN-ID***'
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

Feishu gateway call:

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

The adapter also handles Feishu URL verification payloads by returning the challenge:

```bash
./om inbound feishu --input-json '{"type":"url_verification","challenge":"xxx"}'
```

Text output for chat replies:

```bash
./om inbound handle --text '状态' --format text
```

## Feishu Gateway

`./om inbound feishu-gateway` is the long-running Feishu App event callback receiver for the full Feishu loop:

```text
Feishu Event Subscription HTTPS callback
  -> ./om inbound feishu-gateway
  -> signature/token checks
  -> ./om inbound feishu
  -> OM inbound allowlist/audit/pure-read tools
  -> Feishu message reply API
```

It still does not expose arbitrary shell execution. It only forwards verified Feishu text messages into the same inbound control path.

Required environment values:

```bash
export OM_FEISHU_BOT_APP_ID='<Feishu app_id>'
export OM_FEISHU_BOT_APP_SECRET='<Feishu app_secret>'
export OM_FEISHU_BOT_ENCRYPT_KEY='<Feishu event Encrypt Key>'
export OM_FEISHU_BOT_VERIFICATION_TOKEN='<Feishu event Verification Token>'
export OM_FEISHU_BOT_USER_OPEN_ID='***REMOVED-OPEN-ID***'
export OM_FEISHU_BOT_ALLOWED_OPEN_IDS='***REMOVED-OPEN-ID***'
```

The same Feishu Bot credentials are used for inbound event verification, same-message replies, and proactive OM notifications. There is no fallback to a separate notification app.

Local config check:

```bash
./om inbound feishu-gateway --check
```

Run behind an HTTPS reverse proxy:

```bash
./om inbound feishu-gateway --host 127.0.0.1 --port 8765 --path /feishu/events
```

The external Feishu callback URL should then point at your reverse proxy, for example:

```text
https://your-domain.example/feishu/events
```

Direct TLS is also supported when you really want Python to terminate HTTPS:

```bash
./om inbound feishu-gateway \
  --host 0.0.0.0 \
  --port 8765 \
  --tls-certfile /etc/letsencrypt/live/your-domain/fullchain.pem \
  --tls-keyfile /etc/letsencrypt/live/your-domain/privkey.pem
```

For Linux systemd rendering:

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --env-file /etc/options-monitor/options-monitor.env \
  --markets us hk \
  --accounts lx sy \
  --include-feishu-gateway \
  --output-dir /tmp/options-monitor-service
```

Install the rendered `options-monitor-feishu-gateway.service`, reload systemd, and enable it. The service binds to `127.0.0.1:8765` by default, which is intended for Nginx/Caddy/Cloudflare Tunnel TLS termination.

Supported Feishu events:

- `url_verification`
- `im.message.receive_v1` with text content

Unsupported events are acknowledged and ignored. Encrypted event payloads are supported when `requirements/server.txt` is installed, because decryption uses the server dependency set.

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
