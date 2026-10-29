# OM Capability Surfaces

This document records the three public capability surfaces. Runtime ownership
and Bot design are defined in [ARCHITECTURE.md](ARCHITECTURE.md) and
[BOT_DESIGN.md](BOT_DESIGN.md).

## Surfaces

| Surface | Entry | Authority |
|---|---|---|
| Tool Gateway | `./om-agent` | `src/application/agent_tool_registry.py` |
| Deterministic Control | `./om bot handle` | explicit command parser, pending-operation store, and `inbound_control.py` |
| Bot | free-form text through `./om bot handle` or `./om bot run` | Bot Service + Host + `om_chat` Agent; channel runs may request Control previews |

`./om-agent` exposes structured JSON tools to external Agents. It is not OM's
autonomous Agent. Internal Bot projects a pure-read subset from the same
canonical registry.

The Tool Gateway exposes the canonical pure-read portfolio tools defined in
[`portfolio.TOOLS`](../src/application/agent_tools/portfolio.py).
`portfolio_query` reads portfolio-management `health`, `accounts`, `overview`,
`holdings`, `cash`, `nav`, `distribution`, and `full_report` views over a GET-only
loopback HTTP boundary, subject to `portfolio_management.enabled`.
`portfolio_pnl_bridge` reads portfolio capital facts while option PnL remains
explicitly unavailable. `portfolio_cash_bridge` is unavailable until authoritative
portfolio cash facts and combined option/assignment lifecycle cash are available.
The four option-performance metrics do not supply these missing authorities;
option net cash flow excludes the assignment/stock cash needed for a combined
cash equation. These tools expose no portfolio writes or endpoint arguments.

Internal `om_chat` projects only the fixed read-tool list in
[`om_chat.scene.json`](../src/application/bot/om_chat.scene.json).
That list currently excludes the portfolio tools, including `portfolio_query`.
Enabling `portfolio_management.enabled` does not add them to the Scene, and there
is no Bot toolset activation flag. Tool Gateway availability and internal Bot
projection are separate boundaries.

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

The canonical authoring example for `config.yaml` declares the model profile it
selects:

```yaml
bot:
  enabled: true
  active_model: deepseek-default
  models:
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
      context_window_tokens: 24000
      max_output_tokens: 2048
```

`bot.models` and `bot.active_model` resolve the provider used by
Bot. `api_key_env` names an environment variable; it is not a key value.
`./om config build-bot --dry-run` validates this authoring structure without
contacting the provider or verifying credential availability. Real model runs
require separate configuration and authorization.

The retired top-level `assistant` key is rejected, including the old
`assistant.planner` and `assistant.agent_loop` shapes. Per-scene channel
allowlists are not supported runtime controls.

## Verification

```bash
./om-agent spec
./om bot commands
./om bot capabilities
./om bot llm-check
./om bot run --text "当前期权风险主要集中在哪里" --config-key us
```

Real-model Bot runs can send private OM evidence to the configured provider
and require explicit operator approval.
