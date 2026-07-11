# Tool Gateway Integration

The public Tool Gateway launcher is `./om-agent`.

`./om-agent` is a structured local tool-call entrypoint for external agents,
scripts, Codex, or operators. It is not OM's autonomous/project
Agent, and it should not own multi-step planning or message conversation
state. Current entry and layer terminology is defined in
[ARCHITECTURE.md](ARCHITECTURE.md); channel message handling is defined in
[INBOUND_CONTROL.md](INBOUND_CONTROL.md).

It exposes a stable JSON contract intended for local machine usage:

- `./om-agent add-account --market us|hk --account-label <label> --account-type futu|external_holdings --dry-run`
- `./om-agent spec`
- `./om-agent run --tool <name> --input-json '<json>'`

Capability boundaries, risk classes, Inbound Assistant exposure, and
verification rules are maintained in
[OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
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

Sell Put 现金余量的标准 Tool Gateway 工具是 `query_cash_headroom`。它包装
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

For MacBook-side Codex diagnosis of online quality, candidate-scan behavior,
or Strategy Lab analysis, use the independent Research / Shadow Replay side
lane instead of `om-agent` and instead of calling an online AI provider:

```bash
./om research collect --config-key us --scope full --output both --no-write-outputs
./om research shadow-replay status --min-sample 30
./om research shadow-replay candidate-impact-report --params <params.json> --market us --start-date <YYYY-MM-DD> --account lx --min-sample 30
./om research strategy-lab update --latest
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab readiness --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30
./om research strategy-lab readiness --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30
./om research strategy-lab experiment --dataset output_shared/research/shadow_replay/datasets/<dataset-id> --min-sample 30 --auto
./om research strategy-lab experiment --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30 --auto
./om research strategy-lab proposal --experiment output_shared/research/strategy_lab/experiment.json --markdown-output output_shared/research/strategy_lab/proposal.md
./om research strategy-lab llm-context --experiment output_shared/research/strategy_lab/experiment.json --proposal output_shared/research/strategy_lab/proposal.json --output output_shared/research/strategy_lab/llm_context.json
```

Research / Shadow Replay remains an offline evidence side lane. Strategy Lab is
the product layer above it for evidence update, decision-instance readiness,
hypotheses, scorecards, advisory dry-run proposals, and redacted local LLM
context. Readiness and experiments can read an existing dataset or aggregate a
scanned-run window by date, market, and account. Its `update` command is dry-run by default; `--build-dataset --write`
only builds a local replay dataset from the latest scanned run, and `--write`
only wraps local Shadow Replay collect/settle data-plan. It separates Sell
Put, Covered Call, and Combo Yield through strategy-domain adapters; Combo
Yield must remain group-level and must not use the single-leg parameter model.
Use
`review_readiness` to decide whether evidence is ready for manual strategy
review, and use `candidate-impact` / `candidate-impact-report` to compare how
explicit threshold variants would change the observed candidate set.
This workflow must not call online AI providers, mutate runtime config, write
trade state, or send notifications.

## Inbound Remote Messages

Use `./om assistant handle` when a remote messaging gateway needs to send user text into OM:

```bash
./om assistant handle --text '/positions sy' --sender ou_xxx --channel feishu --message-id msg_xxx
```

This is a controlled Inbound Assistant message entrypoint, not an `./om-agent`
tool and not a shell bridge. It performs sender allowlist checks, message
idempotency, and SQLite audit. Explicit commands and pending-operation replies
enter deterministic Control; every other message enters the single read-first
`om_chat` Copilot Scene when `assistant.copilot.enabled` is true. Copilot gets
canonical pure-read tools and may request one validated deterministic Control
preview; it cannot confirm, cancel, apply, or receive direct notification,
config-write, ledger/trade, broker-write, service-control, or upgrade tools.

The Host persists structured conversation memory, durable runs, cancellation,
safe pure-read resume, coarse progress events, and an idempotent reply outbox.
These are Host governance mechanisms, not additional business-routing layers.

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

`openclaw_readiness` has been retired. Use `healthcheck` for environment readiness and
`runtime_status` for existing runtime artifacts.

## Service Deployment

Treat `./om-agent` as a local Tool Gateway command.

Recommended environment:

- keep repo-local `config.us.json` / `config.hk.json` as generated runtime snapshots
- complete first-time initialization with `./om config init --output config.yaml --runtime-output-dir .`
- use explicit `config_path` input only when you intentionally want to override the default repo-local config
- keep `OM_AGENT_ENABLE_WRITE_TOOLS` unset unless you explicitly want config writes
- use `$RUNTIME/service.profile.json` from `./om service render` when production paths are not repo-local

Recommended first commands:

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
```

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
  "profile_path": "/var/lib/options-monitor/service.profile.json"
}'
```

Default service safety posture:

- Prefer `healthcheck` or `runtime_status` before any runtime command.
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

OpenClaw cron/readiness/profile workflows are retired from the public plugin contract.
