# Gateflow Plan — Release v1.2.413

## Gate

- Work unit: `release-1.2.413`
- Gate: plan
- Branch: `codex/release-1.2.413`
- Base: `origin/main@929aae4b`
- Goal confirmation: user confirmed release/readiness progression on 2026-07-18
- Status: accepted after plan re-review (`pass-with-risks`)
- Artifact path: `docs/gateflow/release-1.2.413-plan-20260718-235059.md`
- Accepted re-review: `docs/reviews/plan-review-20260718-235825.md`

## Goal / Motivation / Success Signal

### Goal

Publish the already-merged Python 3.12 runtime contract as the next patch release, `v1.2.413`, using the repository's VERSION-driven release workflow.

### Direct facts

- `main` currently declares `VERSION=1.2.412`.
- `CHANGELOG.md` has a populated `Unreleased` section containing only the Python 3.12 runtime-contract changes.
- GitHub release `v1.2.412` is published from `e128b412`.
- Tag/release `v1.2.413` does not exist.
- PR #81 merged the reviewed runtime contract at `929aae4b`; its final head passed Agent Plugin, Guardrails, and CodeQL checks.
- The original audit from the stale primary checkout (`v1.2.402`) is retained only as host/runtime-data baseline evidence; it is not release-candidate compatibility evidence.
- Latest-code read-only audit executed from `codex/release-1.2.413@929aae4b` with explicit Python 3.12.13, the primary US/HK runtime configs passed by absolute path, a temporary ignored runtime-dependency `.venv` link, and a temporary read-only `config.yaml` symlink used only to verify generated-config source authority. Both `config_validate` calls pass; both `runtime_status` calls report config identity/freshness authority `ok=true`; the latest runtime artifact is stale (`2026-05-15T18:24:59Z`) rather than missing or unreadable.
- Latest-code `healthcheck` loads both configs and the existing ledger successfully. It remains non-green because OpenD/Telnet are offline; US also lacks external-holdings Feishu credentials in this non-service shell. These are external operational prerequisites, not Python import/runtime failures, and production canary evidence remains deferred until separately authorized service startup.

### Success signals

1. `VERSION` is exactly `1.2.413`.
2. `CHANGELOG.md` retains an empty `Unreleased` heading and moves the existing runtime-contract notes unchanged into `## 1.2.413 - 2026-07-18`.
3. `release_check.py --tag v1.2.413 --render-notes-out ...` passes; rendered notes contain the exact `# options-monitor 1.2.413` heading and do not contain `1.2.412`.
4. Release preflight, full pytest, Ruff, dependency graph, smoke, agent-plugin tests, and example US/HK config validate/build dry-runs pass under explicit Python 3.12.
5. Deepreview and PR review find no unresolved material issue.
6. A draft release PR is created; after merge, the VERSION-driven workflow—not this work unit—creates tag/release `v1.2.413`.

## Scope Boundary / Non-goals

- Modify only release metadata plus required Gateflow review/closeout artifacts.
- Do not change runtime code, dependencies, config, services, notifications, ledger/positions, broker-facing data, or production runtime state.
- Do not manually create or push tag `v1.2.413`.
- Do not perform production upgrade, service restart, OpenD startup, or canary notification in this work unit.
- Do not clean the primary dirty workspace or unrelated worktrees/branches.
- Do not fold later unrelated `main` changes into the release branch after implementation starts; if `origin/main` advances materially, stop and rebase/re-review deliberately.

## Implementation Slice R1 — Version metadata

- **Objective**: prepare the minimal VERSION-driven release bundle for v1.2.413.
- **Allowed product files**:
  - `VERSION`
  - `CHANGELOG.md`
- **Allowed process artifacts**:
  - `docs/gateflow/release-1.2.413-*.md`
  - `docs/reviews/plan-review-*.md`
  - `docs/reviews/code-review-*.md`
  - `docs/reviews/pr-*-review-*.md`
- **Exact changes**:
  - replace `1.2.412` with `1.2.413` in top-level `VERSION`;
  - keep `## Unreleased` present and empty;
  - create `## 1.2.413 - 2026-07-18` immediately below it;
  - move the current Unreleased `Changed` and `Fixed` bullets into the new version section without semantic rewriting.
- **Invariants**:
  - previous `1.2.412` and older sections remain byte-for-byte unchanged;
  - no duplicate `1.2.413` heading;
  - no pre-existing tag/release collision;
  - release notes describe only code already merged to main;
  - immediately before implementation and immediately before draft-PR creation, fetched `origin/main` remains exactly `929aae4b` and no remote tag or GitHub release named `v1.2.413` exists.
- **Non-goals**: no code cleanup, docs refresh, dependency update, or deployment mutation.
- **Completion signal**: metadata diff is minimal, validations pass, deepreview accepts the slice.
- **Stop conditions**:
  - `origin/main` advances with new unreleased product changes before PR creation;
  - tag/release `v1.2.413` appears;
  - release automation contract or expected changelog content differs from current facts.

