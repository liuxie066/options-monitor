# OM Copilot v2 Architecture / Scene v5 Contract

The product architecture remains v2. The general `om_chat` runtime Scene and
prompt contract are versioned independently and are currently `v5`.

The generic Agent runtime has been replaced by Pi Agent Core. This document
continues to own the Copilot product boundary and Scene v5 contract; the
current model/tool loop, session, admission and rollback implementation is
specified in [PI_AGENT_CORE_INTEGRATION.md](PI_AGENT_CORE_INTEGRATION.md).

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
   -> OM local read models
   -> portfolio_query / portfolio_pnl_bridge / portfolio_cash_bridge
      -> portfolio-management loopback HTTP API

Agent
-> request_control_preview
-> deterministic Control preview
-> explicit confirm / cancel
-> deterministic apply and readback
```

The stable layers are `UI -> Service -> Host -> Agent`. Contract preparation,
Scene preparation, structured-memory injection, event storage, and tool projection are
mechanisms inside those layers, not additional architecture layers.

`./om-agent` is a structured Tool Gateway for external agents. It is a UI entry,
not OM's autonomous Agent. Both `./om-agent` and Copilot derive tool schemas and
descriptions from `agent_tool_registry.py` and `agent_tools/`.

## Invariants

- There is one general Scene: `om_chat`.
- The `om_chat` Scene is `v5` and compiles one ordered five-fragment prompt
  pack. Repository operator instructions are not runtime prompt input.
- Service does not classify free text into OM business tasks.
- Service does not parse month, symbol, account, or intent from free text.
- Host owns execution governance; Agent owns generic model/tool iteration.
- After the Agent selects an option-performance read tool, Host may bind only
  the unique current-message period attestation defined below. This is an
  input-authority fence, not Service routing or financial interpretation.
- Agent and Engine contain no OM task routing or strategy-specific branches.
- Copilot receives canonical pure-read tools only. The `portfolio` toolset is an
  optional read boundary, disabled by default and projected only when
  `assistant.enabled`, `assistant.copilot.enabled`, and
  `assistant.copilot.toolsets.portfolio` are all true. It is not a second
  Copilot, Scene, router, or Agent runtime.
- The model may request a validated Control preview but cannot confirm, cancel,
  apply, or call a direct mutation tool.
- Explicit commands and pending-operation replies remain deterministic Control.
- There is no old planner, perception, reasoning, evidence, verifier, or answer
  renderer fallback for free-form chat.
- Missing data is explicit. A tool failure is not converted into an invented
  financial conclusion.
- Trace records execution facts and failures, never private chain-of-thought.
## Typed-Tool-Only Product Boundary

OM Copilot exposes canonical typed read tools rather than an embedded SQL/BI
workspace. Pi Agent Core remains the only model/tool loop; Host enforces scope,
allowlists, budgets, cancellation, audit, and answer admission. Business
calculation stays with each canonical tool owner.

The retired `analysis_catalog` and `analysis_query` names are absent from both
Copilot and `./om-agent`. There is no compatibility alias, replacement DSL,
generic aggregation tool, session adapter, or second Scene. The current
`om_chat` identifier remains `v5`.

### Exit Inventory And Result

The breaking exit was accepted by product/operations owner `liuxie`. The
inventory covered source, tests, scripts, living docs, configs, service
templates, and repository-root entry points at
`origin/main@68b370f6` on 2026-09-02T20:00:18+08:00. No service or runtime-config
caller was found. Repository-independent Tool Gateway telemetry was not
available, so absence of an unknown external caller was never assumed.

| Eager Scene business projection | Business tool count | Serialized business schema | Business catalog hash |
|---|---:|---:|---|
| Before removal | 20 | 24,869 UTF-8 bytes | `sha256:6927c37523411c6104c0ae910078546c77a4f190e1327a2a4a4fabcf57a12d46` |
| Current | 18 | 21,256 UTF-8 bytes | `sha256:be4977e39e2173d8b25a95677f878ad7113d4a2e7dc8ca347f29956d17094b62` |

These measurements cover the business tools selected by the Scene before Host
adds protocol tools. The current local eager provider projection is 19 tools
and 22,626 UTF-8 bytes after adding `submit_answer`; channel runs may also add
Control preview. Runtime rollout verification uses
`scene_prepared.tool_count` and `scene_prepared.tool_schema_sha256`, not the
business catalog hash in this table.

The business schema payload is 3,613 bytes smaller. This is a payload
measurement, not a claim about wall-clock latency. The retired database was in
memory, so the change does not reduce persistent storage.

Against the pinned base, production source and scripts are 4,131 lines smaller
net; tests are 1,409 lines smaller net, for a combined 5,540-line reduction.

### Supported And Removed Capabilities

| Need | Current owner and boundary |
|---|---|
| Period option income and cash components | `option_performance_report`; aggregate evidence only for Copilot. |
| Position facts | `option_positions_read action=list`; only declared bounded rows. |
| Option event history | `option_positions_read action=events`; canonical pagination applies. |
| Symbol configuration | `symbol_config_read`; one symbol per read. |
| Close advice | `close_advice_read`; strict owner. |
| Runtime and delivery diagnosis | Runtime and notification-perception tools; declared facts only. |
| Operation evidence | `operation_timeline`; raw operation facts rather than a derived upgrade summary. |

In the Copilot model projection, symbol performance attribution, bulk
configuration comparison, expiration buckets, grouped lifecycles, replay
views, derived upgrade summaries, arbitrary SQL, cross-view joins, and exact
aggregates over incomplete position coverage are intentionally unavailable. A
typed tool is not expanded merely to preserve an old view. A recurring missing
need requires a separate requirement at the canonical business owner.

### Failure And Compatibility Behavior

- Calling a retired name returns the existing unknown-tool or allowlist failure,
  with no implicit substitute and no side effect.
- Partial, stale, missing, paginated, or insufficiently projected evidence must
  produce a narrowed or incomplete answer, never an inferred exact aggregate.
- Persisted conversation history is preserved but unsupported as current
  evidence. Old observations cannot satisfy current-request admission; if a
  provider rejects old tool history, the operator starts a new conversation.
- A rollout must not mix workers with different catalogs. Deployment and
  per-instance hash verification remain separately authorized operations.

Pi Agent Core, deterministic Control, account/config isolation, write authority,
persistent stores, Tick, ledger, and canonical calculation contracts are
unchanged.

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
- record the exact prompt and provider-visible tool projection fingerprints
  before Engine execution without persisting prompt text;
- recover interrupted pure-read runs;
- maintain the reply outbox;
- expose coarse progress events.

Host must not classify business intent, choose evidence recipes, interpret
financial data, or rewrite the model's answer into a second answer system.

## Deterministic Option-Performance Input Binding

### Goal, Non-Goals, And Success Signals

The goal is to prevent a correct natural-period request from failing because
the model chose the right read tool but supplied a conflicting period payload.
For example, `期权8月收益` must execute as the attested natural month rather
than fail because the model proposed `mtd`.

Success requires all of the following:

- an unambiguous current-message month or year request reaches
  `option_performance_report` with the matching `period` plus `month` or `year`;
- a case-insensitive bare `MTD` or `YTD` token in an option-performance phrase
  remains valid when it directly touches CJK text or punctuation, while an
  ASCII identifier that merely contains `mtd` or `ytd` remains invalid;
- the sanitized model-proposed input and the effective input are both
  auditable, including when preparation rejects the call before execution;
- ambiguous, malformed, or future selectors still fail before any business
  read, while unrelated text retains generic MTD/YTD behavior;
- successful observations returned to the model expose the effective bound
  period scope rather than the conflicting proposal;
- direct Tool Gateway behavior and canonical performance calculations remain
  unchanged;
- model-visible tool descriptions state the valid parameter combinations for
  the performance read and answer submission.

This work does not add a general intent router, a second answer renderer, a new
tool or event store, or a provider-specific strict-tool runtime. Host does not
calculate income, translate a natural month into MTD, or choose a performance
tool for the model.

### Current Facts And Constraints

- The current-message fence parses closed MTD/YTD cutoffs and natural month/year
  selectors for `option_performance_report`.
- The generic MTD/YTD token guard is lexical, not a calendar-period owner. It
  currently uses Python's Unicode word boundary, which incorrectly rejects a
  bare token adjacent to CJK text; this slice changes only that boundary.
- It currently compares the model proposal with that attestation and rejects a
  mismatch, although the attested values are already known deterministically.
- A pre-execution rejection records the failure observation but not the model's
  sanitized business-tool arguments, so the exact bad proposal cannot later be
  reconstructed from the run.
- The canonical period owner accepts `mtd`, `ytd`, `month`, and `year`; it owns
  calendar-window semantics and remains the final validator.
- Current contracts freeze `operating_date` from one `report_now_ms` in
  Asia/Shanghai; Host must not substitute an ambient machine-local date when
  that authority is missing or malformed.
- Result admission requires an empty claim list in `conceptual` mode and at
  least one evidence claim in `evidence` mode. The validator enforces this, but
  the projected `submit_answer` description must make the coupling explicit.

### Chosen Design And Data Flow

The existing closed selector parser remains the sole attestation source. Once
the Agent calls `option_performance_report`, Host performs:

```text
model-selected read tool + model arguments
-> bounded model-input audit projection
-> current-message option-period attestation
-> reconcile with trusted fixed scope
-> bind attested period fields into a copy of model arguments
-> ordinary payload preparation, normalization, and fixed-scope merge
-> canonical tool validation and execution
-> evidence admission
```

Binding is limited to `period`, `as_of_date`, `month`, and `year`. Host replaces
those fields with the attested values and removes incompatible sibling selector
fields. Account, broker, config, and every other model or fixed-scope field are
preserved.

Authority precedence is:

```text
trusted fixed scope > current-message attestation > model proposal/default
```

For this option-performance tool, a trusted fixed `month` denotes
the complete scope `period=month, month=<fixed>`. If the current message has no
selector, that fixed scope is bound. If the message attests the same month,
execution continues; a different month, a year, or an MTD/YTD cutoff conflicts
and returns `INPUT_ERROR` before the business read. Invalid, ambiguous, or
future current-message selectors also reject instead of falling back to the
fixed month. Binding never overwrites trusted fixed scope.

After the authoritative scope is selected, Host copies the model arguments,
replaces the four period fields, removes incompatible siblings, and only then
runs ordinary tool preparation. A conflicting or empty model-proposed period
sibling therefore cannot reject a selector that Host already knows exactly.

Attestation has four closed states:

- `none`: no option-performance selector or selector-like text is present;
- `unique_valid`: exactly one supported selector is present and may be bound;
- `invalid_or_ambiguous`: selector-like option-performance text is malformed,
  incomplete, conflicting, or contains multiple natural selectors;
- `future`: the selector resolves after the frozen Asia/Shanghai
  `operating_date`.

`invalid_or_ambiguous` and `future` reject before the business read. The
malformed boundary is exact: it must match one of the two existing
option-performance phrase orderings (`期权 <selector> 收益` or
`<selector> 期权收益`) while the isolated selector token fails the closed valid
selector grammar. Text that does not match either performance phrase ordering
is `none`; Host does not add a general Chinese date or intent parser. Explicit
MTD/YTD cutoffs must not be after `operating_date`.

Period attestation requires a valid contract `operating_date`, or a valid
frozen `report_now_ms` from which Host derives the same Asia/Shanghai date. If
neither is available, Host returns `INPUT_ERROR` before binding or reading; it
never falls back to the ambient process date. This intentionally makes a legacy
or damaged persisted run non-resumable for this read instead of changing its
calendar scope.

The accepted selector states are:

| Current-message selector | Effective performance input |
|---|---|
| explicit MTD cutoff | `period=mtd`, matching `as_of_date` |
| explicit YTD cutoff | `period=ytd`, matching `as_of_date` |
| one natural month | `period=month`, canonical `month=YYYY-MM` |
| one natural year | `period=year`, canonical integer `year` |
| bare `MTD` or `YTD` in a performance phrase | existing generic MTD/YTD behavior; surrounding whitespace is optional |
| no attested natural selector | existing generic MTD/YTD behavior; unauthorized `as_of_date` is removed |

Bare MTD/YTD detection is case-insensitive and treats only ASCII letters,
digits, and underscore as identifier continuations. `期权ytd收益` and
`mtd期权收益` are therefore valid, while `期权mytd收益` and `期权ytdx收益`
remain invalid. Host does not rewrite the current message or add a second
natural-language parser.

Multiple natural selectors, invalid dates, future periods, and selector-like
text outside the closed grammar remain rejected before execution. The parser is
not broadened to infer arbitrary phrasing. Direct `./om-agent` calls remain
governed by the tool schema and canonical period owner without this
conversation-only binding.

For business read calls, trace extends the existing audit sanitizer with one
bounded input projection used identically for `model_input` and `tool_input`:

- recognized schema fields other than free-form query text retain their values
  after the existing secret/path redaction and cursor hashing;
- `sql` and `query` retain only `{type, length, sha256}`;
- unsupported field names remain visible, while their values retain only type,
  length where applicable, and SHA-256;
- the complete projection uses the existing 4,000-token observation ceiling;
  an oversized field or projection collapses to the same metadata shape.

The event records:

- `model_input`: the bounded projection of arguments as the model proposed them;
- `model_input_hash`: a stable hash of the complete model proposal;
- `tool_input`: the effective bound payload only after ordinary tool preparation
  succeeds, through the same bounded projection;
- the existing error code and message when preparation or attestation rejects.

A rejected call keeps these diagnostics in its existing `tool_result` event;
no synthetic successful `tool_call` is created. Secrets and configured paths
remain redacted, cursor-like values remain hashed, input projection depth and
size remain bounded, and raw SQL/free text, answer text, prompts, messages, and
private reasoning remain absent. `submit_answer` continues to log only mode,
status, references, admission outcome, and approved-answer hash.

An unexpected read-tool implementation exception also keeps the model,
ordinary reply, and public progress surfaces on generic `TOOL_EXCEPTION` text.
Only the failed `tool_result` audit copy adds `failure_stage=tool_execution` and
bounded, single-line, redacted `exception_type` / `exception_reason` fields.
Operators can inspect them explicitly through local `--include-events` output or
`./om copilot events`; failed events remain excluded from resume evidence.

The two business-read audit states are:

1. rejected before execution, including attestation, fixed-scope reconciliation,
   or ordinary preparation: sanitized `model_input` and `model_input_hash`, with
   no `tool_input`;
2. execution attempted: sanitized `model_input`, `model_input_hash`, and the
   effective bound `tool_input` sent to the canonical tool, whether the returned
   observation succeeds or fails.

The projected direct-report schema states that `month` is valid only with
`period=month`, `year` only with `period=year`, and `as_of_date` only with an
explicit MTD/YTD cutoff. The projected `submit_answer` description
states that `conceptual` requires `claims=[]`, while `evidence` requires at
least one claim referencing successful current-request observations. Existing
prompt rules remain the general behavioral owner; no question-specific prompt
is added.

### Failure Behavior And State Transitions

Binding does not introduce a new run state. A call remains either rejected
before execution, executed with a failed observation, or executed with a
successful evidence observation. Only the last state can support factual
claims.

If a natural-period payload lacks a matching attestation, or attestation is
invalid, ambiguous, future, or conflicts with trusted scope, Host returns the
existing recoverable `INPUT_ERROR` observation with the sanitized model input
attached to the trace. `none` still permits generic MTD/YTD without a cutoff and
removes an unauthorized model-proposed `as_of_date`. The model may repair the
call within existing budgets. If no admissible evidence exists, the model may
submit a conceptual missing-evidence explanation with empty claims; Host does
not manufacture an answer on its behalf.

### Implementation Slices

1. Replace comparison-only option-period fencing with narrow pre-normalization
   binding at the existing Host ownership point, enforce trusted-scope
   precedence, and preserve fail-closed cases.
2. Extend the existing audit sanitizer with the bounded input projection and add
   `model_input` alongside effective `tool_input` in existing business read
   events, including pre-execution rejection, without persisting SQL or unknown
   field values.
3. Align the projected performance and `submit_answer` descriptions with their
   validators, and align the linked Pi integration owner with this binding
   contract.
4. Add focused Host, trace-redaction, schema-description, and result-admission
   regressions; then run the existing Copilot and Agent contract checks.
5. Change the existing Host `_OPTION_PERFORMANCE_PERIOD_TOKEN` guard to use
   `re.ASCII | re.IGNORECASE`, and extend the existing public `run_contract`
   parameter tables rather than adding a normalizer, parser, or test harness.

### Validation Plan

- Prove `期权8月收益` executes with the canonical natural-month input even when
  the model proposes MTD, and prove the same behavior for natural year and
  explicit MTD/YTD cutoffs.
- Cover `option_performance_report`.
- Prove the returned observation exposes the bound period scope, followed by a
  source-declared evidence claim that `submit_answer` accepts and a terminal
  `answered` result without `ANSWER_ADMISSION_FAILED`.
- Prove ambiguous, future, and malformed selectors, including `期权13月收益`,
  perform no business read; unrelated text preserves generic MTD/YTD behavior.
- Through the public `run_contract` path, prove `期权yTd收益` and `MtD期权收益`
  work without spaces, while ASCII-prefix, suffix, digit, and underscore
  continuations reject with no business read.
- Prove fixed month scope alone binds `period=month`, equal message attestation
  succeeds, and a different month/year or MTD/YTD message scope rejects before
  the read.
- Prove bare-month resolution across January and the Asia/Shanghai day boundary,
  and prove future MTD/YTD cutoffs reject before the read.
- Prove missing or malformed `operating_date` falls back only to a valid frozen
  `report_now_ms`; when both are unusable, the call rejects without a read.
- Prove successful traces contain sanitized model/effective inputs and rejected
  traces retain sanitized model input without creating a successful tool call.
- Prove every pre-execution rejection has no `tool_input`, while successful and
  failed execution attempts record the effective bound input.
- Prove configured paths remain redacted, cursors remain hashed, and rejected
  answer bodies, SQL, unknown free text, and PII-like marker strings are not
  persisted verbatim.
- Prove exact safe period fields remain visible, SQL/query and unsupported values
  use the declared metadata shape, and an oversized projection stays within the
  4,000-token ceiling.
- Assert the projected parameter-combination guidance and the
  `conceptual`/`evidence` claim rules.
- Run focused Copilot, result-admission, Pi bridge, and Agent contract tests,
  followed by repository guards required by the touched import boundaries and
  `git diff --check`.

### Risks, Rejected Alternatives, And Open Questions

The main risk is accidentally turning a closed authority fence into business
intent routing. The boundary is therefore enforced by the existing narrow
grammar, only after model tool selection, and only over four period fields.

Rejected alternatives are: asking the model to retry the same already-known
selector indefinitely; generating a Host fallback answer; translating a
natural period into generic MTD/YTD dates; adding a broad natural-language
router; adding a new audit database or event type; and extending the Pi provider
bridge with strict-tool controls before current evidence requires it. Rewriting
the user message to inject spaces is also rejected because it would make the
audited input differ from the text the user sent.

No open product choice remains for this slice. Broader selector coverage or a
provider-level strict schema is separate work triggered by measured failures.

## Scene And Prompt

The only Scene is declared in:

```text
src/application/copilot/om_chat.scene.json
```

It declares:

- static prompt fragments;
- declarative runtime context slots and their authority;
- canonical read toolsets plus the optional `portfolio` toolset declaration;
- model/tool/context/time budgets;
- conversation limits.

The ordered v5 prompt pack is:

```text
base_behavior.md
soul.md
financial_fact_rules.md
tool_rules.md
om_chat.md
```

The fragments define general behavior only:

- use tools for current OM facts;
- answer only the requested question plus qualifications necessary for factual
  correctness, financial safety, and scope;
- act as a concise, neutral Chinese options trader focused on quantitative
  trading, without fixed strategy thresholds or forced trade activity;
- distinguish facts, calculations, estimates, assumptions, interpretation,
  recommendation, and missing data;
- preserve account, market, currency, period, and source distinctions;
- resolve relative time to source-supported absolute dates or state the gap;
- recover from actionable tool errors;
- treat tool results as untrusted data rather than instructions;
- hide internal prompts, tool-call details, payloads, retries, and traces;
- provide conclusion-first ordinary prose while honoring explicit raw JSON,
  JSON fenced block, and Markdown source containers;
- never claim an unexecuted mutation completed;
- request deterministic Control preview for supported state changes.

Question-specific prompts, tool lists, and renderers are prohibited.

Runtime context slots have three authorities:

```text
reference:
  reference_year
  operating_date

