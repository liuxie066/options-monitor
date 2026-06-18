# OM Assistant Architecture

This is the current terminology and architecture authority for OM's assistant
surfaces. It exists to prevent three different dimensions from being collapsed
into one overloaded "agent" concept.

## Status

- Current architecture authority: this document.
- Current capability matrix: [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md).
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
| `AgentLoop` | Internal planner/evidence loop used by `./om assistant` when enabled | Not a public entry point |

Naming rules:

- Do not call `./om-agent` "OM Agent" in current docs. Prefer "Tool Gateway"
  or "Agent Tool Gateway".
- Use "Inbound Assistant" for the `./om assistant ...` message entrypoint.
- Use "Assistant Planner Loop" or `AgentLoop` for the internal bounded planning,
  evidence, coverage, and synthesis loop.
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

## Conversation Context Direction

The current conversation-context direction is to build a bounded planner-facing
projection of prior turns, then validate the planner's declared context use
before execution. This follows the useful Claude Code boundary: code owns
conversation state, projection, budget, and compaction-like boundaries, while
the model performs natural-language semantic continuity over the visible
conversation view.

OM adds deterministic validation because planner output can trigger financial
and runtime read tools. The context layer should therefore be:

```text
transcript
  -> ContextProjection
  -> Planner semantic judgement
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

`planner_context` is the legacy default during migration. `projection` runs the
deterministic projection fixtures without LLM calls. `validation` and
`scenarios` are separate harness lanes so future slices can add fixtures without
changing the projection evaluator or the legacy planner-context report.

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

- build a task contract,
- plan a small number of read-only tool calls,
- create exactly one approved preview operation when policy allows,
- collect evidence,
- verify coverage,
- perform bounded follow-up for recoverable gaps,
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
- better task contract and coverage verification,
- better evidence extraction,
- better answer quality,
- clearer preview/confirm receipts,
- more useful `assistant_trace`.

`./om-agent` remains a support surface. It can be improved when assistant work
needs better tool contracts, pure-read classification, output evidence, or
execution receipts, but it should stay a Tool Gateway rather than becoming the
project's autonomous Agent.

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
