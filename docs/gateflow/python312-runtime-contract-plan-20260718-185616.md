# Gateflow Plan — Python 3.12 Runtime Contract

## Gate

- Work unit: make Python 3.12 the repository-wide hard minimum and eliminate silent fallback to older macOS Python
- Gate: plan
- Branch: `codex/python312-runtime-contract`
- Base: `origin/main@e128b412` (`v1.2.412`)
- Goal confirmation: user confirmed Python 3.12 as the hard minimum on 2026-07-18
- Status: accepted after plan re-review (`pass-with-risks`)
- Artifact path: `docs/gateflow/python312-runtime-contract-plan-20260718-185616.md`

## Goal / Motivation / Success Signal

### Goal

Encode one enforceable Python runtime contract across local launchers, installation, release preflight, service upgrade runtime creation, CI, generated developer test commands, and documentation:

```text
Python >= 3.12
```

### Motivation

The current launchers prefer `<repo>/.venv/bin/python` but silently execute bare `python3` when the venv is absent. In temporary worktrees, cron shells, and macOS shells with a different `PATH`, this can select Apple Python 3.9 and fail while importing modern annotations. The resulting error is misclassified as a configuration failure and repeatedly requires a manual rerun.

Direct code evidence:

- `om` and `om-agent` end with `exec python3 ...`.
- `scripts/release_preflight.sh` falls back to `python3` without a version check.
- `scripts/install.sh` currently accepts Python 3.10 and defaults to `python3`.
- `scripts/install_agent_plugin.sh` defaults to `python3`.
- service upgrade venv creation and release checks invoke `python3` from `PATH`.
- all three Python CI workflows currently provision Python 3.11.
- `pyproject.toml` targets `py311`; installation docs state Python 3.10.
- there is no package/import guard that converts an unsupported interpreter into a clear runtime-contract error.

### Success signals

1. `./om` and `./om-agent` never execute an interpreter below 3.12.
2. Missing `.venv` prefers an explicit compatible override or `python3.12`; an incompatible final `python3` candidate fails with the resolved path/version and remediation.
3. Importing `src` or `domain` under Python <3.12 fails immediately with a stable contract error before parsing deeper modules.
4. Install, plugin-install, release-preflight, and service-upgrade runtime creation reject or avoid old interpreters.
5. CI runs the supported 3.12 contract, not 3.11.
6. Repo-generated test/release commands use `.venv/bin/python`, avoiding shell `python3` ambiguity.
7. Focused launcher/install/upgrade tests, release tests, full pytest, Ruff, compileall, dependency graph, and config validate/build dry-runs pass under Python 3.12.

## Non-goals / Scope Boundary

- No business logic, strategy, ledger, notification, broker, config schema, or runtime-state behavior changes.
- No automatic mutation or recreation of an existing incompatible `.venv`; fail with remediation instead.
- No support matrix for Python 3.10 or 3.11 after this change.
- No exact Python 3.12 patch pin. The contract is minimum minor version, so no `.python-version` file that would accidentally impose a specific patch release.
- No mass rewrite of every `#!/usr/bin/env python3` shebang. Supported public/tooling paths receive explicit guards; generic shebang churn would not itself guarantee the selected interpreter.
- No new package/build system or PEP 621 packaging metadata solely to express `requires-python`.

## First-Principles Judgment

The owning boundary is interpreter selection, not individual configuration commands. Fixing commands one by one would preserve the same failure in other worktrees and services. Conversely, rewriting all Python entry modules or adding a packaging system is unnecessary. The minimum sufficient design is:

1. one small shell runtime selector used by repository-owned launchers/tooling;
2. a standalone equivalent check inside the curl-installable installer;
3. import-level fail-fast guards for `src` and `domain`;
4. service-upgrade venv creation bound to the already-running supported interpreter;
5. CI/docs/generated commands aligned to the same contract.

This is not over-designed: it adds one shared shell file because four repository shell entrypoints otherwise need identical candidate selection/error handling, while the standalone installer remains self-contained by necessity.

## Contract / Public Interface Changes

### Runtime contract

- Minimum supported interpreter changes from loosely documented Python 3.10/CI 3.11 to Python 3.12.
- Unsupported interpreters produce a stable non-zero failure with:
  - required version;
  - selected executable/path where available;
  - observed version where available;
  - remediation (`create/recreate .venv with Python 3.12`, install Python 3.12, or set an explicit override).

### Launcher selection contract

Repository-runtime candidate order for `om`, `om-agent`, release/test tooling, and Make targets:

1. explicit `OM_PYTHON` override;
2. existing repo `.venv/bin/python`;
3. explicit generic `PYTHON` override;
4. `python3.12` from `PATH`;
5. `python3` only as a diagnostic final candidate, accepted only if it is >=3.12.

