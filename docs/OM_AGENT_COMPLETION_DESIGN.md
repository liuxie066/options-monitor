# OM Agent Completion Design

> Historical design note. This document is retained for rationale and rollout
> history, but it is not the current architecture terminology authority. Current
> naming and boundaries live in
> [OM_ASSISTANT_ARCHITECTURE.md](OM_ASSISTANT_ARCHITECTURE.md): `./om-agent`
> is the Tool Gateway, `./om assistant` is the Inbound Assistant, and
> `AgentLoop` is an internal OM Copilot task/evidence/answer loop.
>
> Current reading note: keep the high-level goal of bounded multi-tool answers,
> but read older verifier/replan language as guardrail design. Current Tool
> Calling direction is the host-owned Copilot task/evidence loop: Copilot
> chooses what to inspect from task profiles while code enforces scope, risk,
> budget, duplicate prevention, evidence, and trace. Do not preserve old planner
> or answer paths as alternate contracts.

This historical document recorded a path from the older OM Ops Copilot framing
to a complete bounded assistant loop. It is not a new public surface. The
current authority documents are:

- [OM_AGENT_CAPABILITY_MAP.md](OM_AGENT_CAPABILITY_MAP.md) owns capability
  boundaries, risk classes, and LLM authority.
- [INBOUND_CONTROL.md](INBOUND_CONTROL.md) owns remote-message control,
  preview/confirm gates, sender allowlists, and audit semantics.
- [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md) owns the local `./om-agent`
  JSON contract.

## Goal

Make OM behave like one coherent Agent for operations questions:

```text
user asks a goal
-> Agent understands the task
-> Agent selects evidence paths
-> Agent runs bounded tools
-> Agent observes gaps or conflicts
-> Agent replans if useful and safe
-> Agent answers from verified evidence
-> Agent records why the answer is valid
```

The target is not an unrestricted automation agent. OM remains a controlled
financial operations system. The Agent may inspect, reason, summarize, and
create approved previews. It must not bypass deterministic tools, write gates,
broker-facing boundaries, notification gates, or service-operation gates.

## Current State

The current implementation already has the important safety spine:

- Natural-language input can route through the AgentLoop Planner when
  `assistant.enabled` and `assistant.planner.enabled` are true.
- Slash commands and confirm/cancel/apply messages remain deterministic first.
- Planner read steps are checked by the read-only tool policy before execution.
- Preview-write plans are converted back into existing deterministic pending
  operation paths.
- Tool definitions can declare output contracts and payload-dependent
  `output_contract_resolver` metadata.
- `AnswerEvidence` lets the LLM compose user-facing text from tool evidence,
  then answer guard and deterministic provenance protect the final response.
- Deterministic renderers remain fallback and audit evidence.

The prior limitation was shape, not raw capability: the AgentLoop used to be
mostly a one-shot plan executor. The mainline now owns an explicit session
snapshot, durable operator trace, a general evidence model, a bounded
observe/replan loop, and a deterministic contract verifier. New workflow
coverage should extend this same loop instead of adding parallel modes.

## Implementation Status

Current implementation status:

- Phase 1 is implemented: `EvidenceBundle` exists as an internal assistant
  model, tool observations can be converted into source-labeled facts,
  missing-data records, datasets, guard contracts, reconciliation calculations,
  and trace summaries.
- Phase 2 is implemented: `AgentSession` snapshots are generated for AgentLoop
  tool-plan execution, stored inside the tool result/audit payload, and
  persisted in the existing inbound SQLite database through the `agent_sessions`
  table. This creates durable operator trace without moving pending operations
  out of the existing pending-operation store.
- Phase 3 is implemented for the first read-only replan workflows: AgentLoop can
  do bounded follow-up planning when evidence contains a recoverable gap.
  Supported gaps now include assigned-stock quote data recoverable by
  `refresh_quotes`, empty analysis results, account-summary evidence for
  breakdown/source questions, and missing named-account coverage in
  `analysis_query` evidence. Follow-up decisions are recorded with
  `om-agent-loop-followup-decision-v1` in the tool-plan payload and AgentSession
  answer trace; monthly-income detail questions are normalized to
  `include_rows=true` before execution so detail-row requirements are fetched
  directly rather than guessed from summaries.
