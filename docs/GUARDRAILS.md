# Guardrails

## A) Local Commit Gate

Enable hooks once:

```bash
cd <repo-root>
bash scripts/setup_git_hooks.sh
```

Enabled checks:

- Reject commits if repo path/name matches `options-monitor-prod`
- Scan staged index content for high-confidence credentials, private data fingerprints, known personal email addresses, personal paths, and tracked runtime configs; findings are redacted
- Reject a repository-effective Git author email already classified as private; use a GitHub `noreply` identity for public commits
- Reject a missing living-doc authority/indexed target and deterministic repository paths whose owner is absent from the staged Git index; explicit historical, removed, retired, proposed, and example paths are exempt
- Require the first commit-message line to match `<type>(<scope>): <subject>`
- No trailer or co-author line is required by the hooks

## B) Remote Merge Gate (CI)

Workflow: `.github/workflows/guardrails.yml`

- Docs check: forbid treating `config.json` / `config.scheduled` / `config.market_*` as OM runtime entry, require the living-doc authority graph to remain present, and reject deterministic repository paths in indexed living docs when their owner is missing
- Runtime config tracking check: forbid committing root runtime configs such as `config.us.json` / `config.hk.json`; commit only templates under `configs/examples/`
- Sensitive artifact check: reject high-confidence provider credentials, private keys, credentialed URLs, known private runtime/financial/email fingerprints, and literal personal home or mounted-volume paths without printing the blocked value
- Public surface check: compare the declared public names of `src/`, `domain/` and `scripts/` against the merge base of the pull request and reject any name that disappeared without a recorded retirement
- Lint: run `python -m ruff check .`
- Standalone smoke: run `tests/run_smoke.py`
- Launcher spec smoke: render `./om-agent spec` through the public wrapper
- Full regression: run automatic pytest discovery for pull requests and VERSION-changing pushes; ordinary pushes to `main` reuse the required pull-request result. The same full collection runs on two local worker processes, grouped by test file, and a worker crash fails the gate instead of restarting tests silently.

Trigger: `push` and `pull_request` to `main`. The active `main` ruleset requires pull requests, an up-to-date `guardrails` status, and blocks deletion and force pushes.

### Full-regression latency contract

Goal: reduce the pull-request regression wall clock without selecting fewer tests or weakening the single required `guardrails` result. Success requires the complete automatically discovered suite to pass with two workers, with no deselected and no newly xfailed tests; the countable form of that is that a two-worker run covers exactly the cases a serial run of the same revision collects. The absolute counts are a dated observation rather than a constant to preserve, because they move whenever a test is added or retired -- and because the pass/skip split moves by one case between the ubuntu runner and a macOS checkout. On `main` at `c7fc8ed5` (2026-09-20) the suite collects 7292 cases, so the 7057 passed, 1 skipped recorded on 2026-09-14 is superseded rather than a baseline to hold. (2026-09-20: the simplification sweep removed collected cases whose only subjects were retired dead code and retired instruments -- eight of them against the tree as of the order-domain merge -- and every other test file kept its exact case count; the improvement is still measured as wall clock, not as selected tests.) On the same `ubuntu-latest` pull-request workflow, the regression step must complete within 313 seconds and the whole `guardrails` job within 365 seconds, at least 15% below the 367.84-second step and 429-second job baselines recorded on 2026-09-14. A result within 10% of either limit must be rerun once because hosted-runner timing is noisy. The serial command remains available for diagnosis.

The workflow uses pinned `pytest-xdist` and `pytest -n 2 --dist loadfile --max-worker-restart=0`. A fixed worker count keeps CI resource use predictable; `loadfile` keeps each test module in one process, while normal pytest collection still discovers every test. Zero worker restarts makes crashes visible. Lint, privacy checks, smoke tests, release resolution, job name, and release trigger behavior remain unchanged.

Rejected alternatives: test-impact selection would reduce required coverage; a GitHub matrix would duplicate environment setup and complicate the required-check and release aggregation contract; unbounded `-n auto` would make memory and process count depend on runner hardware. If the parallel run exposes a real shared-resource dependency, that resource must be isolated with a temporary path, dynamic port, worker-unique path, or cross-process lock before this contract can ship. This slice has no serial carve-out and must not skip or deselect a conflicting test.

Implementation is one slice: declare `pytest-xdist` in `requirements/dev.txt`, pin its exact version in `constraints/dev.txt`, update the full-regression invocation, and extend the existing workflow contract test to enforce the exact command, dependency declaration, exact pin, and absence of `-k`, `--ignore`, `--deselect`, or test-path selection. Run that contract test, compare serial collection with the complete two-worker result, then use one real pull-request run for the timing acceptance above. The change affects development CI only; it does not alter runtime code, production data, notifications, releases, or deployment.

### Declared public surface

Every module under `src/`, `domain/` and `scripts/` has a public surface, and the pull-request gate compares that surface — for the modules a change touches — against the merge base of the pull request (not against a checked-in expectation, which a deletion could update in the same commit). A declared name that disappears fails the gate unless `docs/public_surface_retirements.json` records it.

