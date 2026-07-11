# OM Capability Surfaces

This document records the three public capability surfaces. Runtime ownership
and Copilot design are defined in [ARCHITECTURE.md](ARCHITECTURE.md) and
[OM_COPILOT_V2_DESIGN.md](OM_COPILOT_V2_DESIGN.md).

## Surfaces

| Surface | Entry | Authority |
|---|---|---|
| Tool Gateway | `./om-agent` | `src/application/agent_tool_registry.py` |
| Deterministic Control | `./om assistant handle` | explicit command parser, pending-operation store, and `inbound_control.py` |
| Copilot | free-form text through `./om assistant handle` or `./om copilot run` | Copilot Service + Host + `om_chat` Agent; channel runs may request Control previews |

`./om-agent` exposes structured JSON tools to external Agents. It is not OM's
autonomous Agent. Internal Copilot projects a pure-read subset from the same
canonical registry.

## Deterministic Control

Control handles only explicit protocol:

- slash commands and other unambiguous operator commands;
- sender allowlist and message-id idempotency;
- read command execution;
- write previews;
- pending-operation confirm and cancel;
- audit and operation receipts.

Control does not classify free-form business questions and does not maintain an
LLM-visible or planner-allowed capability map.

Write-capable actions remain:

```text
explicit command -> preview receipt -> pending operation -> confirm -> apply
```

## Copilot

All non-Control text enters the single `om_chat` Scene when Copilot is enabled.
The model chooses among Host-projected pure-read tools and returns natural
language final text. On channel runs it may also use one generic
`request_control_preview` meta-tool; the available preview capabilities are
projected from the deterministic Control catalog. Service prepares contracts;
it does not route by business keywords, task profile, strategy, month, account,
or expected tool sequence.

Copilot must not:

- receive write, confirm, cancel, or apply tools;
- directly mutate config, positions, trade events, notifications, services, or
  broker state;
- fall back to a fixed evidence collection recipe;
- define monthly-review or other business-specific runtime capabilities.

## Configuration

The canonical authoring shape is:

```yaml
assistant:
  enabled: true
  copilot:
    enabled: true
  active_model: deepseek-default
```

`assistant.models` and `assistant.active_model` resolve the provider used by
Copilot. `assistant.planner`, `assistant.agent_loop`, and per-scene channel
allowlists are not runtime authorities.

## Verification

```bash
./om-agent spec
./om assistant commands
./om assistant capabilities
./om assistant llm-check
./om copilot run --text "当前期权风险主要集中在哪里" --config-key us
```

Real-model Copilot runs can send private OM evidence to the configured provider
and require explicit operator approval.
