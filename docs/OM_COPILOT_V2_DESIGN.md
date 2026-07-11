# OM Copilot v2 Design

## Purpose

OM Copilot is the read-first conversational interface for operating and
analyzing Options Monitor. Its job is to understand a free-form question,
inspect the available OM data through tools, return a useful conclusion, and
translate explicit state-change requests into deterministic Control previews.

The architecture follows the working boundary used by Dayu:

```text
UI -> Service -> Host -> Agent
```

This is a runtime ownership model, not four layers that every request must
reimplement. OM keeps each boundary small and removes concepts that do not own
real behavior.

The design optimizes for:

- high-quality free-form answers;
- one generic model/tool loop;
- deterministic read-tool enforcement and preview-only write handoff;
- shared tool authority with `./om-agent`;
- explicit failure and missing-data reporting;
- deterministic confirmation, apply, and administration ownership in Control.

## Final Runtime Shape

```text
CLI or channel UI
  -> Copilot Service
  -> Copilot Host
  -> generic Agent/Engine loop
  -> canonical pure-read OM tools
  -> optional structured Control preview request
  -> model final text
  -> Host admission and event persistence
  -> UI rendering
```

Explicit commands and Copilot preview requests enter one deterministic path:

```text
CLI or channel UI
  -> explicit command / permission parser / validated Copilot preview request
  -> deterministic Control executor
  -> read tool, preview, confirm, cancel, or apply owner
  -> audited receipt
```

The paths meet at a single structured preview handoff owned by the channel
router. The Copilot model can request only catalog capabilities classified as
`preview_write` or `preview_admin`; it cannot confirm, cancel, apply, or call a
write owner directly.

## Dayu Alignment

OM copies these Dayu decisions:

1. Chat enters an explicit/default Scene instead of being classified into
   business task types.
2. Service prepares an execution contract but does not run the model or choose
   tools per question.
3. Host owns session lifecycle, scene preparation, budgets, cancellation,
   tool projection, event persistence, and final admission.
4. Agent is a generic native model/tool loop.
5. Domain operations remain explicit Services or deterministic owners.

OM intentionally does not copy Dayu's full Host implementation. OM needs one
local process, one general chat Scene, a durable session store, bounded runs,
and a pure-read tool sandbox. More infrastructure is added only when an observed
runtime requirement demands it.

## Architectural Invariants

The following rules are mandatory:

- Copilot has one general Scene: `om_chat`.
- Service does not classify free text into OM business tasks.
- Service does not parse month, symbol, account, or intent from free text.
- Explicit UI scope may be passed through as trusted context.
- Host projects the toolset from the Scene; requests cannot supply tool names.
- Agent uses native model tool calls and model final text.
- Agent and Engine contain no OM task routing or strategy-specific branches.
- Copilot can call only ordinary tools classified as pure read by the canonical
  registry. Channel Host may additionally expose the generic
  `request_control_preview` meta-tool.
- Copilot cannot directly send notifications, mutate config, write positions or
  trades, operate services, upgrade deployments, or call broker-facing write
  APIs.
- Tool metadata is owned by `agent_tool_registry.py` and `agent_tools/`.
- `./om-agent` remains the external structured Tool Gateway. Copilot does not
  shell out to it.
- Preview creation, pending-operation state, confirmation, cancellation, apply,
  and readback remain owned by the deterministic Control path.
- Current pending operations are injected from the operation store on every
  channel turn; Control receipts are recorded in conversation history but are
  never treated as executable state.
- Missing, stale, partial, and failed observations must remain visible to the
  model and to the user.
- There is no ordinary chat fallback when the model or OM evidence is
  unavailable.

## UI Boundary

UI surfaces are:

| Surface | Role |
|---|---|
| `./om copilot run` | Local free-form Copilot question |
| `./om copilot eval` | Deterministic fixture or explicit model-turn evaluation |
| WeChat / Feishu adapters | Channel transport and reply delivery |
| `./om assistant handle` | Compatibility entry for explicit commands and channel messages |
| `./om-agent` | External structured Tool Gateway |

UI owns:

- protocol adaptation;
- request identifiers;
- explicit scope supplied by the operator;
- model configuration path supplied by the local/operator surface;
- channel delivery;
- human-review presentation.

UI does not own:

- natural-language task classification;
- business tool selection;
- model/tool iteration;
- tool execution policy;
- answer synthesis.

The request contract is intentionally small:

```text
CopilotRequest
- request_id
- source_entry
- user_message
- explicit_scope
    - config_key
    - symbol
    - month
- channel_context
- execution_environment: local | eval | channel
- context_messages
- debug_overrides.fixture_id   # eval only
```

