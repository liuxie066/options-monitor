# Agent Integration

The public launcher is `./om-agent`.

It exposes a stable JSON contract intended for local agent usage:

- `./om-agent spec`
- `./om-agent run --tool <name> --input-json '<json>'`

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

## Claude Code

Use the launcher as a local command tool. Typical pattern:

```bash
./om-agent spec
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
```

## Kimi Code

Use the same launcher contract. Kimi Code only needs a local command invocation and JSON parsing.

## OpenClaw

Treat `./om-agent` as a local tool host command.

Recommended environment:

- set `OM_CONFIG_DIR` or explicit config path env vars
- set `OM_PM_CONFIG` if secrets live outside the repo
- keep `OM_AGENT_ENABLE_WRITE_TOOLS` unset unless you explicitly want config writes

OpenClaw integration in this public repo is local-tool oriented. Private cron/deploy workflows are not part
of the public plugin contract.
