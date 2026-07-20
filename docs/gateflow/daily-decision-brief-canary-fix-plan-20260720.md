# Gateflow Plan — HK Daily Decision Brief Canary Correction

- **Work unit**: `daily-decision-brief-canary-correction`
- **Date**: 2026-07-20
- **Status**: revised; ready for plan re-review
- **Trigger**: HK no-send production Canary exposed a canonical candidate-source error and a runtime-root read split
- **Review source**: `docs/reviews/plan-review-20260720-105521.md`
- **Related completed work**: `docs/gateflow/daily-decision-brief-final-closeout-20260719.md`
- **Safety posture**: default-off, no-send first, fail-closed, no production-config mutation in implementation

## 1. Goal

Restore one authoritative Daily Decision Brief chain without changing its delivery state machine:

1. Sell Put Daily Brief candidates come only from the existing canonical `*_sell_put_candidates_labeled.csv` artifacts.
2. Raw `*_sell_put_candidates.csv` artifacts never contribute rows to normal Daily Brief candidates, actions, ranking, events, or summary counts.
3. Production notification preparation, CLI read, and Agent Tool read resolve the same runtime root and therefore the same persisted brief revision.
4. The payload and renderer distinguish executable actions, canonical candidate evidence, data quality, and data gaps without introducing a second eligibility policy.
5. A production-scoped HK no-send Canary proves content correctness and four-surface consistency before any real-send authorization is requested.

## 2. Completion signals

The work unit is complete only when all of the following hold:

- a labeled/raw conflict fixture proves that an attractive raw-only contract cannot enter `candidates`, `actions`, `events`, strategy summary counts, or rendered Markdown;
- labeled empty, missing, malformed, and partial states have deterministic tests matching the state table below;
- CLI and Agent Tool prefer `OM_RUNTIME_ROOT` over repo-local shadow state and fall back to repo root only when no runtime root is configured;
- renderer exposes both actionability and payload data-quality status;
- only `actions[state=active]` are described as executable;
- no Daily Brief schema version, revision allocation, semantic diff, delivery pointer, idempotency key, or confirmation behavior is changed;
- all focused and broad regression gates pass;
- the HK production Canary is no-send, reads one revision through all four surfaces, and leaves delivery pointers unchanged;
- real sending remains blocked pending a separate explicit user authorization.

## 3. Non-goals and protected boundaries

This work unit does **not**:

- change Sell Put scanning, labeling, cash filtering, underwriting thresholds, or ranking policy;
- change Covered Call or Combo Yield artifact precedence;
- modify `repo_base()` semantics or introduce another runtime-root abstraction;
- introduce `daily_decision_brief.v2`, a new candidate state machine, database migration, outbox, queue, or scheduler;
- change `daily_decision_brief_repository.py`, revision numbering, full/delta selection, semantic digest, last-delivered pointers, delivery keys, or confirmation rules;
- delete release-local shadow artifacts;
- enable `notifications.daily_brief.enabled` in committed production config;
- send a real notification;
- fix event rendering in this release-blocking batch; that decision is recorded in section 9.

## 4. Canonical Sell Put artifact contract

### 4.1 Authority

For Daily Brief assembly, `*_sell_put_candidates_labeled.csv` is the only canonical Sell Put candidate source. It represents the rows that have completed the existing labeling, account cash-capacity, and underwriting path.

`*_sell_put_candidates.csv` is raw provenance/diagnostic evidence only. Its contents must never be passed to `rank_candidate_rows()`, `_candidate_view()`, `_candidate_action()`, `_candidate_events()`, or `_strategy_summary()`.

This rule applies only to Sell Put. Covered Call continues to consume `*_sell_call_candidates.csv`; Combo Yield continues to consume `*_combo_yield_candidates.csv`.

### 4.2 Artifact state table

