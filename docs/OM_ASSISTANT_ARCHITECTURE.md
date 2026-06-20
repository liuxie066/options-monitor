# OM Assistant Architecture

This is the current terminology and architecture authority for OM's assistant
surfaces. It exists to prevent three different dimensions from being collapsed
into one overloaded "agent" concept.

## Status

- Current architecture authority: this document.
- Current capability matrix: [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
- Current tool-calling system design:
  [OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md](OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md).
- Current tool-loop completion plan:
  [OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md](OM_ASSISTANT_TOOL_LOOP_COMPLETION_PLAN.md).
- Local tool invocation contract: [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md).
- Remote/message safety contract: [INBOUND_CONTROL.md](INBOUND_CONTROL.md).
- Current conversation-context design:
  [OM_ASSISTANT_CONVERSATION_CONTEXT_DESIGN.md](OM_ASSISTANT_CONVERSATION_CONTEXT_DESIGN.md).
- Current conversation-context implementation plan:
  [OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md](OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md).

Historical design notes such as `OM_AGENT_COMPLETION_DESIGN.md`,
`OM_AGENT_INTELLIGENCE_UPGRADE_PLAN.md`,
`AGENT_RELIABILITY_P0_P2_DESIGN.md`, and
`SQLITE_TOOL_OS_EXPANSION_DESIGN.md` are not current architecture authorities.
They may contain useful rationale, but current source, tests, manifest output,
and this document win when terminology conflicts.

## Terminology

| Name | What it is | What it is not |
|---|---|---|
| `./om` | Human/operator CLI for product workflows | Not a remote message agent |
| `./om-agent` | Tool Gateway CLI for structured JSON tool calls | Not OM's autonomous/project Agent |
| `./om assistant` | Inbound Assistant CLI namespace for local or remote messages | Not the full `./om-agent` manifest |
| `AgentLoop` | Internal planner/event/evidence loop used by `./om assistant` when enabled | Not a public entry point |

Naming rules:

- Do not call `./om-agent` "OM Agent" in current docs. Prefer "Tool Gateway"
  or "Agent Tool Gateway".
- Use "Inbound Assistant" for the `./om assistant ...` message entrypoint.
- Use "Assistant Planner Loop", "Assistant Event Loop", or `AgentLoop` for the
  internal bounded planning, tool-event, evidence, coverage, and synthesis loop.
- When discussing future intelligence work, say it optimizes `./om assistant`
  capabilities, with `./om-agent` and tool metadata as support surfaces.

## Architecture Dimensions

The main confusion risk is treating entrypoints, shared tooling, and internal
assistant orchestration as one hierarchy. They are separate dimensions.

```text
Entry surfaces
  ./om            human/operator CLI
  ./om-agent      Tool Gateway CLI for structured JSON tool calls
  ./om assistant  Inbound Assistant message CLI

Shared tool substrate
  src/application/agent_tools/*
  src/application/agent_tool_registry.py
  src/application/tool_execution.py
  src/application/agent_tools/permissions.py
  output_contract / evidence_contract metadata

Assistant internals
  runtime / router / perception / reasoning / action
  AgentLoop
  evidence / coverage / synthesis / answer verification
  operation_lifecycle / audit / session trace
```

The actual call relationships are:

```text
./om-agent
  -> tool_execution
  -> agent_tool_registry
  -> tool handler

./om assistant
  -> assistant runtime/router
  -> perception/reasoning/action
  -> optional AgentLoop
  -> tool_execution
  -> agent_tool_registry
  -> tool handler
```

`AgentLoop` therefore sits inside the `./om assistant` path. It is not a peer
of `./om-agent` or `./om assistant`.

## Current Assistant Intelligence Loop

The current assistant intelligence upgrade connects trace, capability
selection, evidence collection, progress, and clarification into one audited
assistant loop:

```text
message
  -> perception/router
  -> AgentLoop plan
  -> tool_execution
  -> evidence_bundle
  -> coverage/progress
  -> follow-up or clarification
  -> final response
  -> assistant_trace
```

This loop is owned by `./om assistant`. `assistant_trace` is read through the
local `./om-agent` Tool Gateway as a diagnostic tool, but that does not make
`./om-agent` the assistant or a planner.

Current session snapshots expose these derived fields:

- `capability_selection`: selected, required, satisfied, and rejected
  capabilities/tools derived from the bounded plan and tool transcript.
- `progress`: task state, coverage status, next action, blocker list, pending
  operation ids, and tool/step counts derived from execution and coverage.
- `answer.clarification_request`: structured clarification status and
  questions derived from the assistant response when scope or evidence is not
  safe to infer.

These fields are trace/session state. They are not a new task database, not a
second pending-operation store, not another tool registry, and not a separate
project Agent. The durable store remains the inbound audit SQLite
`agent_sessions` trace table, and the authority path remains
`./om assistant -> AgentLoop -> tool_execution -> agent_tool_registry`.

Implementation note: the current default provider path already uses structured
tool/function calls instead of parsing ordinary assistant `output_text` JSON.
It also routes provider tool-call events through an event-native planning result
into `assistant.tool_loop` instead of converting them back into `PlannerPlan`.
The old `PlannerPlan -> execute_tool_plan(plan_payload)` bridge has been
removed from the assistant package. Current assistant code should treat
`PlannerPlan` as historical terminology only; it must not be reintroduced as
the provider structured runtime contract.

## Tool Calling Event Model Direction

The next tool-calling design should borrow Claude Code's loop shape, not its
full product permission model. The useful shape is: the model emits tool calls,
observes results, and stops when it can answer. OM adds financial-operation
guardrails around that loop.

The current target system design is
[OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md](OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md).
That document supersedes the older text-JSON planner direction: the model
should express tool intent through provider structured tool calls mapped to
internal `ModelEvent` records, not by writing a full `TaskContract` and
`ToolPlan` as ordinary assistant text.

The current implementation boundary is stricter than "provider tool calling":
structured tool calls are the runtime execution contract directly, not an input
that is converted back into `PlannerPlan`. New intelligence work should add
bounded event-loop observations for recoverable schema, scope, safety, and
read-tool errors rather than adding repair branches around historical plan
objects.

Target flow:

```text
message
  -> request/context projection
  -> model emits a structured tool-call event
  -> loop guard checks schema, scope, risk, budget, and duplicates
  -> tool_execution runs the deterministic tool
  -> tool result is added to the transcript/evidence bundle
  -> model decides: another read tool event, clarification, preview, or answer
  -> final answer verification and assistant_trace
```

The intelligence boundary is:

- the model owns intent understanding, capability selection, result
  interpretation, deciding whether another read tool is useful, and composing
  the user-facing answer;
- code owns event protocol mapping, schema validation, scope injection, risk
  class, loop budget, duplicate-call prevention, evidence extraction,
  missing-data declarations, answer guardrails, and trace/audit.

Loop continuation is allowed only when all of these are true:

- the model explicitly requests another tool call;
- the tool is classified as automatic read capability for Inbound Assistant;
- the call remains inside the normalized request scope;
- the call is under tool-count, turn, context, and time budgets;
- the call is not a duplicate of an already observed request/result;
- the call does not cross config, notification, ledger/trade, broker-facing,
  release, service, or admin write boundaries.

Preview/write routes exit the read loop and go through existing deterministic
preview/confirm lifecycle. The model may request a preview event, but the
deterministic operation handler owns pending-operation creation. The model must
never confirm, cancel, apply, send, or mutate state by continuing the tool loop.

`CoverageVerifier` and `AnswerVerifier` are guardrails, not the primary
intelligence engine. If the model stops but verification detects one obvious,
recoverable evidence gap, the assistant may issue at most one bounded repair
prompt/tool follow-up. Otherwise it should answer with explicit missing data or
ask a clarification only for high-risk or unsafe scope gaps. The design goal is:
broader automatic reads, tighter writes, a stronger model-driven observe loop,
and no new tool registry or public agent surface.

## Conversation Context State

The current conversation-context path uses a bounded model-facing projection
of prior turns, then validates the model's declared or derived context use
before execution. This follows the useful Claude Code boundary: code owns
conversation state, projection, budget, and compaction-like boundaries, while
the model performs natural-language semantic continuity over the visible
conversation view.

OM adds deterministic validation because model-selected tool paths can trigger
financial and runtime read tools. The context layer should therefore be:

```text
transcript
  -> ContextProjection
  -> Model semantic judgement
  -> ContextValidator
  -> policy/tool execution
```

It should not grow as a collection of business-specific follow-up branches.
Detailed contracts and rollout plan live in:

- [OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md](OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md)
- [OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md](OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md)
- [OM_ASSISTANT_CONTEXT_EVAL_PLAN.md](OM_ASSISTANT_CONTEXT_EVAL_PLAN.md)
- [OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md](OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md)

The context eval harness is layered by explicit mode:

```bash
./om assistant eval-context --mode planner_context
./om assistant eval-context --mode projection
./om assistant eval-context --mode validation
./om assistant eval-context --mode scenarios
```

`planner_context` is a historical eval lane, not an alternate contract.
Current context work should use `projection`, `validation`, and `scenarios` as
the authoritative regression lanes. New fixtures should move to those modes
instead of preserving old planner payload shape as a parallel contract.

## Claude Code Reference Boundary

The useful Claude Code reference is its module separation, not the exact
implementation or product scope. In the local source, Claude Code is split
roughly this way:

| Claude Code area | Local source examples | OM analogue | Borrowing decision |
|---|---|---|---|
| Entrypoints and UI | `main.tsx`, `cli/`, `commands/`, `components/`, `screens/` | `./om`, `./om assistant`, CLI adapters | Keep entrypoints thin; do not place planning or tool policy in CLI parsing. |
| Model loop and context | `query.ts`, `QueryEngine.ts`, `services/api/`, `services/compact/`, `services/contextCollapse/` | `AgentLoop`, `ContextProjection`, `ContextValidator` | Keep code responsible for projection, budget, and context boundaries; do not copy full compaction machinery unless context pressure proves it is needed. |
| Tool protocol | `Tool.ts` | `agent_tools` metadata, output/evidence contracts, tool registry metadata | Strengthen deterministic metadata and result contracts instead of letting model prose define tool semantics. |
| Tool pool and visibility | `tools.ts`, `ToolSearchTool` | `capability_catalog`, model-visible manifest, `AgentLoop` tool selection | Make the model-visible capability view narrow, auditable, and explainable. Do not expose the full Tool Gateway manifest to Inbound Assistant. |
| Execution orchestration | `services/tools/toolExecution.ts`, `toolOrchestration.ts`, `StreamingToolExecutor.ts` | `tool_execution`, tool-loop guard, evidence bundle, answer verification | Preserve a validate -> policy -> execute -> observe pipeline where the model decides whether to continue and code enforces scope, risk, budget, and duplicate guards. |
| Permissions and hooks | `utils/permissions/`, permission hooks | `agent_tools/permissions.py`, `action_safety`, `operation_lifecycle` | Keep write authority centralized and reason-coded. A model preview request may enter operation lifecycle, but must not apply writes. |
| Tool implementations | `tools/*` | `src/application/agent_tools/*`, domain services | Keep tools deterministic, scoped, and evidence-oriented. Business rules remain in owning domain/application modules. |
| Extension surfaces | `services/mcp/`, `services/plugins/`, `skills/` | Local Tool Gateway integration only, not Inbound core | Do not add remote extension/plugin authority to OM Assistant without a separate safety design. |

This mapping reinforces the existing OM boundary:

```text
./om assistant
  -> AgentLoop / policy / trace
  -> model-visible capability view
  -> tool_execution
  -> agent_tool_registry
  -> deterministic tool
```

The next Claude Code-inspired improvement should therefore be a model-driven
read-tool loop with capability selection explainability and model-visible manifest
hygiene. It should not add a new public agent surface, second tool registry,
global conversation state machine, or broader write/admin execution authority.

## `./om-agent` Boundary

`./om-agent` is a stable local machine interface for external agents, scripts,
OpenClaw, Codex, or operators that need structured OM tool calls.

It owns:

- `spec`: current tool manifest.
- `run`: execute one named tool with JSON input.
- JSON response envelope.
- write-tool env and confirmation gate.

It should not own:

- multi-step autonomous planning,
- message conversation context,
- LLM routing,
- preview/confirm operation lifecycle,
- arbitrary shell bridges,
- broker/service/notification authority beyond the called tool's explicit
  contract.

Changes to `./om-agent` should usually support one of these needs:

- expose or correct tool metadata,
- improve output/evidence contracts,
- add a deterministic tool handler,
- normalize execution receipts,
- keep write gates strict.

## `./om assistant` Boundary

`./om assistant` is the current Inbound Assistant surface. It accepts a user
message, normalizes it, applies policy, and returns an audited response.

It owns:

- slash command and deterministic message handling,
- natural-language capability recognition,
- capability catalog filtering,
- optional bounded AgentLoop planning,
- sender allowlist and idempotency,
- pending preview creation,
- confirm/cancel routing for existing pending operations,
- audit and durable assistant trace.

It must not expose the full `./om-agent` manifest to remote messages. The
assistant capability catalog is intentionally narrower than the local Tool
Gateway manifest.

## AgentLoop Boundary

`AgentLoop` is an internal bounded planning loop. It can help `./om assistant`
answer questions that require choosing evidence paths instead of matching one
slash command.

It may:

- derive and use a host-owned task contract,
- run a bounded sequence of automatic read tool calls selected by the model,
- request a preview event when policy allows, leaving pending-operation creation
  to deterministic handlers,
- collect evidence,
- use coverage and answer verification as post-check guardrails,
- perform at most one bounded repair/follow-up for an obvious recoverable gap,
- synthesize an answer from deterministic evidence,
- record trace/session data.

It must not:

- confirm, cancel, or apply writes,
- run arbitrary shell or Python,
- mutate config/ledger/broker-facing state directly,
- send notifications,
- bypass `tool_execution`, tool policy, operation lifecycle, or audit.

## Optimization Target

The current optimization target is `./om assistant` capability quality:

- better intent recognition,
- better capability selection,
- better host-derived task contract and coverage verification,
- better model-driven read tool iteration,
- better evidence extraction,
- better answer quality,
- clearer preview/confirm receipts,
- more useful `assistant_trace`.

`./om-agent` remains a support surface. It can be improved when assistant work
needs better tool contracts, pure-read classification, output evidence, or
execution receipts, but it should stay a Tool Gateway rather than becoming the
project's autonomous Agent.

The next assistant optimization should focus on capability selection
explainability, not another context state machine. In practice that means:

- keeping model-visible capability manifests narrow and auditable,
- deriving selection reasons from message text, `ContextProjection`,
  open evidence gaps, and `task_contract.required_evidence`,
- adding regression fixtures for selected tools/views and selection sources,
- keeping execution authority inside the existing
  `AgentLoop -> tool_execution -> agent_tool_registry` path.

## Verification Sources

When docs disagree, verify current behavior from:

```bash
./om-agent spec
./om assistant capabilities --format json
```

Then check the source owners:

- `src/interfaces/agent/cli.py`
- `src/application/tool_execution.py`
- `src/application/agent_tool_registry.py`
- `src/application/assistant/capability_catalog.py`
- `src/application/assistant/agent_loop.py`
- `src/application/assistant/operation_lifecycle.py`