- A module's surface is its `__all__` when that is statically resolvable — literal lists and tuples, starred references to module-level literals, `dict.keys()` and `sorted(...)`. A module without `__all__` declares the public names it defines itself, so a re-export must be listed in `__all__` to be protected. Any other way of building `__all__` — an augmented assignment, `.append(...)`, an assignment inside a guard, or more than one assignment — is a failure rather than a module read as declaring nothing: declare it as a single top-level literal list.
- A name counts as present when the module binds it anywhere at module scope, imports included, because `from x import utc_now as utc_now_iso` keeps the name reachable even though the module no longer defines it. On the declaring side a `def`/`class` behind an `if`/`try` counts too, since a version guard still ships that definition; a plain assignment behind a guard does not, because that is how a module spells a platform fallback (`except ImportError: fcntl = None`) or a temporary used by an error message rather than a name it promises. When a module does declare `__all__`, that list is the surface, so narrowing it is a removal.
- The gate reads regular files only. A module that is a symbolic link at either revision is refused rather than compared, because a link's blob is the path it points at, which can pass for valid Python.
- Content that cannot be read is a failure, never an empty surface: a base blob missing from the local object store (a partial clone, a pruned one), or a ledger that is in the base tree but cannot be read, is reported instead of skipped. The diff status is held to the same rule — `A` adds surface and is skipped, `D`/`M`/`T` are compared, and anything else (an unmerged `U`, say) fails rather than passing as nothing to check.
- The ledger is append-only, and the gate rejects a change that drops a recorded entry. An entry is one object with exactly `module`, `name` and `reason`. The gate enforces that `reason` is a sentence rather than a placeholder (at least 20 characters); whether it is a *true* sentence is the reviewer's call, which is why the entry has to be written into this checked-in file in the same change as the deletion, where it shows up next to it in the diff, rather than in an out-of-band tracker a reviewer would have to know to open. `name` `*` retires a whole module and is valid only while that module is really gone — so a deleted module or a module split into a package costs one entry, not one per name. The one exception to append-only is that `*` entry: restoring the module makes it false, so it may be dropped then — and only dropped, since rewriting it into another entry for the same module would exempt that name while it is still exported.
- Leaving a removal unrecorded fails, and so does deleting a recorded entry. Adding a name, or changing a symbol where it already lives, does not: the gate compares which names a module exposes, never how they are implemented. Moving a name to another module does fail for the module it left, unless that module still exposes it — an alias import counts.
- What the gate cannot check, and a reviewer must: whether a `reason` is true, whether an entry registered in an earlier change was honest when a later one matched it against a real deletion (that later diff shows no ledger change at all), and a name that only ever lived inside a guard at the base revision, which is not on the declaring side and so is not reported when it goes.

Run it locally against any revision:

```sh
./.venv/bin/python scripts/guardrails_check.py --check-public-surface --public-surface-base <rev>
```

The base revision is never defaulted: `--check-public-surface` without `--public-surface-base` fails instead of comparing nothing. The workflow runs this check on `pull_request` events and nowhere else — a push to `main` reuses the required pull-request result, which is the same policy the full regression already follows. The workflow passes a merge base, so a branch that trails `main` is not charged for deletions made on `main` itself. Resolving that merge base needs the history behind it, so the workflow checks out the full history instead of a single commit; when the base revision cannot be resolved the step fails with an error annotation rather than skipping the comparison.

## C) Symbol Canonicalization Rule

- Any entrypoint that accepts user-entered symbol, broker raw payload, or OpenD/Futu underlying identifier must canonicalize to the shared symbol format before business logic.
- Canonical market symbols are values like `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not be persisted as runtime symbol config or position symbols.
- Shared alias handling lives in `src/application/opend_utils.py::resolve_underlier_alias`; new entrypoints should reuse it instead of adding ad hoc `upper()` or market-specific parsing branches.

## D) Retired-Column SQL Registry

The lot-identity retirement (slice 3) pins, instead of trusting, the set of live SQL that still names a retired column (`position_lots.expiration`, `position_lots.record_id`, `wheel_events.stock_lot_id`). `docs/retired_column_sql_registry.json` enumerates that set — SQL text, schema-helper call arguments, and standalone index-name constants, per scope — plus the f-string statements that cannot be judged from literals (recorded as `dynamic_sql`, pinned, not silently missed) and the modules exempt because their job is to name these columns (the migration tool and the parity probe; the exemption is asserted non-empty so a rename fails rather than hollows it out).

Run it locally:

```sh
./.venv/bin/python scripts/retired_column_scan.py --check
./.venv/bin/python scripts/retired_column_scan.py --write   # after an intended drift
```

Any drift — a statement added, repointed, or removed — fails `tests/quality/test_retired_column_sql_registry.py`; an intended change reruns `--write` in the same commit and the diff is the review. Repointing statements away from the retired columns is the slice 3 work itself; the registry going empty (outside the exemptions) is its completion signal.