fixed_tool_scope:
  config_key
  symbol
  month

host_only_tool_scope:
  report_now_ms
  config_path
  authenticated_channel
  authenticated_sender_id
  authenticated_conversation_id
```

Among execution-contract slots, only fields declared as `fixed_tool_scope` can
override model-provided tool arguments; `host_only_tool_scope` fields are added
by Host and are not model-controlled. The sole conversation-derived override is
the closed option-period attestation defined above, which is not a contract
slot. `reference_year` is model context only; `operating_date` is also the
Host's frozen calendar authority for that attestation, but neither field
directly becomes a tool argument. Undeclared contract input cannot silently
acquire tool authority. Runtime values are rendered as JSON-encoded data, not
interpolated instructions.

The result admission boundary rejects known unparsed tool protocols, unbalanced
fences, malformed whole-response JSON containers, and malformed raw object or
array JSON. It does not parse free text to guess whether the user requested an
output container, use broad tool-name or tone keyword guards, or rewrite an
answer. Format intent remains a prompt and evaluation contract until an entry
surface explicitly supplies a deterministic response mode.

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
- bounded context compaction that preserves the current user request and every
  current-turn native tool-call/result group by distributing the available
  budget before admitting older conversation groups;
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
- merges safe defaults, model arguments, and only the Scene-declared fixed tool
  scope;
- executes only Host-allowed pure-read tools;
- converts canonical results into flat Agent-friendly observations;
- exposes `portfolio_query`, `portfolio_pnl_bridge`, and
  `portfolio_cash_bridge` through the `portfolio` toolset using GET-only stdlib
  HTTP against `PORTFOLIO_SERVICE_URL` (default
  `http://127.0.0.1:8765`); the two bridges keep total-asset PnL and cash
  movement separate, use PM's actual period-end facts, and return structured
  steps plus Markdown fallback text without image rendering;
