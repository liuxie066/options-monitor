# OM Copilot v2 Design

## Purpose

OM Copilot is the general conversational Agent for options-monitor. It must
answer free-form operational and options-monitor questions with canonical data,
maintain useful multi-turn context, survive runtime failures, and hand requested
state changes to deterministic Control.

Monthly income, option-operation review, exposure analysis, candidate diagnosis,
and notification diagnosis are evaluation cases. None is a dedicated runtime
capability, router branch, Scene, or answer template.

## Runtime Shape

```text
Channel / CLI UI
-> Copilot Service
-> Copilot Host
-> generic Agent / Engine
-> canonical pure-read tools

Agent
-> request_control_preview
-> deterministic Control preview
-> explicit confirm / cancel
-> deterministic apply and readback
```

The stable layers are `UI -> Service -> Host -> Agent`. Contract preparation,
Scene preparation, memory compaction, event storage, and tool projection are
mechanisms inside those layers, not additional architecture layers.

`./om-agent` is a structured Tool Gateway for external agents. It is a UI entry,
not OM's autonomous Agent. Both `./om-agent` and Copilot derive tool schemas and
descriptions from `agent_tool_registry.py` and `agent_tools/`.

## Invariants

- There is one general Scene: `om_chat`.
- Service does not classify free text into OM business tasks.
- Service does not parse month, symbol, account, or intent from free text.
- Host owns execution governance; Agent owns generic model/tool iteration.
- Agent and Engine contain no OM task routing or strategy-specific branches.
- Copilot receives canonical pure-read tools only.
- The model may request a validated Control preview but cannot confirm, cancel,
  apply, or call a direct mutation tool.
- Explicit commands and pending-operation replies remain deterministic Control.
- There is no old planner, perception, reasoning, evidence, verifier, or answer
  renderer fallback for free-form chat.
- Missing data is explicit. A tool failure is not converted into an invented
  financial conclusion.
- Trace records execution facts and failures, never private chain-of-thought.

## UI Boundary

UI adapters own transport concerns:

- message extraction and channel identity;
- sender and conversation identity;
- configuration and model-profile selection;
- rendering the returned response;
- delivery receipts and channel-specific idempotency.

UI selects the default `om_chat` entry surface. It does not choose business
tools, task kinds, evidence plans, or prompt variants.

## Service Boundary

`src/application/copilot/service.py` is a thin contract-preparation service. It:

1. validates non-empty user text;
2. normalizes explicit UI scope only;
3. appends the current user message to supplied conversation context;
4. selects the default `om_chat` Scene;
5. emits a read-first execution contract.

Service must not import Host, Agent, Engine, tool implementations, or model
providers. It cannot decide which OM tool should answer a question.

## Host Boundary

Host responsibilities:

- validate the execution contract and Scene;
- project Scene-approved canonical tools;
- prepare prompt, context, structured memory, and current Control snapshot;
- own session and run lifecycle;
- enforce per-session exclusion and concurrency lanes;
- enforce timeout, turn, tool-call, retry, and context budgets;
- propagate cancellation;
- persist events, run state, metrics, and final result;
- recover interrupted pure-read runs;
- maintain the reply outbox;
- expose coarse progress events.

Host must not classify business intent, choose evidence recipes, interpret
financial data, or rewrite the model's answer into a second answer system.

## Scene And Prompt

The only Scene is declared in:

```text
src/application/copilot/om_chat.scene.json
```

It declares:

- static prompt fragments;
- canonical read toolsets;
- model/tool/context/time budgets;
- conversation limits.

Prompt fragments define general behavior only:

- use tools for current OM facts;
- distinguish facts, interpretation, recommendation, and missing data;
- preserve account, market, currency, period, and source distinctions;
- recover from actionable tool errors;
- provide conclusion-first synthesis instead of raw-row receipts;
- never claim an unexecuted mutation completed;
- request deterministic Control preview for supported state changes.

Question-specific prompts, tool lists, and renderers are prohibited.

## Agent And Engine

The Agent loop is:

```text
prepared messages + projected tools
-> model turn
-> zero or more native tool calls
-> Host-supplied tool execution
-> tool observations
-> next model turn
-> model final text or explicit terminal failure
```

The Engine supports:

- native model tool calls;
- bounded transient retries;
- duplicate-call protection;
- recoverable invalid-argument observations;
- continuation after provider length truncation;
- bounded context compaction;
- observation continuation for large results;
- final-answer reserve;
- cooperative cancellation;
- stable iteration IDs and context hashes;
- token usage and termination metrics.

