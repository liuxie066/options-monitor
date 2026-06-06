# OM Agent Capability Map

This document defines the intended capability boundary for OM Agent. It is a
map, not a new execution path. Current facts should still be verified through
the tool manifest, Assistant capability catalog, source, tests, configs, and
runtime artifacts.

Verified entry points for this snapshot:

- `./om-agent spec`
- `./om assistant capabilities --format json`
- `src/application/agent_tool_registry.py`
- `src/application/tool_allowlist.py`
- `src/application/assistant/commands.py`
- `src/application/assistant/agent_loop.py`

## Operating Boundary

OM Agent is an intelligent operations copilot for OM. It is not a research
analyst, not a chatbot, and not an unrestricted shell bridge.

Rules:

- Facts come from deterministic OM tools, current code, config, runtime
  artifacts, tests, git state, or explicit tool output.
- LLM may classify intent, choose evidence paths, plan bounded tool calls, and
  summarize observations. LLM is not a factual source.
- Intelligence means knowing what to inspect first, what not to touch, and how
  to verify. It does not mean broader execution freedom.
- Writes to config, notification channels, Feishu, ledger/trade state,
  broker-facing data, or production services require explicit human intent and
  the existing preview/confirm gates.

## Surfaces

| Surface | Purpose | Default capability boundary | LLM role |
|---|---|---|---|
| `./om-agent` | Local structured JSON tools for Codex/OpenClaw/local agents | Manifest tools only; write modes require `OM_AGENT_ENABLE_WRITE_TOOLS=true` and payload confirmation where applicable | None inside tool execution; external agent may plan calls |
| `./om assistant handle` | Remote/inbound message control through Feishu, WeChat, or future channels | Assistant catalog only; no arbitrary shell, no direct `om-agent` manifest exposure | May recognize allowed intents; only read-only capabilities are executable by LLM |
| `./om` | Human/operator CLI | Full operator workflows, including live runs and controlled writes | Codex/operator planning only |
| Codex in repo | Local development and release assistance | Inspect, edit, test, git, release workflow when requested | Plan and implement against repo evidence |
| Research side lane | Redacted evidence collection for offline analysis | Evidence bundle and shadow-replay workflows; not Agent Core | Can suggest when evidence is needed; analysis remains separate from production mutation |

## Risk Classes

| Class | Definition | Default exposure | Confirmation rule |
|---|---|---|---|
| Core Read | Reads existing state or validates config without side effects | `om-agent`; selected tools in Assistant | No confirmation, but path/config inputs must stay scoped |
| Local Materialization | Reads OM state and writes local reports/cache only | Local `om-agent` or human CLI when explicitly requested | No production confirmation, but do not run implicitly from remote chat |
| Preview Write | Builds a pending change preview without applying production mutation | Assistant deterministic preview paths; local dry-run helpers | Must clearly present preview; apply remains separate |
| Confirm Write | Applies config, ledger/trade, model, upgrade, or local repo writes | Human/operator only, or Assistant confirm commands for existing pending previews | Explicit confirm, and env/yes gates where implemented |
| Admin / Live Ops | Service install/start/stop, live tick, notification send, Feishu sync, broker-facing operations | Operator-only | Explicit human request and dry-run/read-only check first |
| Research Side Lane | Builds or mirrors evidence for later analysis | Local `om research` / `om-agent research` only | Default no-write; writing local reports requires existing write gates |

## Capability Matrix

