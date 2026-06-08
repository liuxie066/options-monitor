# OM Ops Copilot Capability Map

This document defines the intended capability boundary for OM Ops Copilot. It is
a map, not a new execution path. Current facts should still be verified through
the tool manifest, Inbound capability catalog, source, tests, configs, and
runtime artifacts. Current public names such as `./om-agent`,
`./om assistant ...`, and `src/application/assistant/...` remain compatibility
facades and implementation paths.

Verified entry points for this snapshot:

- `./om-agent spec`
- `./om assistant capabilities --format json`
- `src/application/agent_tool_registry.py`
- `src/application/tool_allowlist.py`
- `src/application/assistant/commands.py`
- `src/application/assistant/agent_loop.py`

## Operating Boundary

OM Ops Copilot is an intelligent operations copilot for OM. It is not Inbound
itself, not a research analyst, not a chatbot, and not an unrestricted shell
bridge.

Rules:

- Facts come from deterministic OM tools, current code, config, runtime
  artifacts, tests, git state, or explicit tool output.
- LLM may classify intent, choose evidence paths, plan bounded tool calls, and
  summarize observations. LLM is not a factual source.
- For analytical answers, use the Data Analysis-style boundary: LLM plans the
  analysis and may interpret results, while deterministic tools, controlled
  analysis artifacts, and renderers own calculations and user-visible facts.
  Do not add natural-language parser or regex guards as the primary way to make
  LLM-generated accounting facts safe.
- Intelligence means knowing what to inspect first, what not to touch, and how
  to verify. It does not mean broader execution freedom.
- Writes to config, notification channels, Feishu, ledger/trade state,
  broker-facing data, or production services require explicit human intent and
  the existing preview/confirm gates.
- Local report/cache generation and Research / Shadow Replay are not Ops
  Copilot capabilities. Ops Copilot may recommend those paths only when the user
  explicitly asks to refresh evidence or evaluate strategy quality.

## Agent Loop Boundary

OM Copilot should be reasoned about as one bounded Agent loop, not as multiple
runtime layers:

```text
Channel input
-> AgentSession (conceptual session boundary)
-> AgentLoop
   -> Perceive
   -> Understand
   -> Decide
   -> Act
   -> Observe
-> Reply
```

This names the authority path:

- `AgentSession` is a conceptual session boundary, not a separate runtime layer
  or required class name. It owns the inbound request, sender/channel/conversation
  scope, audit identity, recent context, and pending-operation context.
- `Perceive` normalizes the channel message into the current request context.
- `Understand` may use slash commands, deterministic parsing as a safety
  component, or the AgentLoop Planner. It only describes intent or proposes a
  bounded plan; it does not execute tools.
- `Decide` owns permission, safety class, capability support, config-scope
  injection, and preview/confirm boundaries. Its conceptual decisions are
  `allow`, `preview`, `ask`, `deny`, or `defer`.
- `Act` executes read/local tools or creates a `PendingOperation` for approved
  preview writes. It does not let LLM-originated plans confirm, cancel, or apply
  writes.
- `Observe` renders deterministic facts, records audit evidence, and builds the
  user-facing reply.

Current implementation names map to this loop rather than replacing it:

| Agent concept | Current implementation handle |
|---|---|
| `AgentSession` | `AssistantRequest`, audit row, conversation context, pending operation store |
| `Understand` output | `PerceptionResult`, or an internal `tool_plan` produced by `agent_loop` |
| `Decide` output | `ReasoningResolution`, planner validation, tool/operation policy checks |
| `Act` output | `ActionResult`, `execute_tool(...)`, or operation preview handlers |
| `Observe` output | `ObservationResponse`, canonical renderer, audit payloads |

Do not add a second `ToolRegistry` module. The registry authority is split by
surface: `src/application/agent_tool_registry.py` remains the `./om-agent` tool
manifest collector, and `src/application/assistant/capability_catalog.py` is the
Inbound capability catalog. AgentLoop read tools are derived from the existing
tool registry plus planner-visible capability metadata; preview-write authority
comes from capability metadata. Future implementation work should keep
consolidating metadata into those existing authorities instead of adding another
runtime layer or parallel tool control plane.