- Phase 4 is implemented for the primary cross-tool accounting workflow:
  `EvidenceBundle` records accounting view summaries and a cross-tool
  reconciliation record when monthly income and assigned-stock lifecycle
  evidence are combined. More tool-family narratives can be added as new
  workflows need them.
- Phase 5 is implemented for current financial answer contracts: the
  contract-based verifier checks explicit `USD` / `HKD` / `CNY` amount claims,
  share/contract/row quantities, dates, symbols, and explicit status claims
  against EvidenceBundle facts and approved reconciliation sums. The older
  targeted answer guard remains only as a narrow safety check for percentages
  and scenario-specific protections while contract verification is the target.
- Phase 6 is implemented: preview-write/admin responses include a unified
  `permission_request` object built from the existing pending-operation store,
  and operators can inspect durable Agent decisions with
  `./om-agent run --tool assistant_trace`. The trace readout includes goal,
  plan revisions, tools, evidence counts/sources, answer guard/fallback state,
  and permission state without introducing a second operation store.
- Phase 7 is implemented for the current gate: focused tests cover
  evidence/session snapshots, durable session trace, read-only replan,
  cross-tool reconciliation, contract verifier fallback, permission requests,
  and assistant scenario evals. Full `pytest` and release metadata checks are
  the release-readiness baseline.

## Product Definition Of "Complete Agent"

For OM, "complete Agent" means:

- It can answer operational and financial questions by deciding what evidence
  to inspect, not only by matching one command.
- It can run multiple read tools when one tool is insufficient.
- It can detect missing data, quote staleness, scope mismatch, or unsupported
  conclusions before answering.
- It can ask a clarification question when the task cannot be safely resolved.
- It can create preview operations for approved write-like intents, but apply
  remains explicit and deterministic.
- It can explain the data source and accounting policy without exposing internal
  implementation ids by default.
- It can preserve task continuity across a conversation and, later, across
  resumable sessions.

It does not mean:

- LLM-generated facts become authoritative.
- `canonical` or `synthesis` become user-visible product modes.
- AgentLoop receives broad shell, service, broker, notification, config-write,
  or database-write authority.
- A second tool registry or parallel operation-control plane is introduced.

## Complete Mainline, Not A Patch Lane

This design is the complete Agent mainline delivered incrementally. A patch lane
would improve one symptom at a time, for example a nicer assigned-stock receipt,
a keyword fallback, or a regex guard for one answer type. That would not solve
the underlying product problem: OM needs one task loop that can understand a
goal, gather the right evidence, detect gaps, verify claims, and stop safely.

The mainline therefore keeps one architecture target:

```text
AgentSession
-> bounded plan
-> authorized tools
-> EvidenceBundle
-> coverage / claim verification
-> natural answer
-> audit trace
```

The delivery can still be phased. Each phase should be independently shippable
and behavior-preserving, but each phase must move toward the same Agent loop
rather than adding another side mode or one-off response path.

User-facing behavior should also be one product experience. The user should not
choose between `canonical`, `synthesis`, `fact`, or `analysis` modes. The Agent
should normally return one concise answer, then append source and accounting
policy lines when they are useful. Debug detail belongs in audit/operator
surfaces, not in normal chat replies.

## OM Tool OS v1

The next Agent increment is a general Tool OS layer, not another
business-specific answer tool. The design follows the useful Claude Code
pattern: the model chooses tools and explains results, while the AgentLoop
runtime owns permission checks, data authority, execution boundaries, and
verification.

This explicitly rejects a narrow tool such as `account_income_compare` as the
main solution. That tool would answer one question shape, but it would not help
with assigned-stock lifecycle, position lots, candidate filters, runtime
diagnostics, strategy config, or future cross-domain questions. OM needs a
generic read-only analysis workspace that can answer many task shapes from the
same audited data sources.

Tool OS v1 has these layers:

| Layer | Responsibility | First implementation |
|---|---|---|
| Tool discovery | Tell the planner what safe tools exist and what each tool is for | Existing registry plus new `analysis_catalog` command spec |
| Data catalog | Expose named business views and field descriptions | `analysis_catalog` |
| Read/query | Let the Agent slice, group, rank, compare, and join read-only data | `analysis_query` with SELECT-only SQL over whitelisted views |
| Compute | Derive totals, differences, rates, rankings, and trends | SQLite aggregate expressions; no arbitrary Python in v1 |
| Evidence | Preserve result rows, columns, cell refs, source labels, coverage, and warnings | `analysis_query` output contract and EvidenceBundle extraction |
| Compose | Let the LLM write the user-facing answer from query evidence | Existing AgentLoop synthesis path |
| Verify | Reject unsupported amounts, accounts, symbols, dates, counts, and statuses | Answer guard reads Tool OS result cells |
| Fallback | Keep task shape if synthesis is unavailable or unsafe | Render the analysis result table, not an unrelated raw report |

The expanded design considered these possible tool families:

| Candidate | Benefit | Pruning decision |
|---|---|---|
| Narrow business answer APIs such as `account_income_compare` | Fast to implement for one question | Rejected as the primary path because each new question shape would require another API |
| Arbitrary Python/dataframe execution | Maximum analytical flexibility | Rejected for v1 because it creates a larger permission and resource surface |
| Read-only SQL over audited business views | Flexible enough for comparisons, grouping, ranking, and joins | Accepted as v1 compute surface |
| Dedicated write/apply tools | Needed for future autonomous workflows | Deferred; v1 is pure read and keeps existing preview/confirm write gates |
| Candidate-filter, close-advice, runtime-event views | Useful for broader Agent diagnosis | Deferred as additive catalog views after the v1 contract is proven |

The final v1 scope is therefore intentionally small but general:

- one catalog tool,
- one SELECT-only query tool,
- whitelisted business views,
- bounded output rows,
- result cells promoted into answer evidence,
- LLM composition when available,
- deterministic task-shaped table fallback when composition is unavailable or unsafe.

The first whitelisted views should cover more than income:

- `monthly_income_summary`
- `monthly_income_return_summary`
- `monthly_income_combined_return_summary`
- `monthly_income_cashflow_rows`
- `monthly_income_realized_rows`
- `monthly_income_premium_rows`
- `assigned_stock_lifecycle`
- `assigned_stock_sales`
- `assigned_stock_review`
- `position_lots`
- `trade_events`
- `symbol_strategy_config`

Additional views such as candidate-filter traces, close-advice rows, and runtime
event snapshots can be added later through the same catalog/query contract.

The SQL surface is intentionally small:

- Only one statement is accepted.
- Only `SELECT` or `WITH` queries are accepted.
- The SQLite authorizer rejects inserts, updates, deletes, DDL, `ATTACH`,
  `DETACH`, `PRAGMA`, and reads from non-whitelisted tables.
- The tool enforces a row cap and reports truncation.
- The tool output contains `cell_refs` such as `r1.net_income_cny`, so the
  final answer can cite and verify dynamic query results without creating a
  one-off output contract for every possible question.

The fallback rule changes for analytical questions. If the user asks "有什么不
同", "对比", "排名", "趋势", "组成", or "为什么", and `analysis_query`
succeeds, fallback must preserve that task shape:

```text
user: 对比 lx 和 sy 的账户收益，有什么不同？
-> analysis_query returns rows grouped by month/account with diff columns
-> legacy model synthesis succeeds: concise comparison + source
-> legacy model synthesis fails or guard rejects it: render the query result table +
   conservative one-line conclusion + source
```

It must not fall back to a nearby raw monthly income report. Raw canonical
renderers remain audit and safety surfaces for the original business tools, but
Tool OS result tables are the product fallback for Tool OS analytical tasks.

The next expansion of this lane is documented in
[SQLite Tool OS Expansion Design](SQLITE_TOOL_OS_EXPANSION_DESIGN.md). That
follow-up keeps SQLite as the controlled computation surface and adds semantic
catalog metadata, business views, query preflight/explain diagnostics, bounded
multi-query follow-up, and richer evidence for answer guard.

## Target Architecture

```text
Channel input
-> AgentSession
   -> Perceive
   -> Understand
   -> Plan
   -> Decide
   -> Act
   -> Observe
   -> Verify
   -> Replan or Stop
   -> Compose
   -> Verify Answer
-> Reply + audit
```

