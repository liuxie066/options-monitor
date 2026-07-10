# OM Copilot v2 Design

This document defines the target architecture for rebuilding OM free-form
question answering after the assistant reset. It also records the executable
local/eval boundary and the Phase 2 multi-scene answer-quality milestone so
implementation can be checked against the target architecture instead of
drifting back into the old assistant pipeline.

Current runtime authority remains [ARCHITECTURE.md](ARCHITECTURE.md) and
[INBOUND_CONTROL.md](INBOUND_CONTROL.md): production/channel free-form
natural-language execution is disabled by default and returns
`NATURAL_LANGUAGE_REBUILDING`. The local `./om copilot ...` surface is the
local/eval entry. A disabled-by-default channel gate exists, but no real
business scene is `channel_ready` yet. Channel execution will require
`assistant.copilot.channel_scenes` to explicitly allowlist a channel-ready scene
plus explicit usable assistant model configuration: config file present, model
profile present, and the referenced API-key environment variable configured.
Without those gates the facade returns `not_ready` before calling tools.
`assistant.copilot.human_review=true` can hold Host-backed channel answers for
manual review: Copilot still records the sanitized audit/event summary, but the
channel reply does not expose the model answer until an operator reviews it
outside the free-form path.

## Design Decision

OM Copilot v2 follows the Dayu-style runtime shape:

```text
UI -> Service -> Host -> Agent
```

This replaces the earlier static pipeline:

```text
Task Frame -> Evidence Plan -> Evidence Ledger -> Answer Verification
```

The important Dayu lesson is not the four names. The important lesson is the
runtime contract:

- Service owns domain task preparation and produces an execution contract.
- Host owns execution governance and scene preparation.
- Agent is a generic bounded message/tool loop, not an OM business module.
- Deterministic tools and service contracts make precise actions reliable.

OM must therefore avoid copying old assistant concepts under new names. The
target is a real agent loop inside a controlled read-only OM environment, with
permissions, budgets, lifecycle, traceability, and write safety enforced by
deterministic host/service code.

## Dayu Alignment Corrections

This version corrects the main drift from the previous draft:

| Previous drift | Corrected direction |
|---|---|
| Service acted like a classifier and safety gate. | Service prepares a domain `ExecutionContract`. Classification is only one input. |
| UI request details leaked into Service and Host decisions. | UI passes a small `CopilotRequest`; startup wires dependencies separately. |
| `RunSpec` became the central boundary object. | `ExecutionContract` is the boundary from Service to Host. |
| `PromptPack` was treated as a standalone quality layer. | Prompt contributions are part of the Service contract; Host composes a `SceneManifest`. |
| `Report`, refs, and report checking were too central. | Final output is a scene-specific result schema. Result checks are Host admission checks. |
| Agent owned OM reasoning semantics. | Agent is a generic message executor; OM semantics live in Service, Scene, and tools. |
| Model/tool execution was spread across Agent, Host, Tools, and ModelClient. | Host owns an internal Engine primitive for model turns, tool calls, budgets, cancellation, and events. |
| Host trusted Service-provided tool policy too much. | Normal `ExecutionContract.policy` carries only `read_only=true`; Host projects tools, environment, readiness, mock policy, synthesis policy, limits, and fixtures from `SceneCatalog`, then rejects conflicting override attempts. |
| Phase 1 only proved diagnostics routing. | Phase 1 must prove a Dayu-like execution contract and real read-only scene loops across diagnostics and analysis scenes; monthly review is one pressure test, not the architecture center. |

Terms used by this document:

| Term | Meaning |
|---|---|
| `ExecutionContract` | Service output. It describes one accepted Agent sub-execution and how Host should govern it. |
| `SceneManifest` | Host-built runtime environment from the contract: messages, tools, context slots, limits, and output schema. |
| `AgentInput` | Host-internal final input for the Agent. It is not an upper-layer public contract. |
| `AppEvent` | Append-only runtime event emitted by Host/Engine/Agent, such as model turn, tool call, observation, budget, result, or failure. |
| `AppResult` | Host-admitted final result rendered by UI. For OM analysis scenes this may contain a structured `AnswerReport`. |
| `ExecutionEnvironment` | Where a run is allowed to execute: `eval`, `local`, or `channel`. It gates mock data and scene readiness. |

## Current Code Reality

The current production/channel runtime is not Copilot. The inbound assistant
still has a deterministic command chain:

```text
src/application/assistant/runtime.py
-> router.py
-> perception.py
-> reasoning.py
-> action.py
-> observation.py
-> renderer.py
-> audit/session/operation persistence
```

That chain may keep serving slash commands, permission replies, preview/confirm
receipts, audit lookup, and deterministic diagnostics while Copilot v2 is
designed and built. It must not become the new free-form question-answering
Agent.

The new local Copilot lane is allowed to exist in parallel for Phase 1:

```text
./om copilot run|eval
-> src/interfaces/cli/copilot_ops.py
   -> src/application/copilot/local_harness.py
      -> src/application/copilot/service.py prepare_contract()
      -> src/application/copilot/host.py run_contract()
-> src/application/copilot/engine.py
-> src/application/copilot/agent.py
```

That direct CLI-to-Host call is only the Phase 1 local/eval harness. Channel
adapters must not call Host, Engine, Agent, or Copilot tools directly. The
channel facade enters Service once for the channel contract gates, then passes
that same prepared contract into the local harness's prepared-contract Host
entry; it must not re-enter Service by wrapping the request as a local run.

Current-state implications:

- `answer_verifier.py`, `evidence.py`, `router.py`, `perception.py`,
  `reasoning.py`, `renderer.py`, `session.py`, and `runtime.py` may still exist
  for the legacy inbound assistant path.
- Copilot v2 must not import or wrap those modules as its Agent loop.
- Architecture guards must distinguish three categories: physically deleted old
  modules, legacy deterministic assistant modules, and forbidden Copilot
  dependencies.
- Channel integration must wait until the local Copilot path is stable and the
  old free-form path remains disabled or is fully retired.

## Goals

- Answer open-ended OM questions with judgement, not raw tool receipts.
- Give the Agent a real bounded tool-use environment prepared by Host.
- Keep writes, notifications, config changes, broker-facing operations, and
  upgrades outside the free-form Agent loop.
- Preserve one canonical OM tool authority instead of creating Copilot-only
  business paths.
- Avoid hardcoded strategy templates, regex intent hacks, and question-specific
  business rules.
- Make failures explicit: missing data, tool errors, stale context, and
  unsupported requests must be visible to the user.

Representative read-only questions:

```text
分析6月的期权操作有没有不合理，需要优化的地方
6月收益主要来自哪里
最近 close advice 为什么没有通知
当前期权风险暴露集中在哪些标的
这个标的为什么没有通过筛选
```

The June option review is a required evaluation benchmark because it exposed the
old raw-row-answer failure. It must not become the architecture center, a fixed
template, or a strategy-specific branch. The same Service/Host/Agent loop must
also pass income attribution, current exposure, diagnostics, missing-evidence,
and refusal cases.

## Non-Goals

- Do not restore `agent_loop.py`, `copilot.py`, `task_profiles.py`,
  `context_projection.py`, `context_validation.py`, `answer_guard.py`,
  `coverage_verifier.py`, or renamed equivalents inside Copilot v2.
- Do not introduce a fixed `Evidence Plan` layer.
- Do not use ordinary LLM chat as a fallback when OM evidence is unavailable.
- Do not add strategy thresholds, symbol preferences, account-specific rules, or
  fixed option-review templates in Copilot or assistant code.
- Do not make `./om-agent` the internal Copilot runtime. It is a northbound
  structured tool gateway for external agents.
- Do not make `./om assistant handle` the long-term Copilot entry. It may remain
  as a compatibility shim for the current inbound assistant.
- Do not allow free-form text to mutate production state.

## Architecture

```text
UI
  adapts inbound/outbound protocol
  -> Service

Service
  prepares domain ExecutionContract
  -> Host

Host
  re-validates ExecutionContract against SceneCatalog, prepares SceneManifest,
  owns lifecycle, budgets, tool sandbox, event log, cancellation, result
  admission, and rendering-safe AppResult
  -> Agent

Agent
  runs generic bounded model/tool loop over AgentInput
  -> Host events and final candidate result
```

Scene preparation, tool projection, event storage, and Engine are Host-internal
mechanisms. They are not new product layers.

## Layer 1: UI

UI owns inbound and outbound adaptation only. It does not own task semantics,
tool choice, planning, or business judgement.

Long-term UI surfaces:

| Surface | User | Output |
|---|---|---|
| `./om` | Human operator | CLI text and operation receipts |
| `./om-agent` | External agents such as Claude Code or Codex | Structured JSON tools |
| Channel adapters | Human operator through ClawBot / Feishu / WeChat | Channel text |

Compatibility or transport shells:

| Shell | Target treatment |
|---|---|
| `./om assistant handle` | Keep temporarily for slash commands, permission replies, and legacy receipts |
| `./om inbound feishu` | Treat as transport handling, not a Copilot entry |
| `./om channel wechat-clawbot ...` | Keep as channel lifecycle and serve commands, not Copilot logic |

Rules:

- New local Copilot commands live under `./om copilot ...`.
- Channel adapters remain outside Copilot Host/Agent internals. The Phase 3
  gate enters through the approved Copilot channel facade, not Host internals.
- No channel adapter may call Host or Agent directly.
- Internal Copilot must not shell out to `./om-agent`.
- `./om-agent` remains a UI/tool-gateway surface. Internal Copilot and
  `./om-agent` must share canonical tool metadata and must not diverge.

UI-to-Service request contract:

```text
CopilotRequest
- request_id
- source_entry: cli | channel | compatibility
- user_message
- explicit_scope:
    config_key
    market
    accounts
    symbols
    date_range
    month
- channel_context:
    channel_id
    conversation_id
    user_alias
- execution_environment: eval | local | channel
- debug_overrides:
    scene_name
    fixture_id
```

Rules:

- UI passes `CopilotRequest` and receives `AppResult` or a
  refusal/clarification envelope from the approved entry for that phase. It does
  not pass raw config paths, model names, tool names, prompt text, or Host
  internals.
- `debug_overrides` are allowed only for the local eval command. Local `run` and
  channel paths must not expose scene or fixture override. When an eval scene
  override is accepted, Service records the selected scene capability with
  `capability_sources.source=scene_override`; it does not require the free text to
  trigger the same capability hint.
- Startup preparation wires dependencies into Service and Host: config loader,
  scene catalog, model registry, canonical tool registry, event store, clock, and
  Host facade. Runtime requests select from those prepared dependencies. In
  Phase 1, `local_harness.py` is the local/eval composition shell that wires
  Service `prepare_contract()` to Host `run_contract()`. Production/channel
  composition is deferred to Phase 3 and must preserve the same boundary.
