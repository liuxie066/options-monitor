# Effect-First Agent Task Runtime Design

## Status

Design proposal for rebuilding the `./om assistant` task execution path. This
document replaces the earlier "single task profile" framing with an
effect-first architecture: free-form natural-language questions should enter a
task runtime, not a loose tool-choice loop.

This document does not approve production rollout, live provider probing,
notification sending, ledger writes, config writes, service operations, or
broker-facing actions. It does approve a broader local runtime refactor than the
previous minimal design.

Current authority boundaries remain:

- Architecture: `docs/OM_ASSISTANT_ARCHITECTURE.md`
- Tool-calling event model: `docs/OM_ASSISTANT_TOOL_CALLING_V2_SYSTEM_DESIGN.md`
- Implementation path: `./om assistant -> AgentLoop -> tool_execution -> agent_tool_registry`
- Tool substrate: `src/application/agent_tools/*`
- Channels such as WeChat ClawBot are transport and verification surfaces, not
  the optimization target.

## Problem

Recent fixes improved individual parts of AgentLoop: tool budgets, provider
event parsing, continuation retries, preview completeness, context projection,
coverage checks, and answer verification. The quality problem remained because
those parts do not yet form the main execution path for a free-form task.

The concrete failure case was:

```text
分析5月6月的期权操作有没有不合理，需要优化的地方
```

The visible bad answer was:

```text
已完成工具调用，但当前结果没有可渲染的文本。
```

The remote trace showed no successful business evidence. The model attempted
`analysis_query` twice with malformed provider arguments; both were denied as
`provider_protocol_error`, then the host fallback described the turn as if a
tool had completed.

That explains the symptom, but the deeper issue is architectural:

```text
natural language
-> model chooses tools opportunistically
-> host validates individual tool calls
-> evidence and coverage are checked after the fact
-> answer verification tries to repair the end of the turn
```

The desired path is:

```text
natural language
-> host derives an AgentTask
-> task profile defines evidence and completion requirements
-> evidence plan shapes the model-visible manifest
-> tool results become observations
-> coverage decides whether follow-up is needed
-> answer synthesis is checked against task completion
-> no-evidence and partial-evidence outcomes are truthful
```

The problem is not that OM lacks tools. It is that free-form input is not yet
owned by a first-class task runtime.

## Design Goal

Rebuild `./om assistant` around an Agent Task Runtime that can complete
free-form read-only analysis, diagnosis, review, explanation, and recommendation
tasks with consistent quality.

The runtime must make these decisions explicit and traceable:

- what task the user asked for;
- what scope is safe to infer;
- what evidence is required;
- which tools/views should be visible to the model;
- whether gathered evidence covers the task;
- what bounded follow-up is allowed;
- whether the final answer satisfies the task;
- what to say when evidence could not be read.

Claude Code is the reference for loop shape, not permission surface. OM should
borrow:

```text
tool call -> tool result observation -> next action -> final answer
```

OM should not borrow broad shell/filesystem/MCP/subagent authority for this
financial production system.

## Non-Goals

- Do not add shell, Python execution, arbitrary file access, arbitrary SQL
  writes, MCP, or open-ended browser access to `./om assistant`.
- Do not turn `./om-agent` into an autonomous assistant.
- Do not add a second tool registry.
- Do not reintroduce ordinary-text JSON Planner as the runtime execution
  contract.
- Do not let the model confirm/apply writes, send notifications, mutate config,
  mutate ledger/trade state, operate services, or touch broker-facing state.
- Do not route production write effects through the task runtime without the
  existing preview/confirm lifecycle.

## Runtime Shape

Introduce a first-class task runtime inside the existing AgentLoop boundary:

```text
AssistantRequest
-> TaskRuntime.derive_task
-> TaskRuntime.plan_evidence
-> AgentLoop model/tool event loop
-> TaskRuntime.observe
-> TaskRuntime.verify_coverage
-> TaskRuntime.plan_followup
-> TaskRuntime.verify_answer
-> AssistantResponse / AssistantTrace
```

This is not a new public entrypoint. It is the internal control path for
free-form natural-language turns.

### Core Types

`AgentTask` is the normalized task the Agent is trying to complete:

```text
name
goal
domain
task_mode
requested_effect
scope
task_profiles
required_evidence
required_answer
answer_shape
evidence_plan
completion_policy
failure_policy
trace
```

