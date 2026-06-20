# OM Assistant Context Implementation Plan

> Concrete rollout plan for replacing case-specific follow-up handling with a
> general conversation context projection and validation path.

## Status

- Target surface: `./om assistant` and internal `AgentLoop`.
- Design authority:
  [OM_ASSISTANT_CONVERSATION_CONTEXT_DESIGN.md](OM_ASSISTANT_CONVERSATION_CONTEXT_DESIGN.md).
- Input contract:
  [OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md](OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md).
- Guard contract:
  [OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md](OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md).
- Eval plan:
  [OM_ASSISTANT_CONTEXT_EVAL_PLAN.md](OM_ASSISTANT_CONTEXT_EVAL_PLAN.md).

This document now records the completed context migration and the rollout order
that got it there. Do not treat the historical slices below as open work unless
current source or evals show a regression.

Current source has completed the context path through projection, eval split,
planner `context_use`, validator enforcement, planner input cutover, legacy
resolver cleanup, and scenario regression coverage. New assistant intelligence
work should move to capability selection, evidence-loop quality, or answer
verification docs instead of extending this context migration plan.

## Success Standard

This work is successful only when multi-turn context behaves as a general
assistant capability:

- The planner receives a bounded `ContextProjection`, not ad hoc active-frame
  hints as the main interface.
- The planner declares context use through `context_use`.
- A deterministic validator checks references and inherited slots before tool
  execution.
- Ambiguous follow-ups ask clarification instead of guessing from keywords.
- Adding a new follow-up domain usually adds fixtures or tool metadata, not a
  new branch in `agent_loop.py`.
- The historical `net_income` cases remain regression fixtures, not design
  drivers.

## Retired Hardcoded Surfaces

The migration target was to remove planner authority from business-specific
follow-up branches. These surfaces are historical cleanup targets, not current
architecture authorities:

| Historical surface | Current state |
|---|---|
| `active_frame` / `followup_resolution` planner payloads | Planner input uses `ContextProjection`; runtime tests assert these legacy authority fields are absent. |
| `_conversation_followup_resolution`, `_is_short_metric_followup`, `_validate_plan_against_conversation_frame`, `PLAN_CONTEXT_DRIFT` | Removed as source-level planner authority; keep their names only in historical docs. |
| named `net_income` / Pop Mart prompt examples | Replaced by general context-use principles and regression fixtures. |
| `metric_glossary.py` namespace branches | Removed from the current assistant planner path. |
| legacy `planner_context` eval | Retained only as a historical/default report; current validation uses `projection`, `validation`, and `scenarios` lanes. |

## Current Code Path

The current path is:

```text
AssistantRequest
  -> build_conversation_context
  -> build_context_projection
  -> planner input
  -> planner context_use declaration
  -> validate_context_use
  -> existing action policy / tool execution
  -> evidence / coverage / answer verification
  -> assistant trace
```

`build_conversation_context` may continue to collect raw recent audit/session
state. It is not the business resolver. Its output becomes input to
`build_context_projection`.

The slices below are preserved as implementation history. They should not be
expanded for new capability work.

## Slice 1: Projection Builder In Shadow Mode (Complete)

Goal: create a generic, deterministic planner-facing projection without
changing runtime behavior.

Primary files:

- Add `src/application/assistant/context_projection.py`.
- Add focused tests in `tests/test_assistant_context_projection.py`.
- Add fixture file `tests/fixtures/assistant_context_projection.jsonl`.
- Extend `context_trace` or AgentSession trace to include compact
  `context_projection` when available.

Implementation notes:

- Define typed dict/dataclass-like helpers for:
  - `ConversationEvent`
  - `ContextProjection`
  - evidence refs
  - recent turns
  - recent successful tools
  - open gaps
- Derive projection from the existing conversation context, recent
  `agent_sessions`, recent audit rows, pending operations, and user profile.
