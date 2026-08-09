# Gateflow Plan — Release and Remote Upgrade v1.2.414

## Gate

- Work unit: `release-1.2.414-and-remote-upgrade`
- Gate: plan
- Branch: `codex/release-1.2.414`
- Base: `origin/main@64d30729`
- Goal confirmation: user explicitly authorized a new release if needed and completion of the remote upgrade.
- Release date: 2026-07-18
- Status: accepted after adversarial plan review (`pass-with-risks`)
- Accepted review: `docs/reviews/plan-review-20260718-174217.md`
- Artifact path: `docs/gateflow/release-1.2.414-plan-20260718.md`

## Goal and Direct Evidence

Publish the already-merged changes after `v1.2.413` as patch release `v1.2.414`, then upgrade the existing `liuxie-incus` deployment through its guarded release-upgrade CLI.

Direct facts:

- `origin/main@64d30729` contains merged PR #84 (release preflight acceleration) and PR #80 (cross-expiry attribution fail-closed fixes).
- Top-level `VERSION` remains `1.2.413`; GitHub latest release is `v1.2.413`, so the merged fixes are not consumable by the release-only remote upgrader.
- Remote `/home/om/apps/options-monitor` is a symlink to release `1.2.413`.
- Remote runtime root is `/var/lib/options-monitor`; `service.profile.json` records YAML-authoring US/HK configs and the five long-running services that require restart reconciliation.
- Pre-upgrade `om update verify --no-check-latest` is green: config identity/freshness and all monitored services pass.

## Success Signals

1. `VERSION=1.2.414`; `CHANGELOG.md` has one `1.2.414` section and an empty `Unreleased` section.
2. Rendered release notes contain `# options-monitor 1.2.414`, exclude `1.2.413`, and describe only changes already merged after `v1.2.413`.
3. Full release preflight, dependency graph, Ruff, focused attribution/release tests, and complete pytest pass on Python 3.12.
4. Release PR is reviewed, CI-green, merged, and the VERSION-driven workflow publishes tag/release `v1.2.414` from the release merge commit.
5. Remote `update check` observes `v1.2.414`; dry-run `update apply` targets `1.2.414` without error.
6. Confirmed remote upgrade succeeds, switches `/home/om/apps/options-monitor` to `releases/1.2.414`, rebuilds/validates US/HK runtime configs, and reconciles configured services.
7. Post-upgrade `update verify --no-check-latest` is green; remote VERSION and generated config versions are `1.2.414`; no service health check fails.

## Scope and Non-goals

Allowed release files:

- `VERSION`
- `CHANGELOG.md`
- `docs/gateflow/release-1.2.414-*.md`
- timestamped review artifacts under `docs/reviews/`

Allowed remote mutation after release publication:

- guarded `om update apply --confirm` against `/home/om/apps/options-monitor` and `/var/lib/options-monitor`;
- upgrade-owned materialization, runtime-config rebuild, symlink switch, and service restart/reconciliation.

Non-goals:

- no manual edits to production `config.yaml`, generated JSON configs, secrets, service units, ledger/position/trade state, notifications, or broker-facing data;
- no manual tag unless the VERSION-driven workflow fails and a separate repair decision is recorded;
- no cleanup of historical remote releases/caches;
- no feature/code changes beyond the already-merged PR #80/#84 scope.

## Sequencing

### R1 — Release metadata

- Bump `VERSION` from `1.2.413` to `1.2.414`.
- Add `## 1.2.414 - 2026-07-18` below empty `Unreleased`.
- Changelog entries:
  - maintainer-facing release preflight no longer runs the focused agent/plugin suite before full pytest;
  - tests no longer incur two real 30-second cooldown waits while preserving the 30-second production-policy assertion;
  - cross-expiry residual-tail quality follows actual gross/net evidence;
  - assigned-stock Combo attribution fails closed on conflicting/incomplete strategy provenance.

### R2 — Review, PR, and publication

- Run planreview, implementation deepreview, aggregate deepreview, and PR deepreview with durable artifacts.
- Run release metadata/rendered-note guards and complete preflight.
- Push release branch, create draft PR, recheck base/tag/release collision, CI, then merge under this user's release authorization.
- Wait for `Release from VERSION`; verify tag/release target commit and release notes.

### R3 — Remote upgrade

- Before mutation, rerun remote read-only `update check` and `update verify`.
- Run remote `om update apply` without `--confirm` and require target `1.2.414` with no planning error.
- Run the same command with `--confirm`.
- If upgrade returns non-zero, do not manually alter symlinks/config/services; inspect `upgrade_status.json` and use the documented rollback only if the failed operation switched current or left health non-green.
- After success, run `update verify --no-check-latest`, inspect current symlink/VERSION/config generated versions, and check the five long-running systemd services.

## Validation Matrix

- `release_check.py --tag v1.2.414 --render-notes-out <tmp>` plus exact heading/old-version exclusion assertions.
- `release_test_plan.py --mode full --base origin/main`.
- `release_preflight.sh --full`, then clean-worktree `--full --require-clean` after accepted commits.
- focused performance attribution and release test-plan suites.
- Ruff on changed production/tests; dependency graph check; `git diff --check`.
- GitHub PR required checks and release workflow.
- Remote pre/post `update verify`; post-upgrade systemd active/enabled checks from the guarded verifier.

## Stop Conditions and Rollback

Stop before release merge if:

- `origin/main` changes from `64d30729` with new unreleased product changes;
- tag/release `v1.2.414` appears;
- release notes or full preflight fail;
- PR review or CI has an unresolved material failure.

Stop before confirmed remote upgrade if:

- GitHub release `v1.2.414` is absent or targets the wrong commit;
- remote dry-run does not select exactly `1.2.414`;
- pre-upgrade verifier becomes non-green;
- upgrade lock is active or profile/config authority is invalid.

Rollback boundary:

- release metadata PR can be closed before merge;
- after publication, do not delete/rewrite the tag; fix-forward with a later patch if code is wrong;
- remote rollback uses `om update rollback --to-version 1.2.413 --confirm` only if post-upgrade health fails or the upgrade switched current and reports a non-recoverable failure.

## Residual Risks

- GitHub workflow or network failure: owned by the release CI repair loop.
- Dependency installation/cache miss on remote: bounded by guarded upgrader and recorded in `upgrade_status.json`.
- Service restart failure: reported by post-upgrade verifier; rollback decision remains operational and evidence-based.
- No unclassified residual risk.