`TaskProfile` is the host-owned profile for a class of read-only tasks:

```text
name
domains
task_modes
trigger_terms
required_evidence
required_views
required_answer
answer_shape
completion_answer_keys
tool_name
```

`EvidencePlan` tells the host which read calls to execute before synthesis:

```text
task_name
calls
required_views
```

`TaskCompletion` records whether the task is done:

```text
coverage_status
missing_evidence
recoverable_gaps
unrecoverable_gaps
answer_shape_status
next_action
user_visible_boundary
```

## Proposed Modules

Create focused modules and wire them into existing owners:

- `src/application/assistant/task_profiles.py`
  - Owns `TaskProfile` definitions and profile selection.
  - No tool execution, no model calls, no user-visible rendering.
- `src/application/assistant/task_runtime.py`
  - Owns `AgentTask` derivation and runtime trace payloads.
  - Wraps existing `TaskContract` rather than replacing it in one step.
- `src/application/assistant/evidence_planner.py`
  - Converts selected profiles into model-visible analysis views and preferred
    evidence paths.
  - Reuses `analysis_query`, `monthly_income_report`, `option_positions_read`,
    and existing read tools.
- `src/application/assistant/task_completion.py`
  - Converts coverage and answer verification into task-level next actions.
  - Owns no-evidence failure semantics.

Update existing modules:

- `src/application/assistant/agent_loop.py`
  - Calls TaskRuntime before model tool-call payload construction.
  - Uses EvidencePlan to shape `_planner_analysis_view_selection`.
  - Uses TaskCompletion for follow-up and final response routing.
- `src/application/assistant/task_contract.py`
  - Carries task runtime metadata in the public task contract trace.
  - Keeps existing scope and requested-effect behavior.
- `src/application/assistant/coverage_verifier.py`
  - Reads task profile requirements from `AgentTask` / `TaskContract`.
  - Emits task-specific recoverable gaps.
- `src/application/assistant/answer_verifier.py`
  - Enforces task answer shapes when task profiles are selected.
- `src/application/agent_tools/analysis.py`
  - Exposes task-oriented investigation recipes through the existing catalog
    surface.
  - Keeps `analysis_query` as the read tool; no new public query executor.

## First Profile Set

The first runtime version should cover the high-frequency free-form classes
that currently produce poor answers. This is broader than the previous single
profile plan.

### 1. `option_operation_review`

Examples:

- `分析5月6月的期权操作有没有不合理，需要优化的地方`
- `复盘 5/6 月期权交易，哪里做得不好`
- `下个月期权操作怎么优化`

Required evidence:

- monthly performance summary;
- income components;
- trade/cashflow/premium/realized rows;
- current open option exposure;
- close/risk advice or explicit absence;
- strategy premise;
- replay/dry-run boundary.

Answer shape:

- overall judgement;
- unreasonable or weak operation patterns;
- optimization options;
- evidence boundary.

### 2. `monthly_income_analysis`

Examples:

- `6月收益主要来自哪里`
- `5月和6月哪个账户表现更好`
- `这个月权利金和已实现收益分别怎样`

Required evidence:

- account monthly performance;
- income components;
- attribution rows when the user asks for drivers;
- comparable same-scope account/month data for comparisons.

Answer shape:

- conclusion;
- drivers;
- comparison when requested;
- source and missing-data boundary.

### 3. `position_risk_diagnosis`

Examples:

- `现在持仓有什么风险`
- `哪些快到期`
- `哪些仓位需要关注`

Required evidence:

- open option exposure;
- expiration buckets;
- position lots;
- quote freshness when market values are used;
- close advice when action recommendations are requested.

Answer shape:

- risk summary;
- highest-priority positions;
- recommended watch/action options;
- data freshness boundary.

### 4. `candidate_strategy_diagnosis`

Examples:

- `为什么 PDD 没通过筛选`
- `泡泡玛特参数是不是太严`
- `这个策略过滤条件哪里卡住了`

Required evidence:

- candidate filter diagnostics;
- strategy config by symbol/account;
- quote freshness when quotes affect rejection;
- replay boundary for claims about changing parameters.

Answer shape:

- rejection or bottleneck summary;
- cause chain;
- adjustable parameters;
- replay boundary.

### 5. `close_advice_review`