- The local CLI may accept model config only as a local harness runtime option:
  Phase 1 explicit JSON, Phase 2 explicit JSON or explicit assistant config
  path.
  Real CLI process runs must not load the default assistant LLM config
  implicitly; production-data synthesis requires explicit local opt-in through
  `--model-config-json` or `--assistant-config`. UI forwards the option to the
  local harness; UI and Host must not parse model config JSON or build model
  clients. It is not part of `CopilotRequest`, not passed through Service, and
  not a channel UI contract.
- `config_key` is a scope value, not a file path. Host resolves it through the
  prepared config boundary.

Entry consolidation:

```text
Copilot lane:
  ./om copilot run
  Channel adapters
  -> Service -> Host -> Agent

Tool Gateway lane:
  ./om-agent
  -> canonical OM tool registry
```

## Phase 0: Entry Consolidation

Phase 0 is the entry consolidation phase. It is not the Agent runtime itself,
but it prevents the old assistant free-form path from competing with Copilot.

1. Freeze default entry behavior. Keep unsupported free text returning
   `NATURAL_LANGUAGE_REBUILDING` unless the explicit Copilot channel gate is
   enabled.
2. Add local `./om copilot run` for read-only evaluation.
3. Add disabled-by-default channel routing only after local evals pass.
4. Retire assistant-specific free-form entry naming only after remote wrappers
   no longer depend on it.

## Layer 2: Service

Service is the OM business entry point. Its output is an `ExecutionContract`,
not a fixed evidence plan and not a raw classifier result.

Service owns:

- command and permission-reply interception before free-form handling;
- domain task understanding;
- scope resolution from request and entry context;
- safety policy for writes, notifications, broker-facing operations, service
  administration, config mutation, trade events, and position writes;
- scene selection;
- toolset policy at capability level;
- prompt contributions that describe the domain task and user intent;
- output schema selection;
- budget and model profile selection at policy level;
- explicit refusal or clarification when a safe contract cannot be prepared.

Service must not own:

- hidden natural-language business routing;
- exact analytical tool sequence;
- row-level or metric-level proof selection;
- final findings or recommendations;
- option strategy thresholds or symbol-specific judgement;
- any mutation path from free-form text.

### Service Scene Selection Boundary

It is reasonable for Service to select a scene. It is not reasonable for Service
to become an implicit business router.

The distinction is:

- Service may decide "this accepted request should run a declared scene".
- Service must not decide "June option trades were unreasonable because of X".
- Service may match declared capabilities, task kind, required scope, safety
  mode, and toolset readiness.
- Service must not contain natural-language regex branches that directly answer,
  choose analytical rows, or encode strategy preferences.
- Host must not trust Service-provided `allowed_tools` as execution authority;
  it re-projects the final tool sandbox from the selected `SceneDefinition`.

Service scene selection must be explicit and auditable:

```text
SceneSelectionRequest
- request_id
- task_kind
- normalized_question
- explicit_scope
- requested_capabilities
- safety_mode
- source_entry
- execution_environment
- capability_sources:
    explicit_command_arg | scope_parser | adapter_metadata | eval_override
- scope_sources:
    explicit_command_arg | channel_context | normalized_text

SceneSelectionDecision
- catalog_version
- selected_scene_name
- selected_scene_version
- candidate_scenes
- rejected_scenes:
    scene_name
    reason
- environment_gate_result
- mock_data_gate_result
- selection_environment
- scene_override
- clarification_question
- refusal_reason
```

Selection flow:

```text
request
-> command / permission interception
-> safety policy
-> scope normalization
-> SceneSelectionRequest
-> declared SceneCatalog filter
-> execution-environment and mock-data gates
-> one scene | clarification | refusal
-> ExecutionContract
```

If multiple scenes match and Service cannot choose from explicit scope and
capability fit, it asks one targeted clarification. It must not pick a scene by
question-specific regex priority. Local eval may override the scene with
`--scene`; local `run` and channel paths must not expose that override. An eval
scene override is an explicit debug input and may provide
`requested_capabilities` directly from the selected scene catalog entry.
Phase 1 implements this by returning clarification on multi-scene matches; it
must not mark later catalog entries as lower-priority matches.

`requested_capabilities` may be inferred from the user's request, but the
inference is only an input to catalog filtering. It must not choose tool order,
SQL, answer outline, finding categories, option thresholds, or recommendations.
Any Phase 1 capability extraction rule must be declared on `SceneDefinition` as
a visible capability hint with bounded `activation_terms`; those terms are
matched mechanically as declared term groups, not arbitrary regular
expressions. Service passes those declared hints into `request_understanding.py`;
the parser must not import the scene catalog or keep private scene/tool names.
The resulting decision trace must record each capability source and reason.
Capability hints must require scene-specific domain terms. They must not match
broad everyday words such as bare `通过`; `通过筛选` may be accepted because
`筛选` is the domain term, while `NVDA 通过了吗` must remain a clarification
request.
The same rule applies to runtime diagnostics: `运行状态` or `健康度` can select
runtime diagnostics, but bare `状态` in a symbol question such as `NVDA 状态怎么样`
must not select a runtime scene by itself.
Scope extraction alone must not create a requested capability. Scope can satisfy
the selected scene's `required_scope`, but a bare symbol such as `NVDA` is an
ambiguous request and should ask for clarification instead of routing to a
business scene.
Market-scoped symbol normalization is allowed only after an explicit market
scope exists. For example, `config_key=hk` may normalize a bare four-digit HK
code such as `0700` to `0700.HK`, but the same text without HK scope or under a
US config must remain insufficient symbol scope. This normalization still must
not create a requested capability by itself.
Relative month scope such as `6月` or `六月` must be normalized with an
explicit Service-provided reference year and recorded in `scope_sources`;
request-understanding code must not read the system date on its own.
Option-side terms such as `put` and `call` are not `symbol` scope. If a later
phase needs option-contract parsing, it should add explicit contract fields
rather than overloading the underlying symbol slot.
If the capability fit is ambiguous, Service asks a clarification instead of
routing by a hidden priority list.

### ExecutionContract

`ExecutionContract` is the Service-to-Host boundary. It should follow Dayu's
contract shape: Service accepts the business request, Contract preparation turns
that decision into a stable hosted execution unit, and Host consumes the result
without reinterpreting the business request.

```text
ExecutionContract
- contract_id
- request_id
- scene_name
- execution_environment: eval | local | channel
- input:
    user_message
    config_key
    symbol
    month
    fixture_id: eval only
- policy:
    read_only: true
- decision_trace:
    selected_scene
    candidate_scenes
    rejected_scenes
    selection_environment
    scene_override
    requested_capabilities
    capability_sources
    scope_sources
    safety_hits
    safety_suppressed_hits
    refusal_reason
```

Classification may help prepare the contract, but it is not the architecture
center. If an intent parser is introduced later, its output is advisory only;
Safety Policy and the final contract-routing decision remain deterministic.
Host execution details such as toolsets, timeouts, model-turn budgets,
mock-observation policy, answer-synthesis policy, output schema, cancellation,
and event handling are projected from `SceneCatalog` into `SceneManifest`; they
are not normal Service-contract fields.

Safety policy rules must be explicit and auditable. They may use operation
vocabulary, but must not encode strategy judgement, account thresholds, symbol
preferences, or recommended trades. They also must not use broad bare words that
collide with OM strategy terms, such as treating `sell` in `sell put` as a
broker write request without an order/action cue.
Notification safety must distinguish send requests from read diagnostics:
`通知我` may be refused as a notification-send request, but `为什么没有通知我`
is a read-like diagnosis and must not be refused by the safety layer.

Phase 1 safety hits are rule-family names, not a single opaque
`write_like_request` bucket. Suppression is a narrow exception for review-style
read questions such as "有没有需要修改的地方"; action-proposal wording such as
"是否新增", "要不要加到配置", or "需不需要更新配置" remains refused until a
separate explicit read-only advisory scene is designed and approved. If a
write-like rule is intentionally suppressed because the phrase is handled as a
read-only diagnostic question, that non-blocking decision must be recorded
separately as `safety_suppressed_hits` so it does not become hidden routing. The
minimum rule families are:

- `config_mutation_request`
- `notification_send_request`
- `broker_trade_request`
- `release_or_service_change_request`
- `state_mutation_request`

Contract gates:

- `execution_environment=eval` is the only environment where mocked observations
  are legal.
- `allow_mock_observations` is a SceneCatalog property, not a Service-contract
  field. It defaults to false and may be true only for declared eval/debug scenes
  with an explicit fixture.
- `phase_readiness=eval_only` scenes cannot produce normal local or channel user
  answers. Outside eval they return a structured not-ready result or
  clarification.
- Host enforces environment, scene readiness, mock fixture policy, and allowed
  tools from `SceneCatalog`. Normal Service contracts must not carry those
  Host-owned fields. If an externally constructed contract tries to include
  conflicting Host-owned policy fields, Host rejects it before tool execution.
  Service must not compensate by changing prompt text.

`ExecutionContract` describes one Agent sub-execution. Direct operations and
future compound workflows may still be submitted to Host, but they must not
force this contract to become a universal workflow schema.

## Layer 3: Host

Host owns execution governance. It accepts an `ExecutionContract`, prepares a
`SceneManifest`, runs the Agent through an internal Engine, records events, and
admits or rejects the final result.

Host owns:

- run/session identifiers;
- session and run lifecycle;
- cancellation, timeout, and resource governance;
- event subscription and append-only event storage;
- concurrency policy hooks;
- later resume, reply outbox, and replay capabilities;
- `SceneManifest` assembly from the contract, canonical tool metadata, and
  runtime context;
- model profile resolution through a shared model boundary;
- tool sandbox construction and read-only enforcement;
- tool error normalization into recoverable observations;
- result admission checks;
- safe failure wrappers.

Host must not own:

- business strategy rules;
- natural-language regex recipes for specific questions;
- option review templates;
- final answer judgement except for safe failure wrappers.

### Host Capability Surface

Host's stable capability surface should be larger than the first local
implementation. Phase 1 may implement a single local session and one run at a
time, but the contracts must already leave room for Dayu-style governance.

| Capability | Phase 1 contract | Phase 1 implementation |
|---|---|---|
| Session | `session_key` exists in `host_policy`; local CLI uses one ephemeral session | in-memory or local record only |
| Run lifecycle | run id, terminal status, failure class, result | one synchronous local run |
| Events | append-only `AppEvent` stream with model/tool/budget/result events | in-memory local event log; JSONL persistence is later |
| Timeout/cancel | timeout and cancellation intent are represented in `host_policy` | per-run timeout and Engine pre-action cancel check |
| Concurrency | lane fields exist but may default to single local lane | no channel concurrency yet |
| Resume | explicit non-goal for Phase 1 runtime, but contract has `resumable` | disabled unless a later phase enables session memory |
| Reply outbox | not used by local CLI | deferred until channel rollout |
| Agent replay | optional future Host ability for invalid/empty result repair | not required in Phase 1 |

The important boundary is that Service talks only to Host's public facade. It
must not reach into Engine, scene preparation, event storage, or tool sandbox
internals.

### SceneCatalog

`SceneCatalog` is a declared registry of scenes. It is the only source Service
may use for scene selection, and the only source Host scene preparation may use
for scene assembly.