Debug data cannot select a production Scene or broaden the tool sandbox.

## Service Boundary

`src/application/copilot/service.py` is a thin chat Service.

It performs only:

1. reject an empty question with a clarification result;
2. append the current user message to supplied conversation messages;
3. normalize explicit UI scope without deriving scope from free text;
4. choose the entry surface's default Scene, always `om_chat`;
5. create a read-only `ExecutionContract`.

Service must not:

- import Host, Agent, Engine, tools, or model providers;
- inspect keywords or regular expressions to select business behavior;
- choose toolsets based on the question;
- create answer templates;
- decide whether evidence is sufficient;
- execute a model or a tool;
- accept write policy overrides.

The contract is:

```text
ExecutionContract
- contract_id
- request_id
- scene_name: om_chat
- execution_environment
- input
    - user_message
    - messages
    - explicit scope values
    - reference_year
    - fixture_id for eval
- policy
    - read_only: true
- decision_trace
```

`decision_trace` records contract preparation facts. It is not a planner and is
not model-visible business reasoning.

## Host Boundary

`src/application/copilot/host.py` owns execution governance.

Host responsibilities:

- validate the contract and reject policy overrides;
- load the single Scene definition;
- compose system prompt, runtime context, and conversation messages;
- project canonical pure-read tools;
- attach Agent-facing tool descriptions;
- create the run and event log;
- enforce one active run per session;
- load and persist session messages;
- enforce timeout, model-turn, tool-call, and context budgets;
- propagate cancellation;
- execute the generic Engine with Host-supplied callbacks;
- construct and structurally admit the final `AppResult`;
- persist final status and events.

Host must not:

- branch on income, positions, diagnostics, symbols, or option strategies;
- infer business intent;
- force a fixed sequence of tools;
- synthesize a financial conclusion in deterministic code;
- scan final prose for task-specific keywords;
- allow caller-supplied tools, prompts, or runtime limits.

Host-internal helpers such as event storage, durable session storage, and scene
preparation are mechanisms, not additional product layers.

## Scene

The only Scene is declared in:

```text
src/application/copilot/om_chat.scene.json
```

It owns:

- the generic OM analyst system prompt;
- declared pure-read toolsets;
- model/tool iteration budgets;
- final-answer time reserve;
- context size;
- conversation retention.

It does not contain:

- business question activation terms;
- account or symbol preferences;
- strategy thresholds;
- monthly review templates;
- fixed SQL;
- expected tool sequences;
- answer text for individual questions.

The prompt instructs the model to:

- understand the user's task itself;
- inspect the tool catalog when data availability is unclear;
- call relevant tools and recover from usable errors;
- avoid duplicate calls with identical arguments;
- continue until evidence is sufficient or a real gap is established;
- distinguish current, historical, stale, and partial data;
- lead with a conclusion;
- explain the basis for judgement in natural language;
- state missing data and uncertainty explicitly;
- stay read-only and refuse to claim external mutations.

Prompt instructions guide behavior. They do not create an alternate planner,
critic, verifier, or business router.

## Agent And Engine

`agent.py` contains generic runtime data and the model-runner protocol.

`engine.py` owns the bounded native model/tool loop:

```text
messages + tool schemas
-> model turn
-> zero or more tool calls
-> Host-supplied tool execution
-> compact observations
-> next model turn
-> model final text
```

Engine responsibilities:

- call the configured model;
- validate native tool-call shape;
- execute only Host-projected tools;
- append tool observations to the conversation;
- prevent identical repeated calls;
- expose continuation for bounded large observations;
- stop on cancellation or budget exhaustion;
- reserve time for one final tool-disabled answer turn;
- return final text and status.

Engine must not know:

- OM business task names;
- monthly or strategy-specific workflows;
- which business tool should be called next;
- what conclusion is financially correct;
- channel configuration;
- write operations.

There is no fixed collection fallback. If the model does not call tools, Host
does not silently execute a canned list. The resulting answer or failure is
evaluated directly.

## Model Layer

`model_client.py` adapts the shared configured provider to the Engine protocol.

Required behavior:

- native function/tool calling;
- non-streaming bounded turns for the current implementation;
- per-turn timeout supplied by Engine;
- maximum output-token configuration;
- provider error normalization;
- no business routing or tool planning in provider adapters;
- tool-disabled final synthesis when `force_finish=true`.

Model credentials and profiles remain outside `CopilotRequest`. Local and
channel composition resolve an explicit assistant configuration and build the
model runner before entering Host.