Examples:

- `最近为什么没有 close advice`
- `哪些平仓建议值得执行`
- `close advice 健康度怎么样`

Required evidence:

- close advice snapshot or explicit missing artifact;
- open exposure;
- runtime tick status if recent generation is questioned;
- strategy config or thresholds when explaining absence.

Answer shape:

- current status;
- likely cause;
- actionable options;
- missing artifact / freshness boundary.

### 6. `runtime_health_diagnosis`

Examples:

- `今天有没有正常扫描`
- `为什么没通知`
- `线上今天有没有通过筛选的标的`

Required evidence:

- runtime tick status;
- scheduler status when scheduling is relevant;
- notification delivery/audit state when delivery is relevant;
- latest output run artifacts when production state is asked.

Answer shape:

- status;
- cause or blocker;
- next safe check/action;
- local-vs-remote evidence boundary.

## Runtime Flow

### 1. Task Derivation

Task derivation is host-owned. The model can provide signals, but the host must
derive the task from the user message, conversation context, and current date.

The host derives:

- `domain`
- `task_mode`
- `requested_effect`
- `scope`
- selected task profiles
- required evidence
- answer shape
- failure policy

For explicit month ranges such as `5月6月`, the derived scope must include both:

```text
requested_months = ["2026-05", "2026-06"]
```

### 2. Evidence Planning

Evidence planning happens before the model chooses the first tool call. The
selected task profiles expand the model-visible manifest and analysis views.

For `option_operation_review`, selected views must include:

- `account_monthly_performance`
- `account_monthly_income_components`
- `monthly_income_cashflow_rows`
- `monthly_income_realized_rows`
- `monthly_income_premium_rows`
- `trade_events`
- `open_option_exposure`
- `close_advice_snapshot`
- `strategy_config_by_symbol_account`
- `strategy_replay_read_surface`

The model can still decide exact tool calls, but it should no longer discover
the needed evidence from a generic default catalog.

### 3. Observation

Every tool result becomes a task observation. Error results are observations
too. Guard denials, schema failures, provider protocol errors, empty rows, and
missing artifacts must stay distinguishable from successful business evidence.

The runtime should classify each observation as:

- successful evidence;
- recoverable failure;
- unrecoverable missing data;
- guard denial;
- provider protocol failure;
- duplicate/no-progress call.

### 4. Coverage

Coverage is task-specific. The question is not "did a tool run?" but "does this
task have enough evidence to answer?"

Coverage must be able to say:

- complete;
- recoverable gap, with suggested tool/view/scope;
- unrecoverable gap, answer with boundary;
- unsafe scope, ask clarification;
- no evidence, return truthful failure.

### 5. Follow-Up

Follow-up is allowed when coverage emits recoverable gaps. It must be bounded by
the task profile:

- only read tools;
- only suggested tools/views;
- no duplicate gap signature;
- no scope expansion without explicit user wording.

### 6. Answer Synthesis

The final answer must be synthesized for the user. It must not expose internal
tool names, SQL, internal ids, artifact paths, or debug trace details unless the
user asks for diagnostics.

### 7. Answer Verification

Answer verification checks the selected task profile, not only generic answer
keys. If an answer lacks required judgement, drivers, options, risk boundary, or
evidence boundary, the runtime should retry synthesis from existing evidence or
fall back to a truthful boundary response.

### 8. Failure Semantics

No-evidence outcomes must be honest. If every attempted tool result is a guard
denial, provider protocol error, malformed call, or duplicate/no-progress event,
the user-visible response must not say:

```text
已完成工具调用
```

It should say that OM did not successfully read the required evidence in this
turn and cannot complete the answer from evidence.

## Evaluation Set

Create a free-form assistant eval set with 20 to 30 real prompts. Each case
should define:

- user prompt;
- expected task profile;
- required scope;
- required evidence categories;
- forbidden answer patterns;
- required answer shape;
- allowed evidence boundary;
- local/remote verification needs.

Minimum first set:

- 5 option operation review prompts;
- 4 monthly income analysis prompts;
- 4 position risk diagnosis prompts;
- 4 candidate/strategy diagnosis prompts;
- 3 close advice review prompts;
- 3 runtime health diagnosis prompts.

This eval set is required because prior local tests passed while the real
free-form experience remained weak.

## Acceptance Criteria

### Task Runtime