```text
SceneCatalog
- catalog_version
- scenes:
    SceneDefinition[]
- output_schemas:
    schema_id
    schema_version
- toolset_aliases:
    alias
    canonical_tool_groups
- execution_environments:
    eval
    local
    channel
```

Catalog rules:

- Catalog entries are static design/config artifacts, not model output.
- Catalog matching uses declared fields only. It must not run hidden prompt
  classification, SQL, or business answer logic.
- Each match or rejection must have a machine-readable reason for traceability.
- Adding a scene is a design change and must include eval cases.
- A scene can be production-targeted while still using mock observations in an
  eval environment.
- Mock observations are legal only through an explicit eval fixture. A production
  target scene with `phase_readiness=eval_only` is not selectable for normal
  local or channel answers.
- Host must fail closed when an `ExecutionContract` does not declare the
  requested scene capabilities. It must not expose a whole scene's tools as a
  fallback for missing Service intent.

Selection uses these fields only:

```text
SceneMatchFields
- accepted_task_kinds
- capability_tags
- required_scope_fields
- optional_scope_fields
- supported_markets
- safety_mode
- required_toolsets
- output_schema_id
- phase_readiness: eval_only | local_only | channel_ready
- allowed_environments
- mock_data_policy: forbidden | eval_fixture_only
```

Typical rejection reasons:

```text
missing_scope
unsupported_market
toolset_not_ready
unsafe_permission_mode
phase_not_enabled
environment_not_allowed
mock_data_not_allowed
ambiguous_scene
```

Current local/eval scene catalog:

| Scene | Match fields | Toolsets | Readiness | Environment |
|---|---|---|---|---|
| `operations_diagnostics` | `task_kind=diagnosis`, capabilities for runtime, candidate-filter, and close-advice notification diagnosis; `config_key` required | `runtime_status`, `candidate_filter_explain`, `close_advice_read`; eval fixtures `candidate_filter_diagnostics_model_ready` and `close_advice_notification_diagnostics_model_ready` only in `eval` | `local_only` | `local`, `eval` |
| `monthly_income_attribution` | `task_kind=read_analysis`, capability `monthly_income_attribution`, `config_key` and `month` required | `analysis_catalog`, `analysis_query` in approved income view-mode, `monthly_income_report`; eval fixture `june_income_attribution_basic` only in `eval` | `local_only` | `local`, `eval` |
| `current_option_exposure` | `task_kind=read_analysis`, capability `current_option_exposure`, `config_key` required | `analysis_catalog`, `analysis_query` over exposure views, `option_positions_read`; eval fixture `current_option_exposure_model_ready` only in `eval` | `local_only` | `local`, `eval` |

Capability activation patterns are task anchors, not answer-intent templates.
`monthly_income_attribution` owns monthly income/source questions and does not
require recommendations.
Likewise, `current_option_exposure` may match current option exposure or
concentration language, but concentration thresholds and recommendations remain
Agent/model conclusions backed by observations, not Service routing rules.
`operations_diagnostics` may match close-advice notification diagnosis only when
the request carries a close-advice or close-action anchor. Broad channel or push
delivery questions remain clarification cases until a dedicated scene/tool
contract exists.

### SceneDefinition

`SceneDefinition` is a declared scene catalog entry read by Host scene
preparation. It is not produced by the model and is not a business routing
layer.

```text
SceneDefinition
- scene_name
- scene_version
- description
- match:
    accepted_task_kinds
    capability_tags
    capability_hints:
      capability
      activation_terms
      activation_reason
    required_scope_fields
    optional_scope_fields
    supported_markets
    safety_mode
    phase_readiness
    allowed_environments
    mock_data_policy
- prompt:
    ordered_slots:
      system_base
      safety
      service_task
      service_context
      tool_use
      output_schema
    required_slots
    static_prompt_asset_id
    service_contribution_policy
- context:
    slots:
      name
      provider
      authority: static | runtime_tool | config | channel
      freshness_policy
      max_chars
      missing_behavior: fail | omit | mark_missing | ask_clarification
- tools:
    required_toolsets
    optional_toolsets
    per_tool_projection
    observation_summary_contract
    common_error_mapping
- refs:
    visible_ref_policy
    artifact_policy
    large_output_compaction_policy
- output:
    output_schema_id
    output_schema_version
    admission_profile
    renderer_expectation
- execution:
    default_execution_permissions
    max_observation_chars
    default_model_profile
    default_budget_profile
- eval:
    eval_tags
    fixture_mode
    allowed_fixture_ids
```

Rules:

- Service selects `scene_name` and supplies `prompt_contributions`.
- Prompt contributions must target declared slots. Unknown slots fail scene
  preparation instead of being appended silently.
- Scene preparation reads the scene definition, consumes the contract, and
  produces `SceneManifest`.
- Scene definition must not decide whether the business request is accepted.
- Scene definition must not contain option-review conclusions, symbol
  preferences, or account-specific strategy judgement.
- Scene definition may declare what evidence categories a scene expects, but not
  the conclusion those categories should imply.
- Scene definition must be sufficient for Host to assemble `SceneManifest`
  mechanically. If Host needs a scene-specific if/else to build messages,
  context, tool projections, refs, or result admission, the missing rule belongs
  in `SceneDefinition`.
- `fixture_mode` may be enabled only for eval scenes. Production-target scenes
  can have eval fixtures, but fixtures do not make the scene production-ready.

### SceneManifest

`SceneManifest` is Host-built runtime input. It is not a standalone layer.

```text
SceneManifest
- schema_version
- run_id
- contract_ref
- scene_ref:
    scene_name
    scene_version
- execution_environment
- messages:
    system
    task
    safety
    tool_use
    output_schema
- tools:
    name
    description
    input_schema
    read_only
    group
    examples
    common_errors
    observation_summary_contract
- tool_binding:
    selected_toolsets
    execution_permissions
    system_hard_boundary
- context_slots:
    name
    value_preview
    source_authority
    freshness
    missing
    missing_behavior
- refs:
    visible_ref_policy
    artifact_policy
    large_output_compaction_policy
- limits:
    max_model_turns
    max_tool_calls
    timeout_seconds
    max_observation_chars
- output_schema
- admission_profile
```

Service contributes task intent, scope, scene id, and prompt contributions.
Host performs mechanical scene preparation: tool projection, context slot
materialization, freshness tagging, and size limiting.

Static context is never runtime authority. Tool observations are the authority
for factual claims.

Manifest assembly rules:

- Prompt slots are assembled in the order declared by `SceneDefinition`.
- Missing required prompt or context slots fail preparation or ask the declared
  clarification; they are not silently replaced with generic chat text.
- Tool descriptions visible to the model come from canonical tool metadata plus
  the scene projection. Prompt text must not redefine a tool capability.
- Observation summaries are produced according to the scene's declared summary
  contract, so the Agent sees compact, citeable facts rather than arbitrary raw
  tables.
- If a manifest uses an eval fixture, the manifest must mark
  `execution_environment=eval` and expose the fixture id in events.

Final tool binding is calculated from required and optional scene toolsets:

```text
required_allowed_tools =
canonical_tool_registry
∩ SceneDefinition.tools.required_toolsets
∩ ExecutionContract.preparation_spec.selected_toolsets
∩ ExecutionContract.preparation_spec.execution_permissions
∩ system hard boundary

optional_allowed_tools =
canonical_tool_registry
∩ SceneDefinition.tools.optional_toolsets
∩ ExecutionContract.preparation_spec.selected_toolsets
∩ ExecutionContract.preparation_spec.execution_permissions
∩ system hard boundary
```

If any required toolset cannot bind to an allowed tool, Host returns a
structured tool-unavailable failure. Missing optional toolsets are omitted and
recorded as preparation warnings. Service must not compensate by embedding
business logic in prompt text.

When Host rejects an `ExecutionContract`, the precise policy reason is recorded
as a Host event. The default `AppResult` should expose only that the execution
policy check failed; it must not turn internal contract strings into the user
answer.

### Host-Internal Engine

Engine is a Host-internal primitive, not a fifth architecture layer.

Engine owns:

- structured model turn calls;
- model-format repair attempts;
- tool-call proposal validation against `SceneManifest`;
- tool execution through Host-supplied tool interfaces;
- observation-event creation through a Host-supplied builder;
- event emission through a Host-supplied recorder;
- budget accounting;
- timeout and cancellation propagation.

Phase 1 Engine requirements:

- non-streaming structured output only;
- one model-call timeout per turn;
- one repair attempt for invalid structured output;
- no provider-specific fallback chat;
- model errors recorded as `AppEvent` records;
- context length or token-budget overflow returns `failed` or
  `insufficient_evidence`, not a generic answer.
- no direct `ExecutionContract` dependency. Host extracts scene input, fixture
  policy, and result projection callbacks before invoking Engine.
- no direct `AppResult` or `result_projection.py` dependency. Host injects the
  observation-event builder and final result projection callbacks.

## Layer 4: Agent

Agent is the only layer that performs free-form reasoning, but it is not an OM
business module. It receives `AgentInput` from Host and produces tool proposals,
finish decisions, or clarification requests. Host-side result projection turns
observations into `AppResult`.

`AgentInput` is Host-internal. Service, UI, tests, and channel adapters should
not construct it or assert against its full shape. The stable cross-layer
contracts are `ExecutionContract`, `AppEvent`, and `AppResult`.

Agent owns:

- reading the prepared messages, tool schemas, context slots, and
  Host-provided budget summary;
- choosing among visible read-only tools;
- deciding whether another observation is needed within budget;
- asking for clarification when the task cannot be completed safely.

Agent must not own:

- permission decisions;
- direct registry access;
- direct filesystem or shell access;
- OM strategy constants;
- `AppResult` / `AnswerReport` construction;
- user-facing answer text;
- write, notification, broker, service, config, trade-event, or position
  mutation.

Phase 1 uses a thin single-role action loop:

```text
Host prepares SceneManifest and Host-supplied tool interfaces
-> Engine asks Agent action decider for tool | finish
-> Engine rejects actions outside SceneManifest and budgets
-> Engine calls Host-supplied ToolSandbox for allowed read-only tools
-> Host records AppEvent observation through Engine callback
-> loop continues within budget or Host-side result projection builds structured AppResult
-> Host admits structured AppResult
-> UI renders user_response from AnswerReport
```

The Phase 1 default action decider is deterministic and selects from manifest
tools only. It is an explicit replacement point for a later model-backed
decision boundary; it must not import the old assistant runtime, tool registry,
or OM business strategy logic. Once Host supplies a custom or model-backed
decider, Engine must not fall back to the default decider for budget or finish
decisions; any remaining-work check is mechanical over the `SceneManifest` and
Agent state.

