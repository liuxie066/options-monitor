# OM Assistant Memory Design

This document defines the current bounded memory proposal surface for
`./om assistant`. It is not a free-form task memory system.

## Goal

OM Assistant helps the operator use OM: diagnose runtime behavior, explain
evidence, operate through safety gates, and collaborate on Sell Put / Covered
Call / Yield Enhancement parameter tuning.

Memory should therefore remember how the operator uses OM and prefers to tune
parameters. It should not become a personal profile database, a market fact
store, or a hidden permission source.

## Current Scope

Implemented scope:

- keep the markdown memory file format and loader available for explicit
  operator commands/tests,
- manage explicit memory proposals under `assistant_memory/proposals/`,
- suggest memory proposals from explicit remember/preference/correction text,
- create one proposal sidecar from explicit remember/preference/correction text
  during `./om assistant handle`,
- accept or reject proposals through deterministic CLI commands.

Not implemented yet:

- model-visible memory projection,
- automatic memory writes,
- automatic background memory extraction,
- session-memory summarization,
- compaction or embedding retrieval,
- any memory-driven config, ledger, notification, or broker mutation.

## Names

Use existing assistant naming:

| Name | Meaning |
|---|---|
| `assistant_memory` | read-only long-term memory source |
| `assistant_trace` | audit surface for explicit proposal commands |
| `session_memory` | future short-term summarization surface, not implemented |
| `EvidenceBundle` | tool evidence surface; memory is not evidence |

Do not introduce product terms for memory journals or decision logs unless a
specific later feature needs them.

## Memory Types

Only these types are accepted:

- `collaboration_preference`
- `om_usage_preference`
- `parameter_tuning_preference`
- `parameter_change_rationale`
- `correction_feedback`
- `terminology`
- `workflow_pattern`

Rejected examples:

- current prices,
- holdings,
- live runtime status,
- generated config values,
- broker state,
- webhook, API key, token, cookie, or secret material,
- any fact that should come from an OM tool or runtime artifact.

## File Format

Each memory item is one markdown file under `assistant_memory/` with simple
frontmatter:

```markdown
---
type: parameter_tuning_preference
title: Parameter tuning preference
summary: The operator wants candidate-filter evidence before changing thresholds.
tags: [parameters, candidate-filter]
status: active
---

When tuning Sell Put parameters, inspect replay, candidate-filter diagnostics,
and reject reasons before discussing threshold changes.
```

The loader intentionally uses simple frontmatter parsing and no YAML
dependency. `status` must be `active` or `accepted`; missing status defaults to
`active`.

## Authority Rules

Memory follows these rules:

- current user message wins over memory,
- deterministic OM tool evidence wins over memory,
- memory is not market data, ledger state, runtime config, or broker state,
- memory cannot authorize writes,
- memory cannot satisfy required evidence,
- memory may guide collaboration style, default investigative order, terminology,
  and parameter-tuning discussion preferences.

In practice, memory may suggest "inspect replay first" but must not say "change
this config value now" or "this symbol is currently safe".

## Runtime Flow

```text
AssistantRequest
  -> command / permission handling
  -> explicit memory suggestion check
     -> optional assistant_memory/proposals/<proposal_id>.json sidecar
     -> response data/meta memory_suggestion audit summary
```

No memory content is passed to a planner or model in the current runtime.

The memory suggestion check runs after the assistant answer is formed. It is
not part of planning and cannot change tool authorization. It only reacts to
explicit remember/preference/correction signals in the current turn. Normal
assistant questions are ignored, and explicit runtime/config facts are reported
as skipped rather than stored.

## Proposal Lifecycle

The proposal lifecycle is deterministic and explicit:

```text
candidate memory
  -> assistant memory propose | assistant memory suggest --text ... | ./om assistant handle
  -> assistant_memory/proposals/<proposal_id>.json
  -> human accepts or rejects
  -> accepted proposal writes assistant_memory/<memory_id>.md
```

Current commands:

```bash
./om assistant memory propose \
  --type parameter_tuning_preference \
  --memory-id parameter-tuning \
  --title "Parameter tuning preference" \
  --summary "Inspect candidate-filter evidence before threshold changes." \
  --content "When tuning Sell Put parameters, inspect replay and reject reasons first."

./om assistant memory suggest \
  --text "Remember: when tuning parameters, inspect replay and reject reasons first."

./om assistant memory list-proposals
./om assistant memory accept <proposal_id>
./om assistant memory reject <proposal_id> --reason "too broad"
```

`propose` writes a manually supplied proposal JSON file. `suggest` writes one or
more proposal JSON files only when the supplied text contains explicit
remember/preference/correction signals and does not look like a secret, a current
market/runtime fact, or a concrete config/parameter value. Neither command makes
the memory model-visible. `accept` is the explicit write boundary that creates
the markdown topic file. `reject` keeps the proposal for audit and does not
create a memory item.

`./om assistant handle` uses the same deterministic suggestion path for the
current turn and writes at most one proposal sidecar. The assistant response
returns `data.memory_suggestion` and `meta.assistant.memory_suggestion` when a
proposal is created or when an explicit memory request is skipped for a safety
reason. Replayed idempotent turns reuse the audited response and do not create a
duplicate proposal.

Proposal payloads are rejected if they contain obvious secret material. Accepted
markdown uses the same memory type allowlist and is loaded by the normal
read-only memory loader on later turns.

## Safety And Budget

The default assistant memory read path is read-only. Proposal commands are the
primary explicit memory write surface. The runtime suggestion sidecar also writes
only under `assistant_memory/proposals/`; it does not write accepted memory files.

Sensitive frontmatter fields and body lines containing token, password, cookie,
authorization, secret, private key, webhook, or API-key markers are redacted
before projection.

Future projection work must define its own bounded context contract before any
memory content becomes model-visible.

## Future Work

A later model-assisted proposal phase may reuse the same deterministic queue
without changing the accept boundary:

```text
observed correction or stable workflow pattern
  -> memory proposal
  -> explicit human accept/reject
  -> append/update topic file
  -> trace
```

That phase should remain separate from read execution. Model confidence must
not infer permission to write accepted memory.