| Capability | Entrypoint | Tool(s) | Fact sources | Side effects | Risk class | LLM role | Verification | Allowed surfaces |
|---|---|---|---|---|---|---|---|---|
| Runtime status | `./om-agent run --tool runtime_status`, `/status` | `runtime_status` | `output_shared/state`, `output_accounts`, `output_runs`, service profile when provided | None | Core Read | Select status path, summarize evidence | `./om-agent run --tool runtime_status --input-json '{"config_key":"us"}'` | `om-agent`, Assistant |
| Health/readiness | `./om-agent run --tool healthcheck`, `/health`, `/doctor` | `healthcheck`, `openclaw_readiness` | Runtime config, data config, OpenD readiness probe, service/channel diagnostics | None | Core Read | Decide when readiness beats runtime artifact reads | `./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'` | `om-agent`, Assistant for `healthcheck`; `openclaw_readiness` local `om-agent` |
| Config validation | `./om-agent run --tool config_validate`, `/config-check` | `config_validate` | Runtime config snapshot | None | Core Read | Route config questions to validator output | `./om-agent run --tool config_validate --input-json '{"config_key":"us"}'` | `om-agent`, Assistant |
| Scheduler diagnosis | `./om-agent run --tool scheduler_status` | `scheduler_status` | Runtime config, scheduler state | None | Core Read | Use after runtime status when skip/timing is suspected | `./om-agent run --tool scheduler_status --input-json '{"config_key":"us","account":"lx"}'` | Local `om-agent`; not default Assistant |
| Run history | `./om-agent run --tool runtime_runs`, `/runs` | `runtime_runs` | `output_runs` snapshots | None | Core Read | Find relevant run before deeper log or candidate evidence | `./om-agent run --tool runtime_runs --input-json '{"limit":10}'` | `om-agent`, Assistant |
| Runtime logs | `./om-agent run --tool runtime_logs`, `/logs` | `runtime_logs` | Run audit/tool/tick/service logs | None | Core Read | Choose log scope and line count after identifying run | `./om-agent run --tool runtime_logs --input-json '{"kind":"all","lines":50}'` | `om-agent`, Assistant |
| Candidate filter explanation | `./om-agent run --tool candidate_filter_explain` | `candidate_filter_explain` | `candidate_filter_trace.jsonl` | None | Core Read | Map symbol/account question to trace filters | `./om-agent run --tool candidate_filter_explain --input-json '{"symbol":"NVDA"}'` | Local `om-agent`; not default Assistant |
| Candidate ranking explanation | `./om-agent run --tool candidate_rank_explain` | `candidate_rank_explain` | Existing candidate CSV/report artifacts | None | Core Read | Compare ranking policy against observed rows | `./om-agent run --tool candidate_rank_explain --input-json '{"mode":"put","top_n":5}'` | Local `om-agent`; not default Assistant |
| Position read | `./om-agent run --tool option_positions_read`, `/positions` | `option_positions_read` | SQLite option-position store, trade events, projection inspection | None | Core Read | Build query filters; explain missing data explicitly | `./om-agent run --tool option_positions_read --input-json '{"config_key":"us","action":"list","account":"lx","status":"open"}'` | `om-agent`, Assistant |
| Monthly income | `./om-agent run --tool monthly_income_report`, `/income` | `monthly_income_report` | Local option positions and income attribution | None | Core Read | Parse account/month; summarize rows | `./om-agent run --tool monthly_income_report --input-json '{"config_key":"us","account":"lx"}'` | `om-agent`, Assistant |
| Close-advice read | `./om-agent run --tool close_advice_read` | `close_advice_read` | Existing close-advice report | None | Core Read | Prefer read path for "should I close" unless refresh is requested | `./om-agent run --tool close_advice_read --input-json '{"config_key":"us"}'` | `om-agent`, Assistant as `position_exit_analysis` |
| Notification preview | `./om-agent run --tool preview_notification` | `preview_notification` | Provided alert/change text or files | None | Core Read | Explain rendered notification shape without sending | `./om-agent run --tool preview_notification --input-json '{"alerts_text":"","changes_text":""}'` | Local `om-agent`; no real send |
| Version check | `./om-agent run --tool version_check` | `version_check` | Local `VERSION`, git release tags | None | Core Read | Use before release planning | `./om-agent run --tool version_check --input-json '{"remote_name":"origin"}'` | Local `om-agent`, Codex/operator |
| Symbol list | `./om assistant handle`, `/symbols` | `inbound.symbols` read path | Runtime/assistant config symbol settings | None | Core Read | Recognize list intent only | `./om assistant capabilities --format json` | Assistant only |
| Pending previews | `./om assistant handle`, `/pending` | `inbound.pending` | Assistant operation store/audit | None | Core Read | Show pending state before confirm/cancel | `./om assistant capabilities --format json` | Assistant only |
| Model list | `./om assistant handle`, `/model list` | `inbound.model` read path | Assistant model profile config | None | Core Read | None; LLM must not route this intent | `./om assistant capabilities --format json` | Assistant deterministic command only |
| Opportunity scan | `./om-agent run --tool scan_opportunities` | `scan_opportunities` | Runtime config, OpenD/Futu data, local reports | Writes local reports | Local Materialization | Suggest only when fresh scan is explicitly desired | Inspect output path and run targeted scan tests if code changes | Local `om-agent`, human CLI |
| Cash headroom | `./om-agent run --tool query_cash_headroom` | `query_cash_headroom` | Runtime config, SQLite data config, OpenD/account context | Writes local reports | Local Materialization | Explain cash/collateral observations | Tool output plus report path | Local `om-agent`, human CLI |
| Portfolio context | `./om-agent run --tool get_portfolio_context` | `get_portfolio_context` | Runtime config, OpenD/account holdings | Writes local cache | Local Materialization | Suggest only when current portfolio context is needed | Tool output/cache metadata | Local `om-agent` |
| Close-advice refresh/build | `./om-agent run --tool prepare_close_advice_inputs`, `close_advice`, `get_close_advice` | `prepare_close_advice_inputs`, `close_advice`, `get_close_advice` | Runtime config, SQLite positions, OpenD required data | Writes local cache/reports | Local Materialization | Choose read-vs-refresh path; explain staleness | Tool output plus generated report inspection | Local `om-agent`, human CLI |
| Research evidence bundle | `./om research collect`, `./om-agent run --tool research` | `research` | Runtime artifacts, healthcheck snapshot when requested, candidate/ledger evidence | Default no write; can write local research reports | Research Side Lane | Suggest evidence collection, not production action | Bundle schema, warnings, handoff content | Local `om-agent` / `om`; not Agent Core |
| Symbol config preview | Assistant natural language or slash commands; local dry-run | `inbound.symbols`, `manage_symbols` dry-run/list | Runtime config / `config.yaml` authoring source | Preview/pending operation; local dry-run may write nothing | Preview Write | LLM may recognize narrow `symbol_edit`; deterministic reasoning owns preview | Assistant pending state, config validation after apply | Assistant preview; local `om-agent` dry-run |
| Manual trade preview | Assistant deterministic commands | `inbound.manual_trade` preview paths | User-provided trade text, ledger validation, config/account context | Pending operation/audit only | Preview Write | LLM must not execute; deterministic parser/commands only | Pending preview and validation warnings | Assistant deterministic command only |
| Model switch preview | Assistant deterministic command | `inbound.model` preview path | Assistant model profiles in config | Pending operation/audit only | Preview Write | None; LLM must not route model mutation | Pending preview; model check after confirm | Assistant deterministic command only |
| Upgrade preview | Assistant deterministic command | `inbound.upgrade` preview path | Release metadata and service/runtime context | Pending operation/audit only | Preview/Admin | None; LLM must not route upgrade | Pending preview; upgrade verify after confirm | Assistant deterministic command only |
| Local VERSION update | `./om-agent run --tool version_update` | `version_update` | `VERSION`, semver rules | Writes `VERSION` only when `apply=true` | Confirm Write / local repo write | Plan release metadata; not execute without request | `git diff`, release check | Local Codex/operator |
| Symbol config apply | Assistant confirm or `manage_symbols` non-dry-run | `inbound.symbols` confirm path, `manage_symbols` | Pending preview, config source | Writes `config.yaml` / runtime config where configured | Confirm Write | None at apply time | `config_validate`, `git diff`, pending cleared | Assistant confirm or local operator |
| Manual trade apply/cancel | Assistant confirm/cancel | `inbound.manual_trade` confirm/cancel paths | Pending preview, ledger validation | Writes trade/ledger state on confirm | Confirm Write | None at apply time | `option_positions_read action=inspect`, audit | Assistant confirm only |
| Model switch apply/cancel | Assistant confirm/cancel | `inbound.model` confirm/cancel paths | Pending preview, assistant config source | Writes assistant model config on confirm | Confirm Write | None at apply time | `./om assistant model current`, config build/check | Assistant confirm only |
| Upgrade apply/cancel | Assistant confirm/cancel or operator CLI | `inbound.upgrade`, `om update ...` | Pending preview, release artifacts, service profile | Updates local install/runtime service state where configured | Confirm Write / Admin | None at apply time | `om update verify`, service/runtime status | Assistant confirm or operator only |
| Test and release workflow | Codex/operator shell | `pytest`, `scripts/release_check.py`, git commands, GitHub release workflow | Current repo, tests, git status, release artifacts | Local repo/test artifacts; remote push/release when requested | Admin / operator-only | Plan exact gate sequence and summarize failures | Focused pytest, full pytest when release, `git diff --check`, release check | Codex/operator only |
| Live tick / notifications | Human CLI / production scheduler | `om run tick`, `om run tick-cron`, notification delivery adapters | Runtime config, market data, scheduler state | Writes run state/reports; may send notifications | Admin / Live Ops | Recommend preflight/read-only checks first | `runtime_status`, scheduler decisions, notification receipts | Operator only; never default Assistant |
| Service install/start/stop | Human CLI / system service manager | `om service ...`, systemd/launchd commands | Service profile, rendered units, runtime root | Writes service files or changes running services | Admin / Live Ops | Plan dry-run/preflight sequence | `service preflight`, `service status`, `runtime_status` | Operator only |