Phase 1 also defines a `ModelActionDecider` protocol wrapper. It accepts an
injected structured-action model callable, builds a compact action request from
`AgentState`, parses `tool` or `finish`, and allows one repair request for
invalid structured output. A `tool` action is valid only when `tool_name` is in
the current `SceneManifest.allowed_tools`; disallowed tool names are repairable
protocol errors. It does not call live providers, read model config, or own tool
execution. If structured output remains invalid after repair, it must return an
invalid action that Engine records as a recoverable observation; it must not
convert model failure into `finish`.
If the injected model callable raises, it returns a sanitized invalid action
using only the exception type. It must not expose provider exception messages or
scope values in the action reason.
Phase 1 `AgentState` contains the Host-prepared `SceneManifest` and live run
state only. It must not hold the full `ExecutionContract`; the contract remains
Host-owned input for policy checks, scene-input extraction, fixture gating, and
final result projection. Engine may receive the Host-prepared scene input and
projection callbacks, but it must not import or inspect `ExecutionContract`.

The model action request is an allowlist, not serialized `AgentState`. Phase 1
includes only the Host-prepared user message from `SceneManifest.messages`,
allowed tools, attempted tools, observation summaries, missing-data labels,
a remaining-budget summary, finish conditions, a quality contract, and
response schema. The raw
`SceneManifest.limits` stay inside Host/Engine enforcement and are not sent to
the model. Model instructions may use `task_guidance` only for scene evidence
expectations and stopping conditions. `quality_contract` is the generic output
contract: answer the user question directly, interpret what cited
observations imply, avoid row receipts or raw field dumps, name missing
evidence, and require next steps to include explicit `action`, `target_scope`,
`summary`, and evidence basis when recommendations are needed. `answer_dimension`
is not a shared recommendation field; it is required only when the selected scene
declares `answer_dimensions`.
Host admission remains structural and safety-focused; it does not become a
semantic answer writer. The request must not read user text directly from
`ExecutionContract.input`, and it must not include scene names, contract scope
values such as `config_key` or `month`, raw observation `data`, compact
`value_preview`, raw Host limits, or raw tool errors. Phase 2 may expose
Host-projected bounded `facts` derived from observations. Those facts are
strings with size/count limits and current-run `obs_<n>` refs; they are not raw
tool payloads or a second tool registry.
Repair requests may include the previous response shape, but only as a compact
protocol summary such as `kind` and whether a tool name was present. They must
not echo arbitrary extra fields or raw disallowed tool names from an invalid
model response.

Later phases may add an explicit Planner/Critic prompt shape only if evals show
the thin loop cannot reliably decide evidence sufficiency. Planner/Critic must
remain an Agent prompt pattern, not new architecture layers.

Evidence sufficiency rules:

- If a requested conclusion depends on missing tools, missing artifacts, invalid
  scope, or stale observations, the candidate result must be
  `insufficient_evidence` or `needs_clarification`.
- If a tool error is itself the evidence, the answer must state that the finding
  is about the failure, not the underlying business state.
- If observations conflict, the answer must surface the conflict and avoid
  choosing a side unless a later observation is clearly more authoritative.
- If remaining budget cannot obtain a necessary observation, the answer must
  stop with partial findings instead of inferring missing facts.

## Result Contract

Final output is a scene-specific schema admitted by Host as an `AppResult`.
Host admits the structured result. UI surfaces render `user_response` from that
structure.
For analysis and diagnostics scenes, the schema may be:

```text
AnswerReport
- status: answered | needs_clarification | insufficient_evidence | failed
- conclusion
- findings
- evidence_refs
- missing_data
- recommendations
- risk_notes
```

Rules:

- Analytical answers must start from a conclusion.
- Tables and rows are supporting material, not the answer itself.
- Numeric claims, symbols, accounts, months, and status claims must be traceable
  to current-run tool observations.
- Recommendations must cite supporting observations; unsupported judgement stays
  in `missing_data` or the conclusion gap, not in admitted recommendations.
- Missing data must be stated when it affects the conclusion.
- Raw grouped rows or empty rendered text are never valid final output, but
  Phase 1 prevents this through scene guidance and eval coverage rather than a
  Host-side semantic scanner.

Host result admission checks:

- result status is valid;
- structured conclusion exists for answered runs;
- required fields are present for the scene output schema;
- no forbidden mutation is claimed in any UI-renderable result text, including
  conclusion, attempted checks, findings, recommendations, refs, and
  `missing_data`;
- cited refs, if present, are visible in the current run's `AppEvent` tool
  observations generated under the current `SceneManifest`;
- eval-only results are clearly marked and cannot be rendered as production
  local/channel answers;
- budget or tool failures are surfaced when they affect the answer.
- When Host admission rejects a model-produced result, the rejected `AppResult`
  is converted to a safe `failed` result whose `missing_data` contains the same
  stable rejection reason recorded in the `result_admission_rejected` event. UI
  rendering maps that token to user-facing text and must not render the rejected
  raw field content.

This is not a revived `answer_guard`. It is a narrow Host admission check over
the scene output schema. In Phase 1 it does not perform semantic fact checking,
claim rewriting, strategy judgement, or answer improvement. A result can pass
admission only as structurally renderable, safe, and traceable to visible
observations; UI rendering is a separate adaptation step and the result is not
certified as financially correct by Host.

## Event Log

The event log replaces the old Evidence Ledger, but it is broader than evidence.
It records execution and supports debugging, audit, eval, and result citations.

```text
AppEvent
- event_id
- run_id
- type:
    contract_received
    scene_prepared
    model_turn
    tool_call
    tool_observation
    tool_error
    budget_event
    result_candidate
    result_admitted
    result_rejected
    model_error
    run_failed
- timestamp
- payload
- visible_ref
- artifact_path
```

Phase 1 refs:

```text
ref_id: obs_<n>
source_event_id
tool_name
summary_text
value_preview
artifact_path
```

Phase 1 rules:

- Every visible tool observation gets one `obs_<n>` ref.
- Agent can cite only refs visible in the observation summary supplied by Host.
- Observation events store compact `value_preview`, not raw tool `data`; row
  sets and nested objects must be summarized or moved to explicit artifacts in a
  later phase.
- Phase 1 checks ref visibility and status shape, not full semantic claim
  verification.
- Large outputs may have artifact paths, but Phase 1 does not require row,
  field, or cell refs.

## Tool Boundary

All tools come from the existing OM tool authority:

```text
src/application/agent_tool_registry.py
src/application/agent_tools/
```

`./om-agent` and internal Copilot must not drift into two different descriptions
of the same business capability. The canonical source remains `AgentTool`
definitions in `agent_tool_registry.py` and `agent_tools/`.

Copilot uses a read-only model-facing projection of canonical tool metadata.
If the current manifest metadata is not sufficient for Copilot, improve the
owning tool definition or add a shared projection helper. Do not hide
Copilot-only semantics in prompts.
The model-facing tool description must include a compact preview of the
canonical `AgentTool.resolve_output_contract(...)` result when available, so
the Agent sees the same fact fields and primary rows exposed by `./om-agent`.

`AgentToolView`:

```text
AgentToolView
- name
- required_scene_fields
- payload_fields
- observation_summary
- evidence_available
- missing_evidence
```

`ToolSandbox` responsibilities:

- enforce the `SceneManifest` allowlist;
- reject write-capable tools regardless of model request;
- normalize tool errors into recoverable observations;
- create compact previews for large outputs;
- write large outputs to artifacts when needed;
- generate visible refs for observation summaries and artifacts.

Tool views may compact tool output into observation summaries. They must not
write final conclusions, recommendations, option-operation judgement, or answer
outlines. Answer synthesis belongs to the model-backed Agent/result layer in a
later phase; Phase 1 Host projection may only report observation availability
and missing data. Projection consumes Host/Engine-supplied run semantics such as
`eval_only`; it must not infer eval/mock status by inspecting raw
`ExecutionContract.input` fields such as `fixture_id`.
Tool descriptions and output contracts must be projected from canonical
`AgentTool` definitions; `AgentToolView` may add only scene-field payload
mapping and compact observation behavior.
Scene-specific static tool payloads, such as approved analysis view-mode lists,
belong to `SceneDefinition` and Host-prepared `SceneManifest`, not to
`AgentToolView`.

If a needed analytical surface does not exist, add or improve a read-only OM
tool at the owning domain/application boundary. Do not compensate by embedding
hidden business logic inside Service, Host, Agent, or prompts.

Tool binding rules:

- Service may request `selected_toolsets` in `ExecutionContract`.
- Scene definition declares candidate toolsets for the scene.
- System hard boundary removes write-capable, notification, broker, service,
  config, trade-event, and position-write tools from read-only scenes.
- ToolSandbox receives only the final intersection and rejects every other model
  request as a recoverable policy observation.

## Hardcoding Policy

No hardcoded strategy policies are allowed in Copilot code without explicit
approval.

Allowed deterministic code:

- safety policy that blocks mutation, notification, broker, service, config,
  trade-event, and position writes;
- scene selection by declared capability and explicit user scope;
- schema validation;
- permission checks;
- output shape checks;
- budget and timeout controls;
- domain logic already owned by `domain/domain/` or application read tools;
- user-provided constraints in the current request;
- future policy/config artifacts approved as a separate design decision.

Disallowed deterministic code:

- option-review conclusion templates;
- symbol/account preference rules;
- hidden SQL recipes for specific natural-language questions;
- regex branches that answer business questions directly;
- assistant-only thresholds for strategy judgement.

## Failure Behavior

Failure responses must be useful and specific.

| Condition | Behavior |
|---|---|
| Missing scope | Ask one targeted clarification |
| Tool unavailable | State the tool/data gap and what was attempted |
| Insufficient evidence | Explain which conclusion cannot be supported |
| Budget exhausted | Return partial findings plus budget limitation |
| Write-like request | Refuse free-form execution and point to explicit flow |
| Result admission failure | Return a safe failure in channel mode and preserve events |

The system must not return "tool call completed but no renderable text" and must
not dump raw rows as the final answer.

## Target Module Layout

The new Copilot runtime lives outside `src/application/assistant/`.

```text
src/application/copilot/
  __init__.py
  contracts.py
  service.py
  request_understanding.py
  safety_policy.py
  host.py
  engine.py
  agent.py
  local_harness.py
  model_decider.py
  model_client.py
  scene.py
  eval_fixtures.py
  tools.py
  result_projection.py
  result_admission.py
  event_store.py

src/interfaces/cli/copilot_ops.py
```

Module ownership:

| Module | Owns | Must not own |
|---|---|---|
| `contracts.py` | `CopilotRequest`, `ExecutionContract`, `SceneDefinition`, `SceneManifest`, Host-internal `AgentInput`, `AppEvent`, `AppResult`, `AnswerReport` schemas | model calls, tool execution, business strategy |
| `service.py` | domain contract preparation, safety policy, scene selection, scope resolution | tool planning, model loop, final answers |
| `request_understanding.py` | thin scope extraction and Service-injected scene-declared capability hints for contract preparation | direct scene catalog imports, safety refusal decisions, scene selection, answer logic |
| `safety_policy.py` | deterministic Service-owned refusal decision for write-like free-form requests | request parsing, scene selection, tool planning, model loop |
| `host.py` | run lifecycle, scene preparation, Engine invocation, budget/timeout/cancel, event append, result-context projection, result admission | business heuristics, natural-language recipes |
| `engine.py` | structured model turns, tool-call protocol, repair attempts, budget events, Host-supplied observation/result callbacks | OM business semantics, permission policy, run lifecycle, `AppResult` construction |
| `agent.py` | generic action/state types and action decider boundary | permission decisions, direct registry access, OM strategy logic, `AppResult` construction |
| `local_harness.py` | Local/eval composition: prepare Service contract, build optional local model action decider from explicit JSON or explicit assistant config path, call Host facade | scene semantics, channel routing, tool execution, business analysis |
| `model_decider.py` | structured model action request/response adapter and repair protocol | live provider calls, model config ownership, tool execution |
| `model_client.py` | provider-specific local model callable construction for local harness opt-in | Host lifecycle, scene selection, tool execution, result admission |
| `scene.py` | declared scene catalog, allowed fixture ids, and mechanical scene manifest assembly from contract and tool/context providers | task classification, final answer writing, eval fixture observation content |
| `eval_fixtures.py` | deterministic eval-only fixture observations keyed by ids allowed by scene policy | scene selection, production tool execution, local/channel data substitution |
| `tools.py` | read-only tool sandbox, canonical tool projection, and compact observation summaries | parallel tool registry, eval fixtures, shelling to `./om-agent` |
| `result_projection.py` | Host-side observation refs and minimal structured `AppResult` projection from Host-supplied result context | `ExecutionContract` inspection, model/tool loop, admission, strategy judgement, user-response rendering |
| `result_admission.py` | structural and safety admission checks for scene result schemas | answer rewriting, strategy judgement |
| `event_store.py` | Phase 1 append-only in-memory run event log | business analysis, tool execution, durable persistence |
| `copilot_ops.py` | Phase 1 local/eval CLI adaptation into `CopilotRequest`, call into `local_harness.py`, and `AnswerReport` rendering | Copilot semantics, Service/Host composition, Host internals beyond the public local harness facade |

Dependency direction:

```text
src/interfaces/cli/copilot_ops.py
  -> src/application/copilot/local_harness.py
  local/eval UI adapter only; no Service, Host, Engine, SceneCatalog,
  ToolSandbox, model client, or Agent imports

src/application/copilot/local_harness.py
  -> service.py prepare_contract()
  -> host.py public run facade
  -> model_config.py only for explicit assistant config loading
  -> model_client.py / model_decider.py only for local model opt-in
  local composition only; no scene semantics, channel routing, or tool execution

src/application/copilot/service.py
  -> contracts.py
  -> request_understanding.py
  -> safety_policy.py
  -> scene.py
  prepares ExecutionContract or refusal/clarification AppResult; does not
  import Host, Engine, ToolSandbox, EventStore, or Host internals

src/application/copilot/host.py
  -> contracts.py
  -> scene.py
  -> eval_fixtures.py
  -> engine.py
  -> tools.py
  -> result_projection.py
  -> result_admission.py
  -> event_store.py
  accepts an already-built ActionDecider; does not parse UI model config or
  build provider clients

src/application/copilot/engine.py
  -> contracts.py
  -> agent.py
  -> result_projection.py
  -> tools.py through a Host-supplied interface

src/application/copilot/agent.py
  -> contracts.py
  -> model/action boundary supplied by Host

src/application/copilot/model_decider.py
  -> agent.py action types
  -> structured model callable supplied by the Phase 1 composition harness

src/application/copilot/tools.py
  -> src/application/agent_tool_registry.py
  -> src/application/agent_tools/
```

Forbidden dependencies:

- `src/application/copilot/` must not import assistant planner, router,
  perception, evidence, verifier, renderer, or runtime-loop modules for Copilot
  behavior.
- `src/application/copilot/` must not import `scripts/`.
- `agent.py` must not import `agent_tool_registry` directly.
- `tools.py` must not define a second manifest or duplicate tool metadata.
- `service.py` must not embed question-specific SQL, symbol-specific strategy
  logic, or answer templates.
- `result_admission.py` must not become a renamed `answer_guard`.

Compatibility adapters may live in the old assistant path temporarily, but only
as thin request/response adapters that call Service. They should be removable
without changing Copilot behavior.

## Reuse Boundary

Copilot must not duplicate stable infrastructure merely to avoid the word
`assistant`, but it also must not inherit the old assistant runtime chain. The
preferred path is to extract reusable infrastructure into shared modules before
Copilot depends on it.

| Capability | Reuse existing code? | New Copilot owner? | Rule |
|---|---:|---:|---|
| LLM provider registry | Yes, after extraction or explicit allowlist | No | Move or wrap provider metadata in a shared boundary |
| Model profiles | Partial | Host scene/model policy | Reuse parsing/catalog behavior if extracted |
| Inbound audit/session record | Partial | Event store remains Copilot-owned | Channel audit may link to Copilot `run_id` |
| Permission and pending-operation flow | Yes for deterministic operations | No free-form execution | Write-like requests redirect to explicit flows |
| Tool registry | Yes | ToolSandbox owns Agent view | Use canonical registry and shared projections |
| Agent loop and planning | No | Yes | New generic Agent/Engine loop; no old evidence pipeline |
| Result checking | No | Yes | Host result admission only |
| Event storage | No | Yes | New append-only event log; may cross-link audit by `run_id` |

If a temporary import from `src/application/assistant/` is unavoidable, it must
be named in an architecture-guard allowlist with a reason and extraction
follow-up. Phase 1 should avoid temporary assistant imports unless needed for
model configuration or inbound audit continuity.

## Phase 1 Executable Blueprint

Phase 1 is a thin local runtime, not the full target architecture. It must prove
that OM can host a generic read-only Agent loop behind a Service/Host contract.
It must not prove production monthly-review quality.

### Phase 1 Outcome

When Phase 1 is done, these representative local/eval lanes work. The lanes are
ordered by Copilot capability breadth, not by business priority.

| Lane | Representative command |
|---|---|
| Candidate diagnosis | `./om copilot run --text "NVDA 为什么没有通过筛选" --config-key us` |
| Close-advice notification diagnosis | `./om copilot eval --scene operations_diagnostics --fixture close_advice_notification_diagnostics_model_ready --text "最近 close advice 为什么没有通知" --model-action-json-file tests/fixtures/copilot/close_advice_notification_diagnostics_model_action.json` |
| Monthly income attribution | `./om copilot eval --scene monthly_income_attribution --fixture june_income_attribution_basic --text "6月收益主要来自哪里" --model-action-json-file tests/fixtures/copilot/june_income_attribution_model_action.json` |
| Current exposure analysis | `./om copilot eval --scene current_option_exposure --fixture current_option_exposure_model_ready --text "当前期权风险暴露集中在哪些标的" --model-action-json-file tests/fixtures/copilot/current_option_exposure_model_action.json` |

The `run` commands execute real read-only OM tools. With an explicit model
action decider, local diagnostics such as close-advice notification questions may
return a synthesized conclusion from runtime and close-advice observations; they
must not claim that a notification was sent unless an observation proves it. The
`eval` without model config runs against deterministic mock observations and is
marked eval-only. `eval` with explicit model config may run answer synthesis
against synthetic fixture facts only; it must not call real OM read tools.
`eval` with explicit model action JSON exercises the same model-action parsing,
repair, Host admission, and CLI rendering path without calling a provider; it is
an eval harness input, not a production answer path or answer template store.
The file form exists only to make long eval actions reproducible; it is still
eval-only and is not accepted by `./om copilot run` or channel paths.

`./om copilot run` is deterministic when no local LLM config is available. Phase
1 allowed only an explicit opt-in model config argument. Phase 2 expands the
same model-backed answer-quality loop across diagnostics, income attribution,
and current exposure before any channel expansion. It may also load an explicit
assistant config path through Copilot's own config adapter, without
importing the old assistant runtime. Default assistant config loading is not a
Copilot path; every model-backed local run must pass model config explicitly so
machine-local state cannot change deterministic runs or silently export
production observations. The harness injects the resulting `ActionDecider` into
Host instead of making Host parse model config. That selector may choose the
next allowed read-only tool or finish with an `AnswerReport`, but it does not
own Service scene selection,
Host tool policy, result admission, channel routing, or any fallback chat path.
Local/eval model configuration failures return a safe `failed` result before
tool execution. The `AnswerReport.missing_data` contains the stable model
configuration reason, and UI rendering maps it to readable Chinese text.
The model structured-output schema requires the `recommendations` array on every
finish report; scenes that do not need recommendations use an empty array, while
scenes marked `requires_recommendations` must provide at least one cited
recommendation to pass Host admission when all required tool evidence is
available. Each admitted recommendation must keep `summary`, `action`,
`target_scope`, and `basis_refs` as explicit fields; `action` and
`target_scope` must be non-empty strings, not content hidden inside `summary`.
When a scene declares `answer_dimensions`, model action parsing and Host
projection accept recommendation `answer_dimension` only when it matches one of
those scene-declared dimensions, and every admitted recommendation in that scene
must include a non-empty string `answer_dimension`. The model may mention related
evidence in `summary`, but the structured dimension field stays a single
auditable scene dimension rather than a free-form label.
Model action parsing also rejects final-report summaries that are raw field
assignment dumps, such as multiple `key=value` tokens in one finding or
recommendation. This is an output-shape rule, not a Host semantic scanner or a
strategy-specific heuristic.
For scenes marked `requires_recommendations`, model action parsing also treats a
finish report with no non-empty recommendations as invalid only when required
tool evidence is complete. When an allowed tool has already been attempted but
returns failed or weak evidence, the model may finish with explicit
`missing_data` and no recommendations; Host projection still returns
`insufficient_evidence` and never admits recommendations for that run. If the
model nevertheless includes recommendations, every admitted recommendation still
must cite current claimable refs; failed, weak, or metadata observations cannot
support recommendations. When Host has claimable observation refs and required
evidence is complete, recommendation `basis_refs` must collectively cite each
claimable required-tool evidence source. Top-level `evidence_refs` and finding
`evidence_refs` do not count as recommendation support. Host-projected gaps for
missing required recommendation citations are rendered as user-facing missing
data, not leaked as raw internal keys. Invalid recommendation
finishes go through the single repair attempt before Host result projection
runs.
For any model finish with current claimable observation refs, model action
parsing also requires at least one non-empty finding that cites those refs in
`evidence_refs`; uncited findings are repaired before Host projection.
For scenes that require recommendations and have complete required-tool
evidence, findings must also collectively cite each claimable required-tool
evidence source. Recommendations alone cannot hide the absence of analysis over
income, exposure, or close-advice evidence.
Every report ref field that is present must contain only current claimable
`obs_*` refs. A finding or recommendation that mixes a valid ref with a failed,
metadata, stale, or non-string ref is invalid and goes through the repair path;
Host projection still keeps a final sanitization fallback for non-model or
future bypass paths. If repair still fails for non-claimable refs, Engine
records the same `non-claimable evidence refs` missing-data category instead of
collapsing it into a generic invalid-action gap.
Strict model action output keeps `answer_report` present on every action: it is
`null` for `tool` actions and an object for `finish` actions.
Model action parsing rejects mixed actions: a `tool` action with an
`answer_report` object or a `finish` action with non-null `tool_name` is invalid
and goes through the normal repair path.
Model action parsing also rejects repeated tool actions in the same run. Once a
tool has produced failed or weak evidence, the model should acknowledge the
observation-level missing evidence or continue with another unattempted allowed
tool; it must not spend budget retrying the same read-only tool.
Model action parsing also repairs a finish report whose conclusion does not
start with `结论`, so conclusion-shape failures are handled before Host
admission. If the model still cannot produce a valid conclusion after repair,
the stable missing-data token is `valid conclusion`.
Host may downgrade a model finish result to `insufficient_evidence` when the
Host-projected required tool evidence is missing or not evidence-ok and the
model report did not explicitly name the missing tool evidence in
`missing_data`.
When Host downgrades a model finish result for missing claimable refs or
required tool evidence, it must replace the model conclusion with a neutral
evidence insufficiency conclusion instead of rendering an unsupported business
claim.
If the model provider is unavailable, Host/Engine may fall back to deterministic
read-only evidence collection, but it must not fall back to ordinary chat
synthesis. A provider exception disables model calls for the rest of that local
run so the same failure is not retried on every tool turn. Live provider probes
that export model-visible observations, fixture facts, prompt instructions, or
schema context require explicit operator approval.