- provides compact previews and continuation metadata.

Tool descriptions, defaults, validation, error hints, and output contracts should
be fixed at the owning tool definition. Copilot must not maintain a second tool
catalog or question-specific evidence recipe. `portfolio_query` accepts only
view and query scope; it rejects model-provided endpoints and non-loopback service
URLs, exposes no portfolio write endpoint, and preserves source/scope/freshness.
Disabling the optional toolset removes both its model-visible description and
its Host allowlist entry before Agent execution. Engine allowlist enforcement
still rejects a model-emitted call that was not projected. Resume rebuilds the
Scene from current assistant config, so a later disable revokes resumed access.

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
history or structured memory.

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

Online request preparation:

- reads already persisted structured memory;
- injects pinned state and recent episodes before the current user message;
- never calls a model, acquires a memory lease, or writes session memory;
- leaves raw turns and malformed or missing stored memory unchanged.

Model-driven conversation-memory compaction is disabled on the online request
path. Reintroducing automatic compaction requires a separate design that proves
foreground latency isolation and replaces the sliding-array count cursor with a
stable turn identity before any memory write is enabled.

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

Before the first model iteration, `scene_prepared` records:

- Scene name and version;
- ordered fragment paths, lengths, and SHA-256 hashes;
- compiled prompt SHA-256;
- selected toolsets;
- provider-visible tool count and schema SHA-256.

