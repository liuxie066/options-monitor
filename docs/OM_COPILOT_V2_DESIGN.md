# OM Copilot v2 Design

This document defines the next-generation free-form OM Copilot task system after
the assistant hard reset. It is a design target, not the current runtime.

Current authority remains [OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md):
free-form natural-language execution is disabled until this design is
implemented and evaluated.

## Goal

Build a read-first Copilot that can answer open-ended OM questions with useful
judgement, not raw tool receipts.

Primary target question:

```text
分析6月的期权操作有没有不合理，需要优化的地方
```

The expected answer must include:

- a direct conclusion;
- supporting evidence from OM read-only tools;
- risk or unreasonable-operation findings;
- concrete optimization suggestions;
- explicit missing-data notes when evidence is incomplete.

It must not return only grouped rows, raw SQL output, or a generic no-tool
fallback.

## Non-Goals

- Do not restore `agent_loop.py`, `copilot.py`, `context_projection.py`,
  `context_validation.py`, `answer_guard.py`, or equivalent renamed copies.
- Do not add natural-language regex routing, channel-specific intent hacks, or
  fixed business-question templates.
- Do not use ordinary LLM chat as a fallback when tool evidence is missing.
- Do not allow write, config, notification, broker, position, or service
  mutations from free-form text.
- Do not make `./om-agent` a conversation runtime. It remains the Tool Gateway.

## Architecture

Copilot v2 is a first-class task runtime inside `./om assistant`, separate from
the deterministic command path.

```text
message
-> command / permission parser
-> if command: existing deterministic path
-> if free text: Copilot v2 task runtime
   -> task frame
   -> evidence plan
   -> guarded tool execution
   -> evidence ledger
   -> sufficiency check
   -> answer composition
   -> answer verification
   -> audit/session trace
```

The runtime has four explicit boundaries:

| Boundary | Owner | Responsibility |
|---|---|---|
| Task frame | Copilot runtime | Capture user goal, scope, constraints, answer shape, and missing slots |
| Evidence plan | Copilot runtime plus model | Choose read-only tools and analysis views from manifests and catalog |
| Execution | Existing tool layer | Validate payloads, enforce read-only policy, run tools, return structured evidence |
| Answer | Copilot runtime plus model | Compose conclusion from evidence, then verify claims against evidence refs |

The model may reason and compose. Code owns permissions, tool schemas, budgets,
evidence references, and final allow/block decisions.

## Key Difference From The Deleted Runtime

The deleted runtime tried to become many things at once: NLU, planner, context
projection, tool loop, coverage verifier, answer guard, and renderer fallback.
Copilot v2 should be smaller but more principled:

- one task runtime;
- one evidence ledger;
- one tool execution adapter;
- one answer verifier;
- one trace schema;
- no parallel tool registry;
- no duplicated business rules hidden in assistant code.

## No Hardcoded Strategy Policy

Copilot v2 must not encode option-strategy thresholds, symbol preferences,
account-specific heuristics, or fixed question recipes in assistant code.

Allowed sources for business judgement are:

- evidence returned by read-only OM tools;
- domain logic already owned by `domain/domain/` or application read tools;
- explicit user-provided constraints in the current task;
- future configuration or policy files added after a separate design decision.

If implementation appears to require a new strategy heuristic, stop and ask for
approval before adding it. The assistant may frame a recommendation as a policy
judgement only when the answer says so explicitly and does not present it as a
fact derived from evidence.

## Free-Form Entry Policy

Free-form routing should be feature-flagged and disabled by default until evals
pass.

Proposed config:

```json
{
  "assistant": {
    "copilot_v2": {
      "enabled": false,
      "read_only": true,
      "max_tool_calls": 6,
      "max_model_turns": 4
    }
  }
}
```

When disabled, current `NATURAL_LANGUAGE_REBUILDING` behavior remains unchanged.

When enabled, only read-only task execution is allowed. Write intents from free
text must return a preview-not-supported message or ask the user to use the
explicit slash command.

## Task Frame

The task frame is the core contract. It is not a business template. It captures
what any analytical question needs before tools run.