| State | Detection | Candidate rows | Availability | Required gap/status behavior | Raw fallback |
|---|---|---:|---:|---|---|
| **valid non-empty** | labeled CSV exists and parses with one or more rows | consume only market-matching labeled rows | `True` | normal; row-level market/identity gaps may still degrade | forbidden |
| **valid empty** | labeled CSV parses with headers and zero rows, or its bytes are exactly the controlled empty encoding `b"\n"` / `b"\r\n"` emitted by the current `pd.DataFrame().to_csv(index=False)` fail-closed path | zero | `True` | authoritative zero-candidate result; no missing-source gap | forbidden |
| **missing** | no labeled artifact exists for the family, or a raw per-symbol artifact has no labeled counterpart | zero for missing scope | `False` when no parseable labeled artifact exists | add explicit canonical-labeled-missing gap; block only if existing global blocker rules conclude all decision sources are unavailable | forbidden |
| **malformed/unreadable** | labeled read raises `ParserError`, `UnicodeError`, or `OSError`; or raises `EmptyDataError` but bytes are zero-length or any whitespace/content other than the controlled `b"\n"` / `b"\r\n"` empty encoding | zero for that file | file unavailable; family `False` only if no other parseable labeled artifact exists | add `csv_unavailable` with masked/relative path and `error_type`; fail closed for affected scope | forbidden |
| **partial** | at least one labeled file is parseable, while another expected per-symbol labeled file is missing or unreadable | consume only rows from parseable labeled files | `True` | preserve reliable rows; add per-symbol/source gaps; payload `status=degraded`; do not globally block if another reliable candidate/close source supports the existing actionability rules | forbidden |

### 4.3 Partial detection and symbol scope

Use the existing run-account directory and filename pairing; do not add a config-driven source registry.

- Discover labeled paths and raw paths separately.
- Derive the per-symbol artifact key by removing exactly `_sell_put_candidates_labeled.csv` or `_sell_put_candidates.csv` from the filename.
- The expected key set for partial diagnostics is the union of labeled and raw keys present in that run-account directory.
- A raw-only key proves that the corresponding canonical labeled artifact is missing. Record a gap, but do not read raw rows into the brief.
- A labeled-only key is valid and requires no raw counterpart; raw is not a prerequisite.
- If neither raw nor labeled exists, emit the existing family-level missing-source gap.
- If one labeled file is valid empty under the exact encoding contract below, it is available for that key and is not partial failure.

#### Empty-file encoding contract

The implementation must not interpret every `EmptyDataError` as a business-empty result.

1. A header-bearing CSV that parses to zero rows is valid empty only when its columns include `symbol` and at least one contract identifier from `contract_symbol|code`; an arbitrary or schema-incompatible header-only file is malformed.
2. For compatibility with the current upstream fail-closed writer, a file whose complete bytes are exactly `b"\n"` or `b"\r\n"` is valid empty.
3. A zero-byte file is malformed/truncated, not valid empty.
4. Any other whitespace-only representation is malformed/unrecognized, not valid empty.
5. `EmptyDataError` must therefore trigger a bounded check: inspect file size first, read at most two bytes only when size is at most two, and accept only the exact controlled encodings above.
6. A parsed zero-row frame must pass the minimal-column check before it can mark the family/source available.

This is a narrow compatibility contract for the existing `sell_put_steps.py` writer, not a general whitespace parser. This batch does not modify upstream artifact generation. A later cleanup may standardize header-only empty CSVs, but normal Daily Brief assembly must remain able to read the current controlled single-line empty artifact.

The raw filename may be inspected to establish provenance coverage. Raw CSV contents are not read by the Daily Brief candidate loader.

### 4.4 Availability and global status consequences

Preserve the existing global actionability rules:

- pipeline failure still blocks;
- all structured decision sources unavailable still blocks;
- all required account-capacity sources unavailable still blocks under the current conditions;
- a Sell Put partial failure alone does not suppress reliable Covered Call, Combo Yield, or Close Advice;
- when at least one reliable source remains and a source gap exists, the brief may be `actionability=live_actionable` and `status=degraded` simultaneously;
- a valid empty labeled artifact is an available source with zero candidates, not a failure and not a reason to reopen raw candidates.