Real-model evaluation can expose private OM financial and runtime evidence to
the configured provider. Such runs require informed operator approval.

## Tool Boundary

Copilot reuses canonical tool definitions:

```text
src/application/agent_tool_registry.py
src/application/agent_tools/
```

The registry owns:

- tool name;
- description;
- input schema;
- risk level;
- side effects;
- confirmation requirement;
- output contract;
- toolset membership.

Copilot's `tools.py` is an adapter only. It may:

- select declared pure-read toolsets;
- convert canonical metadata to model tool descriptions;
- merge Scene-approved static payload fields;
- execute through the canonical tool executor;
- normalize recoverable errors;
- compact large observations;
- provide continuation tokens for omitted content.

It must not:

- define a second tool registry;
- add business facts not present in tool output;
- hide missing or stale-data warnings;
- render the final answer;
- encode question-specific evidence recipes.

Agent-friendly quality should be fixed at the owning tool definition whenever
possible. Internal Copilot and external `./om-agent` must not receive divergent
semantics for the same tool.

## Direct Tool And Control Safety

Safety is enforced structurally:

1. Service emits `policy.read_only=true` only.
2. Host rejects any other policy field or non-read-only contract.
3. Scene declares only read-only toolsets.
4. Host projects only registry tools classified as pure read.
5. Tool execution rechecks the run allowlist.
6. Write-capable tools, confirmation intents, cancellation intents, and apply
   handlers never appear in model tool schemas.
7. Channel Host may expose one generic `request_control_preview` schema whose
   capability enum is projected from the deterministic capability catalog.
8. Router revalidates every returned preview request before invoking Control.

This avoids relying on a natural-language write-intent classifier for safety. A
user can ask about a hypothetical trade without causing a mutation. For an
explicit supported state change, the model may request a preview, after which
the existing pending-operation and confirmation flow is authoritative.

## Explicit Control

Channel and CLI commands use:

```text
command_parser.py or permission_response.py
-> inbound_control.execute_explicit_control()
-> canonical read tool or operation owner
-> renderer
-> audit store
```

The Control executor owns one consolidated result:

```text
ControlExecution
- status
- intent_name
- safety_class
- action_kind
- reason
- tool_name
- payload
- result or error
- response_text
- executed
- ok
- requires_confirmation
```

There are no separate reasoning, action, or observation runtime stages.
Preview/apply semantics remain in the existing operation owners and operation
store. Audit writes `control_json`; old nullable columns are read only for
historical database compatibility.

## Result And Admission

The model's final text is the user answer. Host wraps it in:

```text
AppResult
- status
- user_response
- error
- request_id
- contract_id
- run_id
- events
- decision_trace
- ok
```

Admission is intentionally narrow:

- status must be recognized;
- non-failed results must contain non-empty text.

Admission does not attempt semantic fact checking, business keyword scanning,
or answer rewriting. Financial correctness is improved through better tools,
prompting, model behavior, and answer-quality evaluation rather than a second
deterministic answer system.

## Events, Sessions, And Cancellation

Host records append-only run events for:

- run start and finish;
- model turns;
- tool calls and observations;
- tool failures;
- budget exhaustion;
- cancellation;
- result rejection.

Events are execution trace, not an evidence-planning API.

`CopilotHostStore` persists:

- session messages;
- active-run leases;
- run status;
- sanitized event payloads;
- cancellation requests.

One conversation may have only one active Copilot run. A stale lease expires so
process failure does not permanently block the conversation.

## Failure Behavior

| Failure | Required result |
|---|---|
| Empty question | `needs_clarification` before Host |
| Model not configured | `not_ready`, no tool call |
| Contract or Scene invalid | `failed`, explicit reason |
| Tool arguments invalid | Recoverable observation; model may retry |
| Tool unavailable or data missing | Observation preserves the gap |
| Repeated identical call | Tool call rejected as duplicate |
| Model timeout or provider error | Event recorded; final turn attempted when possible |
| Run timeout or budget exhausted | Bounded final answer or explicit failure |
| Cancellation | Partial events preserved; result `cancelled` |
| Concurrent same-session run | `not_ready`, second run not started |

There is no fallback to old Assistant planning or generic unevidenced chat.

## Evaluation

Evaluation measures answer quality, not just tool-call success.

Required question families:

- monthly income and attribution;
- current option exposure and concentration;
- recent option-operation review;
- candidate-filter diagnosis;
- notification/close-advice diagnosis;
- missing and stale data;
- follow-up questions such as `结论呢`;
- explicit write requests that must remain non-mutating in Copilot.

Each evaluation captures:

