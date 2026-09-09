# OM Capability Surfaces

This document records the three public capability surfaces. Runtime ownership
and Bot design are defined in [ARCHITECTURE.md](ARCHITECTURE.md) and
[BOT_DESIGN.md](BOT_DESIGN.md).

## Surfaces

| Surface | Entry | Authority |
|---|---|---|
| Tool Gateway | `./om-agent` | `src/application/agent_tool_registry.py` |
| Deterministic Control | `./om assistant handle` | explicit command parser, pending-operation store, and `inbound_control.py` |
| Bot | free-form text through `./om assistant handle` or `./om bot run` | Bot Service + Host + `om_chat` Agent; channel runs may request Control previews |

`./om-agent` exposes structured JSON tools to external Agents. It is not OM's
autonomous Agent. Internal Bot projects a pure-read subset from the same
canonical registry.

The `portfolio` toolset contains three pure-read tools. `portfolio_query` lets the
same `om_chat` Bot read portfolio-management `health`, `accounts`, `overview`,
`holdings`, `cash`, `nav`, `distribution`, and `full_report` views over a GET-only
loopback HTTP boundary. The two primary bridge tools keep independent accounting
routes but return explicit unavailable states: option performance no longer owns
option PnL, and its net cash flow excludes assignment/stock cash required by a
combined cash equation. They do not synthesize those missing authorities from
the four option-performance metrics. The old `portfolio_capital_bridge`
has been removed because it mixed total assets with legacy option cash
semantics. None of the current tools exposes portfolio writes, accepts endpoint
arguments, or adds a second Scene/Agent. The toolset is optional and defaults
off for internal Bot projection; `./om-agent` continues to expose all three
canonical tools independently of this Bot setting.

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

## Bot

All non-Control text enters the single `om_chat` Scene when Bot is enabled.
The model chooses among Host-projected pure-read tools and returns natural
language final text. On channel runs it may also use one generic
`request_control_preview` meta-tool; the available preview capabilities are
projected from the deterministic Control catalog. Service prepares contracts;
it does not route by business keywords, task profile, strategy, month, account,
or expected tool sequence.

Bot must not:

- receive write, confirm, cancel, or apply tools;
- directly mutate config, positions, trade events, notifications, services,
  portfolio-management, or broker state;
- fall back to a fixed evidence collection recipe;
- define monthly-review or other business-specific runtime capabilities.

## Configuration

The canonical authoring shape is:

```yaml
assistant:
  enabled: true
  bot:
    enabled: true
    toolsets:
      portfolio: false
  active_model: deepseek-default
```

Set `assistant.bot.toolsets.portfolio: true` to expose the portfolio toolset to
Bot. Effective access requires the assistant, Bot, and portfolio toolset
flags to all be true. Missing toolset configuration is fail-closed.

`assistant.models` and `assistant.active_model` resolve the provider used by
Bot. `assistant.planner`, `assistant.agent_loop`, and per-scene channel
allowlists are not runtime authorities.

## Verification

```bash
./om-agent spec
./om assistant commands
./om assistant capabilities
./om assistant llm-check
./om bot run --text "当前期权风险主要集中在哪里" --config-key us
```

Real-model Bot runs can send private OM evidence to the configured provider
and require explicit operator approval.