LLM authority is deliberately narrow:

- LLM may classify intent, select read evidence paths, plan bounded read tools,
  request clarification, and synthesize observations.
- In `agent_loop`, LLM may initiate exactly one approved preview-write
  capability: `manual_trade_open`, `manual_trade_close`,
  `manual_trade_update`, `symbol_edit`, `model_use`, or `upgrade_now`.
- LLM-originated preview-write plans only create `PendingOperation` records
  through existing deterministic operation handlers.
- LLM must never confirm, cancel, apply, send notifications, write config, write
  ledger/trade state, operate services, or bypass pending-operation gates.
- Explicit confirm/cancel/apply remains deterministic-only and must be bound to
  an existing pending operation plus the existing sender/env/HMAC/TTL gates.

## Surfaces

| Surface | Purpose | Default capability boundary | LLM role |
|---|---|---|---|
| `./om-agent` | Current local structured JSON facade for Ops Copilot deterministic tools | Ops Copilot reads plus selected compatibility helpers; write modes require `OM_AGENT_ENABLE_WRITE_TOOLS=true` and payload confirmation where applicable | None inside tool execution; external agent may plan calls |
| `./om assistant handle` | Current CLI namespace for Inbound remote messages through Feishu, WeChat, or future channels | Inbound catalog only; no arbitrary shell, no direct full `om-agent` manifest exposure | May recognize allowed intents; read/local tools are directly executable, while approved preview-write capabilities may only create pending previews |

Related OM surfaces outside Ops Copilot:

| Surface | Why it is out of core | Default owner |
|---|---|---|
| `./om` human/operator CLI | Full operator workflows include live runs, report generation, release, and controlled writes | Human operator / Codex when explicitly requested |
| Research / Shadow Replay | Offline evidence collection, replay, and parameter-quality evaluation | Research / Strategy Lab workflow |
| Codex in repo | Development, tests, git, release, and code changes | Local development workflow |

## Risk Classes

| Class | Definition | Default exposure | Confirmation rule |
|---|---|---|---|
| Core Read | Reads existing state or validates config without side effects | `om-agent`; selected tools in Inbound | No confirmation, but path/config inputs must stay scoped |
| Preview Write | Builds a pending change preview without applying production mutation | Inbound deterministic preview paths; local dry-run helpers | Must clearly present preview; apply remains separate |
| Confirm Write | Applies config, ledger/trade, model, upgrade, or local repo writes | Human/operator only, or Inbound confirm commands for existing pending previews | Explicit confirm, and env/yes gates where implemented |
| Admin / Live Ops | Service install/start/stop, live tick, notification send, Feishu sync, broker-facing operations | Operator-only | Explicit human request and dry-run/read-only check first |

## Ops Copilot And Guarded Capability Matrix