The important change is that `Observe` and `Verify` become loop inputs, not just
trace output. The Agent should be able to say: "the first tool result is not
enough; I need quotes", or "the question asks realized PnL, but this result only
contains cashflow; fetch lifecycle rows", within a bounded budget.

## Agent Architecture Baseline

The useful Agent architecture pattern is not broad autonomy. The useful pattern
is a controlled loop with durable context, tool transcripts, permission
checkpoints, and answer verification:

| Agent pattern | OM interpretation |
|---|---|
| Session context | `AgentSession` carries request scope, channel/sender, conversation context, pending operations, and audit identity |
| Plan / todo state | Planner output records goal, requirements, tool steps, and revision reasons |
| Tool sandbox | Agent-visible actions are existing OM tools and inbound operation handlers, filtered by risk class and scope |
| Observation transcript | Tool calls, resolved payloads, result summaries, errors, and renderer fallbacks are recorded |
| Permission checkpoint | Preview-write/admin intents create pending operations; apply stays deterministic and explicit |
| Evidence model | Financial facts are extracted from tool contracts into `EvidenceBundle` |
| Final synthesis | LLM may compose the answer, but only from verified deterministic evidence |
| Audit/debug trace | Operator-facing traces explain plan, evidence, gaps, verifier decisions, and fallback reasons |

OM intentionally does not import the unrestricted parts of general coding
agents: arbitrary shell access, file mutation, service operation, broker-facing
actions, proactive notifications, and production data writes remain outside the
Agent loop unless an explicit deterministic operation path already exists.

## Mainline 1: AgentSession And EvidenceBundle

### AgentSession

`AgentSession` is the runtime task boundary. It is internal to the existing
assistant runtime and does not create a new public entrypoint.

Fields:

| Field | Purpose |
|---|---|
| `session_id` | Stable id for one inbound/local Agent turn or resumable task |
| `request` | `AssistantRequest` plus normalized channel, sender, message, and config scope |
| `goal` | User-visible task objective inferred from the request |
| `task_state` | `planning`, `acting`, `waiting_for_permission`, `asking_clarification`, `done`, `failed` |
| `plan_revisions` | Planner outputs and why each revision changed |
| `tool_transcript` | Authorized tool calls, payloads after scope injection, result summaries, errors |
| `evidence_bundle` | Current facts, datasets, provenance, missing-data notes, and conflicts |
| `permission_state` | Preview/write/admin request state, pending operation ids, TTL, confirm requirements |
| `answer_trace` | Composer, guard, verifier, fallback, and provenance decisions |
| `audit_ref` | Link to existing inbound audit rows and pending operation store |

Persistence policy:

1. Keep an in-memory `AgentSession` per request while a turn is executing.
2. Write compact JSON snapshots to the existing audit payload.
3. Persist compact snapshots into the existing inbound SQLite `agent_sessions`
   table for operator trace and later resume support.
4. Keep pending operations in the existing pending-operation store; do not move
   write confirmation into the LLM loop.

### EvidenceBundle

`EvidenceBundle` is the structured authority for final answers. It replaces
single-step `AnswerEvidence` as the general internal model. Do not keep a
long-lived `AnswerEvidence` route switch; update callers toward
`EvidenceBundle` as the answer source.

Recommended shape:

```json
{
  "schema_version": "om-agent-evidence-bundle-v1",
  "scope": {
    "config_key": "us",
    "accounts": ["lx"],
    "symbols": ["FUTU"],
    "time_range": {"start": "2026-06-01", "end": "2026-06-30"}
  },
  "facts": [],
  "datasets": [],
  "calculations": [],
  "missing_data": [],
  "conflicts": [],
  "provenance_lines": [],
  "fallback_renderers": [],
  "guard_contracts": []
}
```

Each fact should be machine-checkable:

| Field | Meaning |
|---|---|
| `fact_id` | Stable id inside the bundle |
| `path` | Source path such as `rows[0].spot_price` |
| `value` | Raw deterministic value |
| `unit` | `share`, `contract`, `percent`, `currency`, `date`, `symbol`, etc. |
| `currency` | Currency when applicable |
| `account` | Account label when applicable |
| `symbol` | Canonical symbol when applicable |
| `as_of` | Data timestamp or report scope |
| `freshness` | `fresh`, `stale`, `missing`, `not_applicable` |
| `source_tool` | Tool that produced the fact |
| `source_label` | User-facing source label from the output contract |
| `source_path` | JSON path inside tool result |

Missing data is first-class, not a warning string:

```json
{
  "kind": "missing_quote",
  "symbol": "0700.HK",
  "account": "sy",
  "impact": "assigned stock unrealized PnL cannot be calculated",
  "recoverable_by": "refresh_quotes",
  "source_tool": "option_positions_read"
}
```

## Mainline 2: Iterative Loop And Multi-Tool Synthesis

### Planner Output

The planner must not choose answer-rendering modes. It only expresses the task
goal, required capabilities, and tool steps:

```json
{
  "schema_version": "om-tool-plan-v2",
  "goal": "explain assigned stock PnL for sy",
  "required_capabilities": ["positions", "quotes", "pnl"],
  "task_contract": {
    "schema_version": "om-agent-task-contract-v1",
    "goal": "explain assigned stock PnL for sy",
    "domain": "position",
    "task_mode": "summarize",
    "requested_effect": "read",
    "scope": {
      "accounts": ["sy"],
      "symbols": [],
      "config_keys": ["us"]
    },
    "required_answer": ["summary", "source_and_policy"],
    "required_evidence": ["current_state", "quote_freshness", "source_policy"],
    "answer_shape": ["conclusion", "evidence_boundary", "source_policy"]
  },
  "steps": [
    {
      "tool_name": "option_positions_read",
      "arguments": {
        "action": "assigned-stock",
        "account": "sy",
        "status": "open",
        "refresh_quotes": true
      },
      "purpose": "fetch open assigned stock lifecycle rows with current quotes"
    }
  ]
}
```

Fields such as `response_mode`, `canonical`, `synthesis`, and renderer choices
are invalid planner output. AgentLoop decides the final answer path from the
user task, tool contracts, and gathered evidence. Normal diagnostic,
analytical, and financial explanations use LLM composition over guarded
evidence; deterministic renderers remain evidence formatters and fallbacks when
composition is unavailable or unsafe.

### Loop Algorithm

The current implementation supports bounded read-only iterative planning for
selected financial workflows. Preview-write creation remains gated by the
existing preview/confirm operation path.

```text
1. Build AgentSession and initial goal.
2. Planner proposes steps and task requirements.
3. Decide validates safety class and injects config/account scope.
4. Act executes authorized read tools.
5. Observe converts tool output into EvidenceBundle facts.
6. Verify checks coverage against task requirements.
7. If coverage is incomplete and budget remains, planner receives the evidence
   gaps and proposes a follow-up read step.
8. If coverage is complete, compose answer from EvidenceBundle.
9. Verify answer claims against EvidenceBundle.
10. Reply with deterministic provenance and audit trace.
```

Default budgets:

| Budget | Default |
|---|---|
| max iterations | 3 |
| max tool calls | 5 |
| max write previews | 1 |
| max LLM composition attempts | 2 |
| max answer length | Channel-specific formatter limit |

Stop conditions:

- Coverage is sufficient for the user question.
- Clarification is required.
- Missing data is not recoverable by allowed tools.
- Tool policy denies the needed step.
- The next step would be a write, live operation, notification, broker-facing
  action, service operation, or report refresh that the user did not request.
- Budget is exhausted.

### Multi-Tool Synthesis

Multi-tool synthesis should be explicit. The Agent should not concatenate tool
summaries.

Evidence merge responsibilities:

- Normalize account, market, symbol, currency, and time-range scope.
- Preserve source tool and source path for every user-visible fact.
- Mark freshness differences, for example ledger facts as historical and quote
  facts as realtime.
- Detect conflicts between tools, such as row counts or symbol aliases.
- Keep unsupported conclusions out of the answer.

Example flow for "为什么 6 月收益和指派正股盈亏对不上":

