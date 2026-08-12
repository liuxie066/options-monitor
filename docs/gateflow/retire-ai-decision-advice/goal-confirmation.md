# Gateflow Goal Confirmation — Retire AI Decision Advice

- Work unit: `retire-ai-decision-advice`
- Gate: `goal confirmation`
- Date: 2026-08-12
- Status: confirmed by user
- Branch: `refactor/retire-ai-decision-advice`
- Base: `origin/main@ded8f882`
- Supersedes work unit: `candidate-brief-evidence-integrity`
- Artifact path: `docs/gateflow/retire-ai-decision-advice/goal-confirmation.md`

## Confirmed goal

Completely retire the AI Decision Advice product capability from active source and public product surfaces. The
supported decision/reporting path after this work unit is deterministic:

`Candidate Engine + account funds/positions + Close Advice -> Daily Brief -> notification`

No scheduled tick, Daily Brief read/render operation, service bundle, or public configuration may invoke or expose
AI Decision Advice, external evidence collection, or its formal Advice records.

## Motivation and first-principles judgment

The feature has remained operationally unreliable and adds an external-news/model dependency to a report whose
candidate, capacity, holdings, and Close Advice facts already have deterministic owners. Increasing the timeout,
enabling deeper reasoning, or changing the model does not remove that availability dependency. Retiring the overlay
removes the failure mode without weakening Candidate Engine or position authority.

Direct code evidence from the confirmed base:

- `src/application/multi_account_tick.py` publishes Advice observation partitions and hands prepared inputs into the
  notification flow when `ai_decision_advice.enabled` is true.
- `src/application/daily_decision_brief_service.py` calls `run_or_reuse_ai_decision_advice()` while assembling every
  account/market Brief and persists `ai_decision_advice` plus its evidence index in the Brief.
- `src/application/daily_decision_brief_renderer.py` and `src/application/agent_tools/daily_brief.py` expose the
  Advice section through notifications, CLI, and Agent reads.
- `src/application/service_deploy.py` renders a dedicated evidence collector service/timer and binds the DeepSeek
  credential to both collector and tick services.
- `src/application/prepared_portfolio_distribution.py` is an Advice-only preparation layer. In contrast,
  `prepared_option_positions_context` also supplies Close Advice and candidate execution and is shared authority.
- Daily Brief delivery retries reuse frozen rendered messages verbatim, so a pre-retirement pending envelope may
  still contain AI copy even after the renderer is removed.

## Success signals

1. Active runtime source has no Advice orchestration, model call, external-evidence collector, observation
   publication, Advice input handoff, or Advice-specific portfolio-distribution preparation path.
2. Newly assembled Daily Briefs, notification renderers, CLI output, Agent output, output contracts, and material
   diffs contain no AI Decision Advice fields, sections, or copy.
3. The authored/runtime config contract no longer supports `ai_decision_advice`; examples and validation describe it
   as retired rather than silently accepting a no-op feature flag.
4. Rendered service bundles contain no AI evidence collector unit and tick services receive no Advice-only DeepSeek
   credential binding. Generic Assistant/Copilot LLM providers remain available.
5. Advice-exclusive modules, prompts, adapters, and tests are physically removed. Shared Candidate Engine, sealed
   opening snapshots, option-position preparation, Close Advice, portfolio query tools, and generic DeepSeek secret
   registry/provider support remain intact.
6. Existing historical Advice/Evidence/Brief files are not deleted or rewritten. Persisted legacy Briefs retain a
   narrowly scoped passive integrity/read compatibility path where required, but public Brief projections strip
   retired AI fields and never enrich from formal Advice JSONL.
7. The notification path refuses to send a retryable frozen envelope containing retired AI Advice copy. It reports a
   typed local blocker and leaves the envelope/state unchanged for explicit operator resolution.
8. Focused config, service, tick, Daily Brief, Agent, legacy-integrity, and notification retry regressions pass;
   repository compile/analyze, dependency-boundary validation, and the full test suite meet the existing baseline.

## Scope boundary

### Included

- Source, tests, documentation, and generated dependency documentation needed to remove the active capability.
- Removal of Advice-only prepared portfolio distribution and its tick metrics/handoff structures.
- Minimal passive compatibility necessary to validate/read existing immutable Daily Brief records without exposing
  retired content.
- A fail-closed guard for frozen legacy notification envelopes.
- A retirement note at the existing AI design-document path so old links state the feature is retired.

### Excluded

- Candidate thresholds, recall, ranking, capacity, quote policy, sealed snapshot schema, Close Advice policy, and
  ledger behavior.
- Removal of generic Assistant/Copilot, generic DeepSeek provider support, portfolio-management query tools, or the
  shared DeepSeek logical credential.
- Deletion, cleanup, migration, or rewriting of historical runtime artifacts, delivery state, evidence, Advice JSONL,
  secrets, or caches.
- Modification of `config.yaml`, `config.us.json`, `config.hk.json`, production runtime configuration, systemd state,
  notification state, ledger state, Feishu, or broker-facing data.
- Release, deployment, remote upgrade, production service changes, live notification tests, merge, approval, marking
  a PR ready, or deleting any branch/worktree.

## Existing work ownership and isolation

The prior work unit `candidate-brief-evidence-integrity` is superseded for product behavior but its dirty workspace is
protected evidence, not implementation input. The user confirmed that it remains untouched while this work unit runs
in an isolated worktree.

- Protected worktree: `<original-options-monitor-worktree>`
- Protected branch: `fix/candidate-brief-evidence-integrity@8a3520f0`
- Protected dirty patch SHA-256 at confirmation: `9ba116779673b0d5485d4ea5cc29cee0950e9b72c972627b395a23cb435de87f`
- Isolated worktree: `<temporary-isolated-worktree>`
- Isolated branch/base: `refactor/retire-ai-decision-advice@ded8f882`

No implementation hunk is copied from the dirty worktree. Non-AI improvements discovered there, including treating
prefetch `fetched` as success and evidence-backed compact reminder behavior, may be re-derived only if they are
necessary to preserve deterministic Daily Brief correctness after Advice removal; otherwise they remain outside this
work unit.

## Overdesign deliberately excluded

- No replacement AI feature, provider abstraction, fallback model, timeout/reasoning tuning, feature tombstone
  service, new report schema version, database migration, or parallel compatibility renderer.
- No broad removal of all LLM, Portfolio, DeepSeek, or research capabilities.
- No automatic historical cleanup or hidden mutation of delivery state.
- No production migration logic inside ordinary runtime paths.

## Delivery boundary

Gateflow may create reviewed local commits, push the isolated branch, and open/review a Draft PR. It must stop short
of merge, release, deployment, remote upgrade, production configuration/service changes, live notification, or
historical-data cleanup unless separately authorized.

## Blocking open questions

None. The user confirmed the isolation/supersession strategy and the complete-retirement design boundary on
2026-08-12.
