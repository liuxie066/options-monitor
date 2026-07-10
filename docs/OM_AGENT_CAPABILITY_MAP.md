# OM Assistant Capability Map

This document maps the current supported capability surface. Architecture
terminology and runtime ownership are defined in [ARCHITECTURE.md](ARCHITECTURE.md).
Channel message control is defined in [INBOUND_CONTROL.md](INBOUND_CONTROL.md).

## Surfaces

| Surface | Entry | Capability Source |
|---|---|---|
| Tool Gateway | `./om-agent` | `src/application/agent_tool_registry.py` |
| Inbound Assistant | `./om assistant handle` | `src/application/assistant/capability_catalog.py` and explicit command parsing |
| Local Copilot v2 | `./om copilot run|eval` | `src/application/copilot/scene.py` and Service-prepared execution contracts |

`./om-agent` is for structured JSON tools. `./om assistant` is for channel
messages and deterministic command handling. `./om copilot` is the local/eval
read-only answer-quality loop; it is not exposed through the Tool Gateway
manifest and is not connected to remote channels.

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
- generic free-form model answers over tool observations;
- fallback to generic LLM chat.

Rejected free text returns `NATURAL_LANGUAGE_REBUILDING` by default. If
`assistant.copilot.enabled=true` is explicitly configured, free text enters the
Copilot channel gate. A scene must also be allowlisted in
`assistant.copilot.channel_scenes`; no business scene is channel-ready in the
current slice. Future channel execution will also require explicit assistant
model configuration before any tool call. `assistant.copilot.human_review=true`
holds Host-backed
channel answers for manual review while retaining sanitized audit summaries.

## Local Copilot v2 Capabilities

Local Copilot v2 currently supports read-only local/eval scenes for operations
diagnostics, income attribution, and current exposure. It selects a declared
scene through Service, executes only Host-admitted read tools from that scene,
and returns `insufficient_evidence` when observations cannot support a
conclusion.

Examples:

```bash
./om copilot run --text "NVDA 为什么没有通过筛选" --config-key us
./om copilot eval --scene current_option_exposure --fixture current_option_exposure_model_ready --text "当前期权风险暴露集中在哪些标的" --model-action-json-file tests/fixtures/copilot/current_option_exposure_model_action.json
./om copilot eval --scene monthly_income_attribution --fixture june_income_attribution_basic --text "6月收益主要来自哪里" --model-action-json-file tests/fixtures/copilot/june_income_attribution_model_action.json
```

This surface is for local validation and answer-quality regression. It must not
send notifications, mutate config or trade state, call broker-facing write
paths, or substitute fixture evidence into local/channel answers.

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
that removal. The replacement is the Copilot v2 Service + Host + Agent task
system, currently exposed only through local/eval `./om copilot ...` commands
for answer-quality work. The Inbound Assistant has only a disabled-by-default
channel gate; no production business scene is channel-ready in the current
slice.