`OM_PYTHON` is the sole explicit escape hatch that may bypass an existing incompatible repo venv. Without `OM_PYTHON`, the presence of `.venv/bin/python` (including a broken/non-executable target) is authoritative: it must validate as >=3.12 or fail before considering `PYTHON` or `PATH`. This prevents silent dependency/interpreter mixing while preserving an intentional recovery path.

Venv-bootstrap tooling uses a separate candidate profile and never selects the target repo `.venv` that it is about to create or update:

1. explicit `OM_PYTHON` override;
2. explicit generic `PYTHON` override;
3. `python3.12` from `PATH`;
4. `python3` only as a diagnostic final candidate, accepted only if it is >=3.12.

An explicitly named override is authoritative in both profiles: if it is missing or incompatible, fail instead of silently falling through.

### No data/schema changes

No JSON, SQLite, config, report, tool payload, or broker-facing schema changes.

## Affected Files / Ownership

### Shared runtime selection and public launchers

- new `scripts/python_runtime.sh`
- `om`
- `om-agent`
- `scripts/install_agent_plugin.sh`
- `scripts/release_preflight.sh`
- `Makefile`

### Import and install contract

- `src/__init__.py`
- `domain/__init__.py`
- `scripts/install.sh`
- `pyproject.toml`

### Runtime upgrade ownership

- `src/application/service_upgrade.py`
- `tests/test_service_deploy.py`

### CI / generated commands / docs

- `.github/workflows/agent-plugin.yml`
- `.github/workflows/guardrails.yml`
- `.github/workflows/_release-reusable.yml`
- `src/application/release_test_plan.py`
- `tests/test_release_test_plan.py`
- `docs/INSTALL.md`
- `CHANGELOG.md`
- generated dependency graph if tests/import counts change

### New/focused tests

- new `tests/test_python_runtime_contract.py`
- `tests/test_install_script.py`
- focused existing entrypoint, installer, service-upgrade, release-plan tests as required

## Implementation Decisions

### Shell selector

`scripts/python_runtime.sh` exposes narrow functions and has no side effects when sourced:

- `om_select_repo_python <repo-root>` implements the repository-runtime profile and treats a present repo venv as authoritative unless `OM_PYTHON` is set;
- `om_select_bootstrap_python <target-venv>` implements the bootstrap profile and rejects any candidate resolving inside the target venv;
- a shared validator checks `sys.version_info >= (3, 12)`;
- failures print one actionable error and return non-zero;
- successful selection prints the compatible executable path to the caller.

The selector must work under `set -euo pipefail`, tolerate commands and broken symlinks that do not exist, resolve/report the selected executable where possible, and avoid executing the target module during selection. `scripts/install_agent_plugin.sh` uses the bootstrap profile before any `.venv` mutation and otherwise preserves its existing `python -m venv .venv` behavior; this work unit does not add automatic clearing or deletion.

### Python package guard

`src/__init__.py` and `domain/__init__.py` perform a tiny `sys.version_info` check using syntax valid on Python 3.9. This ensures old Python reports the contract error before importing modules that use unsupported syntax/type operations. The duplicated two-line boundary check is preferred over making the domain layer import `src` or introducing a cross-layer runtime module.

### Installer

`scripts/install.sh` remains standalone because it is fetched with curl before the repository exists. It prefers `python3.12`, honors `--python`/`PYTHON`, and changes its runtime test to >=3.12. Help and remediation text are updated.

### Service upgrade

Venv creation uses the current supported `sys.executable`, not a fresh `python3` lookup. Dependency-cache identity records the `major.minor` runtime contract rather than the absolute interpreter path, avoiding cache misses across release directories. Release validation runs with the newly prepared target venv Python.

### Generated commands

Release test plans use `./.venv/bin/python` rather than `python3`. This is a display/execution contract change only; test selection remains unchanged.

## Implementation Slices

### S1 — Runtime selector, launcher, import and installer contract

- **Objective**: prevent public/local entrypoints and installers from accepting Python <3.12.
- **Allowed files**:
  - `scripts/python_runtime.sh`
  - `om`, `om-agent`
  - `scripts/install.sh`, `scripts/install_agent_plugin.sh`, `scripts/release_preflight.sh`
  - `Makefile`, `pyproject.toml`
  - `src/__init__.py`, `domain/__init__.py`
  - `tests/test_python_runtime_contract.py`, `tests/test_install_script.py`, relevant entrypoint tests
- **Expected outcome**: deterministic selection and clear fail-fast behavior with no business-code changes.
- **Validation**:
  - shell parse checks;
  - fake-interpreter tests for venv, override, `python3.12`, and old `python3` paths;
  - explicit `OM_PYTHON` recovery over an incompatible repo venv, plus no-override fail-fast on the same venv;
  - bootstrap selection tests proving the source interpreter never resolves inside the target `.venv`;
  - package guard subprocess tests;
  - installer compatible/incompatible runtime tests;
  - existing entrypoint and installer suites.