- Use existing safe payload logic where possible. Do not introduce a second
  tool registry.
- Use tool metadata from `tool_bindings.py`, `agent_tool_registry.py`, and
  analysis view metadata in `agent_tools/analysis.py` when a tool needs safe
  planner-visible summaries.
- Replace large or unsafe tool results with `evidence_refs` and `data_shape`.
- Set `budget.truncated` when omitted turns could matter.

Non-goals:

- Do not change planner prompts yet.
- Do not enforce validation yet.
- Do not remove `active_frame`.

Tests:

```bash
python3 -m pytest tests/test_assistant_context_projection.py
```

Exit criteria:

- Projection eval fixtures cover recent successful tool, failed tool, pending
  operation, open evidence gap, truncation, and sanitization.
- No planner behavior changes.

Historical rollback:

- Stop calling `build_context_projection`; the old context path remains intact.

## Slice 2: Context Eval Harness Split (Complete)

Goal: make eval layers explicit before behavior changes.

Primary files:

- Update `src/application/assistant/context_eval.py`.
- Update `src/interfaces/cli/assistant_ops.py`.
- Add `tests/test_assistant_context_eval.py` or extend the existing assistant
  eval tests narrowly.

Implementation notes:

- Support explicit modes:
  - `projection`
  - `validation`
  - `scenarios`
  - legacy `planner_context` as a historical report
- Keep the current `./om assistant eval-context` command name stable, but allow:

```bash
./om assistant eval-context --mode projection
./om assistant eval-context --mode validation
./om assistant eval-context --mode scenarios
```

- Move fixtures that assert active-frame behavior into a legacy group or rewrite
  them to assert projection fields.

Tests:

```bash
python3 -m pytest tests/test_assistant_context_eval.py
./om assistant eval-context --mode projection --format json
```

Exit criteria:

- Projection and validation fixtures run without LLM calls.
- The current legacy eval still works until cutover.

Historical rollback:

- Keep the default command path pointed at the legacy `planner_context` mode.

## Slice 3: Event Context Declaration In Shadow Mode (Complete)

Goal: let model planning output declare context use without requiring it yet.

Primary files:

- `src/application/assistant/agent_loop.py`
- event-plan normalization/context tests in `tests/test_assistant_runtime.py`
  or a smaller model-event context test file.

Implementation notes:

- Event-native planning results accept optional `context_use`.
- Normalize missing `context_use` to:

```json
{
  "schema_version": "om-planner-context-use-v1",
  "mode": "none",
  "referenced_turn_ids": [],
  "referenced_evidence_refs": [],
  "inherited_slots": {},
  "current_message_slots": {},
  "override_slots": {},
  "requires_clarification": false,
  "clarification_question": null
}
```

- Add planner instructions as principles:
  - current message wins,
  - recent turns are hints,
  - context use must be declared,
  - ambiguous context means clarification.
- Remove named case examples from the prompt only after shadow trace proves
  `context_use` is being populated reliably.
- Trace `planner_context_use` but do not block execution yet.

Tests:

```bash
python3 -m pytest tests/test_assistant_runtime.py -k "planner"
```

Exit criteria:

- Existing planner outputs still parse.
- New planner outputs with `context_use` round-trip into trace.
- Missing `context_use` is observable as `mode=none` rather than a crash.

Historical rollback:

- Ignore the `context_use` field and keep old planning behavior.

## Slice 4: Deterministic Context Validator In Shadow Mode (Complete)

Goal: implement generic structural validation with no business-specific intent
branches.

Primary files:

- Add `src/application/assistant/context_validation.py`.
- Add `tests/test_assistant_context_validation.py`.
- Add fixture file `tests/fixtures/assistant_context_validation.jsonl`.

Implementation notes:

- Implement checks from
  [OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md](OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md):
  - reference existence,
  - slot source,
  - current-message-wins,
  - ambiguity,
  - tool fit,
  - clarification route.
