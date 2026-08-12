# Gateflow Implementation Artifact — Slice 2

- Work unit: `retire-ai-decision-advice`
- Gate: Slice 2 implementation
- Branch: `refactor/retire-ai-decision-advice`
- Base: accepted Slice 1 commit `c37b2098`
- Status: implementation complete; pending Slice 2 DeepReview loop
- Artifact path: `docs/gateflow/retire-ai-decision-advice/slice-2-implementation.md`

## Scope delivered

- Deleted the Advice application package, prompts, evidence collector CLI, dedicated DeepSeek Responses adapter, and
  Advice-only prepared portfolio distribution layer.
- Removed Tick observation publication, Advice handoff fields, Advice-only outcome/metric plumbing, and transitional
  Daily Brief assembler parameters while preserving Candidate Engine snapshots, prepared option-position manifests,
  and Close Advice barriers.
- Rejected the exact retired config key at root YAML, US/HK market YAML, and runtime JSON boundaries. Nearby unknown
  keys retain the generic validation error.
- Removed collector unit rendering and Tick/collector DeepSeek credential consumers while preserving the logical
  DeepSeek credential and explicitly selected Assistant bindings.
- Removed only the Advice-specialized portfolio distribution client API; generic `read_view("distribution")` remains.
- Replaced current Advice documentation with a retirement record, updated operator/product docs and CHANGELOG, and
  regenerated the dependency graph.
- Deleted Advice-exclusive tests and updated shared contract tests to prove absence and retained shared behavior.

## Changed targets

- Deleted: `src/application/ai_decision_advice/**`,
  `src/interfaces/cli/ai_evidence_collector.py`, `src/infrastructure/deepseek_responses.py`,
  `src/application/prepared_portfolio_distribution.py`, and their exclusive tests.
- Runtime/config/service boundaries: `src/application/{multi_account_tick,tick_account_execution,tick_notification_flow,
  daily_decision_brief_service,config_validator,config_yaml,service_deploy}.py`,
  `src/application/secret_store/registry.py`, and `src/infrastructure/portfolio_management_client.py`.
- Shared tests: architecture, config, Tick/barrier, prepared option positions, service/credential, portfolio client,
  notification, and Agent contract suites selected by direct import/call-path search.
- Docs/config: `CHANGELOG.md`, `configs/examples/config.yaml.example`, the approved live docs, and generated dependency
  graph artifacts.

## Scope amendment

The repository dependency generator owns both `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd`. The accepted
plan required regeneration but named only the Markdown document in the allowed-file list; the generated Mermaid
companion is therefore included as a necessary, generator-owned output rather than a new hand-edited surface.

## Implementation decisions

- Retired configuration fails closed instead of being silently ignored; production removal of the key remains a
  separately authorized cutover.
- Historical persisted Brief compatibility remains in Slice 1's narrowly typed normalizer/retry classifier; no
  generation compatibility shim survives.
- The generic portfolio-management distribution route remains because it is a shared client capability. Only the
  Advice-specific query builder, validator, and fixed valuation assumption were removed.
- Secret values and logical Assistant credentials were not deleted. Consumer ownership was narrowed at the service
  registry and rendered-unit boundary.

## Validation before review

```text
focused Slice 2 suite plus architecture guard: 450 passed, 1 warning
US/HK example config validate: passed
US/HK example config build --dry-run: passed
python -m compileall -q domain src tests: passed
dependency graph check: current; production_modules=568; cycles=0
ruff check on changed Python targets: passed
git diff --check: passed
active production import/entry-point search: no retired generator, collector, adapter, or service declaration found
```

The first focused run exposed an over-broad removal of the generic distribution view (`448 passed, 1 failed`). The
shared route was restored, an explicit retention regression was added, and the complete focused suite then passed.

## Docs decision

Current docs describe deterministic Daily Brief behavior, the retired source/config/service boundary, historical-data
policy, and a separately authorized production cutover. Historical release/review artifacts are intentionally not
rewritten.

## Residual risks and uncovered areas

- **requiring new issue or explicit user decision**: installed collector units, deployed runtime config, and production
  service reconciliation are operational state outside this source-only work unit.
- **requiring new issue or explicit user decision**: external private consumers importing removed Advice-only Python
  modules are not discoverable from this repository; no compatibility shim is provided by design.
- **fixed in current slice**: the generic portfolio distribution view and Assistant DeepSeek credential binding have
  explicit regressions proving they remain available.
- **covered by aggregate validation**: full-suite compatibility, whole-branch dependency checks, and the protected
  original-worktree patch hash remain to be verified after the Slice 2 review loop.

No model/network call, notification, release, deployment, service mutation, secret mutation, or production data write
was performed.