1. `monthly_income_report` with `include_rows=true` for June.
2. `option_positions_read action=assigned-stock refresh_quotes=true`.
3. Evidence merger separates realized cashflow, option premium attribution,
   assigned-stock realized PnL, assigned-stock unrealized PnL, and missing
   quote impacts.
4. Composer explains that net cashflow, realized PnL, and lifecycle holding PnL
   are different accounting views.
5. Verifier checks every amount and count against the merged bundle.

### Normal Answer UX

The normal answer should be synthesized by the Agent, not displayed as separate
"facts" and "analysis" sections. The preferred shape is:

```text
<direct answer in natural Chinese>

数据来源：...
口径：...
缺失数据：...  # only when relevant
```

Rules:

- Lead with the user's answer, not the internal tool receipt.
- Hide internal ids, lot ids, operation ids, JSON paths, and verifier details
  unless the user asks for debug detail or the operation requires a confirm id.
- Keep currency/account/symbol separation visible when it affects the answer.
- Mention missing quote, stale data, unsupported period, or scope mismatch only
  when it changes the conclusion.
- Use deterministic renderer fallback if legacy model synthesis fails verification.
- Keep source and accounting policy lines short and stable.

## Mainline 3: Contract Verifier And Evaluation

### Contract-Based Verification

The current answer guard should shrink to targeted safety checks while the
target verifier becomes contract-based:

```text
tool output contract
-> fact extractor
-> EvidenceBundle
-> answer claim extractor
-> claim-to-fact verifier
-> pass / rewrite / fallback / ask
```

Verifier responsibilities:

| Verifier | Checks |
|---|---|
| Coverage verifier | Required accounts, symbols, periods, quote freshness, and detail rows are present |
| Claim verifier | Amounts, dates, counts, currencies, symbols, percentages, and statuses appear in facts |
| Policy verifier | Answer does not expose internal ids by default and does not imply unsupported writes |
| Reconciliation verifier | Cross-tool totals are either reconciled or explicitly marked as different accounting views |
| Missing-data verifier | Missing facts are stated with impact and recovery path |

Unsupported answer claims should trigger one retry with guard feedback. If the
retry still fails, use deterministic fallback.

### Tool Contract Requirements

Every Agent-visible tool that can feed user-facing financial answers should
declare:

- `schema_version`
- `canonical_renderer`
- `source_label`
- `guard_profile`
- `primary_rows`
- `row_count_field`
- `fact_fields`
- Optional `freshness_fields`
- Optional `missing_data_fields`
- Optional `accounting_policy_lines`

Payload-dependent output must continue to use `output_contract_resolver` so the
actual contract travels with each observation.

### Evaluation Suite

Add scenario evals before broadening autonomy:

| Scenario | Expected behavior |
|---|---|
| Single-tool assigned stock PnL | Concise Agent answer with no internal lot ids |
| Missing quote | Explicit missing quote impact, no invented PnL |
| Multi-contract income row | Contract count cannot drift |
| Cross-account income | Account scope and currency remain separated |
| Cashflow vs realized PnL | Answer explains accounting view difference |
| Ambiguous write | Preview or clarification, never direct apply |
| Unsupported refresh | Explain that refresh/generation needs explicit request |
| LLM unavailable | Deterministic renderer fallback remains useful |
| Guard failure | Retry once, then fallback |
| Budget exhausted | Partial evidence plus clear next step, no invented conclusion |

These evals should live with the existing assistant eval fixtures and focused
runtime tests.

## Implemented Checkpoints

### Evidence Model

`EvidenceBundle` is implemented as an internal assistant model. Tool
observations plus output contracts are converted into source-labeled facts,
datasets, missing-data records, guard contracts, and reconciliation summaries.
Deterministic renderer fallback and provenance output remain available when LLM
composition is unavailable or unsafe.

### AgentSession

`AgentSessionSnapshot` records goal, plan revisions, tool transcript, evidence
metadata, answer verifier result, fallback state, and pending operation
references. Compact snapshots are stored in the existing audit payload and
persisted in the inbound SQLite `agent_sessions` table. Confirm/cancel/apply
remain deterministic-only.

### Read-Only Iterative Loop

