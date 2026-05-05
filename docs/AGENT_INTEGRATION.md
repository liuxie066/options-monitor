# Agent Integration

The public launcher is `./om-agent`.

It exposes a stable JSON contract intended for local agent usage:

- `./om-agent add-account --market us|hk --account-label <label> --account-type futu|external_holdings`
- `./om-agent spec`
- `./om-agent run --tool <name> --input-json '<json>'`

也支持：

- `./om-agent run --tool <name> --input-file payload.json`

其中 `--input-file` 会覆盖 `--input-json`。

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
- 某些底层诊断项（例如 OpenD doctor 的细粒度失败原因）可能会体现在 `checks[]` 中，而不是顶层错误 code 枚举中。

## Claude Code

Use the launcher as a local command tool. Typical pattern:

```bash
./om-agent spec
./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'
./om-agent run --tool config_validate --input-json '{"config_key":"us"}'
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"user1"}'
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"user1"}'
./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"user1","month":"2026-04"}'
./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"user1","status":"open"}'
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

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

Treat `openclaw_readiness` as OpenClaw-specific. It is safe to call outside OpenClaw, but the
`openclaw_binary` check may return `warn` when the `openclaw` command is not installed.

## OpenClaw

Treat `./om-agent` as a local tool host command.

Recommended environment:

- keep repo-local `config.us.json` / `config.hk.json` as the default runtime configs
- complete first-time initialization from the local WebUI after OpenD is ready
- use explicit `config_path` input only when you intentionally want to override the default repo-local config
- keep `OM_AGENT_ENABLE_WRITE_TOOLS` unset unless you explicitly want config writes

Recommended first commands:

```bash
./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'
./om-agent run --tool openclaw_readiness --input-json '{"config_key":"us"}'
```

Use `openclaw_readiness` when you need a one-shot readiness summary. It combines:

- `runtime_status`
- existing `healthcheck`
- local `openclaw` command availability

Use `runtime_status` when you only want to inspect existing runtime files. It does not run a pipeline, send
notifications, or write state. It summarizes:

- `output_shared/state/last_run.json`
- `output/state/last_run.json`
- `output/reports/symbols_notification.txt`
- `output_accounts/<account>/state/last_run.json`
- `output_accounts/<account>/reports/symbols_notification.txt`
- the latest `output_runs/<run_id>` pointer when available

If the production layout uses non-default paths, pass them explicitly:

```bash
./om-agent run --tool runtime_status --input-json '{
  "config_path": "/home/node/.openclaw/workspace/options-monitor-prod/config.us.json",
  "report_dir": "/home/node/.openclaw/workspace/options-monitor-prod/output/reports",
  "state_dir": "/home/node/.openclaw/workspace/options-monitor-prod/output/state",
  "shared_state_dir": "/home/node/.openclaw/workspace/options-monitor-prod/output_shared/state",
  "accounts_root": "/home/node/.openclaw/workspace/options-monitor-prod/output_accounts",
  "runs_root": "/home/node/.openclaw/workspace/options-monitor-prod/output_runs"
}'
```

Default OpenClaw safety posture:

- Prefer `openclaw_readiness` or `runtime_status` before any runtime command.
- Do not run `./om run tick`, the deprecated `scripts/send_if_needed.py` wrapper, or notification send commands unless the user explicitly asks for a live run.
- Keep real writes behind both `OM_AGENT_ENABLE_WRITE_TOOLS=true` and a payload-level confirmation such as `confirm=true`.

## `spec` 的行为说明

`./om-agent spec` 输出的是当前环境下的 tool manifest。

也就是说它不是完全静态文本，至少这些值会受环境影响：

- `write_tools_enabled`
- 默认写工具可用性
- 每个工具的 `risk_level` / `requires_confirm` / `requires_env` / `safe_default_input`

如果你打开了：

```bash
OM_AGENT_ENABLE_WRITE_TOOLS=true
```

那么 `spec` 里的默认能力描述也会随之变化。

## 写操作门禁

当前写操作不是只靠一个开关就能执行。

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
