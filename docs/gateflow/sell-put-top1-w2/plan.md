# Gateflow Plan — Sell Put Top1 W2

- Gate: `plan`
- Work unit: `sell-put-top1-w2`
- Branch: `feat/sell-put-top1-w2`
- Base: `origin/main@c626e965`
- Goal contract: `docs/gateflow/sell-put-top1-w2/goal-confirmation.md`
- Artifact path: `docs/gateflow/sell-put-top1-w2/plan.md`
- Current gate: `accepted plan commit`

## 1. Goal, motivation, and success signal

Implement the producer-owned `recommendation_point.v1` seam for official scheduled Sell Put decisions. The production tick must publish at most one canonical run/account artifact after its scheduler target watermark is committed and before notification delivery, while all experiment and later corpus/lifecycle concerns remain absent.

Completion is proven when scheduled eligible points publish idempotently, source conflicts fail closed, excluded triggers never publish, W1A validates the clean point/snapshot accepted set, and injected observer failures leave the existing tick result, scheduler commit, and provider path unchanged.

## 2. Non-goals and scope boundary

### Included

- Strict point identity, payload, source-binding, load, and byte-level write-once contracts.
- Exact maintainer availability read from the already-loaded process environment.
- One best-effort notification-flow observer immediately after scheduler-target commit.
- Narrow extraction of the existing release-aware source-commit resolver so W2 and the existing ledger migration share one implementation.
- Focused contract, seam, architecture, and regression tests.

### Excluded

- SQLite, account opt-in, effective two-layer feature status, corpus, research, validation, outcomes, timers, CLI/tools, LLM, Prompt, backfill, retry queues, or permanent point storage.
- Candidate Engine, filters, ranks, production config, scheduler rules, watermark semantics, notification policy/provider behavior, release, deployment, or service changes.
- Generic observer, feature-flag, provenance, repository, workflow, or event abstractions.

## 3. Goal alignment

| Decision or validation | Confirmed goal / success signal |
|---|---|
| One run/account point file | Producer-owned official recommendation identity without a new database |
| Observer after watermark and before delivery | Point means the official target was committed but is independent of provider delivery |
| W1A projection call during clean-point build | Validate snapshot binding and producer accepted IDs now, not first in W4 |
| Exact process env gate | Default-off and removable experimental feature without a flag platform |
| Shared source-commit extraction | Preserve the already-tested installed-release behavior without importing ledger migration or duplicating it |
| Broad exception containment at observer boundary | Production tick, watermark, and notification remain authoritative |

## 4. Design-document alignment

- MVP §4.3: only `OM_STRATEGY_LAB_TOP1_AVAILABLE == "1"` enables the producer observer; it does not read account opt-in or experiment state.
- MVP §7.1: identity is market/account/strategy/target based; only scheduled, actually-run, watermark-committed account pipelines can publish; notification delivery is not a precondition.
- Modular technical plan M2: point is run/account-scoped, write-once, best-effort, and forbidden from M3–M8 dependencies.
- Modular control W2: builder/validator/publisher, scheduled identity, manifest/opening bindings, excluded triggers, and W1A seam are all included; nothing from W3 or W4 is pulled forward.

## 5. First-principles judgment and direct code evidence

- `run_tick_notification_flow()` already has the only correct ordering boundary: `_commit_scan_targets_before_delivery(request)` completes before any provider execution. One call immediately after it is enough.
- `ran_pipeline_accounts`, `scheduler_decisions_by_account`, and `scheduled_scan_targets_by_account` already carry the account execution/identity facts. No new tick DTO or event bus is needed.
- `load_candidate_snapshot_bundle()` already verifies the terminal manifest, status index, exact owner set, file hashes, opening snapshot current contract, config hash, and policy hash. W2 must consume it rather than reimplement those checks.
- `write_account_run_state_bytes_once_safely()` already provides no-follow, atomic link, same-byte adoption, and different-byte conflict semantics. W2 only supplies canonical bytes and a domain error mapping.
- `build_ranking_projection()` already validates the point binding against the opening snapshot and derives the ordered accepted Sell Put IDs with baseline parity. W2 calls it for evaluable `candidates_found` / `no_candidate` points and records its accepted IDs.
- The private ledger `_source_commit()` already supports both Git worktrees and installed release directories backed by the upgrade cache, and rejects dirty production source. Extracting those exact lines is smaller and safer than a second resolver.

## 6. Contract and schema

### 6.1 Constants