## Default Evidence Plans

| User intent | First evidence path | Escalate only when | Success standard |
|---|---|---|---|
| "Is OM healthy?" | `runtime_status` | Status is stale, missing, or contradictory | Current runtime summary has explicit ok/warn/error evidence |
| "Can this environment run?" | `healthcheck` | Dependency/config checks fail | Checks identify missing config/dependency without sending notifications |
| "Why did the run skip?" | `runtime_status` -> `scheduler_status` -> `runtime_logs` | Scheduler state or logs point to a deeper runtime failure | Skip reason is tied to scheduler/log artifact |
| "Why did symbol X not appear?" | `candidate_filter_explain` -> `candidate_rank_explain` | Trace is missing or stale | Accepted/rejected/not-observed result comes from trace/report evidence |
| "What positions/income do I have?" | `option_positions_read` or `monthly_income_report` | User asks for repair or apply | Rows come from local position/income sources with missing data called out |
| "Should I close this position?" | `close_advice_read` | User explicitly asks to refresh market data | Answer references existing close-advice rows or says no fresh row exists |
| "Preview a config/symbol/trade/model change" | Assistant preview command or local dry-run | User confirms exact pending operation | Preview includes diff/normalized fields and risk before apply |
| "Release / push / upgrade" | `git status`, `version_check`, tests/release check | User explicitly asks to commit, push, release, or apply upgrade | Release gate passes and final state is verified |
| "Analyze strategy quality" | Research side lane or existing replay artifacts | User approves offline analysis workflow | Recommendations cite replay/evidence and do not mutate production config |

