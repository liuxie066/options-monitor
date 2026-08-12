# Gateflow Plan — Retire AI Decision Advice

- Work unit: `retire-ai-decision-advice`
- Gate: `plan`
- Date: 2026-08-12
- Status: accepted after adversarial PlanReview re-review (`pass-with-risks`)
- Initial PlanReview: `docs/reviews/plan-review-20260812-112642.md`
- Accepted re-review: `docs/reviews/plan-review-20260812-113353.md`
- Branch: `refactor/retire-ai-decision-advice`
- Base: `origin/main@ded8f882`
- Goal artifact: `docs/gateflow/retire-ai-decision-advice/goal-confirmation.md`
- Artifact path: `docs/gateflow/retire-ai-decision-advice/plan.md`

## Goal, motivation, and success signals

Retire AI Decision Advice as an active product capability and leave one deterministic reporting chain:

`Candidate Engine + account funds/positions + Close Advice -> Daily Brief -> notification`.

Completion is measured by the eight confirmed success signals in the goal artifact: no Advice generation or external
evidence path, no public Advice projection, no supported Advice config, no collector/service credential binding,
physical removal of exclusive code, immutable historical compatibility without public exposure, fail-closed rejection
of frozen legacy AI delivery envelopes, and focused/full validation at the existing repository baseline.

## Non-goals and safety boundary

- Do not change candidate eligibility, thresholds, ranking, quantities, capacity, quote policy, sealed snapshots,
  Close Advice policy, ledger semantics, or option-position authority.
- Do not remove generic Assistant/Copilot, DeepSeek/Kimi/OpenAI provider support, portfolio-management queries,
  research surfaces, or shared option-position preparation.
- Do not delete or rewrite historical Advice, Evidence, Brief, delivery, secret, cache, ledger, or runtime files.
- Do not change authored/generated production configs, systemd state, Feishu, broker, or remote environments.
- Do not merge, release, deploy, upgrade, approve, mark ready, or delete branches/worktrees.

## Goal alignment

| Plan item | Confirmed success signals |
|---|---|
| Slice 1 — remove Advice from Brief/public delivery and guard legacy retries | 2, 6, 7, 8 |
| Slice 2 — remove generation/config/service subsystem and exclusive code | 1, 3, 4, 5, 8 |
| Documentation and dependency graph updates inside the owning slice | 2–5, 8 |
| Aggregate and PR validation | 1–8 |

Two slices are the smallest behavior-oriented split. Slice 1 independently guarantees that no newly created or
retried product output exposes Advice. Slice 2 removes the now-unreachable generation and deployment machinery.
Splitting by file/module would add review gates without an independently useful behavior; combining both would make
legacy integrity/delivery regressions difficult to isolate from large physical deletions.

## First-principles judgment and direct code evidence

The deterministic owners already exist and do not depend on AI Advice:

- Candidate Engine owns recall, eligibility, capacity, and ranking; its sealed `opening_candidate_snapshot.v1` is the
  report fact authority.
- The SQLite ledger and shared `prepared_option_positions_context` supply option-position/Close Advice facts.
- Daily Brief owns deterministic assembly, persistence, diff, rendering, delivery envelopes, and retries.

The removable overlay is directly visible in source:

- `multi_account_tick.py` conditionally publishes Advice observation partitions and transfers Advice-only prepared
  inputs into `TickNotificationRequest`.
- `daily_decision_brief_service.py` invokes `run_or_reuse_ai_decision_advice()` and adds two persisted Brief fields.
- `daily_decision_brief_renderer.py` and `agent_tools/daily_brief.py` render/enrich those fields.
- `domain/domain/daily_decision_brief.py` normalizes and diffs Advice, while repository semantic digests currently
  include the normalized overlay.
- `tick_notification_flow.py` reuses persisted `rendered_message` bytes for retries; removing the renderer alone cannot
  prevent an old AI-containing envelope from being sent.
- `tick_account_execution.py` prepares portfolio-management strategic distribution solely for Advice, but its option
  context is also used by Close Advice and must remain.
- `service_deploy.py` renders dedicated collector units and binds the DeepSeek credential to collector/tick units.

## Contract and state decisions