```json
{
  "schema_version": "om-copilot-task-frame-v1",
  "task_id": "generated id",
  "user_goal": "raw goal in user language",
  "task_kind": "analysis|diagnosis|lookup|comparison|unknown",
  "scope": {
    "market": "us|hk|null",
    "accounts": [],
    "symbols": [],
    "months": [],
    "date_range": null
  },
  "constraints": {
    "read_only": true,
    "allow_realtime_quote_refresh": false,
    "allow_write_preview": false
  },
  "answer_shape": {
    "requires_conclusion": true,
    "requires_recommendations": true,
    "requires_evidence": true,
    "allow_table": true
  },
  "missing_slots": []
}
```

For the target question, the frame should infer `task_kind=analysis` and
`month=2026-06` from "6月" using the request date context. It should not infer a
symbol or account unless the user said one.

## Evidence Planning

Evidence planning must be driven by tool metadata and `analysis_catalog`, not
by hardcoded question templates.

The planner receives:

- the task frame;
- a narrow read-only tool manifest;
- `analysis_catalog` view metadata when the task is analytical;
- prior evidence summaries from the same task only.

The planner emits an evidence plan:

```json
{
  "schema_version": "om-copilot-evidence-plan-v1",
  "steps": [
    {
      "tool_name": "analysis_catalog",
      "purpose": "inspect available analysis views",
      "payload": {"config_key": "us"}
    },
    {
      "tool_name": "analysis_query",
      "purpose": "summarize June option operations by account, symbol, and component",
      "payload": {
        "config_key": "us",
        "month": "2026-06",
        "sql": "select ..."
      }
    }
  ],
  "expected_evidence": [
    "monthly performance",
    "symbol attribution",
    "open exposure",
    "assignment and realized PnL"
  ]
}
```

The SQL is model-generated from the catalog, then validated by
`analysis_query`. Code must not contain a special branch for the target
question. If a fixed domain recipe appears necessary, stop and ask for explicit
approval before adding it.

## Evidence Ledger

Tool outputs are normalized into an evidence ledger. The ledger is append-only
within a task and contains only read-only observations.

```json
{
  "schema_version": "om-copilot-evidence-ledger-v1",
  "observations": [
    {
      "ref_id": "obs_1",
      "tool_name": "analysis_query",
      "ok": true,
      "purpose": "summarize June option operations",
      "row_count": 14,
      "columns": ["account", "symbol", "net_income_cny"],
      "cells": [
        {"ref": "obs_1.row_1.net_income_cny", "value": 1234.56}
      ],
      "warnings": []
    }
  ],
  "missing_data": [],
  "conflicts": []
}
```

The final answer may only cite values that appear in the evidence ledger. This
prevents unsupported judgement while still allowing qualitative synthesis.

## Sufficiency Check

Before answer composition, Copilot v2 checks whether the task has enough
evidence for the requested answer shape.

For an analysis question with recommendations, sufficiency requires:

- at least one successful analytical evidence observation;
- relevant scope coverage for requested month/account/symbol;
- enough rows or aggregates to support a conclusion;
- no unresolved tool failure that blocks the main claim.

If evidence is insufficient:

- ask a targeted clarification when scope is missing;
- run another read-only tool if the gap is recoverable within budget;
- otherwise answer with a clear evidence limitation.

It must not dump raw rows as the final answer.

## Answer Composition

The answer composer receives only:

- the task frame;
- compact evidence ledger entries;
- allowed answer rules;
- missing-data and conflict notes.

Output contract:

```json
{
  "schema_version": "om-copilot-answer-v1",
  "status": "answered|needs_clarification|insufficient_evidence|failed",
  "conclusion": "...",
  "findings": [
    {
      "claim": "...",
      "evidence_refs": ["obs_1.row_1.net_income_cny"]
    }
  ],
  "recommendations": [
    {
      "text": "...",
      "basis_refs": ["obs_2.row_3.assignment_cash"]
    }
  ],
  "missing_data": [],
  "response_text": "user visible answer"
}
```

The user-visible answer should be Chinese, concise, and judgement-oriented.
Tables are supporting evidence, not the main answer.

## Verification

Verification is not a renamed `answer_guard`. It is a narrow claim checker over
the structured answer:

- every numeric value, account, symbol, month, and status in a finding must
  have an evidence ref;
- recommendations must cite at least one supporting observation or explicitly
  state that they are a policy judgement;
- no write, notification, broker, or config action may be suggested as already
  executed;
- missing data must be surfaced when required evidence is absent.

Failed verification blocks the answer and returns an internal error in local
debug mode. In channel mode, it returns a safe insufficiency response.