## 5. Strict no-fallback implementation boundary

### 5.1 Owning file

`src/application/daily_decision_brief_service.py` owns artifact discovery and application-layer assembly.

Required change:

- replace the Sell Put `paths_to_try` list with a labeled-only canonical selection path;
- if needed, add one narrow private helper in the same file to pair labeled/raw filenames and emit missing/partial diagnostics;
- continue to reuse `_load_candidate_family()` for parsing canonical files, or specialize it narrowly if the current boolean availability contract cannot express partial availability;
- do not move filename precedence into domain ranking, renderer, repository, or notification delivery code.

Explicitly forbidden implementations:

```text
labeled rows if non-empty else raw rows
labeled parse error -> raw rows
missing labeled symbol -> raw rows for that symbol
merge labeled and raw -> dedupe -> rerank
```

### 5.2 Required conflict proof

The production-shaped conflict fixture contains:

- labeled accepted rows: `0700` P430 and P440 with usable cash capacity;
- raw rows: the same rows plus a higher-ranked P450 contract that was not accepted;
- expected result: only P430/P440 may appear in Sell Put candidates/actions and P450 must be absent from the whole brief and rendered message.

This P430/P440/P450 assertion is a deterministic fixture oracle only. It must not be reused as a live-production Canary oracle because live labeling membership can change with market data, capacity, and underwriting inputs.

## 6. Runtime-root ownership and exact file scope

The existing resolver is authoritative:

```python
src.application.runtime_paths.resolve_runtime_root()
```

Do not change `src/application/runtime_paths.py` unless a test reveals an existing resolver defect. Do not change global `repo_base()`.

### 6.1 CLI ownership

Modify only:

- `src/interfaces/cli/daily_brief_ops.py`
- `tests/test_daily_decision_brief_cli.py`

At the CLI handler boundary:

```python
repo_root = repo_base_fn()
runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
read_daily_brief_view(base=runtime_root, ...)
```

The CLI remains pure read. It does not create directories, refresh data, or mutate state.

### 6.2 Agent Tool ownership

Modify only:

- `src/application/agent_tools/daily_brief.py`
- `tests/test_daily_decision_brief_agent_tool.py`

At the Agent Tool handler boundary:

```python
repo_root = repo_base()
runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
read_daily_brief_view(base=runtime_root, ...)
```

No new input field is added. Runtime-root source metadata is not added to the public output in this batch; the existing masked state path remains the public provenance surface.

### 6.3 Root precedence contract

Both read surfaces must prove this precedence:

1. explicit runtime-root argument, if a future caller directly invokes the resolver with one;
2. effective `OM_RUNTIME_ROOT`;
3. repo root fallback.

This work unit exercises items 2 and 3 at the CLI and Agent Tool integration boundary.

## 7. Payload and renderer invariants

### 7.1 Payload authority

The existing `daily_decision_brief.v1` fields remain sufficient:

- `actions`: action authority;
- `candidates`: canonical accepted evidence;
- `capacity`: known account capacity evidence;
- `status`: payload data quality (`ready|degraded|blocked`);
- `actionability`: temporal/execution posture (`live_actionable|planning_only|blocked`);
- `data_gaps`: explicit unavailable or incomplete evidence.

No schema version bump and no new `candidate.state` or `candidate.eligibility` field is introduced.

### 7.2 Action invariant

- Only an item in `actions` with `state=active` is an executable recommendation.
- `actions[state=observe|blocked|invalidated]` remain non-executable state evidence.
- A candidate with missing required capacity may remain canonical candidate evidence but must not produce an active action.
- Renderer and consumers must never infer executability from candidate rank or candidate priority.

### 7.3 Candidate invariant

