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

Goal: reduce the pull-request regression wall clock without selecting fewer tests or weakening the single required `guardrails` result. Success requires the complete automatically discovered suite to pass with two workers and preserve the baseline outcome counts: 7057 passed, 1 skipped, with no deselected or newly xfailed tests. (2026-09-20: the simplification sweep removed collected cases whose only subjects were retired dead code and retired instruments -- eight of them against the tree as of the order-domain merge -- so the two-worker count is lower by that amount; every other test file kept its exact case count, and the improvement is still measured as wall clock, not as selected tests.) On the same `ubuntu-latest` pull-request workflow, the regression step must complete within 313 seconds and the whole `guardrails` job within 365 seconds, at least 15% below the 367.84-second step and 429-second job baselines recorded on 2026-09-14. A result within 10% of either limit must be rerun once because hosted-runner timing is noisy. The serial command remains available for diagnosis.

The workflow uses pinned `pytest-xdist` and `pytest -n 2 --dist loadfile --max-worker-restart=0`. A fixed worker count keeps CI resource use predictable; `loadfile` keeps each test module in one process, while normal pytest collection still discovers every test. Zero worker restarts makes crashes visible. Lint, privacy checks, smoke tests, release resolution, job name, and release trigger behavior remain unchanged.

Rejected alternatives: test-impact selection would reduce required coverage; a GitHub matrix would duplicate environment setup and complicate the required-check and release aggregation contract; unbounded `-n auto` would make memory and process count depend on runner hardware. If the parallel run exposes a real shared-resource dependency, that resource must be isolated with a temporary path, dynamic port, worker-unique path, or cross-process lock before this contract can ship. This slice has no serial carve-out and must not skip or deselect a conflicting test.

Implementation is one slice: declare `pytest-xdist` in `requirements/dev.txt`, pin its exact version in `constraints/dev.txt`, update the full-regression invocation, and extend the existing workflow contract test to enforce the exact command, dependency declaration, exact pin, and absence of `-k`, `--ignore`, `--deselect`, or test-path selection. Run that contract test, compare serial collection with the complete two-worker result, then use one real pull-request run for the timing acceptance above. The change affects development CI only; it does not alter runtime code, production data, notifications, releases, or deployment.

### Declared public surface

Every module under `src/`, `domain/` and `scripts/` declares a public surface, and the pull-request gate compares that surface against the merge base of the pull request (not against a checked-in expectation, which a deletion could update in the same commit). A declared name that disappears fails the gate unless `docs/public_surface_retirements.json` records it.

- A module's surface is its `__all__` when that is statically resolvable — literal lists and tuples, starred references to module-level literals, `dict.keys()` and `sorted(...)`. A module without `__all__` declares the top-level public names it defines itself, so a re-export must be listed in `__all__` to be protected. An `__all__` that cannot be resolved statically is itself a failure: declare it as a literal list of names.
- A name still counts as present when the module binds it at top level by any means, imports included, because `from x import utc_now as utc_now_iso` keeps the name reachable even though the module no longer defines it. When a module does declare `__all__`, that list is the surface, so narrowing it is a removal.
- The ledger is append-only, and the gate rejects a change that drops a recorded entry. An entry is one object with exactly `module`, `name` and `reason`. The gate enforces that `reason` is a sentence rather than a placeholder (at least 20 characters); whether it is a *true* sentence is the reviewer's call, which is why the entry lands in the diff next to the deletion instead of in a separate bookkeeping file. `name` `*` retires a whole module and is valid only while that module is really gone — so a deleted module or a module split into a package costs one entry, not one per name.
- Leaving a removal unrecorded fails, and so does deleting a recorded entry. Adding a name, or changing a symbol where it already lives, does not: the gate compares which names a module exposes, never how they are implemented. Moving a name to another module does fail for the module it left, unless that module still exposes it — an alias import counts.

Run it locally against any revision:

```sh
./.venv/bin/python scripts/guardrails_check.py --check-public-surface --public-surface-base <rev>
```

The base revision is never defaulted: `--check-public-surface` without `--public-surface-base` fails instead of comparing nothing. The workflow runs this check on `pull_request` events and nowhere else — a push to `main` reuses the required pull-request result, which is the same policy the full regression already follows. The workflow passes a merge base, so a branch that trails `main` is not charged for deletions made on `main` itself.

## C) Symbol Canonicalization Rule

- Any entrypoint that accepts user-entered symbol, broker raw payload, or OpenD/Futu underlying identifier must canonicalize to the shared symbol format before business logic.
- Canonical market symbols are values like `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not be persisted as runtime symbol config or position symbols.
- Shared alias handling lives in `src/application/opend_utils.py::resolve_underlier_alias`; new entrypoints should reuse it instead of adding ad hoc `upper()` or market-specific parsing branches.