```text
RECOMMENDATION_POINT_SCHEMA = "recommendation_point.v1"
RECOMMENDATION_POINT_FILE = "recommendation_point.sell_put.json"
STRATEGY_FAMILY = "sell_put"
AVAILABILITY_ENV = "OM_STRATEGY_LAB_TOP1_AVAILABLE"
```

### 6.2 Canonical identity

`build_recommendation_point_id(market, account, scheduled_scan_target_market)` first canonicalizes the timezone-aware target to UTC ISO-8601 `Z`, then returns `canonical_sha256()` of exactly:

```json
{
  "schema_version": "recommendation_point.v1",
  "market": "HK|US",
  "account": "lowercase",
  "strategy_family": "sell_put",
  "scheduled_scan_target_market": "<UTC ISO-8601 Z>"
}
```

Run ID, execution time, source commit, candidate rows, and notification state are deliberately absent, so a retry/catch-up for the same official target has the same identity.

### 6.3 Exact point fields

`recommendation_point.v1` has no optional or extra top-level keys:

```text
schema_version
recommendation_point_id
strategy_family
market
account
run_id
scheduled_scan_target_market
decision_at_utc
terminal_sell_put_status
account_config_sha256
strategy_policy_sha256
terminal_manifest_ref
terminal_manifest_sha256
opening_snapshot_ref
opening_snapshot_sha256
source_commit_sha
producer_accepted_candidate_ids
content_sha256
```

Semantics:

- target and decision time are canonical UTC `Z` values; target comes from account scheduler `scheduled_scan_target_market`, decision time from scheduler `now_utc`;
- `terminal_sell_put_status` is exactly one of `candidates_found`, `no_candidate`, `partial_data`, `data_unavailable`;
- `producer_accepted_candidate_ids` always preserves the opening snapshot's ordered accepted Sell Put IDs, including incomplete points that retain usable candidates from some scopes;
- `candidates_found` requires at least one accepted ID and `no_candidate` requires an empty list; `partial_data/data_unavailable` may have zero or more accepted IDs and are never inferred from list length;
- status combines the existing aggregate Sell Put result with `candidate_universe_summary()` completeness: an aggregate `data_unavailable` remains `data_unavailable`; otherwise any affected `strategy_mode=put` scope forces `partial_data`; only an unaffected Sell Put universe may remain clean `candidates_found/no_candidate`;
- `terminal_manifest_ref` is exactly `output_runs/<run_id>/accounts/<account>/state/candidate_snapshot_manifest.v1.json` and its hash is the SHA-256 of canonical file bytes; the builder recomputes the supplied hash from the exact canonical manifest payload so a valid dict cannot be paired with an unrelated file hash;
- `opening_snapshot_ref` is exactly `output_runs/<run_id>/accounts/<account>/state/opening_candidate_snapshot.json` and its hash is the opening snapshot `content_sha256`, matching W1A;
- config/policy hashes must match both terminal manifest and opening snapshot;
- source commit is exactly 40 lowercase hex characters;
- `content_sha256` is `canonical_sha256(payload_without_content_sha256)`;
- persisted bytes are sorted, indented, no-NaN UTF-8 JSON plus one newline.

### 6.4 Public functions

`src/application/recommendation_point.py` adds:

```text
strategy_lab_top1_available(environ=None) -> bool
build_recommendation_point_id(market, account, scheduled_scan_target_market) -> str
build_recommendation_point(scheduler_decision, terminal_manifest, opening_snapshot, *, terminal_manifest_sha256, source_commit_sha) -> dict
validate_recommendation_point(payload) -> dict
point_binding_from_recommendation_point(payload) -> dict
publish_recommendation_point(base, payload) -> "published" | "idempotent"
load_recommendation_point(base, run_id, account) -> dict
capture_scheduled_recommendation_point(base, run_id, account, scheduler_decision, *, source_commit_sha) -> (status, payload)
```

`RecommendationPointError.reason_code` is limited to current boundary failures such as `official_point_invalid`, `official_point_identity_missing`, `official_point_source_unavailable`, `official_point_unavailable`, and `official_point_conflict`. Conflict is raised, not returned as a successful publication state.

## 7. Source and binding validation

`capture_scheduled_recommendation_point()`:

1. loads `load_candidate_snapshot_bundle(base, run_id, account)`;
2. reads and hashes the exact terminal manifest bytes through the safe run-state reader;
3. requires the bundle's `opening` owner;
4. calls the strict builder;
5. publishes/adopts canonical point bytes and returns status plus payload.

The builder:

1. requires scheduler `should_run_scan is True`, a timezone-aware scheduled scan target, and a timezone-aware decision clock;
2. validates the terminal manifest and current opening snapshot contracts;
3. requires run/account/market/config/policy and manifest opening-owner bindings to agree;
4. requires at least one terminal Sell Put scope and exactly one Sell Put strategy result, combines that result with existing `candidate_universe_summary()` affected Sell Put scopes to derive the point status, then derives ordered accepted Sell Put IDs from the validated opening snapshot;
5. attempts W1A `build_ranking_projection()` with the point binding for every point; whenever it succeeds, its ordered accepted IDs must match exactly;
6. clean `candidates_found` / `no_candidate` requires W1A success; `partial_data` / `data_unavailable` remains non-evaluable by point status whether W1A succeeds on the usable accepted subset or fails closed, and still publishes all producer facts;
7. validates the final strict point before returning it.

The point's binding projection contains exactly W1A's existing keys: point ID, market, account, run ID, opening ref/hash, decision time, and source commit.

## 8. Observer state transition and error handling

`run_tick_notification_flow()` keeps its current preparation and commit behavior, then performs:

```text
_commit_scan_targets_before_delivery(request)  # existing, authoritative; may raise
_observe_recommendation_points_best_effort(request)  # new, never raises out
existing notification branch/provider behavior
```

Observer eligibility is the conjunction of:

- `trigger_kind == "scheduled"`;
- not `delivery_only`;
- maintainer availability is exactly enabled;
- account is in `ran_pipeline_accounts`;
- account has a non-empty committed target in `scheduled_scan_targets_by_account`;
- account scheduler decision exists, has `should_run_scan is True`, and its canonical target equals the committed target;
- one clean source commit SHA is resolvable.

Manual, force, replay-like unknown trigger kinds, smoke/failed pipelines (no ran account), delivery-only, disabled availability, or missing identity are skipped or recorded as a point gap and never call the publisher. Each account is isolated: one account failure does not block another. Audit attempts are themselves exception-contained. No retry/backfill occurs because a post-watermark missing point is a meaningful `official_point_missing` gap.

## 9. Source identity extraction

Add `src/application/source_identity.py::source_commit_sha(root=None, run_cmd=None)` by moving the existing algorithm unchanged:

- Git worktree: resolve `HEAD`, require no tracked or untracked changes under `domain`, `src`, or `scripts`;
- installed release: resolve `v<VERSION>^{commit}` from the existing upgrade cache with a temporary index and apply the same clean-source check;
- timeout/command/source failure: return `None`.

`position_projection_migration._source_commit()` remains as a compatibility wrapper passing its module `subprocess.run`, preserving existing monkeypatch tests and callers. No ledger behavior changes.

## 10. Affected files

### Production

- New `src/application/recommendation_point.py`.
- New `src/application/source_identity.py`.
- Modify `src/application/ledger/position_projection_migration.py` only to delegate its private compatibility function to the shared resolver and remove moved imports.
- Modify `src/application/tick_notification_flow.py` only for imports, one post-commit observer call, and a narrow private best-effort helper.

### Tests and generated documentation

- New `tests/test_recommendation_point.py`.
- Modify `tests/test_daily_decision_brief_notification_flow.py` for observer ordering/exclusion/failure isolation.
- Modify `tests/test_strategy_lab_top1_architecture.py` to forbid M2 dependencies on experiment stores and M3–M8 modules.
- Existing `tests/test_position_projection_migration.py` remains the regression authority for source identity behavior; change only if extraction requires import-path alignment not covered by the compatibility wrapper.
- Regenerate `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` only if the repository graph check reports the expected new import edges.
- Add Gateflow/review artifacts; do not edit product design documents or public operator docs because W2 adds no public command/config surface.

## 11. Implementation slice

One slice is sufficient because the artifact contract and its observer call form one deployable behavior; splitting by file would add review gates without producing an independently useful product increment.

### S1 — Official scheduled point publication seam

- Objective: publish a strict point after successful official target commit without changing production control flow.
- Allowed production files: the four production paths listed in §10.
- Allowed test/docs files: the test, generated dependency graph, Gateflow, and review artifacts listed in §10.
- Prerequisites: merged W1A contracts on `origin/main`; valid terminal candidate bundle fixtures.
- Exact changes: §§6–9 only.
- Non-goals: all §2 exclusions.
- Completion signal: every expected assertion in §12 passes, Kimi code/aggregate/PR reviews have no unresolved accepted finding, and the accepted slice/deepreview/PR review commits exist.
- Stop condition: any required change to Candidate Engine, scheduler/watermark semantics, experiment DB/schema, account opt-in, public config, service rendering, release/deployment, or actual notification/experiment execution requires a new user decision.