There is no fixed collection fallback. If the model does not call a necessary
tool, that is an answer-quality failure to diagnose through trace and evaluation,
not a reason for Host to run a hidden business workflow.

## Tool Boundary

`src/application/copilot/tools.py` is a generic adapter. It:

- selects pure-read definitions from the canonical registry;
- exposes canonical descriptions and JSON schemas;
- merges safe defaults and explicit Scene scope;
- executes only Host-allowed pure-read tools;
- converts canonical results into flat Agent-friendly observations;
- provides compact previews and continuation metadata.

Tool descriptions, defaults, validation, error hints, and output contracts should
be fixed at the owning tool definition. Copilot must not maintain a second tool
catalog or question-specific evidence recipe.

## Deterministic Control

The model-visible `request_control_preview` surface is generated from the
deterministic Control capability catalog. A valid request creates a Control
preview and pending operation. It does not apply the operation.

```text
model preview request
-> schema and capability validation
-> deterministic preview
-> pending operation
-> explicit contextual confirmation or cancellation
-> deterministic apply
-> readback receipt
```

Current pending operations are injected into every channel turn from the
operation store. This snapshot is newer and more authoritative than conversation
history or compacted memory.

`取消分析` targets an active Copilot run. `取消执行` targets a pending Control
operation. These are distinct state machines.

## Conversation Memory

Host stores raw turns separately from structured memory.

```text
pinned_state:
  current_goal
  confirmed_scope
  user_constraints
  open_questions

episodes:
  confirmed_facts
  completed_actions
  tool_findings
  user_constraints
  open_questions
  next_step
```

Compaction:

- keeps recent raw turns;
- summarizes only older uncompacted turns;
- uses the configured model with tools disabled;
- rejects malformed summaries without deleting raw turns;
- uses optimistic compacted-turn checks to reject stale writes;
- never turns a preview or chat claim into authoritative execution state.

Memory is contextual and may be stale. Current financial and runtime questions
must still use canonical tools.

## Durable Runs, Resume, And Cancellation

Host persists:

- execution contract;
- session key;
- run state and events;
- cancellation request;
- resumed-from identity and attempt count;
- termination reason and aggregate metrics;
- final response.

Active states are `running`, `waiting_model`, and `waiting_tool`. Terminal states
include `answered`, `control_requested`, `failed`, `cancelled`, and `interrupted`.
Stale active runs are marked interrupted after process failure.

Resume rules:

- only failed or interrupted read-first contracts are eligible;
- attempts are bounded;
- resume creates a new run linked by `resumed_from`;
- only successful pure-read observations are recovered;
- identical recovered reads are not repeated;
- Control previews, confirmations, cancellations, applies, and writes are never
  replayed automatically.

Cancellation is checked before and after provider calls, during retry backoff,
and before and after tool execution. The current synchronous provider transport
cannot forcibly abort an already-blocked socket read; cancellation still prevents
the next model or tool step and is observed immediately after the call returns.

## Trace And Progress

Every model iteration records:

- `iteration_id`;
- sanitized context hash and size;
- force-finish state and tool count;
- finish reason and attempt count;
- input/output/total token usage where available;
- categorized provider failure;
- partial malformed tool-call arguments where available.

Run records aggregate model turns, tool calls, retries, token usage, status, and
termination reason. Trace payloads are sanitized execution facts, not reasoning.

Public progress is derived from stable events and exposes only labels such as:

- `正在分析`;
- `正在读取数据`;
- `正在继续分析`;
- `正在整理结论`;
- `等待确认`;
- `已取消`;
- `执行完成`.

## Reply Outbox

Channel replies use a SQLite outbox:

```text
pending -> delivering -> delivered
                    -> retryable_failed -> delivering
                    -> terminal_failed
```

`delivery_key` is unique. Enqueue is idempotent, successful delivery is recorded,
and retryable channel failures are retried by the channel worker after process or
transport recovery. Existing channel-level provider receipts remain an additional
idempotency layer.

## Concurrency

OM uses lightweight Host leases rather than a general multi-tenant governor:

```text
chat_read: 2
memory_compact: 1
control: independent
```

The same conversation permits one active Agent run. Expired leases are removed
so process failure cannot permanently block a session. Control remains outside
the read-analysis lane and must not wait behind a long analysis.

`heavy_analysis` is not introduced until measured production contention proves a
separate lane is necessary; adding it now would require business classification
that the Service is explicitly forbidden to perform.

## Failure Behavior

