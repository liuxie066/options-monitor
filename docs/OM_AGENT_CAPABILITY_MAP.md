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

The `portfolio` toolset contains three pure-read tools. `portfolio_query` lets the
same `om_chat` Copilot read portfolio-management `health`, `accounts`, `overview`,
`holdings`, `cash`, `nav`, `distribution`, and `full_report` views over a GET-only
loopback HTTP boundary. The two primary bridge tools keep independent accounting
equations: `portfolio_pnl_bridge` combines PM capital facts with
`option_performance_report.pnl.period_total_net`, while `portfolio_cash_bridge`
combines PM `/analysis/cash-facts` with
`option_performance_report.cash.total_cash_change_net`. Both return MTD/YTD
waterfall `steps[]` and Markdown `fallback_text`; missing or incomplete evidence
remains unavailable instead of becoming zero. The old `portfolio_capital_bridge`
has been removed because it mixed total assets with legacy option cash
semantics. None of the current tools exposes portfolio writes, accepts endpoint
arguments, or adds a second Scene/Agent. The toolset is optional and defaults
off for internal Copilot projection; `./om-agent` continues to expose all three
canonical tools independently of this Copilot setting.

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
- directly mutate config, positions, trade events, notifications, services,
  portfolio-management, or broker state;
- fall back to a fixed evidence collection recipe;
- define monthly-review or other business-specific runtime capabilities.

## Configuration

The canonical authoring shape is:

```yaml
assistant:
  enabled: true
  copilot:
    enabled: true
    toolsets:
      portfolio: false
  active_model: deepseek-default
```

Set `assistant.copilot.toolsets.portfolio: true` to expose the portfolio toolset to
Copilot. Effective access requires the assistant, Copilot, and portfolio toolset
flags to all be true. Missing toolset configuration is fail-closed.

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