- **Stop condition**: any ambiguity about override precedence or existing incompatible venv handling.

### S2 — Service upgrade, CI, generated commands and documentation

- **Objective**: eliminate remaining repository-owned automation that provisions or advertises Python <3.12.
- **Allowed files**:
  - `src/application/service_upgrade.py`
  - `tests/test_service_deploy.py`
  - three Python workflows
  - `src/application/release_test_plan.py`, `tests/test_release_test_plan.py`
  - `docs/INSTALL.md`, `CHANGELOG.md`
  - dependency graph outputs if required
- **Expected outcome**: upgrade-created runtimes inherit a supported interpreter; CI and operator guidance exercise the same contract.
- **Validation**:
  - focused service-upgrade runtime tests;
  - release-test-plan tests;
  - workflow/static assertions;
  - install docs wording checks;
  - config validate/build dry-runs.
- **Stop condition**: service-upgrade cache identity would become tied to a release-specific absolute path or target runtime is validated with the wrong interpreter.

## Test / Validation Matrix

Focused:

```bash
/Users/om/.pyenv/shims/python3.12 -m pytest \
  tests/test_python_runtime_contract.py \
  tests/test_refactor_entrypoints.py \
  tests/test_install_script.py \
  tests/test_release_test_plan.py -q

/Users/om/.pyenv/shims/python3.12 -m pytest tests/test_service_deploy.py -q
```

Static/tooling:

```bash
bash -n om om-agent scripts/python_runtime.sh scripts/install.sh scripts/install_agent_plugin.sh scripts/release_preflight.sh
ruff check .
/Users/om/.pyenv/shims/python3.12 -m compileall -q domain src scripts
/Users/om/.pyenv/shims/python3.12 scripts/generate_dependency_graph.py --check
git diff --check
```

Runtime contract assertions:

- fake Python 3.9 and 3.11 are rejected with required/observed version evidence;
- fake/real Python 3.12 is selected and receives the expected module argv;
- incompatible existing `.venv` is not bypassed implicitly; explicit `OM_PYTHON` recovery is allowed and tested;
- without `OM_PYTHON`, a present broken/incompatible repo venv blocks `PYTHON` and PATH fallback;
- bootstrap tooling never selects the `.venv` it is creating/updating;
- `OM_PYTHON` and generic `PYTHON` precedence are explicit and tested for both selector profiles;
- service upgrade commands use the running interpreter/target venv, not bare `python3`.

Operational dry-runs:

```bash
./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run
```

Final:

```bash
/Users/om/.pyenv/shims/python3.12 -m pytest -q
```

## Docs Decision

Update installation documentation and Unreleased changelog because the minimum supported Python version is a public operational contract. Do not add a new design document or config key.

## Risks / Residual Risks

| Risk | Planned control / classification |
|---|---|
| Existing installations created with Python 3.10/3.11 stop launching after upgrade | intentional breaking runtime contract; document recreation/remediation in current work unit |
| Custom `--python`/override path is old | explicit overrides are authoritative and validated before venv creation/launch; fixed in current work unit |
| `OM_PYTHON` intentionally bypasses an incompatible repo venv | explicit recovery path only; document and test separately from silent fallback |
| Bootstrap script selects the venv it mutates | separate bootstrap profile rejects target-venv candidates; fixed in current work unit |
| Temporary worktree lacks dependencies | selector chooses Python 3.12 but normal import may still report missing dependency; expected and distinct from version failure |
| Direct execution of an arbitrary internal file with an old generic shebang | import guards cover normal `src`/`domain` paths; unsupported direct internal-script invocation remains outside public entrypoint contract |
| Exact 3.12 patch drift | no patch pin; accepted by minimum-version contract |

No unclassified residual risk or blocking open question remains entering plan review.

## Completion Report Format

Final closeout will include:

- selected runtime contract and precedence;
- changed public/tooling paths;
- focused/full test counts;
- config dry-run results;
- review finding status;
- migration/remediation wording;
- Draft PR URL and final CI/mergeability state;
- remaining classified risks and next entry point.

## Plan Review Finding Decisions

| Finding | Decision | Plan change |
|---|---|---|
| `PR-001` override vs incompatible venv contradiction | accepted | define `OM_PYTHON` as the sole explicit recovery override; without it, a present repo venv is authoritative and blocks fallback |
| `PR-002` bootstrap script selecting its target venv | accepted | split the shared helper into repository-runtime and bootstrap selection profiles; bootstrap rejects candidates inside the target venv and preserves existing non-clearing behavior |

Review artifact: `docs/reviews/plan-review-20260718-190126.md`.

Re-review artifact: `docs/reviews/plan-review-20260718-190335.md` (`pass-with-risks`).