| Capability | Entrypoint | Tool(s) | Fact sources | Side effects | Risk class | LLM role | Verification | Allowed surfaces |
|---|---|---|---|---|---|---|---|---|
| Runtime status | `./om-agent run --tool runtime_status`, `/status` | `runtime_status` | `output_shared/state`, `output_accounts`, `output_runs`, service profile when provided | None | Core Read | Select status path, summarize evidence | `./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'` | `om-agent`, Inbound |
| Health/readiness | `./om-agent run --tool healthcheck`, `/health`, `/doctor` | `healthcheck`, `openclaw_readiness` | Runtime config, data config, OpenD readiness probe, service/channel diagnostics | None | Core Read | Decide when readiness beats runtime artifact reads | `./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'` | `om-agent`, Inbound for `healthcheck`; `openclaw_readiness` local `om-agent` |
| Config validation | `./om-agent run --tool config_validate`, `/config-check` | `config_validate` | Runtime config snapshot | None | Core Read | Route config questions to validator output | `./om-agent run --tool config_validate --input-json '{"config_key":"us"}'` | `om-agent`, Inbound |
| Scheduler diagnosis | `./om-agent run --tool scheduler_status` | `scheduler_status` | Runtime config, scheduler state | None | Core Read | Use after runtime status when skip/timing is suspected | `./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'` | Local `om-agent`; not default Inbound |
| Run history | `./om-agent run --tool runtime_runs`, `/runs` | `runtime_runs` | `output_runs` snapshots | None | Core Read | Find relevant run before deeper log or candidate evidence | `./om-agent run --tool runtime_runs --input-json '{"limit":10}'` | `om-agent`, Inbound |
| Runtime logs | `./om-agent run --tool runtime_logs`, `/logs` | `runtime_logs` | Run audit/tool/tick/service logs | None | Core Read | Choose log scope and line count after identifying run | `./om-agent run --tool runtime_logs --input-json '{"kind":"all","lines":50}'` | `om-agent`, Inbound |
| Candidate filter explanation | `./om-agent run --tool candidate_filter_explain` | `candidate_filter_explain` | `candidate_filter_trace.jsonl` | None | Core Read | Map symbol/account question to trace filters | `./om-agent run --tool candidate_filter_explain --input-json '{"symbol":"NVDA"}'` | Local `om-agent`; not default Inbound |
| Candidate ranking explanation | `./om-agent run --tool candidate_rank_explain` | `candidate_rank_explain` | Existing candidate CSV/report artifacts | None | Core Read | Compare ranking policy against observed rows | `./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'` | Local `om-agent`; not default Inbound |
| Position read | `./om-agent run --tool option_positions_read`, `/positions` | `option_positions_read` | SQLite option-position store, trade events, projection inspection | None | Core Read | Build query filters; explain missing data explicitly | `./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'` | `om-agent`, Inbound |
| Monthly income | `./om-agent run --tool monthly_income_report`, `/income` | `monthly_income_report` | Local option positions and income attribution; target analytical answers use an artifact derived from `include_rows=true` | None | Core Read | Plan account/month/detail scope and interpretation angle; deterministic artifact/renderer owns amounts, rows, counts, dates, symbols, and currencies | `./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx"}'` | `om-agent`, Inbound |
| Close-advice read | `./om-agent run --tool close_advice_read` | `close_advice_read` | Existing close-advice report | None | Core Read | Prefer read path for "should I close" unless refresh is requested | `./om-agent run --tool close_advice_read --input-json '{"config_key":"us"}'` | `om-agent`, Inbound as `position_exit_analysis` |
| Notification preview | `./om-agent run --tool preview_notification` | `preview_notification` | Provided alert/change text or files | None | Core Read | Explain rendered notification shape without sending | `./om-agent run --tool preview_notification --input-json '{"alerts_text":"","changes_text":""}'` | Local `om-agent`; no real send |
| Version check | `./om-agent run --tool version_check` | `version_check` | Local `VERSION`, git release tags | None | Core Read | Use before release planning | `./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'` | Local `om-agent`, Codex/operator |
| Symbol list | `./om assistant handle`, `/symbols` | `inbound.symbols` read path | Runtime/current assistant config symbol settings | None | Core Read | Recognize list intent only | `./om assistant capabilities --format json` | Inbound only |
| Pending previews | `./om assistant handle`, `/pending` | `inbound.pending` | Inbound operation store/audit | None | Core Read | Show pending state before confirm/cancel | `./om assistant capabilities --format json` | Inbound only |
| Model list | `./om assistant handle`, `/model list` | `inbound.model` read path | Inbound model profile config | None | Core Read | Not planner-routed; deterministic command only | `./om assistant capabilities --format json` | Inbound deterministic command only |
| Symbol config preview | Inbound natural language or slash commands; local dry-run | `inbound.symbols`, `manage_symbols` dry-run/list | Runtime config / `config.yaml` authoring source | Preview/pending operation; local dry-run may write nothing | Preview Write | AgentLoop Planner may create a pending `symbol_edit` preview; no confirm/apply | Inbound pending state, config validation after apply | Inbound preview; local `om-agent` dry-run |
| Manual trade preview | Inbound natural language or deterministic commands | `inbound.manual_trade` preview paths | User-provided trade text, ledger validation, config/account context | Pending operation/audit only | Preview Write | AgentLoop Planner may create pending `manual_trade_open` / `manual_trade_close` / `manual_trade_update` previews; deterministic operation handler owns parsing and validation | Pending preview and validation warnings | Inbound preview only |
| Model switch preview | Inbound natural language or deterministic command | `inbound.model` preview path | Current assistant model profiles in config | Pending operation/audit only | Preview Write | AgentLoop Planner may create a pending `model_use` preview; no config write | Pending preview; model check after confirm | Inbound preview only |
| Upgrade preview | Inbound natural language or deterministic command | `inbound.upgrade` preview path | Release metadata and service/runtime context | Pending operation/audit only | Preview/Admin | AgentLoop Planner may create a pending `upgrade_now` admin preview; no upgrade/apply | Pending preview; upgrade verify after confirm | Inbound preview only |
| Local VERSION update | `./om-agent run --tool version_update` | `version_update` | `VERSION`, semver rules | Writes `VERSION` only when `apply=true` | Confirm Write / local repo write | Plan release metadata; not execute without request | `git diff`, release check | Local Codex/operator |
| Symbol config apply | Inbound confirm or `manage_symbols` non-dry-run | `inbound.symbols` confirm path, `manage_symbols` | Pending preview, config source | Writes `config.yaml` / runtime config where configured | Confirm Write | None at apply time | `config_validate`, `git diff`, pending cleared | Inbound confirm or local operator |
| Manual trade apply/cancel | Inbound confirm/cancel | `inbound.manual_trade` confirm/cancel paths | Pending preview, ledger validation | Writes trade/ledger state on confirm | Confirm Write | None at apply time | `option_positions_read action=inspect`, audit | Inbound confirm only |
| Model switch apply/cancel | Inbound confirm/cancel | `inbound.model` confirm/cancel paths | Pending preview, current assistant config source | Writes current assistant model config on confirm | Confirm Write | None at apply time | `./om assistant model current`, config build/check | Inbound confirm only |
| Upgrade apply/cancel | Inbound confirm/cancel or operator CLI | `inbound.upgrade`, `om update ...` | Pending preview, release artifacts, service profile | Updates local install/runtime service state where configured | Confirm Write / Admin | None at apply time | `om update verify`, service/runtime status | Inbound confirm or operator only |