## Validation Plan

Use explicit Python 3.12.13. A temporary ignored `.venv` link may point to the primary workspace runtime-dependency venv solely so existing subprocess tests can spawn `<repo>/.venv/bin/python`; run pytest/preflight through `OM_PYTHON=/Users/om/.pyenv/shims/python3.12`, then remove the link. A temporary `config.yaml` symlink to the primary authoring file is allowed only for the read-only latest-code authority audit and must be removed immediately afterward; it must never be edited.

### Remote drift/collision guard

Run immediately before implementation and repeat immediately before draft-PR creation:

```bash
git fetch origin main
test "$(git rev-parse origin/main)" = "929aae4b5e92ffb62e5437118f3ab16e3912a405"
test -z "$(git ls-remote --tags origin refs/tags/v1.2.413)"
! gh release view v1.2.413 --repo liuxie066/options-monitor
```

### Latest-code read-only readiness evidence

Run from the clean release worktree with the primary runtime-config paths and runtime-artifact paths passed explicitly. Record config authority, runtime freshness, healthcheck external dependencies, and remove temporary symlinks afterward. This audit does not start OpenD or send notifications.

### Release and repository validation

```bash
OM_PYTHON=/Users/om/.pyenv/shims/python3.12 \
  /Users/om/.pyenv/shims/python3.12 scripts/release_check.py \
    --tag v1.2.413 \
    --render-notes-out /private/tmp/options-monitor-v1.2.413-release-notes.md

grep -Fx '# options-monitor 1.2.413' /private/tmp/options-monitor-v1.2.413-release-notes.md
! grep -q '1.2.412' /private/tmp/options-monitor-v1.2.413-release-notes.md

OM_PYTHON=/Users/om/.pyenv/shims/python3.12 \
  /Users/om/.pyenv/shims/python3.12 scripts/release_test_plan.py --mode full --base origin/main

OM_PYTHON=/Users/om/.pyenv/shims/python3.12 make release-preflight ARGS="--full"

/Users/om/.pyenv/shims/python3.12 -m ruff check .
/Users/om/.pyenv/shims/python3.12 scripts/generate_dependency_graph.py --check
git diff --check

OM_PYTHON=/Users/om/.pyenv/shims/python3.12 ./om config validate --source yaml --market us --config-yaml configs/examples/config.yaml.example
OM_PYTHON=/Users/om/.pyenv/shims/python3.12 ./om config validate --source yaml --market hk --config-yaml configs/examples/config.yaml.example
OM_PYTHON=/Users/om/.pyenv/shims/python3.12 ./om config build --source yaml --market us --config-yaml configs/examples/config.yaml.example --dry-run
OM_PYTHON=/Users/om/.pyenv/shims/python3.12 ./om config build --source yaml --market hk --config-yaml configs/examples/config.yaml.example --dry-run
```

After the accepted metadata commit, rerun release preflight with `--require-clean` before draft PR creation.

## Docs Decision

`CHANGELOG.md` is the only public documentation change. Existing runtime/install/runbook documentation was already updated by PR #81. No additional public docs are required.

## Risks / Residual Risks

| Risk | Control / classification |
|---|---|
| Release number collides with an asynchronously created tag/release | recheck immediately before implementation and before PR merge; stop on collision |
| New main changes expand Unreleased after branch creation | compare `origin/main`; stop and re-scope rather than silently include them |
| Clean worktree lacks `.venv` and existing tests spawn it | temporary ignored runtime-dependency venv link plus explicit `OM_PYTHON`; remove after validation |
| Latest-code healthcheck is non-green because OpenD/Telnet are offline; US external holdings credentials are unavailable in the non-service shell | not a release-metadata blocker; config parsing, config authority, and ledger loading succeed; production canary and service-env verification are assigned to post-release operational work with explicit service authorization |
| Auto-release workflow fails after merge | handled as release CI/repair loop; no manual tag unless explicitly authorized |
| Python 3.12 breaks an unknown production host | contract is already merged; post-release canary must inventory/rebuild old venv before confirmed upgrade |

## Planreview Finding Disposition

| Finding | Disposition | Evidence |
|---|---|---|
| `R413-PR-001` | accepted and fixed | stale-checkout audit downgraded to baseline; latest-code audit rerun from `929aae4b` with absolute primary config/runtime paths and explicit Python 3.12; config validation/authority pass, runtime artifacts are readable but stale, external readiness gaps are classified |
| `R413-PR-002` | accepted and fixed | validation now renders and asserts release notes; executable `origin/main`, tag, and GitHub release collision guards run before implementation and draft PR |

No unclassified residual risk or blocking open question remains entering plan re-review.