- final answer;
- model turns;
- selected tools and arguments;
- duplicate or failed calls;
- missing-data observations;
- elapsed time and budget termination;
- session context behavior.

Deterministic tests use fixture observations or explicit model-turn JSON.
Real-model acceptance uses actual read-only OM evidence and must be reviewed for:

- correct tool choice;
- recovery from tool errors;
- conclusion-first synthesis;
- no raw-row receipt as the final answer;
- explicit uncertainty;
- no unsupported mutation claims;
- useful follow-up continuity.

No individual benchmark becomes a runtime capability or Scene.

## Implementation Phases

The phases are a gated vertical-slice plan, not six independent refactoring
projects. Dayu's code has a dependency direction from accepted Scene and
execution contract, through canonical tools and the generic Agent, to Host and
Service delivery. OM already has that skeleton. The remaining priority is to
prove the whole path with a real model and real read-only OM data, then repair
the owning boundary exposed by each failed run.

P2, P3, and P4 are therefore failure-remediation queues behind P1. They may be
implemented far enough to make P1 runnable, but none is considered complete in
isolation. Every change in those phases must be justified by a captured P1
failure and followed by a rerun of the fixed question set.

| Priority | Gate | Why it comes here |
|---|---|---|
| P0 green baseline | Focused deterministic tests pass | A dirty rebuild cannot produce trustworthy traces while imports, contracts, or tests are broken. |
| P1 real vertical slice | Real model and real read-only evidence produce reviewable traces | This is the first proof that the architecture improves answers rather than only changing names and layers. |
| P2 tool remediation | A P1 failure is attributable to tool schema, arguments, result shape, or missing canonical read capability | Dayu treats tool result and argument validation as Engine contracts; OM changes them only where a real run proves they block reasoning. |
| P3 loop remediation | A P1 failure is attributable to timeout, truncation, retries, duplication, context pressure, or termination | Runner resilience is tuned from observed failures, not from speculative budgets. |
| P4 Scene and prompt remediation | Correct evidence reached the model but selection, synthesis, uncertainty, or follow-up quality failed | Prompt changes must not conceal broken tools or loop behavior. |
| P5 cutover and cleanup | The complete P1 set passes locally | Only then remove remaining compatibility code, enable channels, release, and upgrade. |

### P0: Stabilize The Rebuild Baseline

- stop further structural deletion and renaming while the baseline is red;
- repair imports, contracts, tests, and documentation already affected by the
  rebuild;
- prove that the current Service -> Host -> Agent path and deterministic
  Control path both execute under focused tests;
- classify the dirty diff and preserve unrelated user work;
- freeze new abstractions, DTOs, stores, and compatibility helpers.

Exit evidence:

- the current Service -> Host -> Agent path passes focused tests;
- deleted answer-pipeline modules have no runtime callers;
- the remaining dirty diff is classified as intended rebuild work or unrelated
  user work;
- there is no known test or import failure that would contaminate a P1 trace.

### P1: Prove The Real Vertical Slice

- use one fixed question set covering income, exposure, candidate diagnosis,
  operation review, missing data, and follow-up continuity;
- capture model turns, tool calls, arguments, errors, termination reason,
  elapsed time, and final answer for every run;
- run deterministic fixtures in normal CI;
- after explicit operator approval, run the same questions with the configured
  real model and actual read-only OM data;
- record the first failing trace before changing a tool, budget, loop, or
  prompt;
- assign every failure to exactly one primary owner: canonical tool, Agent
  loop, Scene/prompt, unavailable source data, provider, or channel/session.

P1 is a hard quality gate, not a runtime capability. If real-model execution is
blocked by configuration or approval, work may continue only on deterministic
failures required to make P1 runnable. Do not continue broad cleanup, channel
cutover, release work, or speculative P2-P4 expansion while this gate is
blocked.

Exit evidence:

- every question has a reproducible baseline artifact;
- failures can be attributed to tool contract, loop behavior, prompt behavior,
  unavailable data, provider behavior, or channel/session behavior;
- no benchmark-specific branch, Scene, or answer template is added.

### P2: Repair Canonical Tool Contracts From P1 Failures

This follows Dayu's `tool_result` and argument-validation boundary before
prompt tuning.

- keep one internal success/error result contract shared by the registry,
  executor, Host, and trace;
- project successful tool results to the model as useful flat data plus optional
  `truncation`, without making the model unpack transport envelopes;
- project failures as flat `error`, `message`, and actionable `hint` fields;
- validate tool arguments against the canonical JSON schema and return repair
  hints for missing, unsupported, invalid, or out-of-range fields;