### Minimum Implementation Shape

Phase 1 may implement fewer files than the target module layout. The required
boundary is behavior, not file count.

Required write set:

| File | Phase 1 responsibility |
|---|---|
| `src/interfaces/cli/copilot_ops.py` | Parse local CLI args into `CopilotRequest`; call Phase 1 `local_harness.py`; render `AppResult` text. |
| `src/application/copilot/contracts.py` | Minimal `CopilotRequest`, `ExecutionContract`, `SceneManifest`, `AppEvent`, `AppResult`, `AnswerReport` data shapes. |
| `src/application/copilot/local_harness.py` | Local/eval composition shell: call Service, construct the optional local model action decider from explicit JSON or explicit assistant config path, call Host facade. |
| `src/application/copilot/request_understanding.py` | Phase 1 thin request understanding: scope extraction, consumption of Service-injected scene-declared capability hints, and audit trace; no direct scene catalog import, private scene routing, safety decision, or answer logic. |
| `src/application/copilot/safety_policy.py` | Deterministic Service-owned safety decision for write-like, notification-send, broker-facing, config, release, trade-event, and position mutation requests. It emits auditable hits only; it does not select scenes or tools. |
| `src/application/copilot/service.py` | Consume request understanding and safety policy, refuse unsafe requests, match declared scenes, enforce environment/mock gate, create `ExecutionContract`. |
| `src/application/copilot/scene.py` | Declared scene catalog, capability hint declarations, allowed fixture ids, scene policy projection, required-scope checks, and mechanical `SceneManifest` assembly. |
| `src/application/copilot/eval_fixtures.py` | Deterministic eval-only fixture observations for Phase 1 eval commands. |
| `src/application/copilot/host.py` | Host lifecycle, local event list or JSONL persistence, contract-to-scene-input projection, model/tool loop orchestration, result projection, result admission. |
| `src/application/copilot/event_store.py` | Append-only per-run event log used by Host; no business routing or result judgement. |
| `src/application/copilot/result_projection.py` | Host-side observation refs, missing-data collection, and minimal `AppResult` projection from Host-supplied result context and observations. It may state observation availability, but must not import or inspect `ExecutionContract`, claim business analysis, or claim diagnosis is complete. |
| `src/application/copilot/result_admission.py` | Narrow Host result admission: status/shape and forbidden mutation-claim checks only. |
| `src/application/copilot/engine.py` | Host-internal action-turn, budget, action validation, and tool-call orchestration over Host-supplied scene input, tool interfaces, observation-event builder, and result projection callbacks. It does not import or inspect `ExecutionContract`, `AppResult`, or `result_projection.py`. |
| `src/application/copilot/agent.py` | Agent action/state types and default action decider; no direct registry access or `AppResult` construction. |
| `src/application/copilot/model_decider.py` | Structured model-action protocol wrapper with one repair attempt; no live provider dependency. |
| `src/application/copilot/tools.py` | Minimal `AgentToolView` projection for declared scene tools, read-only registry enforcement, and compact observations. |

Phase 1 must not extract a broader `result_admission.py` or verifier unless it
remains a Host admission boundary. It must not become answer rewriting,
semantic fact checking, or a renamed `answer_guard`.

Phase 1 must not create empty target-layout modules just to match the future
directory diagram.

`tools.py` must not become a second OM tool registry. Phase 1 may declare the
small `AgentToolView` records needed to map scene scope into canonical tool
payloads and compact observations, but execution authority still comes from
`agent_tool_registry.py` and `definition.is_pure_read()`. Adding a Phase 1 scene
tool requires both a scene declaration and an `AgentToolView` projection test.
The Phase 1 `TOOL_VIEWS` key set must equal the tool names declared by
`SCENE_CATALOG`; undeclared tool views are treated as a second registry and are
not allowed.
Phase 1 `AgentToolView` fields are intentionally narrow:
`required_scene_fields`, `payload_fields`, `observation_summary`,
`evidence_available`, and `missing_evidence`.
The output observation may still expose protocol fields named `summary`,
`evidence_ok`, and `missing_data`, but the tool view itself must not use those
names as an invitation to write answer summaries or fact judgements.
Scene-specific static payload values must be declared on `SceneDefinition` and
projected into `SceneManifest`; `AgentToolView` may only merge those
Host-supplied values into canonical tool payloads.

### Minimal Contracts

Phase 1 implements only these fields.

```text
CopilotRequest
- request_id
- source_entry: cli
- user_message
- explicit_scope:
    config_key
    symbol
    month
- execution_environment: local | eval
- debug_overrides:
    scene_name
    fixture_id

ExecutionContract
- contract_id
- request_id
- scene_name
- execution_environment
- input:
    user_message
    config_key
    symbol
    month
- policy:
    read_only: true
- decision_trace:
    selected_scene
    candidate_scenes
    rejected_scenes
    selection_environment
    scene_override
    requested_capabilities
    capability_sources
    scope_sources
    safety_hits
    safety_suppressed_hits
    refusal_reason

SceneDefinition
- name
- capabilities
- required_scope
- allowed_tools
- environments
- phase_readiness
- output_schema
- requires_answer_synthesis
- requires_recommendations
- answer_dimensions
- allow_mock_observations
- mock_environments
- fixture_ids
- capability_hints:
    capability
    activation_terms
    activation_reason
    required_scope
    tools

SceneManifest
- run_id
- scene_name
- execution_environment
- messages
- allowed_tools
- limits
- output_schema

AppEvent
- event_id
- run_id
- type
- timestamp
- payload
- visible_ref

AppResult
- status
- user_response: empty until UI rendering for Host-produced analysis results
- answer_report
- request_id
- contract_id
- run_id
- events
- decision_trace
```

Phase 1 `ExecutionContract.policy` is intentionally minimal: normal Service
contracts carry only `read_only=true`. Host-owned scene details such as
`allowed_tools`, `allowed_environments`, `phase_readiness`,
`allow_mock_observations`, `requires_answer_synthesis`,
`requires_recommendations`, `max_model_turns`, `max_tool_calls`,
`timeout_seconds`, `mock_environments`, and `fixture_ids` are read from
`SceneCatalog` while preparing `SceneManifest`; if an externally constructed
contract includes conflicting values for those fields, Host rejects it before
any tool call.

Do not implement full `SceneDefinition` DSL fields in Phase 1 unless one of the
two Phase 1 scenes needs them.

Phase 1 `task_guidance` is allowed to describe evidence categories and stopping
conditions. It must not contain fixed conclusions, recommendation wording,
answer-field examples, raw row shapes, account/symbol-specific judgement, or
canonical tool names. Tool names stay in declared tool policy; answer shape stays
in the output schema and Host admission checks.

`tool_attempt` events record tool name, tool_call_id, turn, and payload keys
only. They must not persist raw payload values. The matching `observation` and
`tool_failed` events carry the same tool_call_id so a Host trace can be audited
without relying on event order. Observations provide compact `value_preview`
instead of raw tool `data`. `agent_action` events record action shape only:
turn, kind, an allowed tool name when applicable, and whether a reason existed;
they must not persist raw model/tool-selection reasons. Tool-failure events
record error code only. Observation errors may expose code and whether a
message existed, but not the raw error object. Tool observation summaries must
be compact observations, not raw bottom-layer warning or error-message
transcripts; Host projection also normalizes them to a single bounded text
preview before storing events or feeding a model action request. Warnings are
represented by count/status labels unless a later scene explicitly defines a
safe warning taxonomy. Tool payload preparation failures expose only a stable
input-missing category; the internal payload builder reason is not rendered or
recorded as user-visible evidence. Engine-generated recoverable observations
use stable user-facing categories such as execution-policy rejection, missing
tool input, timeout, cancellation, and budget exhaustion; detailed control-flow
facts remain in `AppEvent` payloads.
`contract_received` records only contract-level facts such as `read_only`,
requested capabilities, scope-key presence, and fixture presence. `scene_prepared`
records Host-projected scene facts including `requires_answer_synthesis` and
`requires_recommendations`;
`final_result` includes sanitized `missing_data` so Host traces can explain why
observation-only runs remain insufficient or why Host admission rejected a
model-produced result.

`decision_trace.requested_capabilities` is a required execution input, not just
debug metadata. Host projects `SceneManifest.allowed_tools` by intersecting those
capabilities with the selected `SceneDefinition.capability_hints[*].tools`. If
the contract omits requested capabilities, names capabilities outside the scene,
or names capabilities that map to no declared tools, Host returns `not_ready`.
It must not fall back to all `SceneDefinition.allowed_tools`.

### Scenes

| Scene | Environment | Tool source | Acceptance |
|---|---|---|---|
| `operations_diagnostics` | `local`, `eval` | local: `runtime_status`, `candidate_filter_explain`, `close_advice_read`; eval fixtures `candidate_filter_diagnostics_model_ready` and `close_advice_notification_diagnostics_model_ready` | Local run can answer runtime-health, candidate-filter, and close-advice notification diagnosis questions with conclusion, attempted checks, evidence, and remaining gaps. Eval verifies the same Host-admitted model answer path on non-analysis diagnostics scenes and must label the result as eval-only. |
| `monthly_income_attribution` | `local`, `eval` | local: `analysis_catalog`, `analysis_query` view-mode over approved income views, `monthly_income_report`; eval: fixture `june_income_attribution_basic` | Local run attempts real read-only income evidence. Without a model action decider it remains `insufficient_evidence`; with a configured local model it may return an evidence-backed income attribution answer and does not require recommendations. Eval `june_income_attribution_basic` verifies both the no-model answer shape and the explicit model-action answer-quality admission path, and must label the result as eval-only. |
| `current_option_exposure` | `local`, `eval` | local: `analysis_catalog`, `analysis_query` view-mode over `open_option_exposure` and `expiration_risk_buckets`, `option_positions_read`; eval: fixture `current_option_exposure_model_ready` | Local run attempts real read-only exposure evidence. Without a model action decider it remains `insufficient_evidence`; with a configured local model it may return an evidence-backed exposure concentration answer. Eval verifies the same answer-quality admission path on a non-review synthesis scene and must label the result as eval-only. |

