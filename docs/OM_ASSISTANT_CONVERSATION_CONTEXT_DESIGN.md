# OM Assistant Conversation Context Design

> Current design note for `./om assistant` multi-turn conversation quality.
> This document is about conversation context, not wider tool capability,
> strategy research, or a new public Agent surface.

## Status

- Target surface: `./om assistant` and its internal `AgentLoop`.
- Authority documents:
  - [OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md) owns naming
    and entrypoint boundaries.
  - [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md) owns capability
    and LLM authority boundaries.
- Detailed contracts:
  - [OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md](OM_ASSISTANT_CONTEXT_PROJECTION_CONTRACT.md)
  - [OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md](OM_ASSISTANT_CONTEXT_VALIDATION_CONTRACT.md)
  - [OM_ASSISTANT_CONTEXT_EVAL_PLAN.md](OM_ASSISTANT_CONTEXT_EVAL_PLAN.md)
- Implementation plan:
  [OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md](OM_ASSISTANT_CONTEXT_IMPLEMENTATION_PLAN.md)
- Current implementation state: projection, validation, planner input cutover,
  legacy resolver cleanup, and scenario regression coverage are complete.

## Problem

The assistant needs better multi-turn continuity, but the previous
`net_income` follow-up slice is not a general capability improvement. It made a
single ambiguous metric case work by adding business-specific logic around
active frames, metric namespaces, and planner drift. That pattern is too narrow:
each new follow-up shape would invite another keyword branch.

The implemented capability is more general:

```text
conversation transcript
  -> model-facing context projection
  -> planner semantic judgement
  -> deterministic context validation
  -> bounded tool execution
```

The code should not try to decide every business follow-up through if/else
rules. It should prepare a reliable context view and verify that the planner's
use of that context is explicit, bounded, and safe.

## Claude Code Reference

The local `Claude-Code` source does not implement an application-layer business
resolver that outputs `carry`, `refine`, `override`, or `ambiguous`.

Its relevant pattern is lower-level:

- `QueryEngine` owns conversation state across turns.
- `query.ts` builds a model-facing view from message history.
- history is projected from the latest compact boundary.
- tool results are budgeted and may be replaced.
- snip, microcompact, context collapse, and auto compact control context size.
- user and system context are layered into the model request as hints.

The model sees ordered conversation history and reasons over it. Code owns
projection, budget, compaction, and prompt assembly.

OM should copy that boundary, not the exact implementation. OM differs because
its planner can trigger deterministic financial and runtime tools. Therefore OM
needs an extra validation layer after planning.

## Design Principle

Do not build a hardcoded follow-up resolver.

Instead:

- preserve structured conversation events,
- project recent relevant context into a bounded planner input,
- let the planner perform natural-language semantic continuity,
- require the planner to declare any context reference it relies on,
- validate those references deterministically before execution.

This keeps the general conversation mechanism separate from business-specific
metrics, symbols, tools, and report shapes.

## Current Pipeline

```text
Inbound message
  -> assistant runtime/router
  -> ConversationEvent append
  -> ContextProjection build
  -> Planner
     -> TaskContract
     -> ToolPlan
     -> ContextUse declarations
  -> ContextValidator
  -> Policy / ActionPolicy
  -> ToolExecution
  -> EvidenceBundle
  -> Coverage / follow-up / clarification
  -> Composer / AnswerVerifier
  -> Final response + AgentSession trace
```

The important boundary is where context logic lives:

| Concern | Owner |
|---|---|
| durable raw audit/session trace | existing inbound audit and `agent_sessions` |
| model-facing recent context view | `ContextProjection` |
| semantic use of "continue", "this", "that metric" | planner |
| legality of inherited context | `ContextValidator` |
| evidence sufficiency | existing coverage verifier |
| factual answer claims | existing answer verifier / guard |

## Context Projection

`ContextProjection` is the planner-facing view. It is not a domain resolver.
It should include enough recent, structured context for the planner to decide
whether the current message naturally refers to prior work.

Projection includes:

- current user message,
- recent turn summaries,
- recent successful tool calls,
- known evidence references,
- open evidence gaps and clarification state,
- bounded safe tool payload excerpts,
- budget and truncation metadata,
- policy reminders such as current-message-wins and context-is-hint.

Projection excludes:

- full raw tool outputs,
- production paths, secrets, hostnames, or injected config paths,
- arbitrary SQL result dumps when an evidence reference is enough,
- case-specific instructions such as "after a named symbol metric, force a
  particular business namespace".

See the projection contract for schema details.

## Planner Context Use

The planner may use prior context, but it must make the use explicit. A plan
that carries previous context should identify:

- which prior turn or evidence reference it is using,
- which scope slots are inherited,
- which scope slots come from the current message,
- whether the current message overrides prior scope,
- whether ambiguity requires a clarification instead of a tool call.

The planner prompt should contain principles, not case examples:

```text
Use current_user_message as authority.
Use recent_turns only when the message contains continuation, anaphora, or
implicit reference.
If multiple recent topics fit, ask clarification.
Do not silently switch source, metric, entity, account, month, or market scope.
Declare any context reference used by the plan.
```

## Deterministic Validation

`ContextValidator` does not understand business meaning in natural language. It
checks structural safety:

- context references point to real projected turns or evidence refs,
- inherited slots are allowed by the projection,
- explicit current-message slots are not overwritten by previous payloads,
- planner tools fit the declared context refs,
- the plan asks clarification when projection is truncated or multiple context
  candidates are plausible,
- no hidden system/path/config/audit fields are copied from context into tool
  arguments.

Validator failures should block execution and ask for clarification, or request
planner repair if the route is safe.

## Implementation State

Current context code should be treated as the implemented projection and
validation path, not as a future migration. Historical surfaces may still appear
in old fixtures or audit artifacts, but they are not planner authority.

Current state:

- `net_income` remains a regression case, not the architecture center.
- `_conversation_followup_resolution` and active-frame style business
  resolution are historical, not current planner authority.
- Planner input uses `ContextProjection` plus declared `context_use`.
- `ContextValidator` checks context references before execution.
- Scenario fixtures cover representative follow-up behavior.

The goal is not to delete every domain hint. Tool and evidence metadata remain
valuable. The goal is to stop using hardcoded business follow-up recognition as
the conversation engine.

## Non-Goals

- No new public entrypoint.
- No broad shell, Python, service, config-write, notification, Feishu, ledger,
  or broker-facing authority.
- No second tool registry.
- No business-specific `account_income_compare` style shortcut as the main
  solution.
- No planner-visible private implementation paths.
- No attempt to replicate Claude Code's compact machinery in full unless future
  context pressure proves it is needed.

## Acceptance Criteria

This design counts as general conversation context work only if:

- adding a new follow-up domain does not require changing the main context
  projection or validator flow,
- planner prompts do not include named case examples,
- evals assert projection and validation behavior, not only final answer text,
- validator checks context use through declared references and slots,
- `net_income` remains one fixture among many, not the architecture center.