## Out-Of-Core OM Tool Surfaces

These tools may appear in `./om-agent spec` or `./om` because the public
facade still supports them, but they are not Ops Copilot capabilities. Ops
Copilot should not use them as default evidence paths.

| Surface | Examples | Purpose | Ops Copilot rule |
|---|---|---|---|
| Local report/cache generation | `scan_opportunities`, `query_cash_headroom`, `get_portfolio_context`, `prepare_close_advice_inputs`, `close_advice`, `get_close_advice` | Refresh market-derived local reports or caches | Use only when the user explicitly asks to refresh/generate evidence; prefer existing read artifacts first |
| Research evidence collection | `./om research collect` | Build redacted evidence bundle / handoff | Out of core; suggest only for offline quality analysis |
| Shadow Replay / parameter evaluation | `./om research shadow-replay ...` | Evaluate candidate quality, replay outcomes, and dry-run-only parameter hypotheses | Out of core; belongs to Research / Strategy Lab, not operations copilot |
| Test and release workflow | `pytest`, `scripts/release_check.py`, git commands, GitHub release workflow | Validate and publish code changes | Codex/operator-only; Ops Copilot may plan gates but does not own release execution |
| Live tick / notifications | `om run tick`, `om run tick-cron`, notification delivery adapters | Run production scan/report/notification workflows | Operator-only; Ops Copilot should recommend read-only preflight first |
| Service install/start/stop | `om service ...`, systemd/launchd commands | Modify or operate production services | Operator-only; require explicit human request and dry-run/preflight first |

## Default Evidence Plans

