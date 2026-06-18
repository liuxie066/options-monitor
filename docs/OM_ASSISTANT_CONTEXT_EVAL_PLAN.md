# OM Assistant Context Eval Plan

> Long-term regression plan for multi-turn conversation context.

## Status

This eval plan covers context projection and validation. It intentionally does
not treat any single business example as the architecture center.

## Eval Layers

Context quality should be tested in three layers:

```text
transcript -> projection eval
projection + planner output -> validation eval
end-to-end assistant answer -> scenario eval
```

The first two layers are the core of this work. Final answer evals remain useful
but are too late to diagnose context capability.

## Layer 1: Projection Eval

Purpose: verify that raw conversation state becomes a stable, bounded,
sanitized planner-facing view.

Fixture shape:

```json
{
  "id": "projection_keeps_recent_tool_ref",
  "mode": "context_projection",
  "history": [],
  "current_user_message": "这个怎么算？",
  "expect": {
    "recent_turn_count": 1,
    "evidence_ref_count": 1,
    "truncated": false,
    "forbidden_payload_keys_absent": ["config_path", "audit_db"]
  }
}
```

Required cases:

| Case family | What it proves |
|---|---|
| latest successful tool preserved | recent evidence is visible |
| failed tool summarized | failures do not become factual evidence |
| large tool result replaced | budget produces evidence ref and data shape |
| open coverage gap preserved | follow-up hints survive projection |
| pending operation preserved | preview/confirm continuity remains visible |
| truncation boundary recorded | planner can know context is incomplete |
| sanitized payload | hidden system args are absent |

## Layer 2: Context Validation Eval

Purpose: verify that planner use of context is explicit and structurally safe.

Fixture shape:

```json
{
  "id": "validation_blocks_unreferenced_carry",
  "mode": "context_validation",
  "projection": {},
  "plan": {
    "context_use": {
      "mode": "carry",
      "referenced_turn_ids": [],
      "referenced_evidence_refs": [],
      "inherited_slots": {
        "symbol": ["9992.HK"]
      }
    },
    "steps": [
      {
        "tool_name": "candidate_filter_explain",
        "arguments": {
          "symbol": "9992.HK"
        }
      }
    ]
  },
  "expect": {
    "status": "blocked",
    "code": "CONTEXT_REF_NOT_FOUND"
  }
}
```

Required cases:

| Case family | Expected behavior |
|---|---|
| referenced carry | pass |
| unreferenced carry | block |
| explicit account override | old account blocked |
| explicit symbol override | old symbol blocked |
| explicit month override | old month blocked |
| hidden injected arg copied | block |
| ambiguous multi-topic carry | ask clarification |
| truncated context with carry | require visible evidence ref or clarification |
| clarification mode with tool steps | block |

## Layer 3: End-To-End Scenario Eval

Purpose: verify user-visible behavior after projection and validation are wired
into `AgentLoop`.

These should remain small and high-signal. They should not encode implementation
internals beyond trace fields.

Required scenario families:

| Family | Example shape |
|---|---|
| metric follow-up | "这个指标怎么算？" after a metric answer |
| candidate follow-up | "为什么被过滤？" then "这个参数是什么？" |
| income follow-up | account comparison then "主要差在哪里？" |
| position follow-up | assigned stock answer then "现在这个浮盈亏呢？" |
| runtime follow-up | health issue then "继续查回执" |
| config follow-up | symbol config answer then "这个阈值为什么这样？" |
| explicit switch | prior lx then current sy |
| multi-topic ambiguity | two recent topics then "继续看这个" |
| evidence gap carry | prior answer says missing quote, next says "继续查" |
| no context | standalone question should not inherit old scope |

`net_income` must remain a regression case, but only as one member of the metric
follow-up family.

## Fixture Policy

Fixtures should assert:

- projection fields,
- context use declarations,
- validation status and code,
- selected evidence refs,
- current-message-wins behavior,
- final clarification when required.

Fixtures should not assert:

- long exact final answer paragraphs unless necessary,
- raw SQL strings beyond payload subsets,
- private helper function names,
- business-specific branch names.

## Trace Requirements

Agent session trace should expose:

```json
{
  "context_projection": {
    "schema_version": "om-context-projection-v1",
    "budget": {}
  },
  "planner_context_use": {
    "schema_version": "om-planner-context-use-v1",
    "mode": "refine"
  },
  "context_validation": {
    "schema_version": "om-context-validation-v1",
    "status": "passed",
    "code": "ok"
  }
}
```

Trace fields should be compact and redacted. Full evidence stays in existing
evidence/session stores.

## Commands

Target commands after implementation:

```bash
./om assistant eval-context --mode projection
./om assistant eval-context --mode validation
./om assistant eval-context --mode scenarios
```

The current `eval-context` command can be retained during migration, but the
new mode names should make the tested layer explicit.

## Success Criteria

- Projection eval passes without LLM calls.
- Validation eval passes without LLM calls.
- Scenario eval proves final behavior for the main follow-up families.
- Adding a new context case normally means adding a fixture, not editing the
  validator or projection core.
- Historical `net_income` regressions fail if the assistant silently switches
  scope, but the implementation that prevents this is not metric-specific.