- `candidates[family]` contains only canonical, strategy-accepted evidence selected from that family's authoritative artifact.
- Candidate `priority` is evidence tier/importance, not proof of an executable action.
- Raw candidates and rejected rows belong only to provenance/rejection diagnostics and never to `candidates`.
- Renderer must describe candidate sections as evidence and direct the operator to the action sections for execution.

### 7.4 Summary invariant

`strategy_summary` must separately report:

1. count of `actions[state=active]`;
2. candidate evidence count by strategy family;
3. count of data gaps when non-zero.

It must not call all candidates “actions” and must not include raw/rejected rows in candidate counts.

Expected shape, preserving concise Chinese tone:

```text
有效行动 2 条；候选证据：Sell Put 2，Covered Call 0，Combo Yield 0；数据缺口 1 条。
```

For `actionability=blocked`, retain the existing blocker-focused summary.

### 7.5 Renderer invariant

The header shows both independent dimensions:

```text
状态：可执行（LIVE） | 数据质量：降级（DEGRADED）
```

Add a bounded label map for `ready|degraded|blocked`; unknown values render explicitly rather than silently becoming `ready`.

Candidate headings become evidence-oriented, for example:

```text
## Sell Put 候选证据（非行动）
```

Candidate lines may display whether capacity evidence is known, but renderer must not recreate strategy eligibility rules. The authoritative rule remains: execution comes only from an `active` action in `actions`.

Blocked rendering remains blocked and does not expose candidates as executable alternatives.

### 7.6 Diff and persistence invariant

Candidate-content correction may legitimately change the semantic brief content and therefore the computed semantic digest. However this work unit must not alter:

- normalization schema;
- stable action identity;
- material-diff classification;
- revision allocation;
- full/delta selection;
- delivery key construction;
- pointer confirmation.

Regression tests must prove no-send does not advance delivery state and an account with no prior delivery pointer still prepares a full lifecycle.

## 8. Test and fixture matrix

### 8.1 Service fixtures

Add production-shaped tests to `tests/test_daily_decision_brief_service.py`:

| Fixture | Input | Required assertions |
|---|---|---|
| `conflict` | labeled P430/P440; raw also contains higher-ranked P450 | only P430/P440 in candidates/actions; P450 absent from entire normalized brief, events, summary, and renderer input |
| `empty_header_only` | header-bearing labeled CSV with zero rows and minimal canonical columns; non-empty raw | Sell Put candidates/actions empty; source available; no raw fallback; no missing-source gap |
| `empty_wrong_header` | zero-row labeled CSV with unrelated/incompatible columns; valid raw | malformed/unavailable gap; no fallback |
| `empty_controlled_newline` | labeled bytes exactly `b"\n"` and separately `b"\r\n"`; non-empty raw | authoritative empty and available; no fallback |
| `empty_zero_byte_truncated` | zero-byte labeled; valid raw | malformed/unavailable gap; no fallback; raw contract absent |
| `empty_unrecognized_whitespace` | labeled contains spaces/tabs plus newline; valid raw | malformed/unavailable gap; no fallback |
| `missing` | raw exists; labeled missing | no Sell Put candidates/actions; canonical-missing gap; raw contract absent; family unavailable |
| `malformed` | syntactically malformed labeled; valid raw | no fallback; `csv_unavailable` with error type; family unavailable if no other labeled file |
| `partial` | one valid labeled symbol; one raw-only symbol; optionally one malformed labeled symbol | valid symbol retained; failed symbols absent; per-source gaps present; `status=degraded`; reliable other actions preserved |

Add a partial cross-family assertion: a usable Covered Call active action remains available when one Sell Put symbol is missing/malformed. Expected result is `actionability=live_actionable` plus `status=degraded`.

### 8.2 Renderer fixtures

Update `tests/test_daily_decision_brief_renderer.py` to prove:

- header renders both actionability and `ready|degraded|blocked` data quality;
- candidate sections are explicitly evidence/non-action sections;
- an evidence candidate with no capacity is not rendered as an action;
- action sections distinguish `active` from `observe|blocked` through existing state labels;
- summary separates active action count, family candidate counts, and data-gap count;
- bounded message and blocked-message behavior remain intact.

### 8.3 CLI `OM_RUNTIME_ROOT` integration fixture

In `tests/test_daily_decision_brief_cli.py`:

1. create different canonical brief revisions under `repo_root` and `runtime_root`;
2. set `OM_RUNTIME_ROOT=runtime_root`;
3. invoke the actual CLI handler with the actual read service, not a mocked `read_daily_brief_view`;
4. assert the runtime-root run/revision is returned and repo-local shadow content is absent;
5. clear `OM_RUNTIME_ROOT` and assert repo-root fallback still works;
6. assert no files or pointers were modified.

### 8.4 Agent Tool `OM_RUNTIME_ROOT` integration fixture

In `tests/test_daily_decision_brief_agent_tool.py`:

1. create distinct repo-root and runtime-root briefs;
2. monkeypatch only `repo_base()` to the repo fixture and set the real `OM_RUNTIME_ROOT` environment variable;
3. invoke `DAILY_DECISION_BRIEF_READ_TOOL.call()` through its public contract;
4. assert the runtime-root run/revision wins;
5. clear `OM_RUNTIME_ROOT` and assert repo fallback;
6. retain path masking and pure-read manifest assertions.

### 8.5 Notification/delivery regression fixtures

Update focused tests in `tests/test_daily_decision_brief_notification_flow.py` and reuse repository tests to prove:

- no-send prepares content but does not confirm or advance a delivery pointer;
- no existing pointer produces `delivery_kind=full`;
- corrected candidate content is rendered from the same lifecycle brief that was persisted;
- revision/diff/delivery-key contracts remain unchanged.

## 9. Event rendering decision

**Decision: defer event rendering to a separate follow-up work unit.**

Reasoning:

- current assembler emits plural fields `event_types` and `event_dates`, while renderer reads singular/generic `event_type|kind|type` and `reason|status`;
- this is a real presentation defect, but it does not cause the canonical candidate-source error or runtime-root split;
- fixing it in the release-blocking batch would expand renderer/schema/digest review scope and make Canary failure attribution less clear;
- events generated from raw-only rows disappear automatically once the canonical source is fixed, which removes the safety-critical part of the symptom.

Follow-up scope, after the Canary correction is accepted:

- decide whether plural values remain strings or normalize to arrays;
- align assembler and renderer field names;
- add event dedupe and rendering fixtures;
- assess semantic-digest impact explicitly.

The deferred event issue does not authorize suppressing or fabricating event data in this batch. Existing event fields remain unchanged.

## 10. Implementation slices

### S1 — Canonical Sell Put source and failure semantics

**Objective**: enforce labeled-only Sell Put assembly and the artifact state table.

**Allowed files**:

- `src/application/daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_service.py`
- S1 Gateflow artifacts

**Required tests**: conflict, empty, missing, malformed, partial, cross-family degraded/live scenario.

**Stop condition**: focused service tests pass; no raw row can reach the brief; code review confirms Covered Call behavior is unchanged.

### S2 — Runtime-root read authority

**Objective**: make CLI and Agent Tool read the canonical stateful runtime root.

**Allowed files**:

- `src/interfaces/cli/daily_brief_ops.py`
- `src/application/agent_tools/daily_brief.py`
- `tests/test_daily_decision_brief_cli.py`
- `tests/test_daily_decision_brief_agent_tool.py`
- S2 Gateflow artifacts

**Stop condition**: both integration fixtures prove `OM_RUNTIME_ROOT` precedence and repo fallback; no global root helper changes.

### S3 — Payload presentation and Canary observability

**Objective**: expose action/candidate/data-quality distinctions and make the actual no-send prepared message verifiable without persisting message content.

**Allowed files**:

- `src/application/daily_decision_brief_service.py` — summary wording/counts only
- `src/application/daily_decision_brief_renderer.py` — header/candidate-evidence presentation plus the shared public render-limit normalizer
- `src/application/tick_notification_flow.py` — prepared-message digest and resolved render-context metadata only
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- S3 Gateflow artifacts

Because the production no-send flow does not persist prepared message content, add bounded, non-sensitive observability to each `tick_metrics.daily_brief.prepared[]` item after the actual message is rendered:

```text
brief_id
message_sha256
message_chars
render_limits:
  max_actions_per_priority
  max_candidates_per_strategy
  max_rejection_reasons
```

The hash must be SHA-256 of the exact UTF-8 prepared message passed into `messages_by_account`. `render_limits` must contain the three bounded effective integer limits actually passed to `render_daily_brief_lifecycle()`, after renderer/default normalization—not merely the raw config mapping. Add one narrow public renderer helper, `resolve_daily_brief_render_limits(limits)`, in `daily_decision_brief_renderer.py`; all full/delta renderer entry points and `tick_notification_flow.py` must reuse that helper so normalization is not duplicated or imported through a private function. Do not persist the message body. For `delivery_kind=none`, omit the message digest/length or store them as null consistently while still recording the resolved limits if rendering was evaluated. This metadata is operational evidence only and must not enter brief semantic digest, material diff, or delivery-key calculation.

Do not make CLI or Agent Tool load production notification config merely to reproduce notification bytes. Their read contract remains independent and pure-read.

**Stop condition**: renderer and notification-flow focused tests pass; the prepared digest is demonstrably derived from the exact outgoing no-send message; repository/diff contracts remain unchanged.

### S4 — Regression and production Canary

**Objective**: close regressions, publish through the normal release flow, then run a production-scoped HK no-send Canary.

S4 contains no feature expansion. Event rendering remains deferred.

## 11. Validation ladder

Run after each slice, then cumulatively:

```bash
# S1
python3 -m pytest tests/test_daily_decision_brief_service.py

# S2
python3 -m pytest \
  tests/test_daily_decision_brief_cli.py \
  tests/test_daily_decision_brief_agent_tool.py

# S3
python3 -m pytest \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_notification_flow.py

# Daily Brief subsystem regression
python3 -m pytest tests/test_daily_decision_brief_*.py

# Agent public contract regression
python3 -m pytest \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py

# Formatting/static diff hygiene
git diff --check
```

Before release, run the repository's current release validation ladder, including supported Python and `scripts/release_check.py`, without weakening any check.

## 12. Four-surface HK no-send production Canary

### 12.1 Preconditions

Before any remote mutation:

- release artifact and server version are verified through the normal VERSION-driven flow;
- production config remains default-off outside the controlled Canary invocation;
- Canary command is explicitly `no-send`;
- record pre-Canary delivery pointer state for `lx` and `sy`;
- record the active `OM_RUNTIME_ROOT` and verify it is the production state root;
- do not delete release-local shadow artifacts; they are useful negative controls.

### 12.2 Required four surfaces

For each account (`lx`, `sy`):

1. Capture the Canary `run_id`, `market_trading_date`, `revision`, and `brief_id` from the run-scoped JSON/prepared audit first.
2. Freeze those immutable identifiers for the rest of verification; do not use a mutable `latest` read as primary evidence.
3. Collect these four surfaces:

   1. **Production JSON**: immutable/run-scoped Daily Brief JSON plus the canonical revision JSON under production runtime root.
   2. **Prepared notification**: `tick_metrics.daily_brief.prepared[]` entry from the actual no-send notification path, including `delivery_kind`, revision, `brief_id`, `message_sha256`, and effective `render_limits`.
   3. **CLI exact read**:

      ```bash
      ./om daily-brief day \
        --account <account> \
        --market HK \
        --date <market_trading_date> \
        --revision <revision> \
        --json
      ```

   4. **Agent Tool exact read**:

      ```bash
      ./om-agent run --tool daily_decision_brief_read --input-json \
        '{"account":"<account>","market":"HK","date":"<market_trading_date>","revision":<revision>}'
      ```