## Current Implementation Notes

- `./om-agent spec` exposes more than Agent Core. It includes pure read,
  local materialization, local write, and the Research side lane.
- `research` remains available in `om-agent` for compatibility and evidence
  export, but it is not part of default Assistant core.
- Assistant has special inbound tools such as `inbound.symbols`,
  `inbound.pending`, and `inbound.model` that are not `om-agent` registry tools.
  Any future policy derivation must handle these separately.
- `PURE_READ_TOOLS` is currently a neutral allowlist in
  `src/application/tool_allowlist.py`. It is shared by Assistant policy code,
  but it duplicates facts that also exist in the `om-agent` registry.
- `src/application/assistant/agent_loop.py` has a narrower planner allowlist
  than the full pure-read manifest. That is intentional for remote LLM planning,
  but it should remain tested against the public capability catalog.
- `src/application/tool_execution.py` still has tool-name-specific write
  detection for `research`, `version_update`, and `manage_symbols`. A later
  refactor can move those write predicates into tool policy metadata.

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

- LLM-executable Assistant capabilities are read-only.
- LLM-recognizable but non-executable capabilities cannot apply writes.
- Confirm/cancel/apply intents are not LLM-recognizable.
- Pure-read Assistant tools either appear in `PURE_READ_TOOLS` or are explicitly
  documented special inbound read tools.
- Tools with side effects are not treated as Core Read even when their
  `read_only` field is true.
- Research is classified as a side lane, not default Agent Core.