- improve descriptions, defaults, enums, examples, and output semantics at the
  canonical tool owner, not in a second Copilot registry;
- implement one generic continuation contract for large results;
- add a missing read capability only when the source data exists and the gap is
  general-purpose, not because one benchmark expects a specific answer;
- change only tools exercised by a captured P1 failure before considering broad
  metadata cleanup.

Exit evidence:

- model-visible success results do not require nested `ok/value/data` decoding;
- invalid calls produce a repairable next action;
- large results can be continued without raw internal trace access;
- `./om-agent` and Copilot derive schemas and descriptions from the same tool
  definitions;
- the triggering P1 case and the full fixed set are rerun after each change.

### P3: Repair The Generic Agent Loop From P1 Failures

This follows Dayu's runner behavior without copying its full infrastructure.

- preserve provider `finish_reason` and continue a truncated model answer when
  `finish_reason=length` within the remaining budget;
- retry only transient provider failures with bounded backoff;
- track context pressure in model tokens where provider usage is available,
  using a conservative estimate otherwise;
- compact conversation and observations without dropping account, currency,
  period, source, errors, or unresolved questions;
- distinguish an identical no-information call from a legitimate retry or
  polling call;
- stop after bounded consecutive failed tool batches;
- reserve enough time for a final model turn and force a conclusion from the
  accumulated observations when normal iteration cannot continue;
- persist termination reason, continuation count, compaction count, and retry
  count in Host events;
- change budgets only when the trace shows that useful progress was stopped by
  a budget, rather than using larger limits to hide repeated or unproductive
  calls.

Exit evidence:

- timeout, truncation, provider error, duplicate call, and context-pressure
  fixtures end with either a useful bounded answer or an explicit failure;
- increasing `max_iterations` is not required to hide a loop defect;
- Agent and Engine contain no OM business routing;
- the triggering P1 case and the full fixed set are rerun after each change.

### P4: Repair Scene And Prompt Behavior From P1 Failures

Prompt work comes after P2 and P3 so it does not compensate for broken tool or
runner contracts.

- keep one `om_chat` Scene;
- compose a small set of static prompt fragments for base behavior, financial
  fact rules, tool-use rules, and OM read-only behavior;
- distinguish facts, calculations, interpretation, recommendation, and missing
  evidence;
- tell the model how to recover from flat tool errors and use continuation;
- require conclusion-first synthesis instead of row dumps;
- preserve account, market, currency, period, and source distinctions;
- use conversation history for follow-ups such as `结论呢` without adding a
  follow-up router;
- tune only when the trace proves that the model received usable evidence and
  the remaining failure is tool selection, synthesis, uncertainty handling, or
  conversation continuity;
- rerun the full fixed set after each prompt change.

Exit evidence:

- the real-model question set passes the documented answer-quality review;
- missing-data answers remain useful and honest;
- no question-specific prompt, tool list, or renderer exists.

### P5: Collapse Control, Cut Over, And Release

- enter P5 only after the complete local P1 acceptance set passes;
- finish removing planner/LLM-routing metadata from deterministic Control;
- keep explicit commands plus Copilot-requested previews, confirmations,
  cancellations, and writes in one audited deterministic Control path;
- delete obsolete Assistant DTOs, stage traces, compatibility helpers, and
  modules only after caller/import audit proves they are unused;
- rewrite architecture guards around the final invariants;
- align public docs and CLI examples with one Scene and one Control path;
- run focused and broad regression suites;
- verify local real-model behavior before enabling channel traffic;
- verify channel session continuation, cancellation, timeout, and user-facing
  answers under human review before normal delivery;
- publish and upgrade only after implementation and acceptance evidence are
  complete.

Exit evidence:

- all free-form channel text reaches Copilot; the model can request a preview
  but cannot directly mutate OM state;
- explicit operations remain deterministic and audited;
- local and channel answers pass the fixed real-model acceptance set;
- release, remote upgrade, and post-upgrade verification are complete.

## Completion Criteria

The rebuild is complete only when all of the following are true:

- one general Scene exists and no business activation router remains;
- Service is thin and business-neutral;
- Host owns governance and Agent owns generic model/tool iteration;
- Copilot exposes only canonical pure-read tools;
- old Assistant answer and explicit-command stage modules are deleted;
- explicit operations use one deterministic Control contract;
- current audit writes no old stage payloads;
- deterministic Copilot, Assistant, tool-contract, config, and architecture
  tests pass;
- real-model acceptance questions produce useful conclusions from real evidence;
- channel follow-ups use persisted conversation context;
- no free-form request can mutate OM state;
- docs and public commands describe the implementation that actually runs.