- The validator may inspect projection shape, planner-visible tool manifest,
  plan steps, and declared safe slots.
- The validator must not infer business meaning by keyword.
- Initially record `context_validation` trace as `passed`, `blocked`, or
  `ask_clarification`, but do not block live execution.

Tests:

```bash
python3 -m pytest tests/test_assistant_context_validation.py
./om assistant eval-context --mode validation --format json
```

Exit criteria:

- Validation fixtures cover referenced carry, unreferenced carry, explicit
  override, hidden injected args, ambiguity, truncation, and clarification with
  tool steps.
- Validator has no references to `net_income`, candidate-specific rules, or
  account-income-specific rules.

Historical rollback:

- Keep validator output trace-only.

## Slice 5: Enforce Validator Before Tool Execution (Complete)

Goal: make invalid context use fail closed before any tool call.

Primary files:

- `src/application/assistant/agent_loop.py`
- `src/application/assistant/context_validation.py`
- assistant runtime tests around clarification and blocked plans.

Implementation notes:

- Call `validate_context_use` after plan normalization and before existing
  policy/tool execution checks.
- For `status=passed`, continue to existing policy and tool validation.
- For `status=ask_clarification`, return a clarification response and no tool
  steps.
- For `status=blocked`, request planner repair when safe; otherwise fail with a
  user-facing clarification boundary.
- Preserve existing write/preview, injected-argument, coverage, and answer guard
  behavior.

Tests:

```bash
python3 -m pytest tests/test_assistant_context_validation.py tests/test_assistant_runtime.py -k "context"
```

Exit criteria:

- Invalid inherited account/symbol/month does not execute a tool.
- Ambiguous multi-topic follow-up asks clarification.
- Existing non-context plans still execute.

Historical rollback:

- Switch validator back to trace-only. Existing old guards remain available
  until Slice 7 completes.

## Slice 6: Planner Input Cutover To Projection (Complete)

Goal: replace the planner-facing `active_frame` / `followup_resolution` payload
with `ContextProjection`.

Primary files:

- `src/application/assistant/agent_loop.py`
- `src/application/assistant/context_projection.py`
- `src/application/assistant/perception.py`
- `src/application/assistant/llm_reply.py` if direct LLM reply context still
  needs projected context.

Implementation notes:

- Change `_planner_input_payload` to include:
  - `current_user_message`
  - `context_projection`
  - planner-visible policy
  - recent turn and evidence refs
- Do not include legacy `followup_resolution` as planner authority.
- Keep legacy fields under a clearly named historical trace only if tests
  still need comparison.
- Replace active-frame analysis view selection with projection-informed
  selection:
  - current message match first,
  - open evidence gap suggested views,
  - referenced recent evidence refs,
  - bounded fallback manifest.
- Remove the named `net_income` / Pop Mart prompt example.

Tests:

```bash
python3 -m pytest tests/test_assistant_context_projection.py tests/test_assistant_context_validation.py
python3 -m pytest tests/test_assistant_runtime.py -k "context or planner"
./om assistant eval-context --mode scenarios --format json
```

Exit criteria:

- Planner input trace shows projection, not `active_frame` as the main context
  interface.
- Planner prompt contains general principles and no named business follow-up
  examples.
- Historical `net_income` fixtures pass through projection and validation, not
  through `_conversation_followup_resolution`.

Historical rollback:

- Restore old planner input payload while keeping projection and validation
  modules unused.

## Slice 7: Remove Legacy Hardcoded Resolver (Complete)

Goal: delete temporary scaffolding after the new path has coverage.

Primary files:

- `src/application/assistant/conversation_context.py`
- `src/application/assistant/agent_loop.py`
- `src/application/assistant/metric_glossary.py`
- `src/application/assistant/context_eval.py`
- `tests/fixtures/assistant_agent_eval.jsonl`

Removal targets:

