# OM Assistant Context Projection Contract

> Planner-facing schema for bounded multi-turn conversation context.

## Status

This contract defines the shape of context that `AgentLoop` should pass to the
planner before tool planning. It is intentionally generic. Business-specific
tools may contribute metadata, but the projection builder must not contain
case-level follow-up logic.

## Purpose

The projection separates complete history from model-visible context:

```text
full audit/session transcript
  -> ConversationEvent list
  -> ContextProjection
  -> planner input
```

The full transcript can stay durable and detailed. The projection is bounded,
sanitized, and optimized for the planner's semantic judgement.

## ConversationEvent

`ConversationEvent` is the normalized unit derived from inbound audit rows,
agent session snapshots, tool transcripts, and assistant answers.

Schema:

```json
{
  "schema_version": "om-conversation-event-v1",
  "event_id": "turn-20260618-0001:tool-1",
  "turn_id": "turn-20260618-0001",
  "event_type": "user_message",
  "created_at": "2026-06-18T10:00:00+08:00",
  "summary": "Asked why the prior candidate was filtered",
  "text_excerpt": "为什么净收入非正？",
  "tool_name": "candidate_filter_explain",
  "tool_payload_excerpt": {
    "account": "lx",
    "symbol": "9992.HK",
    "function": "sell_put"
  },
  "result_status": "ok",
  "evidence_refs": [
    {
      "ref_id": "ev_001",
      "source": "tool_result",
      "label": "candidate_filter_explain result"
    }
  ],
  "open_gaps": [],
  "budget": {
    "summary_chars": 120,
    "payload_chars": 90
  }
}
```

Allowed `event_type` values:

| Value | Meaning |
|---|---|
| `user_message` | user-authored message |
| `planner_plan` | planner task contract and tool plan summary |
| `tool_call` | planned tool invocation summary |
| `tool_result` | deterministic tool observation summary |
| `assistant_answer` | final assistant response summary |
| `clarification_request` | assistant asked user for missing scope |
| `operation_preview` | preview-write pending operation created |
| `system_boundary` | compact/truncation/projection boundary |

## ContextProjection

Schema:

```json
{
  "schema_version": "om-context-projection-v1",
  "current_user_message": {
    "text": "这个怎么算？",
    "received_at": "2026-06-18T10:01:00+08:00"
  },
  "recent_turns": [],
  "recent_successful_tools": [],
  "available_evidence_refs": [],
  "open_evidence_gaps": [],
  "pending_operations": [],
  "user_profile": {},
  "policy": {
    "current_message_wins": true,
    "context_is_hint": true,
    "ask_when_ambiguous": true,
    "declare_context_use": true
  },
  "budget": {
    "max_recent_turns": 6,
    "max_successful_tools": 5,
    "max_chars": 12000,
    "truncated": false,
    "truncation_reason": null
  }
}
```

## Recent Turns

Each recent turn is a compact summary of user intent, tool evidence, and final
answer.

```json
{
  "turn_id": "turn-20260618-0001",
  "created_at": "2026-06-18T10:00:00+08:00",
  "user_summary": "Asked why 9992.HK was filtered",
  "assistant_summary": "Answered from candidate filter trace",
  "tools": ["candidate_filter_explain"],
  "safe_slots": {
    "account": ["lx"],
    "symbol": ["9992.HK"],
    "function": ["sell_put"]
  },
  "evidence_refs": ["ev_001"],
  "result_status": "ok"
}
```

`safe_slots` are not business decisions. They are sanitized fields that are safe
for planner inspection and validator checks.

Default safe slot names:

- `account`
- `symbol`
- `month`
- `market`
- `status`
- `action`
- `function`
- `strategy`
- `option_type`
- `side`
- `expiration`
- `strike`
- `operation_id`
- `run_id`

Adding a new safe slot requires updating this contract or a related tool
metadata contract. It should not be smuggled through arbitrary payload JSON.

## Recent Successful Tools

Tool summaries let the planner see what evidence was recently gathered without
receiving raw results.

```json
{
  "turn_id": "turn-20260618-0001",
  "tool_name": "analysis_query",
  "purpose": "Compared monthly income by account",
  "safe_payload": {
    "month": "2026-06",
    "account": ["lx", "sy"]
  },
  "evidence_refs": ["ev_010"],
  "data_shape": {
    "row_count": 2,
    "columns": ["month", "account", "net_income_cny"]
  },
  "result_status": "ok"
}
```

The planner may use `data_shape` to decide whether evidence is likely relevant,
but facts must still come from tool observations and evidence bundles.

## Evidence Refs

Evidence refs are stable handles into already observed evidence. They avoid
putting full data into context.

```json
{
  "ref_id": "ev_010",
  "turn_id": "turn-20260618-0002",
  "source_type": "tool_result",
  "source_tool": "analysis_query",
  "label": "2026-06 account monthly performance rows",
  "safe_slots": {
    "month": ["2026-06"],
    "account": ["lx", "sy"]
  },
  "data_shape": {
    "row_count": 2,
    "truncated": false
  }
}
```

The ref id is planner-visible. Raw evidence remains in `EvidenceBundle` and
assistant trace.

## Open Evidence Gaps

Coverage and answer verification may leave recoverable gaps for the next turn.

```json
{
  "gap_id": "gap_001",
  "turn_id": "turn-20260618-0002",
  "kind": "missing_same_scope_data",
  "summary": "The prior comparison only covered lx",
  "suggested_tools": ["analysis_query"],
  "suggested_views": ["account_monthly_performance"],
  "safe_slots": {
    "month": ["2026-06"]
  }
}
```

These hints are allowed because they are generated by coverage logic, not by a
business follow-up resolver.

## Budget Policy

Projection should be deterministic:

- include the current user message untruncated unless the inbound transport
  already enforced a limit,
- keep the latest successful turns first,
- keep clarification and open-gap turns even if older than ordinary turns,
- replace large tool results with evidence refs and data shape,
- include a `system_boundary` event when truncation happens,
- set `budget.truncated=true` when any potentially relevant turn was omitted.

Default limits for the first implementation slice:

| Field | Default |
|---|---|
| `max_recent_turns` | 6 |
| `max_successful_tools` | 5 |
| `max_open_gaps` | 5 |
| `max_chars` | 12000 |
| `max_text_excerpt_chars` | 360 |
| `max_payload_chars` | 1000 |

## Sanitization Rules

Projection must not expose:

- filesystem paths unless already user-provided and required,
- config paths or runtime root paths,
- hostnames, ports, service names, secrets, or tokens,
- audit database paths,
- webhook or notification details,
- full SQL result cells when an evidence ref and data shape are enough,
- hidden system-injected arguments such as `config_path`, `audit_db`,
  `output_dir`, `state_dir`, `timeout_sec`, or transport internals.

## Planner Input Requirements

The planner input should include:

- `current_user_message`,
- `context_projection`,
- planner manifest,
- context use policy.

It must not include old `active_frame` or `followup_resolution` as privileged
planner authority. If an older fixture or diagnostic report needs historical
fields, they must be derived from the projection, clearly marked as historical,
and kept out of executable planner input.

## Acceptance Criteria

- Projection is deterministic for a given transcript.
- Projection can be evaled without calling an LLM.
- Projection carries evidence refs, not raw large outputs.
- Projection does not decide business follow-up semantics.
- Planner can declare context use by turn id or evidence ref.