An additional `latest` read may verify where the current pointer points at collection time, but it is not part of the primary four-surface equality proof.

### 12.3 Structured source-identity criteria

The four surfaces prove source identity independently of presentation bytes:

- prepared audit, production JSON, CLI, and Agent Tool agree on account, market, market trading date, `run_id`, `brief_id`, and revision;
- CLI `brief` and Agent Tool `brief` are exact structural matches for the persisted canonical revision JSON;
- actionability and status stored in the brief are identical;
- active-action count, candidate count by family, and data-gap count are identical;
- the run-scoped brief JSON and canonical immutable revision JSON represent the same normalized brief;
- any concurrently newer `current` revision is recorded as an observation but does not invalidate the exact-revision proof.

### 12.4 Renderer-integrity criteria

Presentation verification is separate because notification, CLI, and Agent reads may have different valid render contexts.

For a first/full no-send preparation:

1. `delivery_kind` must be `full`.
2. Re-render the persisted lifecycle/brief offline with:
   - the persisted original `actionability`;
   - the exact effective `render_limits` recorded in prepared audit;
   - the same `render_daily_brief_lifecycle()` production renderer.
3. SHA-256 of that re-rendered UTF-8 message must equal prepared `message_sha256`, and its character count must equal `message_chars`.
4. Inspect the reproduced prepared message for the content criteria in the next section.

CLI and Agent Tool Markdown are checked separately:

- their structured `brief` must already satisfy section 12.3;
- record each `effective_actionability` and query timestamp;
- if both reads have the same effective actionability, their Markdown SHA-256 should match because both use the same default read renderer context;
- if the reads straddle `valid_until_utc`, Markdown hash equality is not required; repeat both after the boundary or compare only the structured brief and explicitly record the expected LIVE-to-PLANNING transition;
- neither CLI nor Agent Markdown is required to equal prepared notification Markdown when production render limits differ.

If `delivery_kind=none` because the exact semantic brief was already delivered, the Canary is inconclusive for prepared-message integrity; generate a fresh production-scoped no-send run or use an account/date with no delivery pointer. Do not force or mutate the delivery pointer merely to obtain a full message.

### 12.5 Content criteria

> **Amended 2026-07-20**: live Canary acceptance uses exact-run artifact identity sets. Named P430/P440/P450 contracts remain deterministic fixture cases only and are not live-market acceptance constants. See `docs/gateflow/daily-decision-brief-canary-fix-acceptance-amendment-20260720.md`.

For each exact `run_id` and account, construct these sets from the immutable run-scoped account directory:

- `L`: normalized non-empty `contract_symbol` identities from canonical `*_sell_put_candidates_labeled.csv` artifacts;
- `R`: normalized non-empty `contract_symbol` identities from matching raw `*_sell_put_candidates.csv` artifacts;
- `U = R - L`: raw-only identities for that exact run and account;
- `C`: normalized `contract_symbol` identities in `candidates.sell_put`;
- `A`: normalized `contract_symbol` identities in Sell Put actions where `action_type=open_candidate`. Close/observe/blocked position actions are not candidate-derived and are outside `A`.

Identity normalization is trim plus uppercase on non-empty `contract_symbol`. A Sell Put candidate or open-candidate action with an empty identity cannot satisfy membership and is a Canary failure. Build `L` as an identity-to-core-fields map using symbol normalized through the existing `domain.domain.symbol_identity.canonical_symbol()`, ISO expiration, and numeric strike normalized through decimal value equality. Exact duplicates may deduplicate; conflicting labeled core fields for one identity fail closed. Candidate/action core fields must match the unique labeled row. Numeric formatting such as `450` versus `450.0` must compare equal. Sets must never be unioned across accounts, runs, or mutable `current` paths.