For representative prompts, tests must prove:

- the host derives an `AgentTask` before tool selection;
- selected profiles are visible in the task trace;
- scope includes explicit months, accounts, symbols, and config keys when
  present;
- `requested_effect` remains read-only unless the user asks for preview/write;
- TaskRuntime does not execute tools directly.

### Evidence Planning

For each first-set profile, tests must prove:

- selected analysis views include the profile's required views;
- the fallback is not only default views when a profile matches;
- read tools remain within the existing Tool Gateway registry;
- write/preview tools are not introduced for read-only questions.

### Coverage And Follow-Up

Tests must prove:

- summary-only evidence is insufficient when the profile requires detail rows;
- missing required views produce recoverable gaps with suggested tools/views;
- missing artifacts produce explicit evidence boundaries when no follow-up can
  recover them;
- duplicate follow-up requests stop the loop;
- no follow-up expands scope beyond the normalized task scope.

### Answer Completion

Tests must prove:

- profile-specific answer shape is enforced;
- valid partial-evidence answers can pass only when they state the boundary;
- raw tool receipts are rejected for user-facing free-form answers;
- answer verification can request one synthesis retry from existing evidence.

### Failure UX

Tests must prove:

- malformed provider `analysis_query` arguments do not render "已完成工具调用";
- all-guard-denial turns produce a truthful no-evidence response;
- successful fallback rendering still works when at least one read tool returns
  `ok=true`.

### End-To-End Quality

The eval set must show that representative free-form questions:

- select the expected task profile;
- gather the expected evidence or state the missing boundary;
- produce synthesized Chinese answers;
- avoid internal debug details;
- keep write effects behind preview/confirm gates.

### Remote Validation

After local tests and release preparation, verify the real remote Agent path
with a read-only live probe through a production channel such as ClawBot. This
is required because previous local gates did not prove real provider/channel
behavior.

## Implementation Slices

### Slice 1: Runtime Skeleton And Honest Failure

Create `task_profiles.py`, `task_runtime.py`, and `task_completion.py`. Wire the
runtime into AgentLoop trace and fix no-evidence failure semantics.

This slice changes the main path shape but keeps tool execution behavior the
same.

### Slice 2: Evidence Planner And Manifest Control

Create `evidence_planner.py` and route analysis view selection through selected
task profiles. The manifest must become task-shaped before the model's first
tool call.

### Slice 3: First Profile Set

Add the six first-set profiles and task derivation tests. This is the point
where free-form input starts consistently landing in task profiles.

### Slice 4: Task-Specific Coverage

Extend `CoverageVerifier` so profile evidence requirements produce recoverable
or unrecoverable gaps.

### Slice 5: Bounded Follow-Up

Make AgentLoop follow-up use task coverage gaps rather than only generic
retries. Stop duplicate/no-progress loops.

### Slice 6: Task Answer Completion

Extend `AnswerVerifier` and answer routing so final answers must satisfy the
selected task profile or explicitly state evidence boundaries.

### Slice 7: Free-Form Eval Set

Add the 20 to 30 prompt eval set and make it part of the assistant validation
bundle.

### Slice 8: Release And Live Probe

Prepare the VERSION-driven release and verify the remote read-only Agent path.

## Risks And Controls

- Risk: task profiles become a brittle keyword router.
  Control: profiles select evidence requirements and answer shape, not final
  business conclusions. The model still composes from observations.

- Risk: the new runtime duplicates existing `TaskContract`.
  Control: `AgentTask` wraps `TaskContract` first. Replace only when tests prove
  the old field is no longer needed.

- Risk: larger manifests increase provider argument failures.
  Control: profiles add curated views only. `analysis_catalog` remains available
  when fields are unknown.

- Risk: the eval set overfits.
  Control: include phrasing variants, context-carry cases, explicit scope
  switches, and missing-evidence cases.

- Risk: remote channel behavior differs from local tests.
  Control: remote read-only live probe is a release gate for this work.

## Decision

Proceed with an effect-first Agent Task Runtime rebuild:

- first-class `AgentTask`;
- first-class `TaskProfile`;
- evidence planning before tool choice;
- task-specific coverage and follow-up;
- task-specific answer completion;
- honest no-evidence failure semantics;
- six high-frequency profile classes in the first version;
- no broad Claude Code permission surface.