The tool fingerprint covers exactly `name`, `description`, and `input_schema`,
including the projected Control preview tool. It changes when optional toolsets
change. Prompt text, user messages, tool results, and secrets are never included.
The static Scene fingerprint is separate from the per-turn dynamic
`context_hash`. A resumed run rebuilds the current Scene and records its own
fingerprint.

Run records aggregate model turns, tool calls, retries, token usage, status, and
termination reason. Business read events retain sanitized model-proposed input
and effective input when available, including the proposal attached to a
pre-execution rejection. Trace payloads are sanitized execution facts, not
reasoning.

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
control: independent
```

The same conversation permits one active Agent run. Expired leases are removed
so process failure cannot permanently block a session. Control remains outside
the read lane and must not wait behind a long model run.

`heavy_analysis` is not introduced until measured production contention proves a
separate lane is necessary; adding it now would require business classification
that the Service is explicitly forbidden to perform.

## Failure Behavior

| Failure | Required result |
|---|---|
| Empty question | `needs_clarification` before Host |
| Model not configured | `not_ready`, no tool call |
| Contract or Scene invalid | explicit failure |
| Tool arguments invalid | bind a unique attested option-period scope; otherwise return a recoverable observation with repair hint |
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
- no unsolicited expansion;
- evidence-based challenge to a high-yield/add-position premise;
- no-trade and wait conclusions;
- raw JSON, one JSON fenced block, and one Markdown source block;
- conclusion follow-up.

Each case captures all events, run identity, elapsed time, termination reason,
failure owner, selected tools, actual provider/model/runtime version, tool-call
and continuation metrics, output contract checks, Scene/tool fingerprints,
evidence-health checks, final answer, and six human-review dimensions:

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

The output contract is `om.copilot.p1_eval.v4`. Structural and evidence checks
are mandatory CLI exit gates. Human answer-quality is also mandatory after
review, while an otherwise valid unreviewed report remains available for
offline scoring. Human review applies to the exact saved report without
rerunning the model:

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
| P1 | Production answer-quality baseline | The configured production model produces sanitized eval-v4 traces and human scores. |
| P2 | Structured memory | Existing pinned state and episodes remain injectable without request-path model calls or memory writes. |
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
- three independent real-model acceptance runs use the expected stable Scene v5
  prompt/tool fingerprints and pass every format and safety hard gate;
- quantitative persona cases use relevant supported evidence, avoid false
  precision and emotional language, and permit wait/no-trade conclusions;
- channel follow-ups preserve scope and current Control context;
- reply failure is retryable and idempotent;
- no free-form request can directly mutate OM state;
- docs and public commands describe the implementation that actually runs.