Raw reads used to construct `R` and `U` are audit-only after run outputs are frozen. Raw rows must never enter Daily Brief assembly, ranking, candidate/action/event builders, or renderer inputs; audit helpers must remain outside the normal `daily_decision_brief_service.py` candidate-loading path.

The production Canary passes the Sell Put authority check only if all of the following hold:

```text
C subset-of L
A subset-of L
(C union A) intersect U = empty
```

Additionally:

- every Sell Put candidate and Sell Put `open_candidate` action reports a source path ending in `_sell_put_candidates_labeled.csv`;
- every identity in `U` is absent from `candidates.sell_put`, Sell Put `open_candidate` actions, candidate-derived events carrying a contract identity, and their rendered recommendation sections; explicitly labeled rejection/provenance diagnostics may mention rejected identities without making them actionable;
- a missing raw counterpart does not invalidate a valid labeled artifact; `R` and `U` may therefore be empty;
- the re-evaluation consumes a sorted SHA-256 manifest of every raw/labeled, four-surface, prepared/diff/renderer, and safety input; missing files or manifest drift fail closed;
- no candidate lacking required capacity may appear as an active action;
- candidate evidence is visibly identified as non-action evidence;
- if partial/missing evidence remains, `status=degraded` and the relevant `data_gaps` are visible while reliable active actions remain usable;
- summary active-action count equals the number of `actions[state=active]`.

The P430/P440/P450 conflict remains required in the fixed test fixture from sections 5.2 and 10; fixture assertions prove the known raw-only counterexample, while live Canary assertions prove exact-run set membership without assuming which strikes are labeled that day.

### 12.6 Safety criteria

The Canary passes only if:

```text
no_send = true
send_attempted_count = 0
send_confirmed_count = 0
send_failed_count = 0
```

Additionally:

- delivery pointer before and after is byte-for-byte unchanged or absent in both snapshots;
- no provider message ID exists;
- production default-off configuration is restored/unchanged;
- no config, ledger, broker-facing state, or Feishu state was written by the verification steps.

Any mismatch is a stop condition. Do not authorize real sending on a partial pass.

## 13. Rollback and stop conditions

Stop implementation or rollout if:

- any exact-run identity in `U = R - L` appears in candidates, candidate-derived actions/events, or rendered recommendation sections;
- one labeled identity maps to conflicting core fields, or candidate/action core fields disagree with the unique labeled row;
- raw audit rows enter the normal runtime candidate/ranking/render path, or re-evaluation inputs do not match their sorted SHA-256 manifest;
- a valid empty labeled artifact causes raw fallback;
- CLI or Agent Tool still reads release-local shadow state when `OM_RUNTIME_ROOT` is set;
- one read surface points to a different revision;
- no-send advances a delivery pointer or attempts provider delivery;
- the fix requires changing global `repo_base()`, repository lifecycle, semantic diff, delivery key, or production configuration contract.

Rollback is the normal release rollback to the previous verified version. Because this work unit has no schema or state migration, rollback must not require state rewriting.

## 14. Residual risks and follow-ups

- Existing release-local shadow artifacts remain on disk but become non-authoritative; cleanup is a separate guarded operations task.
- Event rendering remains a tracked follow-up per section 9.
- Historical briefs created from mixed raw/labeled inputs remain historical evidence and are not rewritten.
- Real provider behavior is not exercised by no-send; real-send authorization remains a distinct user decision after Canary evidence review.

## 15. Gate transition

- **Current gate**: revised implementation plan
- **Next gate**: adversarial plan re-review
- **Implementation entry condition**: plan re-review returns `pass` or `pass-with-risks` with no unresolved correctness/safety blocker
- **Production entry condition**: implementation review, focused tests, broad regressions, release verification, and explicit no-send Canary authorization
- **Real-send entry condition**: successful four-surface Canary plus separate explicit user authorization