| Failure | Required result |
|---|---|
| Empty question | `needs_clarification` before Host |
| Model not configured | `not_ready`, no tool call |
| Contract or Scene invalid | explicit failure |
| Tool arguments invalid | recoverable observation with repair hint |
| Tool unavailable or data missing | explicit gap preserved |
| Repeated identical call | duplicate call rejected |
| Provider timeout/error | categorized event and bounded failure |
| Run budget exhausted | bounded final answer or explicit failure |
| Cancellation | partial events preserved; run `cancelled` |
| Process failure | stale active run becomes `interrupted` |
| Concurrent same-session run | second run `not_ready` |
| Channel delivery failure | outbox `retryable_failed` and later retry |

There is no fallback to old Assistant planning or unevidenced generic chat.

## Evaluation

Deterministic CI uses fixture observations and explicit model turns. Real-model
acceptance is executed by the trusted production environment with actual
read-only OM data.

The fixed set covers:

- income and attribution follow-up;
- exposure concentration;
- option-operation review;
- account-scope follow-up;
- candidate diagnosis;
- close-advice notification diagnosis;
- missing-data honesty;
- write safety;
- conclusion follow-up.

Each case captures all events, run identity, elapsed time, termination reason,
failure owner, selected tools, actual provider/model/runtime version, tool-call
and continuation metrics, evidence-health checks, final answer, and six
human-review dimensions:

- intent fulfillment;
- factual accuracy;
- scope and currency;
- missing-data honesty;
- actionability;
- conversation continuity.

No benchmark may become runtime routing, a dedicated Scene, or an answer template.

Production evaluation must receive the runtime root explicitly instead of
depending on a shell-specific inherited environment:

```bash
python3 scripts/copilot_p1_eval.py \
  --assistant-config /var/lib/options-monitor/resolved/config.assistant.json \
  --config-key us \
  --runtime-root /var/lib/options-monitor \
  --output /tmp/om-copilot-p1.json
```

The output contract is `om.copilot.p1_eval.v3`. Structural, evidence, and
human answer-quality results are separate gates. Human review applies to the
exact saved report without rerunning the model:

```bash
python3 scripts/copilot_p1_eval.py \
  --review-report /tmp/om-copilot-p1.json \
  --review-input /tmp/om-copilot-p1-review.json \
  --output /tmp/om-copilot-p1-reviewed.json
```

The review input must contain every report case and all six 0..2 dimensions;
a reviewed case passes at 10/12 or higher. The report records the model actually
configured at runtime and must not assume a provider.

## Delivery Phases

| Phase | Deliverable | Exit gate |
|---|---|---|
| P0 | Stable rebuild baseline | Focused tests, guards, dependency graph, and diff checks pass. |
| P1 | Production answer-quality baseline | The configured production model produces sanitized v3 traces and human scores. |
| P2 | Structured memory | Follow-up scope survives compaction; stale writes and malformed summaries are safe. |
| P3 | Durable run control | Interrupted reads resume safely and cancellation stops further work. |
| P4 | Trace/model protocol | Iteration identity, usage, termination, and failure categories are persisted. |
| P5 | Progress/outbox | Coarse progress is pollable and final replies are idempotent and retryable. |
| P6 | Lightweight concurrency | Session and lane leases enforce limits and recover after expiry. |
| P7 | Tool remediation | Only production-trace-proven canonical tool gaps are changed. |
| P8 | Prompt remediation | Only failures with correct model-visible data justify prompt changes. |
| P9 | Cleanup/docs | One free-form path, one Scene, one registry, one Control owner remain. |
| P10 | Release/acceptance | Full checks and production behavioral acceptance pass. |

P1 is the gate for P7 and P8. Engineering work on generic Host reliability may
continue while production evaluation is scheduled, but tool- and prompt-specific
changes require captured evidence.

## Completion Criteria

The rebuild is complete only when:

- one general Scene exists and Service remains business-neutral;
- Host owns governance and Agent owns generic model/tool iteration;
- Copilot exposes canonical pure-read tools plus validated Control preview only;
- free-form chat has no old planner/evidence/verifier/renderer fallback;
- structured memory, durable runs, resume, cancellation, trace, progress,
  outbox, and concurrency leases have regression coverage;
- explicit operations use one deterministic audited Control contract;
- deterministic Copilot, Control, channel, config, and architecture tests pass;
- production real-model questions produce useful, factual conclusions;
- channel follow-ups preserve scope and current Control context;
- reply failure is retryable and idempotent;
- no free-form request can directly mutate OM state;
- docs and public commands describe the implementation that actually runs.