## Trace

Copilot v2 needs a new trace schema. Do not reuse the old AgentLoop trace names.

```json
{
  "schema_version": "om-copilot-session-v1",
  "task_frame": {},
  "evidence_plan_revisions": [],
  "tool_calls": [],
  "evidence_ledger": {},
  "sufficiency": {},
  "answer": {},
  "verification": {},
  "final_route": "copilot_v2|clarification|insufficient_evidence"
}
```

Historical `assistant_trace` can read this schema as another session type, but
the writer should not produce old fields such as `agent_loop`, `context_projection`,
or `answer_guard`.

## First Phase Implementation Plan

### Phase 0: Design Checkpoint

Status: this document.

Deliverables:

- architecture design;
- module boundaries;
- first eval set;
- no runtime behavior change.

### Phase 1: Offline Task Runtime Prototype

Add a local-only command that does not affect inbound channels:

```bash
./om assistant copilot-run --text "分析6月的期权操作有没有不合理，需要优化的地方" --config-key us --dry-run
```

Status: implemented as a local prototype. The runtime can use an injected fake
model in tests or the configured assistant LLM locally. If the model is
unavailable, it returns a structured failure instead of a raw traceback or a
generic chat fallback.

Required modules:

- `src/application/assistant_copilot/task_frame.py`
- `src/application/assistant_copilot/evidence_plan.py`
- `src/application/assistant_copilot/evidence_ledger.py`
- `src/application/assistant_copilot/answer.py`
- `src/application/assistant_copilot/verification.py`
- `src/application/assistant_copilot/runtime.py`

The package name is intentionally new. It should not live under deleted
module names.

Phase 1 only supports read-only tools:

- `analysis_catalog`
- `analysis_query`
- `monthly_income_report`
- `option_positions_read`
- `runtime_status`

Acceptance:

- with a capable configured model, target June options question returns
  conclusion plus evidence-backed findings and recommendations;
- no write tools are visible;
- failed model synthesis returns explicit evidence limitation, not raw rows;
- trace records task frame, plan, observations, and verification result.

### Phase 2: Inbound Gated Free-Form Read-Only

Wire Copilot v2 into `PerceptionEngine` only when
`assistant.copilot_v2.enabled=true`.

Acceptance:

- slash commands remain unchanged;
- permission replies remain unchanged;
- free-form read-only analytical questions use Copilot v2;
- free-form write-like requests are refused with a slash-command hint;
- channel responses are capped and safe.

### Phase 3: Conversation Context

Add task-local conversation context only after Phase 2 is stable.

Rules:

- no global projection engine;
- no hidden historical carry;
- context is evidence-summary based and opt-in per turn;
- ambiguous follow-ups ask clarification.

### Phase 4: Preview Suggestions

Only after read-only quality is stable, Copilot may suggest an explicit slash
command for write previews. It still must not create write previews from
free-form text without a separate approval decision.

## Eval Plan

Create a new fixture set:

```text
tests/fixtures/copilot_v2_readonly_eval.jsonl
```

Each case should include:

- user text;
- config scope;
- expected task frame fields;
- required tools or allowed tool families;
- required answer elements;
- forbidden output patterns.

Initial cases:

1. June option-operation review.
2. Monthly income comparison across accounts.
3. Assigned-stock PnL question.
4. Current option exposure concentration.
5. Candidate filter diagnostic summary.
6. Runtime notification diagnosis.
7. Ambiguous month or market asks clarification.
8. Free-form write request is refused or redirected to slash command.

Quality assertions must check for conclusion and recommendations, not only tool
selection.

## Release Strategy

Do not expose Copilot v2 to remote channels until:

- Phase 1 local evals pass;
- deterministic command tests still pass;
- Tool Gateway contract tests still pass;
- at least one live read-only local probe succeeds;
- `assistant.copilot_v2.enabled` remains false by default.

Remote rollout should be a separate VERSION release after explicit approval.

## Open Decisions

1. Model provider and timeout budget for Copilot v2.
2. Whether the first prototype may call the configured LLM locally, or should
   use a recorded mock provider for deterministic evals first.
3. Whether any domain-specific task recipe is allowed. Default answer: no,
   unless explicitly approved before implementation.
4. Whether read-only realtime quote refresh is allowed in free-form questions.
   Default answer: no for Phase 1.