### New Daily Brief writes

- `assemble_daily_decision_brief()` no longer accepts prepared Advice inputs, invokes a model, or adds
  `ai_decision_advice` / `ai_decision_advice_evidence_index`.
- `normalize_daily_decision_brief()` unconditionally strips those retired keys, including when a caller supplies a
  legacy Brief object. This makes the current normalized/write contract incapable of re-persisting Advice.
- The existing `daily_decision_brief.v1` schema remains. Removing optional overlay fields does not create a new
  decision fact contract, so a v2 schema/migration is unnecessary.

### Passive historical compatibility

- The current normalizer strips legacy retired fields and performs no Advice-specific validation or enrichment.
- `daily_brief_compatible_digests()` receives the original persisted mapping, computes the current stripped digest,
  then—only when original retired keys exist—computes an overlay-era digest by attaching those exact original
  key/value pairs to the same normalized core. It never inserts Advice defaults, parses Advice content, or accepts an
  arbitrary mismatch. Tests use legacy fixtures with known source digests.
- Repository reads, product projections, renderers, Agent, and CLI therefore consume one stripped current contract and
  never access formal Advice JSONL. No per-consumer strip list is needed.
- Material diff has no Advice change type. Full semantic delivery digest defensively excludes retired keys at its raw
  input boundary, so old Advice state cannot create a new material candidate alert or change a new deterministic
  delivery key.
- Repository bytes and delivery pointers are never rewritten merely because a legacy field exists.

### Frozen retry state

- Before copying a retryable envelope into outbound messages, notification flow consumes one repository-owned,
  read-only retired-payload inspection result.
- For `successful_brief`, the repository inspection reuses strict revision path, identity, and compatible-digest
  validation and classifies the raw referenced revision as retired when either Advice key is present. For every source
  kind it scans the frozen fallback message and normalized card transport for a narrow set of legacy AI Advice
  section markers as defense in depth. Result values are exactly `clean` or `legacy_ai_payload_retired`; storage paths
  and raw mappings stay private to the repository.
- A blocked envelope remains `pending`/`ambiguous`; its rendered bytes and delivery state are unchanged. No replacement
  envelope is automatically fabricated.
- Tick metrics/audit record stable code `legacy_ai_payload_retired`; the account is omitted from outbound messages.
  Fixed-failure envelopes without retired content remain retryable.

### Configuration

- `config.yaml` authoring removes `ai_decision_advice` from allowed passthrough keys. The YAML unknown-key owner
  recognizes that exact retired key at both root and market scope and emits a targeted removal error; nearby typos
  retain the ordinary unknown-key behavior.
- Runtime `validate_config()` recognizes the exact key only as retired and emits a targeted configuration error. It is
  never accepted as a silent no-op.
- No config migration or production config write is performed in this work unit.

### Services and secrets

- Service rendering never emits `options-monitor-ai-evidence-collector.service/.timer`.
- Tick services no longer bind `LLM_DEEPSEEK_API_KEY` for Advice. Collector is removed from credential consumers.
- `LLM_DEEPSEEK_API_KEY`, provider registry support, and Assistant/Feishu/Clawbot bindings remain because they are
  shared capabilities.
- Existing installed collector units are production drift to retire during a separately authorized upgrade; source
  implementation does not call `systemctl`.

### Portfolio-management boundary

- Delete `prepared_portfolio_distribution.py` and its tick fields/metrics/recovery because the layer is Advice-only.
- Remove only the Advice-specialized `PortfolioManagementClient.read_distribution()` and strict single-account Advice
  response validator/constants.
- Preserve generic `read_view("distribution")`, portfolio query tools, assignment scenario, valuation evidence, and
  holdings sync.

## Slice 1 — Deterministic Brief/public cutover and legacy-delivery guard

### Objective and expected outcome

No new Brief or public read/render contains AI Advice, no formal Advice record is read, and no frozen legacy
AI-containing delivery envelope can be sent. Historical revisions remain integrity-valid and unchanged.

### Allowed files/modules

- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `src/application/daily_decision_brief_repository.py`
- `src/application/agent_tools/daily_brief.py`
- `src/application/tick_notification_flow.py`
- `tests/notification_format_assertions.py`
- `tests/test_daily_decision_brief_domain.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_agent_tool.py`
- `tests/test_daily_decision_brief_repository_v2.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- Slice Gateflow implementation/review/fix artifacts.

### Exact allowed changes

1. Remove Advice imports/model calls, persisted fields, renderer branches/copy, Agent output-contract entries,
   formal-record enrichment, and Advice-specific diff types. Temporarily retain the existing private Brief assembler
   handoff parameters so the Slice 1 Tick call contract remains runnable, but ignore all Advice-only values; the
   deterministic opening snapshot parameter may continue to feed its existing non-model loader.
2. Strip retired keys at the canonical normalizer/write/read boundary. Preserve legacy raw key/value pairs only as
   local inputs to exact compatible-digest calculation; never expose them on the normalized Brief.
3. Exclude legacy retired fields from full semantic delivery digest and all material-diff decisions.
4. Add the repository-owned read-only legacy-envelope inspection contract and consume it in both delivery-only and
   post-scan retry selection paths before populating outbound message or transport maps.
5. Record `legacy_ai_payload_retired` in lifecycle audit/metrics without mutating the envelope.
6. Update tests so current normalization strips legacy keys, new writes omit fields, overlay-era source digests remain
   valid while core/legacy-field tampering does not, renderers ignore old fields, Advice-only changes are non-material,
   the transitional old handoff arguments do not trigger a model or appear in output, and both retry paths refuse raw
   field/message-only/card-only old AI envelopes while normal/fixed-failure retries remain unchanged.

### Invariants and error handling

- Candidate/position/funds/action/rejection/event data and their ordering remain unchanged.
- Historical digest acceptance remains exact; do not weaken SHA-256 validation or accept arbitrary normalized drift.
- The repository inspection must reuse the envelope's already-owned account/market/date/revision/source-digest
  contract; notification code must not construct revision paths or parse raw Brief files.
- A missing/corrupt referenced revision continues through the existing state-error path; do not guess whether it is
  safe to send.
- The guard must inspect before writing `messages_by_account`; blocked payloads cannot leak through card transport.
- No delivery-state write, expiry, resolution, or replacement occurs in the guard.

### Non-goals

- No deletion of core Advice modules yet; no config/service/tick-execution changes; no unrelated Brief layout rewrite.
- Do not copy compact-card or reminder changes from the protected dirty worktree.

### Validation

```bash
./.venv/bin/python -m pytest -q \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_agent_tool.py \
  tests/test_daily_decision_brief_repository_v2.py \
  tests/test_daily_decision_brief_notification_flow.py