## 12. Tests and validation

### Contract and publisher tests

`tests/test_recommendation_point.py` must prove:

- offset-equivalent targets produce the same UTC target and point ID; account/market/target changes alter the ID;
- a valid manifest-bound candidates-found point has exact keys, expected refs/hashes/status/accepted IDs, valid content hash, and a W1A projection whose accepted IDs equal the point;
- clean no-candidate is valid; unavailable/partial evidence may preserve accepted IDs but remains non-evaluable by point status even if W1A can project its usable subset;
- one successful Sell Put scope with an accepted candidate plus one failed sibling scope publishes an incomplete point, preserves the accepted ID, and is rejected by W1A projection rather than dropped;
- a completed Sell Put scope carrying `reason=partial_data` plus an accepted candidate is normalized to point status `partial_data`; its IDs are retained and any successful W1A subset projection cannot make the point clean;
- scheduler false/missing/naive target, naive decision clock, wrong source hash, point/snapshot/manifest/config/policy mismatch, unsafe/tampered refs, unexpected keys, content-hash drift, and inconsistent IDs fail closed;
- first publish returns `published`, exact replay returns `idempotent`, different canonical bytes at the same path raise `official_point_conflict`, and load rejects non-canonical/tampered bytes;
- availability is true only for exact value `1`;
- capture uses the exact terminal bundle and does not depend on an experiment store.

### Tick seam tests

`tests/test_daily_decision_brief_notification_flow.py` must prove:

- order is `watermark commit -> observer -> provider`;
- observer success and observer exception both preserve the pre-existing return code and provider execution;
- commit failure prevents observer and provider exactly as before;
- disabled availability, manual, force, delivery-only, empty `ran_pipeline_accounts`, and missing/mismatched target never call capture;
- a failing account does not prevent a second eligible account from being observed.

### Regression and architecture commands

Run from the W2 worktree with the existing project Python 3.12 environment:

```text
python -m pytest -q tests/test_recommendation_point.py tests/test_daily_decision_brief_notification_flow.py tests/test_position_projection_migration.py tests/test_strategy_lab_top1.py tests/test_strategy_lab_top1_architecture.py
python -m pytest -q tests/test_candidate_snapshot_manifest.py tests/test_opening_candidate_snapshot.py tests/test_multi_account_tick.py tests/test_tick_account_execution_barrier.py
ruff check src/application/recommendation_point.py src/application/source_identity.py src/application/ledger/position_projection_migration.py src/application/tick_notification_flow.py tests/test_recommendation_point.py tests/test_daily_decision_brief_notification_flow.py tests/test_strategy_lab_top1_architecture.py
basedpyright src/application/recommendation_point.py src/application/source_identity.py src/application/tick_notification_flow.py
python scripts/generate_dependency_graph.py --check
python -m pytest -q
```

Expected: focused tests and static checks pass; dependency graph has no cycle; full-suite failures, if any, are classified with exact evidence and cannot be used to claim a broad pass without completion.

## 13. Docs decision

No public documentation or config example changes in W2. The env variable is already the accepted product contract but service/profile authoring belongs to W7. Gateflow artifacts record the internal schema and seam; generated dependency graph changes are mechanical only.

## 14. Risks, open questions, and residual ownership

- Git/source resolution latency or unavailability: current slice fails open for production behavior and records a point gap; no cache or fallback identity is added. Owner: W2 behavior, tested synthetically.
- Point lost after watermark due crash/failure: intentional `official_point_missing`; W4 treats it as an immutable denominator gap. No backfill. Owner: W4 consumption.
- Source `output_runs` retention: W4 copies the minimal projection before deletion. W2 adds no global store.
- Availability service/profile rendering and account opt-in: W7 and W3 respectively.
- Runtime provider/pilot readiness: W0R.
- Blocking open questions: none.

## 15. No-overdesign / no-goal-drift check

The plan adds one artifact owner, one existing-flow call, and one shared extraction required by two current callers. It reuses every existing validation/write primitive and introduces no generic framework or future module. All other accepted product features remain assigned to their named later work units.

## 16. Completion report format

Final closeout will report:

- changed production/test/generated-doc files and accepted commit hashes;
- exact focused/full validation results and any environment-only limitations;
- code, aggregate Kimi DeepReview, and PR review findings with final status;
- Draft PR URL and CI state;
- residual risks with W3/W4/W7/W0R ownership;
- next entry point: merge authorization for W2, then W3 only after its dependency base is available.
