# OM Assistant Context Validation Contract

> Deterministic validation for Copilot use of multi-turn context.

## Status

This contract defines the validation layer between Copilot evidence plans and tool
execution. It does not replace policy, coverage verification, answer guards, or
tool output contracts.

## Purpose

Copilot may decide that the current message refers to prior context. Because
Copilot can trigger deterministic tools, OM needs a structural guard before
execution.

The validator checks whether the plan's declared context use is legal. It does
not interpret business language itself. In the current Copilot path, evidence
call `arguments` are the current turn's explicit execution intent; the validator
must not reclassify normalized explicit arguments as inherited context merely
because their serialized value does not appear verbatim in the current user
message.

## ContextUse Declaration

Copilot task/evidence planning should include a `context_use` object:

```json
{
  "context_use": {
    "schema_version": "om-copilot-context-use-v1",
    "mode": "none",
    "referenced_turn_ids": [],
    "referenced_evidence_refs": [],
    "inherited_slots": {},
    "current_message_slots": {},
    "override_slots": {},
    "requires_clarification": false,
    "clarification_question": null,
    "reason": "current message is self-contained"
  }
}
```

Allowed `mode` values:

| Mode | Meaning |
|---|---|
| `none` | current message is self-contained |
| `carry` | current message intentionally carries prior context |
| `refine` | current message narrows or asks details inside prior context |
| `override` | current message explicitly replaces prior scope |
| `ambiguous` | Copilot cannot safely choose one context |

These declarations are not resolver outputs. The validator only checks whether
the declaration is consistent with the projection and plan.

`context_use` may be host-derived when the current turn is a deterministic
preview or command adapter rather than a full Copilot evidence plan. That
derivation is only for tracing and validating actual inherited context. It must
not turn current-turn normalized arguments into inherited slots simply because
host text extraction did not produce the same serialized value.

`safe_slots` in `ContextProjection` are projection and audit metadata. They are
not a second natural-language parser. `context_use.inherited_slots` should be
populated only for scope or filter values intentionally carried from referenced
prior context.

## Validation Inputs

```text
current_user_message
ContextProjection
CopilotEvidencePlan / deterministic preview adapter
TaskContract
ContextUse
Copilot-visible tool manifest
```

The validator must not read hidden runtime state to rescue an invalid context
declaration. If the projection omitted a turn due to budget, Copilot must
not rely on it.

## Core Checks

### 1. Reference Existence

Every `referenced_turn_id` must exist in `recent_turns`.

Every `referenced_evidence_ref` must exist in `available_evidence_refs` or
inside a referenced recent turn.

Failure:

```json
{
  "status": "blocked",
  "code": "CONTEXT_REF_NOT_FOUND"
}
```

### 2. Slot Source

Every tool argument that is treated as inherited context must be declared in
`context_use.inherited_slots`.

Every declared inherited slot must be present in the referenced turn or evidence
ref.

Tool arguments that belong to the current model turn do not become inherited
slots only because the user used a synonym, alias, Chinese wording, or spacing
that the model normalized into an internal enum. For example, a current-message
candidate diagnostic for `0700.HK` may include `function="sell_put"` while the
user wrote `sell put`; if the required current scope is present, `function`
remains a current tool argument unless the model explicitly carries it from a
referenced prior turn.

Failure:

```json
{
  "status": "blocked",
  "code": "CONTEXT_SLOT_NOT_AVAILABLE"
}
```

### 3. Current Message Wins

If the current message explicitly supplies a safe slot, the plan must not use a
different inherited value for the same slot unless it declares `override` and
the current-message slot is the value being used.

Examples:

- current says `sy`, inherited says `lx`: using `lx` is invalid.
- current says `2026-06`, inherited says `2026-05`: using `2026-05` is invalid.
- current gives a new symbol: previous symbol cannot silently persist.

Failure:

```json
{
  "status": "blocked",
  "code": "CONTEXT_CURRENT_MESSAGE_OVERRIDDEN"
}
```

### 4. Ambiguity

If `ContextProjection.budget.truncated=true` and the plan uses context, the
planner must either reference visible evidence or ask clarification.

If multiple recent turns expose overlapping safe slots and the planner declares
`carry` without a reference, the plan is invalid.

Failure:

```json
{
  "status": "ask_clarification",
  "code": "CONTEXT_AMBIGUOUS"
}
```

### 5. Tool Compatibility

The validator checks structural fit:

- referenced evidence source must be visible to the planner,
- planned tool must be in the planner manifest,
- planned tool arguments must not contain hidden injected fields,
- inherited slots must be among the tool's allowed planner arguments or known
  safe scope slots.

The validator should not encode business-specific mappings such as "net income
belongs to candidate metrics". It may check whether the planner declared a
source ref and whether the chosen tool is allowed by manifest metadata.

Failure:

```json
{
  "status": "blocked",
  "code": "CONTEXT_TOOL_MISMATCH"
}
```

### 6. Clarification Route

If `context_use.mode="ambiguous"` or
`context_use.requires_clarification=true`, the plan must not include executable
tool steps. It should produce a clarification response.

Failure:

```json
{
  "status": "blocked",
  "code": "CONTEXT_CLARIFICATION_WITH_TOOL_STEPS"
}
```

## Validation Output

```json
{
  "schema_version": "om-context-validation-v1",
  "status": "passed",
  "code": "ok",
  "context_use_mode": "refine",
  "referenced_turn_ids": ["turn-20260618-0001"],
  "referenced_evidence_refs": ["ev_001"],
  "validated_slots": {
    "inherited": {
      "symbol": ["9992.HK"]
    },
    "current_message": {}
  },
  "warnings": []
}
```

Allowed `status` values:

| Status | Meaning |
|---|---|
| `passed` | safe to continue to normal policy/tool validation |
| `blocked` | planner repair or deterministic fallback required |
| `ask_clarification` | do not execute tools; ask user to disambiguate |

## Validator Boundary

The validator may:

- check references and slot inheritance,
- enforce current-message-wins,
- enforce no hidden injected arguments,
- block plans that use context without visible refs,
- require clarification when ambiguity is declared or structurally obvious.

The validator must not:

- decide business intent by keyword,
- compute accounting formulas,
- infer missing facts,
- choose tools for the planner,
- rewrite user intent,
- broaden execution permissions,
- bypass existing policy, coverage, or answer verification.

## Relationship To Existing Guards

| Existing layer | Still owns |
|---|---|
| planner validation | plan schema, allowed tool count, read vs preview composition |
| action policy | permission and preview-write gate |
| tool execution | deterministic tool handler and injected system args |
| coverage verifier | evidence completeness and recoverable gaps |
| answer verifier / guard | factual claim verification |
| context validator | safe use of prior conversation context |

## Compatibility Notes

Older audit/session artifacts or historical fixtures may still describe behavior
that originally came from active-frame logic. If that context is needed, it must
be represented as a projected recent turn or evidence ref. The validator
validates the planner's explicit reference to that projection and must not call
legacy `_conversation_followup_resolution` as an authority.

## Acceptance Criteria

- A model event that inherits context without a visible turn or evidence ref is
  blocked.
- An event plan that silently reuses old account, symbol, month, or operation scope
  against explicit current-message slots is blocked.
- Ambiguous multi-topic follow-ups ask clarification instead of executing a
  guessed tool.
- The validator has no named business case branches.