Normal `./om copilot run` must not use fixture observations in local mode. Phase
1 proved that the Dayu-style Service/Host contract could run read-only scenes
without the old evidence pipeline. Phase 2 extends answer-quality validation
across multiple scene types: diagnostics, income attribution, and current
exposure.
The Phase 2 success criterion is that at least one diagnostics scene and one
analysis scene can both use the same model-backed Agent loop to produce
Host-admitted answers.
`requires_answer_synthesis=true` remains the scene-level guard that prevents
Host-side observation projection from being reported as a completed analysis.
`current_option_exposure` follows the same synthesis guard for current exposure
analysis: Host may collect read-only exposure observations, but it must not
convert grouped exposure rows into the final concentration judgement by itself.

### Runtime Flow

```text
CLI
-> CopilotRequest
-> Service:
     receive explicit runtime context such as reference_year from the local/eval harness
     normalize explicit scope
     apply deterministic safety policy and refuse unsafe requests
     choose one declared scene or return clarification/refusal
     build ExecutionContract
-> Host:
     re-check scene, environment, mock gate, and tool policy against SceneCatalog
     build SceneManifest
     expose only read-only tools projected from requested scene capabilities or fixture observations
     run bounded Agent/action loop
     record AppEvents with sanitized tool-attempt payloads
     admit AppResult by shape/safety checks
-> CLI renders user_response
```

Service must not read the system date while preparing a contract. Relative month
normalization such as `6月` uses the explicit `reference_year` supplied by the
entry harness and records that source in `decision_trace.scope_sources`.

Agent/action loop requirements:

- action turns must be bounded and at least cover the projected manifest tools;
- tool calls must be bounded and at least cover the projected manifest tools;
- no provider fallback chat;
- default local execution uses deterministic tool selection unless the local
  harness can construct a model action decider from explicit JSON or an explicit
  assistant config path;
- model-backed action deciders, when introduced, get at most one
  structured-output repair attempt;
- model provider failures may fall back to deterministic read-only tool
  collection, but never to generic chat synthesis;
- model-backed action deciders reject disallowed `tool_name` before Engine tool
  execution;
- model-backed action requests include Host-computed finish conditions, including
  visible-ref requirements, cited-finding requirements for all model-produced answers,
  cited-recommendation requirements for scenes that declare them, and missing
  Host-projected allowed-tool evidence;
- model-backed finish actions for scenes that declare cited recommendations are
  repaired once before result projection if the report omits non-empty
  recommendations or cites no current claimable observation ref;
- model-facing observations include `evidence_context`, a sanitized tool-owned
  map that states the observation's time scope, record type, supported use, and
  any explicit non-use boundary such as current snapshots not being monthly
  transaction history;
- model-facing observations filter bounded row-sample facts such as
  `view[1]: ...` and `*.remaining_rows` into `facts_omitted`, while preserving
  aggregate facts, diagnostics, freshness, coverage, and evidence-context refs;
- finish conditions include `claimable_refs`, the evidence-ok observation refs
  that may support findings and recommendation basis refs;
- finish conditions include `claimable_ref_context`, a sanitized per-ref map of
  tool name, time scope, record type, supported use, and non-use boundary, so the
  model can choose refs without treating current snapshots as monthly history;
- finish conditions include `requested_scope_refs` and `current_context_refs`
  when available, so model synthesis can cite requested-period evidence for
  requested-period claims and use snapshots only as current context;
- finish conditions include `unattempted_tools_without_evidence`, the precise
  set of projected tools that still need a first tool attempt before any model
  finish can be admitted;
- finish conditions keep `allowed_tools_without_evidence` only as a compatibility
  status summary; models must not use it as a next-tool queue;
- finish conditions include `attempted_tools_without_evidence`, the subset of
  projected tools that already produced failed or weak observations and should
  be acknowledged as missing evidence instead of retried blindly;
- finish conditions also include `missing_allowed_tool_evidence`, a sanitized
  list of attempted allowed tools whose observations are failed or weak, so a
  model finish can copy explicit missing-evidence text instead of hiding the
  failed check or retrying it blindly;
- model-backed finish actions cannot complete while Host-projected allowed
  tools remain both unattempted and without evidence-ok observations; the model
  must request the missing tool call first, or Host repairs/rejects the finish
  action before result projection;
- an allowed tool with a failed or weak observation counts as attempted; it is
  handled through `missing_allowed_tool_evidence`, not through the unattempted
  tool gate;
- invalid, disallowed, or repeated tool calls become recoverable observations
  and do not execute tools;
- cancellation is checked before each Agent action;
- budget exhaustion returns partial findings or `insufficient_evidence`.

### Result Admission

Phase 1 admission checks only:

- status is one of `answered`, `needs_clarification`, `insufficient_evidence`,
  `cancelled`, `refused`, `not_ready`, `failed`;
- structured conclusion is non-empty unless status is `failed`;
- no UI-renderable result text claims a write, notification, broker, service,
  config, trade-event, or position mutation;
- Host rejects contracts whose execution environment or eval fixture id does not
  match the declared scene policy;
- Host rejects contracts that explicitly carry conflicting Host-owned scene
  policy, including mock policy and result-shaping policy such as
  `requires_answer_synthesis` and `requires_recommendations`;
- Host rejects eval/mock-environment contracts that omit the required fixture id
  before any real read-only tool can run;
- Host rejects contracts with missing, unknown, or tool-less
  `requested_capabilities`;
- Host rejects contracts missing the selected scene's `required_scope` or a
  requested capability's `required_scope`;
- contract-rejection user text is generic; the detailed rejection reason stays
  in the event log;
- answered diagnostics results mention attempted checks;
- eval-only results are marked eval-only;
- answer-quality drift such as returning grouped row dumps is covered by the
  model-facing `quality_contract`, model action parsing, narrow Host
  projection/admission checks, and eval assertions.

CLI renders `user_response` from `AnswerReport`, then outputs the answer,
report summary, ids, decision trace, and event count. Full AppEvents are
available only through an explicit debug flag. Findings and recommendations
render all surviving current-run refs from `evidence_refs` and `basis_refs`, not
only the first ref, so the user can see which observations support each claim.
Stable internal `missing_data` tokens and tool ids may remain in Report and
Trace for tests/audit, but the channel-facing `user_response` must render them
as readable Chinese gap descriptions, including required-input and
evidence-unavailable cases. Model-synthesis state is also represented by
stable tokens such as `model_synthesis_not_enabled` and
`model_synthesis_unavailable`; invalid model answer actions use
`model_synthesis_invalid_action`. Raw Chinese explanatory sentences belong only
in the renderer.

Host-side observation projection may render only the state of available
observations. It must not say the business analysis, diagnosis, option review,
or recommendation work is complete; that quality bar belongs to model-backed
Agent synthesis constrained by `quality_contract`, finish conditions, Host
admission, and evals. For scenes marked
`requires_answer_synthesis`, observation projection must not turn observation
summaries into `findings` or recommendations; it may expose attempted checks,
visible refs, and missing-data categories only.
Model-produced `AnswerReport` candidates are still Host-admitted results. A
candidate with claimable refs can answer when Host-projected required tool
observations are present and evidence-ok, or when missing required tool evidence
is explicitly named in `missing_data` and the report still contains cited
findings from evidence-ok observations. Failed or weak observations may explain
missing data, but they cannot support model findings or recommendations.
Tool observations may also be marked `claimable=false`; these observations can
prove execution or environment readiness but cannot support business findings
or recommendations. Catalog and metadata observations are useful for tool setup,
but they are not claim evidence for an option-operation judgement.
For scenes marked `requires_recommendations`, missing required tool evidence is
not waivable by model-written `missing_data`; recommendations require all
required tool observations to be evidence-ok. If a model still provides
recommendations while required evidence is missing, Host drops those
recommendations and records `recommendations blocked by missing evidence` so
the channel can explain why no advice was rendered. Host bases that block on the
raw model recommendation value before filtering, so malformed or partial
recommendations cannot be silently erased into a generic missing-recommendation
gap.
Unattempted required tools are not waivable by model-written `missing_data`;
they remain `required evidence missing` until a tool attempt produces either
evidence or a stable missing-evidence observation. Host
merges observation-level `missing_data` into the final report so a model answer
cannot hide known tool failures or weak-evidence gaps. That Host-injected
missing data is not treated as the model explicitly acknowledging missing
required evidence; required-tool waivers still come from the model report's own
`missing_data`.
Explicit missing required-tool evidence must use stable missing-evidence wording,
such as `<tool> evidence unavailable`, `<tool> required input`, or
`required evidence missing: <tool>`; merely mentioning a tool name is not enough
to waive required evidence.
Any model-produced answer without at least one surviving cited finding is
downgraded to `insufficient_evidence`; conclusion-only answers are not admitted
as complete Copilot answers.
When multiple model-report admission requirements fail, Host keeps the stable
missing-data reasons together instead of reporting only the first failed check.
`requires_answer_synthesis` still adds the stronger rule that observation
projection alone cannot complete the scene.
Scenes marked `requires_recommendations` add one more admission rule: a model
finish with findings but no surviving cited recommendation is downgraded to
`insufficient_evidence` with `cited recommendations` in `missing_data`.
When any model answer is downgraded to `insufficient_evidence`, Host keeps
supported findings and missing-data gaps but drops recommendations so the UI
does not render advice beside a failed evidence bar.

Phase 1 does not do semantic claim verification, row refs, cell refs, answer
rewriting, strategy judgement, or recommendation scoring.

### Eval Set

Current deterministic tests cover four answer-quality lanes:

| Case | Expected result |
|---|---|
| Candidate-filter question with symbol and config | `operations_diagnostics` selected; real tool projection visible; conclusion-first response. |
| Runtime-health question with config | `operations_diagnostics` selected; attempted checks listed. |
| Close-advice notification diagnosis in local model run | `operations_diagnostics` selected; configured local model synthesizes runtime notification diagnosis and close-advice observations without recommendations or send claims. |
| Candidate-filter diagnostics through eval fixture | `operations_diagnostics` selected; eval-only model action can produce an evidence-backed diagnostic answer without recommendations. |
| Close-advice notification diagnosis through eval fixture | `operations_diagnostics` selected; eval-only model action can explain missing close-advice notifications from runtime notification diagnosis and close-advice observations without recommendations. |
| Monthly income attribution in local run | `monthly_income_attribution` selected; income read-only tools called; no-model or weak evidence returns `insufficient_evidence`; configured local model may synthesize an evidence-backed attribution answer; recommendations are not required. |
| Monthly income attribution through eval fixture | `monthly_income_attribution` selected; eval-only answer-shaped result and explicit model-action admission path; no production tool calls; recommendations are not required. |
| Current option exposure through eval fixture | `current_option_exposure` selected; eval-only model action can produce an evidence-backed exposure answer without recommendations. |
| Current option exposure in local run | `current_option_exposure` selected; real read-only exposure tools called; no-model or weak evidence returns `insufficient_evidence`; configured local model may synthesize an evidence-backed exposure answer. |
| Eval scene override without matching free text | `capability_sources.source=scene_override`; fixture still runs. |
| Local scene override attempt | Rejected before Host; local `run` must use message-derived scene selection. |
| Write-like request | Refused before Host. |
| Missing config or symbol when required | `needs_clarification`. |
| Tool failure | Tool error surfaced; no generic chat fallback. |