./.venv/bin/python -m compileall -q domain src tests
git diff --check
```

Expected assertions: normalizing a legacy object and all new persisted Briefs omit retired keys; known overlay-era
fixtures retain valid digest while any covered-field tamper fails; public JSON/Markdown contains no retired keys/copy;
AI-only before/after differences are non-material; old private handoff inputs are ignored; pending and ambiguous AI
envelopes—including card-only cases—remain byte/state-identical and are not outbound; ordinary retries still pass.

### Completion and stop condition

Complete when focused tests pass and the diff contains only allowed files/artifacts. Stop if legacy integrity cannot be
preserved without a schema migration or if blocking a retry would require mutating state.

## Slice 2 — Remove generation, configuration, services, and exclusive implementation

### Objective and expected outcome

Physically remove the unreachable Advice subsystem and all generation/deployment entry points while preserving shared
LLM, portfolio, Candidate Engine, option-position, and Close Advice capabilities.

### Prerequisite

Accepted Slice 1 commit.

### Allowed files/modules

- `src/application/ai_decision_advice/**` (delete)
- `src/interfaces/cli/ai_evidence_collector.py` (delete)
- `src/infrastructure/deepseek_responses.py` (delete)
- `src/application/prepared_portfolio_distribution.py` (delete)
- `src/application/daily_decision_brief_service.py` for removing the now-unused transitional private handoff parameters
- `src/application/multi_account_tick.py`
- `src/application/tick_account_execution.py`
- `src/application/tick_notification_flow.py` only for removing now-dead Advice handoff request fields/helper
- `src/application/config_validator.py`
- `src/application/config_yaml.py`
- `src/application/service_deploy.py`
- `src/application/secret_store/registry.py`
- `src/infrastructure/portfolio_management_client.py`
- `configs/examples/config.yaml.example`
- Live docs: `docs/AI_DECISION_ADVICE_DESIGN.md`, `docs/AGENT_WIKI.md`, `docs/DEPLOY_LINUX_MAC.md`,
  `docs/SECRET_STORAGE.md`, `docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`, `CHANGELOG.md`,
  `docs/DEPENDENCY_GRAPH.md`.
- Advice-exclusive tests (delete): `tests/test_ai_decision_advice_*.py`,
  `tests/test_deepseek_responses.py`, `tests/test_prepared_portfolio_distribution.py`.
- Shared affected tests: config YAML/validator coverage, multi-account tick, tick execution barrier, option-position
  preparation, service deploy/credential materializer, portfolio-management client/contract, Agent plugin contract,
  and dependency-boundary tests selected from direct imports.
- Slice Gateflow implementation/review/fix artifacts.

### Exact allowed changes

1. Remove observation-set/symbol-identity publication and Advice-gated candidate/portfolio/option handoff from Tick.
2. Remove Advice-only distribution preparation, recovery, outcome fields, metrics, PM read counts, request/result
   plumbing, `_advice_handoff_for_account()`, and the transitional Brief assembler parameters retained in Slice 1.
   Retain shared option-position manifests and Close Advice barrier behavior.
3. Remove authoring/runtime config support and add targeted retired-key regressions for root YAML, market YAML, and
   runtime JSON; verify a nearby misspelling still receives the generic unknown-key response.
4. Remove collector unit rendering, Advice-enabled service branching, tick/collector DeepSeek credential binding, and
   obsolete consumer metadata. Preserve Assistant credential resolution/bindings.
5. Delete exclusive modules/prompts/CLI/adapter and their exclusive tests after proving no active production import.
6. Remove only Advice-specialized PM distribution client methods/contracts while retaining generic distribution view.
7. Update current docs and example config. Replace the AI design document with a concise retirement record containing
   scope, historical-data policy, and separate production cutover requirements. Append a CHANGELOG entry without
   rewriting release history. Regenerate the dependency graph using the repository-supported generator.
8. Update shared tests to assert absence, preserved shared behavior, and exact service/config contracts.

### Invariants and error handling

- `opening_candidate_snapshot.v1` remains produced/consumed by Candidate Engine and Daily Brief.
- `prepared_option_positions_context` remains authoritative and validated for Close Advice/pipeline use.
- No import boundary may move business policy out of `domain/domain/` or introduce a replacement layer.
- Config with retired key fails before runtime side effects; config without it remains valid for US/HK examples.
- Default and Assistant-selected DeepSeek providers remain discoverable and credential-addressable.
- Service drift source support remains able to identify now-extra installed collector units during a later authorized
  reconcile; this work unit does not apply drift.
- Historical docs/reviews/Gateflow/release coverage remain immutable history and may retain old names.

### Non-goals

- No source compatibility shim that silently enables/accepts Advice; no automatic production migration; no secret
  deletion; no cleanup of historical artifacts; no generalized portfolio-client refactor.

### Validation

```bash
./.venv/bin/python -m pytest -q \
  tests/test_config_yaml.py \
  tests/test_multi_account_tick.py \
  tests/test_tick_account_execution_barrier.py \
  tests/test_prepared_option_positions_context.py \
  tests/test_service_deploy.py \
  tests/test_service_credential_materializer.py \
  tests/test_portfolio_management_client.py \
  tests/test_portfolio_management_contract_vendor.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run
./.venv/bin/python -m compileall -q domain src tests
git diff --check
```

Also run repository searches proving no active production import/entry point remains. Search allowlist is limited to the
retirement note, CHANGELOG/history, typed legacy compatibility constants/tests, and operational retirement tests; no
live generation, renderer, config, or service declaration may remain.

Slice 2 also asserts that `TickNotificationRequest` and both Brief assembler signatures contain no Advice-only
distribution/option-position handoff fields after their same-slice callers have been removed.

### Completion and stop condition

Complete when focused tests/config builds pass, deleted modules have no imports, shared behavior is proven, and docs
match the new product boundary. Stop if any allegedly exclusive module has a live non-Advice consumer or removing the
config/service path requires production mutation.

## Aggregate validation

After both accepted slice commits:

1. Re-run both focused suites from committed `HEAD`.
2. Run all tests selected by import/call-path search plus the full suite:
   `./.venv/bin/python -m pytest -q`.
3. Run `./.venv/bin/python -m compileall -q domain src tests`.
4. Run the repository dependency graph/boundary generator and verify zero parse errors/cycles/boundary violations.
5. Run four example-config validate/build dry-runs above.
6. Render systemd bundles in tests and assert collector units/tick Advice credentials are absent while Assistant
   DeepSeek binding remains covered.
7. Run `git diff --check`, inspect `git status --short`, inspect every cached commit patch, and verify the protected
   dirty-worktree patch SHA-256 remains unchanged.

No network model call, live notification, remote command, production service action, or runtime artifact write is an
allowed validation.

## Documentation decision

- Current user/operator docs and config examples change with the owning implementation slice.
- `docs/AI_DECISION_ADVICE_DESIGN.md` becomes a retirement record at the same path to prevent stale links from
  presenting an active feature.
- Historical Gateflow/review/release evidence is not rewritten.
- `docs/DEPENDENCY_GRAPH.md` is regenerated only after deletion and validated against its generator.

## Risks and classification

| Risk | Planned handling |
|---|---|
| Frozen retry sends old Advice after renderer removal | Fixed in Slice 1 with read-only fail-closed guard |
| Historical Brief digest fails after normalizer change | Fixed in Slice 1 with exact legacy fixtures |
| Intermediate Slice 1 Tick call breaks on removed kwargs | Fixed by retaining ignored compatibility parameters until same-slice caller cleanup in Slice 2 |
| Retired YAML key gets only a generic unknown-key error | Fixed at the YAML root/market validation owner in Slice 2 |
| Shared option context accidentally removed | Fixed in Slice 2 by retaining path and barrier regressions |
| Shared DeepSeek credential/provider accidentally removed | Fixed in Slice 2 with registry/service tests |
| Installed collector continues in production | Later separately authorized production cutover; documented, not executed |
| Historical artifact disk usage | Assigned to a later cleanup work unit requiring explicit destructive approval |
| Protected prior dirty work changes while Gateflow runs | Stop condition; verify patch hash before every protected commit |

No residual risk is unclassified. Implementation discoveries outside these mappings are recorded as deferred or stop
for renewed goal confirmation; they are not folded into the work unit.

## No overdesign / no goal drift

The plan deletes one overlay and adds only the two compatibility mechanisms required by confirmed correctness:
passive legacy digest handling and a read-only frozen-retry blocker. The temporary ignored parameters are deleted in
the next approved slice and are not a public compatibility feature. It adds no new public schema, provider,
service, storage object, automatic migration, or replacement recommendation engine. Every allowed change maps directly
to a confirmed success signal; unrelated candidate-evidence improvements from the superseded work unit remain out of
scope.

## PlanReview finding decisions

- `PR-01`: accepted; fixed by retaining ignored private handoff parameters in Slice 1 and removing caller/callee
  plumbing atomically in Slice 2.
- `PR-02`: accepted; fixed by stripping at the canonical normalizer and reconstructing only exact legacy digest
  candidates from the raw persisted mapping.
- `PR-03`: accepted; fixed by specifying a repository-owned validated inspection result covering raw source, fallback
  message, and card transport before either outbound map is populated.
- `PR-04`: accepted; fixed by specifying targeted YAML root/market errors as well as runtime JSON validation.

## Completion report format

Final closeout will report:

- accepted commits and Draft PR URL;
- deleted capability and preserved shared authorities;
- focused/full validation results and dependency/config checks;
- review findings with final status;
- documentation changes;
- protected original-worktree integrity result;
- remaining production cutover and historical-cleanup risks with explicit owners/authorization boundaries;
- next entry point: user review/merge of the Draft PR, with release and production retirement still separate.
