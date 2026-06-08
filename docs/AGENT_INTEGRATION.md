# Ops Copilot Integration

The public launcher is `./om-agent`.

It exposes a stable JSON contract intended for local Ops Copilot usage:

- `./om-agent add-account --market us|hk --account-label <label> --account-type futu|external_holdings --dry-run`
- `./om-agent spec`
- `./om-agent run --tool <name> --input-json '<json>'`

Capability boundaries, risk classes, Inbound exposure, and verification
rules are maintained in [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
This document only describes integration contracts and invocation patterns.

也支持：

- `./om-agent run --tool <name> --input-file payload.json`

其中 `--input-file` 会覆盖 `--input-json`。

Implementation ownership:
- Tool implementation source of truth: `src/application/agent_tools/<domain>.py`
- Tool manifest collector: `src/application/agent_tool_registry.py`
- Tool write permission gate: `src/application/agent_tools/permissions.py`
- Tool response contract: `src/application/agent_tool_contracts.py`
- Runtime config helpers: `src/application/agent_tool_config.py`
- Runtime config initialization/account mutation helpers: `src/application/agent_tool_init_local.py`
- Public CLI owner: `src/interfaces/agent/cli.py`
- Runtime tick is not a separate single-account / multi-account split. The live chain is `./om run tick` -> `src.application.multi_account_tick.run_tick`; pass one account for single-account execution or multiple accounts for multi-account execution.

## Contract

All tool responses return:

```json
{
  "schema_version": "1.0",
  "tool_name": "healthcheck",
  "ok": true,
  "data": {},
  "warnings": [],
  "error": null,
  "meta": {}
}
```

Errors are normalized to stable codes such as:

- `CONFIG_ERROR`
- `INPUT_ERROR`
- `DEPENDENCY_MISSING`
- `PERMISSION_DENIED`
- `CONFIRMATION_REQUIRED`
- `INTERNAL_ERROR`

说明：
- 这些是顶层错误 envelope 的稳定代码。
- 某些底层诊断项（例如 OpenD readiness probe 的细粒度失败原因）可能会体现在 `checks[]` 中，而不是顶层错误 code 枚举中。

## Claude Code

Use the launcher as a local command tool. Typical pattern:

```bash
./om-agent spec
./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
./om-agent run --tool version_update --input-json '{"bump":"patch"}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"sy"}'
./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'
./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx","month":"2026-04"}'
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

Sell Put 现金余量的标准 Ops Copilot 工具是 `query_cash_headroom`。它包装
`src.application.cash_headroom_query` 里的 `query_sell_put_cash(...)`，用于返回账户现金、
Sell Put 担保占用和剩余可用现金，并支持按账户和币种折算到 CNY。

如果 payload 很长，优先用：

```bash
./om-agent run --tool get_close_advice --input-file payload.json
```

## Kimi Code

Use the same launcher contract. Kimi Code only needs a local command invocation and JSON parsing.

## Codex

Use the same launcher contract as Claude Code. For first-pass troubleshooting, prefer:

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

For MacBook-side Codex diagnosis of online quality or candidate-scan behavior,
use the independent Research / Shadow Replay side lane instead of `om-agent`
and instead of calling an online AI provider:

```bash
./om research collect --config-key us --scope full --output both --no-write-outputs
./om research shadow-replay status --min-sample 30
```

## Inbound Remote Messages

Use `./om assistant handle` when a remote messaging gateway needs to send user text into OM:

```bash
./om assistant handle --text '持仓 sy' --sender ou_xxx --channel feishu --message-id msg_xxx
```

This is a controlled Inbound message entrypoint, not an `om-agent` tool and not a shell bridge. It performs Inbound command parsing, deterministic parsing or bounded AgentLoop planning, sender allowlist checks, message idempotency, SQLite audit, and then calls an allowlisted tool or creates a pending preview through the existing operation path. The current `./om assistant ...` CLI namespace and `assistant` config keys remain compatibility names. Inbound config may opt into LLM intent translation/planning; translated intents and plans still return into the same allowlist, audit, pending-operation, and renderer path. LLM-originated plans may read or initiate approved previews only; confirm/cancel/apply remains deterministic-only.

Remote channels require:

```bash
OM_FEISHU_BOT_USER_OPEN_ID='ou_xxx'
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx'
```

The remote capability surface is intentionally smaller than the full
`om-agent` manifest. Inspect it with `./om assistant capabilities --format json`
and keep boundary decisions in [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
Do not connect Feishu, WeChat, or Hermes to arbitrary shell execution. Gateways
should call only `./om assistant handle`. See [INBOUND_CONTROL.md](INBOUND_CONTROL.md).

For Feishu event JSON specifically, use the thin adapter:

```bash
OM_FEISHU_BOT_ALLOWED_OPEN_IDS='ou_xxx' \
./om inbound feishu --input-file feishu_event.json --format text
```

It extracts `im.message.receive_v1` text fields and then delegates to the same Inbound control path.

For the full Feishu loop, run the long-connection service:

```bash
./om inbound feishu-ws --check
./om inbound feishu-ws --config-key us --config-path /var/lib/options-monitor/config.us.json --lock-path /var/lib/options-monitor/locks/feishu-ws.lock
```

The long-connection client receives Feishu events through the authenticated SDK connection, delegates text messages to Inbound control, optionally adds the configured Inbound `inbound.feishu_ws.ack_reaction`, and replies through the Feishu message reply API. Render it as a long-running service with `./om service render --include-feishu-ws ...`; no public callback URL or reverse proxy is required.

Treat `openclaw_readiness` as OpenClaw-specific. It is safe to call outside OpenClaw, but the
`openclaw_binary` check may return `warn` when the `openclaw` command is not installed.

## OpenClaw

Treat `./om-agent` as a local tool host command.

Recommended environment:

- keep repo-local `config.us.json` / `config.hk.json` as generated runtime snapshots
- complete first-time initialization with `./om config init --output config.yaml --runtime-output-dir .`
- use explicit `config_path` input only when you intentionally want to override the default repo-local config
- keep `OM_AGENT_ENABLE_WRITE_TOOLS` unset unless you explicitly want config writes
- optionally copy `configs/examples/openclaw.profile.example.json` to `openclaw.profile.json`
  and fill in production paths/accounts/cron job ids

Recommended first commands:

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
```

Use `openclaw_readiness` when you need a one-shot readiness summary. It combines:

- `runtime_status`
- existing `healthcheck`
- local `openclaw` command availability
- optional profile-backed cron checks
- notification route shape checks
- machine-readable next actions

Use `runtime_status` when you only want to inspect existing runtime files. It does not run a pipeline, send
notifications, or write state. It summarizes:

- `output_shared/state/last_run.json`
- `output_shared/state/last_run.json`
- `output_shared/reports/symbols_notification.txt`
- `output_accounts/<account>/state/last_run.json`
- `output_accounts/<account>/reports/symbols_notification.txt`
- the latest `output_runs/<run_id>` pointer when available
- freshness and per-account summary fields

If the production layout uses non-default paths, pass them explicitly:

```bash
./om-agent run --tool runtime_status --input-json '{
  "config_path": "/home/node/.openclaw/workspace/options-monitor-prod/config.us.json",
  "report_dir": "/home/node/.openclaw/workspace/options-monitor-prod/output_shared/reports",
  "state_dir": "/home/node/.openclaw/workspace/options-monitor-prod/output_shared/state",
  "shared_state_dir": "/home/node/.openclaw/workspace/options-monitor-prod/output_shared/state",
  "accounts_root": "/home/node/.openclaw/workspace/options-monitor-prod/output_accounts",
  "runs_root": "/home/node/.openclaw/workspace/options-monitor-prod/output_runs"
}'
```

Default OpenClaw safety posture:

- Prefer `openclaw_readiness` or `runtime_status` before any runtime command.
- Do not run `./om run tick` or notification send commands unless the user explicitly asks for a live run.
- Keep real writes behind both `OM_AGENT_ENABLE_WRITE_TOOLS=true` and a payload-level confirmation such as `confirm=true`.
- `add-account` / `edit-account` / `remove-account` are write-capable commands; use `--dry-run`
  first, then rerun with `OM_AGENT_ENABLE_WRITE_TOOLS=true` and `--confirm` only when the config write is intended.

## `spec` 的行为说明

`./om-agent spec` 输出的是当前环境下的 tool manifest。

也就是说它不是完全静态文本，至少这些值会受环境影响：

- `write_tools_enabled`
- 默认写工具可用性
- 每个工具的 `risk_level` / `requires_confirm` / `requires_env` / `safe_default_input`

`safe_default_input` 不会替 agent 选择 `config_key` 或 `config_path`。凡是需要 runtime config 的工具，调用方必须显式传 `config_key: us|hk` 或 `config_path`。

如果你打开了：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true
```

那么 `spec` 里的默认能力描述也会随之变化。

## 写操作门禁

当前写操作不是只靠一个开关就能执行。

门禁入口在 `src/application/tool_execution.py`，但“这个 payload 是否请求写入”
由 `src/application/agent_tools/<domain>.py` 的工具定义/写入策略决定，并由
`src/application/agent_tools/permissions.py` 统一执行 env/confirm 门禁。执行层不再按
具体工具名维护特殊分支。

通常需要两层门禁：

1. 环境变量允许写：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true
```

2. 调用 payload 显式确认（例如 `confirm=true`）

以 `manage_symbols` 为例：

- `list` 永远允许
- 真正写入需要环境变量 + 显式确认

OpenClaw integration in this public repo is local-tool oriented. Private cron/deploy workflows are not part
of the public plugin contract.
