# OM Assistant Capability Map

This document maps the current supported capability surface. Architecture
terminology and runtime ownership are defined in
[OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md).

## Surfaces

| Surface | Entry | Capability Source |
|---|---|---|
| Tool Gateway | `./om-agent` | `src/application/agent_tool_registry.py` |
| Inbound Assistant | `./om assistant handle` | `src/application/assistant/capability_catalog.py` and explicit command parsing |

`./om-agent` is for structured JSON tools. `./om assistant` is for channel
messages and deterministic command handling.

## Current Inbound Capabilities

Inbound Assistant currently supports:

- slash-command parsing;
- sender allowlist checks;
- read-only command execution;
- write preview creation for explicit write commands;
- pending-operation confirm/cancel;
- audit, trace, and session summaries;
- assistant model/config diagnostics.

Inbound Assistant currently rejects:

- arbitrary natural-language analysis;
- automatic tool selection from free text;
- LLM synthesis over tool observations;
- fallback to generic LLM chat.

Rejected free text returns `NATURAL_LANGUAGE_REBUILDING`.

## Read Boundary

Read commands call the same tool registry used by `./om-agent`. The command
surface must stay narrower than the full tool manifest, and missing data must be
reported explicitly.

Examples:

```bash
./om assistant handle --text '/status' --config-key us
./om assistant handle --text '/income lx 2026-06' --config-key us
./om assistant handle --text '/positions lx' --config-key us
```

## Write Boundary

Write-capable actions must go through the preview/confirm lifecycle:

```text
explicit command -> preview receipt -> pending operation -> confirm -> apply
```

The assistant must not apply config, trade, position, notification, upgrade, or
broker-facing changes directly from natural language.

## Model Boundary

Model configuration remains inspectable for future rebuild work:

- `assistant.llm`
- `assistant.models`
- `assistant.active_model`

These fields do not enable free-form execution in the current runtime.

## Removed Capability Class

The previous free-form task/evidence/answer stack has been removed. Do not add
hardcoded question templates or channel-specific intent rules to compensate for
that removal. The replacement should be a first-class task system with explicit
evidence acquisition, bounded actions, and quality evals.
