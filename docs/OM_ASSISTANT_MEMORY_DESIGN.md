# OM Memory Boundaries

OM has two deliberately separate memory surfaces:

- accepted operator preferences under `assistant_memory/`;
- bounded Copilot conversation memory in the Copilot Host SQLite store.

Neither surface is execution authority, current market data, or permission to
write OM state.

## Long-Term Preference Memory

The deterministic Assistant commands manage operator-approved preferences. The
supported types are:

- `collaboration_preference`;
- `om_usage_preference`;
- `parameter_tuning_preference`;
- `parameter_change_rationale`;
- `correction_feedback`;
- `terminology`;
- `workflow_pattern`.

Current prices, holdings, runtime status, generated config values, broker state,
credentials, tokens, webhooks, and other secrets are not valid memory.

Each accepted item is a Markdown file under `assistant_memory/`. Explicit
suggestions first enter `assistant_memory/proposals/` and require a human accept
or reject decision:

```text
explicit remember/preference/correction text
-> proposal sidecar
-> human accept or reject
-> accepted Markdown preference
```

Commands:

```bash
./om assistant memory propose \
  --type parameter_tuning_preference \
  --memory-id parameter-tuning \
  --title "Parameter tuning preference" \
  --summary "Inspect candidate-filter evidence before threshold changes." \
  --content "Inspect replay and reject reasons before tuning thresholds."

./om assistant memory suggest \
  --text "Remember: inspect replay and reject reasons before tuning parameters."

./om assistant memory list-proposals
./om assistant memory accept <proposal_id>
./om assistant memory reject <proposal_id> --reason "too broad"
```

The runtime may create at most one proposal sidecar from an explicit memory
request. It never accepts the proposal automatically. Accepted preference files
are not projected into the Copilot model in the current runtime.

## Copilot Conversation Memory

Copilot stores recent raw turns and structured summaries separately:

```text
pinned_state:
  current_goal
  confirmed_scope
  user_constraints
  open_questions

episodes:
  confirmed_facts
  completed_actions
  tool_findings
  user_constraints
  open_questions
  next_step
```

After more than eight uncompacted turns, Host asks the configured model to
compact older turns with tools disabled and keeps two recent raw turns. Invalid
model output leaves raw turns intact. A single `memory_compact` lease prevents
parallel compaction, and an optimistic compacted-turn check rejects stale
writes.

Conversation memory is explicitly labelled as potentially stale context:

- the current user message wins;
- current canonical tool results win;
- the current pending-Control snapshot wins;
- old chat claims cannot become completed operations;
- current financial or runtime questions must still use canonical tools.

The authoritative runtime contract is
[OM_COPILOT_V2_DESIGN.md](OM_COPILOT_V2_DESIGN.md).

## Safety

Memory cannot authorize config, position, trade-event, notification, service,
upgrade, or broker mutations. Sensitive preference fields are redacted before
projection or display. Copilot conversation summaries and traces may contain
account and operational context, so production retention and access must use the
same controls as the Host database.

Any future model-assisted long-term proposal must reuse the existing explicit
human accept/reject queue. Model confidence must never infer write permission.