| User intent | First evidence path | Escalate only when | Success standard |
|---|---|---|---|
| "Is OM healthy?" | `runtime_status` | Status is stale, missing, or contradictory | Current runtime summary has explicit ok/warn/error evidence |
| "Can this environment run?" | `healthcheck` | Dependency/config checks fail | Checks identify missing config/dependency without sending notifications |
| "Why did the run skip?" | `runtime_status` -> `scheduler_status` -> `runtime_logs` | Scheduler state or logs point to a deeper runtime failure | Skip reason is tied to scheduler/log artifact |
| "Why did symbol X not appear?" | `candidate_filter_explain` -> `candidate_rank_explain` | Trace is missing or stale | Accepted/rejected/not-observed result comes from trace/report evidence |
| "What positions/income do I have?" | `option_positions_read` or `monthly_income_report` | User asks for repair or apply | Rows come from local position/income sources with missing data called out |
| "Should I close this position?" | `close_advice_read` | User explicitly asks to refresh market data | Answer references existing close-advice rows or says no fresh row exists |
| "Preview a config/symbol/trade/model change" | Inbound preview command or local dry-run | User confirms exact pending operation | Preview includes diff/normalized fields and risk before apply |
| "Release / push / upgrade" | `git status`, `version_check`, tests/release check | User explicitly asks to commit, push, release, or apply upgrade | Release gate passes and final state is verified |
| "Analyze strategy quality or tune parameters" | Out-of-core Research / Shadow Replay workflow | User approves offline analysis workflow | Recommendations cite replay/evidence and do not mutate production config |

## Current Implementation Notes

- `./om-agent spec` exposes more than Ops Copilot. It includes Ops Copilot
  read tools, selected local report/cache helpers, and selected local write
  helpers for compatibility. The Ops Copilot boundary in this document is
  narrower than the raw manifest.
- Research and Shadow Replay form an independent offline evidence/replay module
  under `./om research ...`. They are used to evaluate strategy quality and
  dry-run-only parameter hypotheses. They are not Inbound core and are not
  `om-agent` tools.
- `healthcheck.tools` is derived from the same `om-agent` registry used by
  `./om-agent spec`; Research/Shadow Replay is reported separately as a
  non-Ops-Copilot side lane.
- Inbound has special tools such as `inbound.symbols`,
  `inbound.pending`, and `inbound.model` that are not `om-agent` registry tools.
  Any future policy derivation must handle these separately.
- `PURE_READ_TOOLS` remains owned by neutral
  `src/application/tool_allowlist.py`, but its value is derived from
  `agent_tool_registry` tool metadata via `is_pure_read()`: `read_only=true`,
  resolved `risk_level=read_only`, no `side_effects`, and no confirmation
  requirement.
- `src/application/assistant/agent_loop.py` has a narrower Inbound planner allowlist
  than the full pure-read manifest. That is intentional for remote LLM planning,
  but it should remain tested against the public capability catalog.
- `assistant.mode` is a legacy compatibility field only. The active product
  controls are `assistant.enabled` and `assistant.planner.enabled`. The design
  target is one conceptual `AgentSession` boundary + `AgentLoop` with
  deterministic parsing and optional model planning inside `Understand`.
- Write-request detection is owned by registry metadata/policy. `version_update`
  and `manage_symbols` carry explicit write predicates in their
  `AgentTool`; `src/application/tool_execution.py` delegates env/confirm gates
  to `src/application/agent_tools/permissions.py`.

## Policy Tests To Preserve

The capability boundary should be protected by tests that keep these sources in
sync:

- `./om-agent spec`
- `src/application/agent_tool_registry.py`
- `src/application/tool_allowlist.py`
- `./om assistant capabilities --format json`
- `src/application/assistant/commands.py`
- `src/application/assistant/agent_loop.py`

Minimum invariants:

- LLM-executable Inbound capabilities are read-only.
- LLM-recognizable but non-executable capabilities cannot apply writes.
- Confirm/cancel/apply intents are not LLM-recognizable.
- Pure-read Inbound tools either appear in `PURE_READ_TOOLS` or are explicitly
  documented special inbound read tools.
- Tools with side effects are not treated as Core Read even when their
  `read_only` field is true.
- Research and Shadow Replay are classified as an offline side lane, not Ops
  Copilot core.
