# Gateflow Final Closeout — Python 3.12 Runtime Contract

## Gate

- Work unit: `python312-runtime-contract`
- Gate: final closeout
- Branch: `codex/python312-runtime-contract`
- Base: `main@e128b412c3b1d1a9f8aeaa92bb9c9f37fe2d2f39`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/81`
- Status entering closeout: `draft-PR-pass` at `629b795a98e83dbcbac6188552ddd5367ef5ab34`
- Artifact path: `docs/gateflow/python312-runtime-contract-final-closeout-20260718-195302.md`

## What Changed

The repository now enforces one public runtime contract:

```text
Python >= 3.12
```

Repository runtime selection precedence is:

1. `OM_PYTHON` — sole explicit recovery override, validated for Python 3.12+.
2. Authoritative repository `.venv/bin/python` — preserved as the execution entrypoint so venv dependencies remain active.
3. `PYTHON` — only when the repository venv is absent.
4. `python3.12` from `PATH`.
5. `python3` from `PATH` as a diagnostic final candidate; rejected if below 3.12.

Bootstrap selection is separate from repository execution selection. It never chooses the target venv it is about to create/update, and physical-path containment covers symlinked target venvs and outside aliases resolving into the target.

The contract now covers:

- `om` and `om-agent`;
- shared shell runtime selection;
- standalone installation and agent-plugin bootstrap;
- release preflight and Makefile tooling;
- `src` / `domain` import guards;
- service-upgrade venv creation, dependency-cache identity, and release validation;
- generated release/test commands;
- Python CI workflows and Ruff target;
- current install, deployment, release, agent, operator-runbook, and dependency-graph guidance.

No strategy, config schema, notification, ledger, broker, option-position state, or production runtime-state behavior was changed.

## Verification

- Full pytest after final CI fix: `2680 passed, 10 skipped in 95.57s`.
- Focused S1 runtime/entrypoint/install validation: `28 passed`.
- Focused S2 release/upgrade validation: `106 passed`.
- Final runtime-contract regression: `11 passed`.
- Real venv-symlink launcher smoke: `./om-agent spec` passed.
- `ruff check .`: pass.
- Python 3.12 compileall: pass.
- Bash parse: pass.
- Dependency graph: 468 production modules, zero cycles.
- `git diff --check`: pass.
- US/HK YAML config validation and config-build dry-runs: pass.
- GitHub Actions for `629b795a`: Agent Plugin pass, Guardrails pass, CodeQL Actions/Python pass.

The clean-worktree full-suite harness temporarily linked the repository runtime-dependency venv because several existing tests intentionally spawn `<repo>/.venv/bin/python`; the link was removed after validation.

## Docs Updates

Updated current operational guidance in `AGENTS.md`, `README.md`, `RUNBOOK.md`, install/deployment/release/tool-reference docs, platform hints, changelog, and generated dependency documentation. Historical gateflow/review/memory snapshots remain unchanged as evidence.

## Finding Status

| Review stage | Findings | Final disposition |
|---|---:|---|
| Plan review | 2 | both accepted/fixed/re-reviewed |
| S1 deepreview | 1 | accepted/fixed/re-reviewed |
| S2 deepreview | 0 | pass-with-risks |
| Aggregate deepreview | 2 | both accepted/fixed/re-reviewed |
| PR deepreview | 2 | both accepted/fixed/re-reviewed |
| PR CI | 1 | accepted/fixed/re-reviewed; remote checks passed |

No accepted finding remains open.

## Remaining Risks / Owners

- Arbitrary direct execution of internal files with generic `python3` shebangs remains outside supported public entrypoints. Owner: operator/public-entrypoint contract; no follow-up issue required unless such a file becomes a supported entrypoint.
- Exact Python 3.12 patch versions remain intentionally unpinned. Owner: dependency/release policy; no follow-up issue required under the minimum-version contract.
- Historical documents may retain older commands as snapshots. Owner: documentation evidence policy; current runbooks are covered by regression tests.

All residual risks are classified; none blocks the PR.

## Draft PR / Issue Status

- Draft PR: `#81`, open, draft, mergeable.
- PR body matches the final runtime semantics, validation counts, and review history.
- Requested reviewers: none.
- External PR comments: none.
- Issue link: not applicable; this work unit was not created from a numbered GitHub issue.
- Issue closeout comment: not applicable.

## Completion Status / Next Entry Point

`final closeout pass` after this artifact is committed, pushed, and the resulting docs-only head is checked.

Next entry point: the human operator may review PR #81 and separately authorize marking it ready, requesting reviewers, or merging. Gateflow does not perform those actions automatically.