- `_conversation_followup_resolution`
- `_is_short_metric_followup`
- `_question_explicitly_requests_account_income`
- `_question_explicitly_requests_candidate_metrics`
- `_validate_plan_against_conversation_frame`
- `PLAN_CONTEXT_DRIFT`
- `_BUSINESS_READ_TOOL_PRIORITY` as context authority
- `_read_semantics` / `_infer_analysis_namespace` as follow-up resolver logic
- prompt-level named case example

Important distinction:

- Do not remove useful tool semantics. Move reusable metadata to tool contracts
  or analysis view metadata when it helps projection describe evidence safely.
- Do remove business-case branching whose purpose is to guess what a follow-up
  means.

Tests:

```bash
python3 -m pytest tests/test_assistant_context_projection.py tests/test_assistant_context_validation.py tests/test_assistant_context_eval.py
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py
```

Exit criteria:

- No `rg` hits for legacy resolver names except changelog/docs migration notes.
- Context evals pass through projection/validation/scenario modes.
- Legacy `planner_context` fixture path is either removed or marked historical.

Historical rollback:

- Revert this cleanup slice only. Earlier projection/validation slices should
  remain usable.

## Slice 8: Scenario Regression Expansion (Complete)

Goal: lock the general capability into long-term regression coverage.

Primary files:

- `tests/fixtures/assistant_context_scenarios.jsonl`
- `tests/test_assistant_context_eval.py`
- possibly existing `tests/test_assistant_agent_eval.py` for end-to-end answer
  assertions.

Required scenario families:

| Family | Required behavior |
|---|---|
| metric follow-up | carry only through visible turn/evidence refs |
| candidate follow-up | explain prior candidate context without metric-specific branches |
| income follow-up | same-scope comparison and breakdown use declared context |
| position follow-up | assigned-stock or option position scope is preserved safely |
| runtime follow-up | health/runtime follow-up uses prior diagnostic evidence refs |
| config follow-up | symbol config scope is explicit |
| explicit switch | current account/symbol/month wins |
| multi-topic ambiguity | asks clarification |
| evidence gap carry | uses open gap hints |
| no context | standalone request does not inherit old scope |

Exit criteria:

- Adding a new family requires a fixture and, at most, tool metadata updates.
- Projection and validator core remain unchanged for ordinary new domains.

## Completed Cutover Order

Implemented order:

1. Projection builder and projection eval.
2. Eval harness split.
3. Planner `context_use` schema in shadow mode.
4. Validator in shadow mode.
5. Validator enforcement.
6. Planner input cutover.
7. Legacy resolver cleanup.
8. Scenario expansion.

Historical guardrail: Slice 5, Slice 6, and Slice 7 were intentionally kept as
separate concepts because combining validator enforcement, planner-input
cutover, and legacy cleanup makes regressions hard to localize.

## Current Quality Gates

Focused context checks:

```bash
git diff --check
python3 -m pytest tests/test_assistant_context_projection.py
python3 -m pytest tests/test_assistant_context_validation.py
python3 -m pytest tests/test_assistant_context_eval.py
```

Broader assistant checks:

```bash
python3 -m pytest tests/test_assistant_runtime.py tests/test_assistant_evidence_session.py tests/test_assistant_agent_eval.py
./om assistant eval-context --mode projection --format json
./om assistant eval-context --mode validation --format json
./om assistant eval-context --mode scenarios --format json
```

If a slice touches CLI argument shape, also run the relevant CLI smoke command
with `--format json`.

## Safety Boundaries

This implementation must not:

- mutate `config.yaml`, `config.us.json`, or `config.hk.json`,
- send notifications,
- write Feishu, option-position state, trade events, or broker-facing state,
- add shell/Python execution authority to the assistant,
- broaden remote message access to the full Tool Gateway manifest,
- introduce a parallel tool registry.

All new context behavior must remain inside the `./om assistant -> AgentLoop ->
tool_execution -> agent_tool_registry` authority path.