The loop has bounded budgets and can feed recoverable evidence gaps back into
follow-up read planning. Current high-value paths include missing assigned-stock
quote recovery through `refresh_quotes=true` and monthly-income detail recovery
through `include_rows=true`. Follow-up steps stay inside the read whitelist.

### Multi-Tool Evidence Merge

Assigned-stock lifecycle evidence and monthly-income evidence can be merged into
one accounting view by account, symbol, currency, and period. The Agent keeps
cashflow, realized PnL, open holding PnL, lifecycle PnL, and missing quote state
separate.

### Contract Verifier

The financial answer verifier checks explicit currency amounts,
share/contract/row quantities, dates, symbols, and status claims against
`EvidenceBundle` facts and approved reconciliation sums. Older targeted guards
may remain only as scenario-specific safety checks while contract verification
absorbs those cases.

### Permission Request And Debug UX

Preview-write/admin responses include a structured `permission_request` object
derived from the existing pending-operation store. Operator debug uses the
read-only `assistant_trace` tool.

Implemented debug surface:

```bash
./om-agent run --tool assistant_trace --input-json '{"limit":10}'
./om-agent run --tool assistant_trace --input-json '{"command_id":"in_..."}'
```

`assistant_trace` is read-only and reads durable `agent_sessions` snapshots from
the existing inbound SQLite database. It does not execute business tools, send
notifications, write ledger/config state, or mutate pending operations.

Permission request object:

```json
{
  "schema_version": "om-agent-permission-request-v1",
  "operation_id": "op_...",
  "operation_type": "manual_trade_open",
  "risk_class": "preview_write",
  "status": "previewed",
  "confirm_required": true,
  "apply_allowed": false,
  "expires_at": "2026-06-13T22:30:00+08:00",
  "scope": {
    "channel": "feishu",
    "sender": "user-id",
    "conversation": "chat-id",
    "config_key": "us",
    "account": "sy"
  },
  "target_summary": "preview one manual FUTU sell put trade",
  "evidence_refs": ["tool:option_positions_read", "audit:..."],
  "confirm_hint": "/confirm trade op_..."
}
```

This object is written beside existing pending-operation/audit payloads. It is
not a second pending-operation store. Existing operation handlers remain the
only place that creates, confirms, cancels, or applies mutations.

### Eval Gate

Focused tests cover `EvidenceBundle`, `AgentSession` snapshots, iterative loop,
permission request formatting, verifier failures, and fallback paths. The full
test suite remains the release-readiness baseline.

## Extension Rules

Keep future workflow coverage on the same Agent loop:

1. Add one high-value workflow at a time.
2. Reuse `EvidenceBundle`, output contracts, answer verification, and
   `AgentSession` trace.
3. Keep follow-up planning bounded and read-only unless the user is explicitly
   entering an existing preview/confirm operation path.
4. Generalize only after two or more workflows use the same abstraction.

Do not add:

- A second tool registry.
- A second pending-operation store.
- A second assistant CLI surface.
- Public `canonical` / `synthesis` mode switches.
- LLM-owned accounting calculations.

## Decisions

- Durable sessions use a new `agent_sessions` table inside the existing inbound
  SQLite database. This keeps audit/session/operator trace in one physical
  store and avoids a second control plane.
- The next workflow families after assigned-stock and monthly income are
  position rows and runtime diagnostics. Both must reuse `EvidenceBundle`,
  output contracts, and the same answer verifier; neither gets a separate mode.
- Answer claim extraction is deterministic-primary. Structured LLM extraction
  may be added later only as an optional candidate extractor whose claims still
  pass deterministic verification against `EvidenceBundle`.
- The operator debug surface is `./om-agent run --tool assistant_trace`. It
  reads durable session snapshots and formats the trace; no separate assistant
  CLI debug mode is added.

## Success Criteria

The Agent work is complete enough when:

- A user can ask a financial operations question without knowing the exact tool.
- The Agent gathers the necessary read evidence within a bounded budget.
- Missing data is explicit and tied to impact.
- Every visible financial fact can be traced to a tool fact.
- Cross-tool answers reconcile different accounting views instead of mixing
  them.
- Write-like tasks create previews only, and apply remains explicit.
- Debug/audit can explain what the Agent did and why.
- Fallback remains useful when LLM planning or composition is unavailable.