### Phase 1 Non-Goals

- channel rollout;
- `./om assistant ...` Copilot commands;
- close-advice notification diagnosis;
- broad conversation memory;
- full SceneDefinition DSL implementation;
- full persistent session/replay/outbox;
- row/field/cell refs;
- semantic claim verification;
- write previews or any mutation from free-form text;
- shelling out to `./om-agent`.

### Phase 1 Done Criteria

Phase 1 is done only when:

- the required local and eval commands behave as described above;
- all eval cases pass deterministically;
- event records include contract, scene preparation, agent actions/tool attempts,
  observations, failures, and final result, while tool attempts expose payload
  shape rather than raw payload values;
- default old assistant free-form behavior still returns
  `NATURAL_LANGUAGE_REBUILDING`;
- `./om-agent` behavior is unchanged;
- architecture guards prevent Copilot imports of old assistant
  planner/router/evidence/verifier/runtime-loop modules.

## Later Phases

### Phase 2: Model-Backed Answer-Quality Loop

Phase 2 proves that the same generic Service/Host/Agent loop can synthesize
dependable answers across multiple read-only OM scenes. Scene-specific evidence
rules belong in scene contracts, tool evidence context, fixtures, or eval cases,
not in shared Agent, Engine, Host, ModelClient, or generic result admission
code.

Phase 2 starts with a model-backed local loop, not channel rollout. The loop is
complete only when the same runtime path can answer, refuse, or ask for scope
across multiple read-only scenes:

- diagnostics, income attribution, and current exposure are all exercised
  through the same Service/Host/Agent loop. This is the lane order for
  implementation and review: diagnostics first, income attribution second,
  current exposure third;
- at least one diagnostics scene, such as close-advice notification diagnosis,
  uses the same model action loop to return a Host-admitted answer without
  recommendations or mutation/send claims;
- at least one non-review synthesis scene, such as current option exposure or
  monthly income attribution, uses the same model action loop to return a
  Host-admitted answer with cited findings;
- architecture guards prevent shared Agent/Host/ModelClient/Engine code from
  importing old assistant runtime modules or hardcoding business scene names,
  fixture names, answer dimensions, or business thresholds;
- for every exercised scene, the Agent attempts the scene-declared read-only
  tool set before finishing, unless missing evidence or budget exhaustion is
  explicit;
- tool wrappers expose bounded observations, stable errors, omitted-fact counts,
  freshness/time-basis markers, and `evidence_available` status without exposing
  raw tool payloads;
- shared model prompts describe evidence context, requested-scope refs,
  current/latest context refs, omitted facts, and non-use boundaries in generic
  terms;
- model requests expose Host-computed finish conditions such as claimable refs,
  missing required evidence, unattempted tools, refs with omitted facts,
  non-use boundaries, and valid empty-result meanings;
- Host admission keeps the result structural and safety-focused: cited
  claimable refs, no mutation/send/trade claims, no grouped-row receipt as a
  final answer, no unsupported symbol/account/numeric top-line claims, and no
  fallback answer rewriting;
- deterministic evals prove the model-facing request includes the facts, finish
  conditions, and answer-quality contract needed for diagnostics, attribution,
  and exposure answers. Recommendation rendering is verified only in scenes that
  require recommendations;
- eval-only action JSON can exercise model-action parsing, Host admission, and
  rendering without a live provider call. Eval-only answers must disclose that
  they are fixtures, and live provider probes that export eval fixture or
  production observations remain explicit operator-approved actions;
- multi-month wording such as "May and June", "5、6月", or "5-6月" is not
  silently narrowed to the first detected month. Single-month scenes must ask
  for a specific month before calling tools.

Phase 2 shared runtime rules are intentionally scene-neutral:

- `SceneDefinition` and `CapabilityHintDefinition` own tool allowlists,
  activation hints, `requires_recommendations`, `answer_dimensions`, and
  scene-specific guidance.
- Agent, Engine, Host, ModelClient, result admission, and result projection may
  enforce the generic contract implied by those fields, but must not own income,
  exposure, or diagnostics semantics.
- Observation evidence context may declare `time_basis`, `use_as`,
  `not_evidence_for`, `empty_result_meaning`, `answer_dimensions`,
  `facts_omitted`, and similar compact markers. The shared loop forwards and
  checks those markers; it does not reinterpret them as business strategy.
- Metadata observations such as catalogs are not claimable evidence for
  findings or recommendations. Failed, stale, warning-level, or metadata-only
  observations remain weak evidence and must surface stable `missing_data`
  instead of supporting unsupported conclusions.
- Answer-quality checks are structural, not strategy advice: the model must
  make an actual judgment or explicit insufficiency statement, cite current
  claimable refs, and keep claims consistent with the cited evidence boundaries.

Phase 2 completion checklist:

| Lane | Required proof |
|---|---|
| Diagnostics | Local model loop and eval fixture both return Host-admitted diagnostic answers without recommendations or mutation/send claims; local model-loop coverage also proves missing candidate-filter trace evidence prevents unsupported filter-cause conclusions, and missing `close_advice_read` evidence prevents unsupported close-advice notification root-cause conclusions. |
| Income attribution | Local model loop and eval fixture both return Host-admitted attribution answers from income/analysis observations; local model-loop coverage also proves missing `monthly_income_report`, missing attribution view, and stale analysis evidence prevent unsupported attribution conclusions. Recommendations are not required. |
| Current exposure | Local model loop and eval fixture both return Host-admitted exposure answers from exposure/position observations; local model-loop coverage also proves stale analysis evidence and missing `option_positions_read` evidence prevent unsupported concentration conclusions. Recommendations are not required. |
| Missing/stale evidence | Tool compaction turns missing-view, warning, and stale-view diagnostics into weak evidence with stable missing-data items; model action parsing requires those gaps to be reported before accepting a final answer. This must be proven on at least one local model loop such as `current_option_exposure`. |
| Shared runtime boundary | Architecture guards show Agent, Engine, Host, Service, ModelClient, and generic admission code do not hardcode scene or fixture names. |
| Channel boundary | Channel facade stays scene-neutral; channel rollout remains gated by explicit scene allowlist and explicit usable assistant model config. |

Current shared-runtime audit status: scene names, fixture names,
answer-dimension labels, and evidence-boundary labels are forbidden from Agent,
Engine, Host, Service, ModelClient, local harness, contracts, result admission,
result projection, and generic answer-quality code unless they are part of a
scene catalog or tool-view declaration.

The broader answer-quality milestone depends on these read-only tool views:

- diagnostics reads;
- income and attribution reads;
- positions and exposure reads;
- close-advice snapshot/read surface;
- analysis catalog/query reads for approved artifacts: partially ready in the
  local analysis path through view-mode only, not model-generated SQL.

Each tool group may be added only after it has canonical metadata, compact
output behavior, recoverable error observations, read-only enforcement, and eval
cases.

Follow-on answer-quality eval cases for the broader Copilot surface:

1. Candidate rejection diagnosis.
2. Close-advice notification diagnosis.
3. Monthly income attribution.
4. Current option risk exposure concentration.
5. Missing archive/artifact handling.
6. Stale data handling.
7. Write-like request refusal.

### Phase 3: Channel Rollout

Channel rollout starts only after local evals pass and the old assistant
free-form path remains disabled or is retired.

Current implemented Phase 3 slice:

- `assistant.copilot.enabled` is a disabled-by-default channel gate.
- `assistant.copilot.channel_scenes` is a separate explicit channel scene
  allowlist; enabling the gate alone does not open any analysis scene.
- `assistant.copilot.human_review` can hold Host-backed Copilot answers at the
  channel boundary; gate/refusal responses still return directly.
- Default inbound behavior is unchanged: free-form text returns
  `NATURAL_LANGUAGE_REBUILDING`.
- When the gate is explicitly enabled, `./om assistant handle` routes
  free-form text through `src.application.copilot.channel_facade`.
- The facade enters Copilot Service with `execution_environment=channel`.
- After channel gates pass, the facade passes the same prepared
  `ExecutionContract` into Host execution; channel execution does not re-run
  request understanding or scene selection through the local CLI harness.
- No production scene is `channel_ready` in the current slice. The channel
  facade still proves the gate order and will require an allowlisted
  channel-ready scene plus explicit usable assistant model configuration before
  calling tools. That means the assistant config file must exist, contain an
  enabled model profile, and point to an API-key environment variable that is
  actually configured. Without those gates, channel free-form returns
  `not_ready` and does not call tools or use the old assistant planner/evidence
  chain.
- The channel facade admits only one Copilot run per channel conversation in the
  current service process. A second same-conversation run returns controlled
  `not_ready` with `channel_gate=channel_run_already_running` before local Host
  execution.
- Successful and failed Host-backed channel runs persist a sanitized
  `copilot_events` summary through the existing inbound audit row. The summary
  records event count, event types, observation refs, tool names, failure
  reasons, and a compact timeline. Failure reasons in that public summary are
  stable token-like values; arbitrary event `reason` or `error_code` strings
  fall back to the event type. Full raw `AppEvent` payloads are not written to
  inbound audit.
- If the prepared-contract Host entry itself raises before returning an
  `AppResult`, the channel facade returns controlled `failed` with
  `channel_error=channel_run_failed`; raw exception text is not rendered to the
  channel.
- When `assistant.copilot.human_review=true` holds a Host-backed answer, the
  channel/inbound public payload also replaces `copilot.user_response` with the
  hold notice and clears `copilot.answer_report`; only status, run identity,
  decision trace, and sanitized event summary remain available through inbound
  audit.
- Write-like free-form text is still refused by Copilot safety before scene
  execution.

Requirements:

- disabled by default;
- read-only free-form tasks only;
- channel concurrency limits: process-local same-conversation lock implemented;
  cross-process distributed locking remains a future rollout concern;
- persisted event summaries for Host-backed success and failure implemented;
- optional human-review mode: implemented as a channel reply hold for
  Host-backed runs, with sanitized audit/event summaries retained;
- rollback returns free-form text to `NATURAL_LANGUAGE_REBUILDING`.

## Final Target Summary

```text
UI
  adapts protocol

Service
  prepares OM ExecutionContract

Host
  prepares SceneManifest, runs Engine/Agent, enforces limits, records events,
  admits AppResult

Agent
  executes a generic bounded message/tool loop
```

Service defines the domain contract.
Host controls the run.
Agent executes the loop.
UI only adapts input and output.
