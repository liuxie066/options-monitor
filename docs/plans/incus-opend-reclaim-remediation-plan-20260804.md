# Incus `liuxie` OpenD reclaim/refault remediation plan

- Revision: 2
- Prepared: 2026-08-04
- Status: plan only; no source, service, Incus, Futu state, or production configuration change is authorized by this artifact
- Primary implementation slice: explicit Strategy Lab recorder account/endpoint binding and one matching OpenD dependency
- Production choice for the first rollout: preserve the historical sampler behavior explicitly as `lx@127.0.0.1:11111`

## 1. Decision summary

The incident is treated as a cgroup reclaim/refault problem triggered by two large OpenD mmap working sets, not as a disk-capacity or ordinary `read()` problem. The current Strategy Lab sampler is one avoidable activation path: it connects only to the historical default `127.0.0.1:11111`, but its generated systemd unit `Wants=` and `After=` every rendered OpenD service. In production that means a task using `lx` can also activate `sy` without consuming it.

This revision makes five explicit decisions:

1. Preserve the documented two-hour OpenD-backed Strategy Lab sampling contract. Do not invent a successful `skipped/opend_unavailable` result in this slice.
2. Make the recorder's account and endpoint explicit, pass the resolved host and port to the existing research CLI, and depend on at most the one OpenD unit that owns that account.
3. Preserve current production behavior by selecting `lx`; this turns the existing implicit `11111` behavior into a visible contract rather than changing quote ownership.
4. Determine the required Incus memory envelope through a separate host-authorized experiment. Do not prescribe `7 GiB`, `8 GiB`, or any other unmeasured target.
5. Exclude startup/readiness orchestration and per-account Futu HOME migration. Either requires its own plan and planreview only if the smaller fix plus a proven memory envelope is insufficient.

## 2. Evidence and causal model

### Current repository/runtime facts

- `src/application/service_deploy.py` builds the Strategy Lab sample command without `--opend-host` or `--opend-port`, then places the complete `opend_dependency_units` list in both `After=` and `Wants=`.
- `src/interfaces/cli/research.py` defaults that command to `127.0.0.1:11111`.
- Production runtime configuration maps `lx` to `127.0.0.1:11111` and `sy` to `127.0.0.1:11112` in both US and HK configurations.
- Both OpenD units are enabled under `multi-user.target`; both use `Type=simple`, `Restart=always`, and `RestartSec=10`.
- The current Strategy Lab sample unit names both OpenDs in `After=` and `Wants=` even though it uses only port `11111`.
- The latest observed sample unit result was successful. Its journal also shows provider rate-limit and quote-login warnings, so this plan preserves the current application outcome rather than reclassifying those conditions.
- The incident counters were cumulative and collected while the cgroup was repeatedly crossing `memory.high`; they establish reclaim pressure but do not establish the uncapped dual-cold-start working set.

### Causal statement used by this plan

The supported claim is: simultaneous or overlapping OpenD cold working sets inside the existing memory envelope can drive page eviction, mmap refault, sustained NVMe reads, system time, and iowait. The overbroad Strategy Lab dependency is one concrete trigger that should be removed. The evidence does **not** prove that shared HOME alone caused the read storm, that one fixed memory value is sufficient, or that HOME separation would reduce memory pressure.

## 3. Goal, non-goals, and completion definition

### Goal

Prevent an unrelated one-endpoint research sample from activating both OpenDs, then prove the smallest host-safe memory envelope that can sustain the two required gateways through a controlled cold-start sequence.

### Non-goals

- No change to Strategy Lab missing-data, error, receipt, retry, or notification semantics.
- No generic OpenD readiness state machine, `ExecStartPost=`, fixed sleep, start-limit policy, or restart-loop redesign.
- No change to trade intake, account mapping, ledger, notification, broker write, or option-position state.
- No Futu HOME, `Device.dat`, login, 2FA, cache, mmap, or state-directory migration.
- No IO throttle, `drop_caches`, cache deletion, log deletion, or destructive reproduction.
- No automatic release, deployment, service restart, Incus mutation, or production configuration write.

### Completion definition

The incident is not considered closed when the source patch merely passes tests. Closure requires all of the following independent evidence:

1. The production sampler command explicitly uses `lx@127.0.0.1:11111` and its systemd dependency set contains `options-monitor-opend-lx.service` but not `options-monitor-opend-sy.service`.
2. Applying that unit/profile change does not restart either OpenD process.
3. A host-authorized capacity experiment first proves a safe sequential cold start and then proves a parallel two-OpenD cold start within a pre-approved host budget, or the experiment fails closed and routes to a separately reviewed startup-sequencing design.
4. During the final warm observation window, `memory.events` high/oom counters are flat, memory pressure has returned to its pre-registered idle envelope, and refault/read activity is not sustained at incident levels.
5. Both account endpoints pass read-only identity/readiness checks, and at least one subsequent two-hour sample runs without activating or restarting `sy`.

## 4. Authorization and sequencing gates

| Gate | Scope | Mutation boundary | Entry condition | Exit evidence |
|---|---|---|---|---|
| A | Implement and test source-only recorder binding | Repository files only | This plan passes planreview | Focused tests, lint, full test baseline, rendered-unit assertions |
| B | Deliver source | Commit/push/merge only if separately requested | Gate A passes | Commit/ref evidence; no VERSION/tag/Release/deploy |
| C | Apply the released change without restarting OpenD | Release/upgrade and named systemd/profile changes; separately authorized | A released version exists; dry drift against the current profile succeeds | New unit/profile installed; both OpenD PIDs/timestamps unchanged |
| D | Measure and, if justified, change Incus memory | Host Incus config and controlled OpenD stop/start; separately authorized | Host budget, rollback owner, safe fallback account, and maintenance window are recorded | Smallest tested limit retained or baseline restored with a failed experiment artifact |
| E | Observe | Read-only production measurements | C and D pass | Dual-gateway and next-sampler evidence satisfy the completion definition |

Do not combine Gate B with C, or Gate C with D, by implication. Until Gate D passes, any production upgrade must use the existing no-restart option and an explicit non-OpenD restart allowlist; a normal all-service restart remains unsafe.

## 5. Gate A — source-only implementation plan

### 5.1 Public render contract

Add an optional `strategy_lab_recorder_account: str | None` argument to `render_service_bundle()` and expose it as:

```text
./om service render ... --strategy-lab-recorder-account lx
```

The argument follows these exact rules:

1. Normalize a supplied value to lowercase and require it to be present in the normalized `--accounts` selection.
2. If the recorder is not included, reject a supplied recorder account as contradictory input.
3. If `strategy_lab_recorder_source=local`, reject a supplied recorder account and render no OpenD host, port, or dependency.
4. If `strategy_lab_recorder_source=opend`:
   - resolve the selected accounts from the same authoring/runtime configuration used by service rendering;
   - retain only accounts whose runtime plan has `account_type=futu`;
   - require an explicit recorder account when more than one selected Futu account exists;
   - allow omission only when exactly one selected Futu account exists, in which case infer that sole account;
   - fail if there are zero Futu accounts, the explicit account is unknown/non-Futu, or its host/port is missing or invalid;
   - resolve the selected account in every requested market and fail if account type, host, or port differ across markets. When an account-owned OpenD unit is being rendered, its OpenD root must also agree across markets.
5. Never choose an account from list order, service order, the lowest port, or a silent fallback to `11111` for a fresh multi-account render.

The current helpers that select only the first market are insufficient for this binding check. Add a narrow internal resolver in `src/application/service_deploy.py`; do not add a new public domain entity or a second account-configuration authority.

### 5.2 Command and dependency projection

For `source=opend`, append the resolved values to the existing sample command:

```text
--opend-host <resolved-host> --opend-port <resolved-port>
```

Determine systemd dependencies as follows:

- If the bundle renders account-aware OpenD plans, require exactly one plan whose `account` equals the recorder account and use only that plan's service name.
- If `include_opend=false`, pass the explicit endpoint but add no OpenD unit dependency; the endpoint is externally managed and the documented prerequisite remains in force.
- A single legacy account-less OpenD plan may be used only when the selected configuration contains exactly one Futu account. A multi-Futu render with an account-less legacy plan must fail rather than guess which account the root owns.
- If `include_opend=true` and no unique permitted match exists, fail before returning or writing a bundle.

For the selected production binding the resulting unit must contain the equivalent of:

```ini
After=network-online.target options-monitor-opend-lx.service
Wants=options-monitor-opend-lx.service
ExecStart=... --source opend ... --opend-host 127.0.0.1 --opend-port 11111
```

It must not contain `options-monitor-opend-sy.service`. The launchd plist receives the same explicit host/port arguments but no invented dependency mechanism.

### 5.3 Service profile contract

Keep `service.profile.json` schema version `1`; this is an additive, optional-for-read compatibility field rather than a wholesale profile migration. A freshly rendered OpenD recorder profile must include:

```json
{
  "strategy_lab_recorder": {
    "enabled": true,
    "source": "opend",
    "binding": {
      "account": "lx",
      "host": "127.0.0.1",
      "port": 11111,
      "service_name": "options-monitor-opend-lx.service"
    }
  }
}
```

`service_name` is omitted when `include_opend=false`; for launchd it contains the selected launchd label when one is rendered. The existing recorder fields remain unchanged. A local-source recorder has no `binding` object.

The profile is a deployment receipt, not endpoint authority. On drift, `binding.account` is fed back into rendering, while host/port are freshly resolved from canonical config. A config endpoint change must therefore appear as profile/unit drift instead of being hidden by replaying stored host/port.

### 5.4 Legacy profile compatibility

Production currently has a multi-account `source=opend` profile with no binding. New drift code must support exactly one deterministic migration path:

1. Treat the legacy sampler's effective endpoint as its historical CLI default, `127.0.0.1:11111`.
2. Resolve all selected Futu accounts from the profile's canonical config source.
3. If exactly one account matches that endpoint across all requested markets, use it as the expected recorder account and report a structured compatibility warning containing the account and historical endpoint.
4. If zero or multiple accounts match, return the existing structured drift error and perform no writes, even with `--confirm`.
5. The first successful confirmed drift writes the explicit `binding`; subsequent drift runs must no longer use or report legacy inference.

Do not infer from the first account, first service, service-name suffix, or profile ordering. Local-source legacy profiles need no migration.

The drift response uses this stable warning shape and counts it in `summary.warning_count`, making `summary.status=warn` until the profile is migrated:

```json
{
  "compatibility_warnings": [
    {
      "code": "legacy_strategy_lab_recorder_binding_inferred",
      "account": "lx",
      "host": "127.0.0.1",
      "port": 11111
    }
  ]
}
```

This warning does not by itself block a separately confirmed drift write. A zero/multiple-match resolution is an error, not a warning, and must block all writes.

### 5.5 Owning files

- `src/interfaces/cli/service_ops.py`: parser and `render_service_bundle()` argument plumbing.
- `src/application/service_deploy.py`: binding resolution, validation, sample command/dependency projection, and profile receipt.
- `src/application/service_drift.py`: new-profile round trip and the one legacy effective-endpoint compatibility path.
- `tests/test_service_deploy.py`: renderer, CLI, profile, drift, legacy, systemd, and launchd regressions.
- `docs/DEPLOY_LINUX_MAC.md`: public render option, account selection rule, and externally managed OpenD behavior.
- `docs/SHADOW_REPLAY_RUNBOOK.md`: sampler endpoint ownership and preserved two-hour sampling/error semantics.
- `docs/STRATEGY_LAB_DESIGN.md`: only if its rendered-service example or profile contract is currently normative.

Do not edit `src/application/strategy_lab/`, `src/application/shadow_replay/`, OpenD watchdog behavior, account schema, runtime JSON/YAML config, systemd restart policy, or Futu data directories in Gate A.

### 5.6 Required tests

Add or update tests proving all of these cases:

1. Multi-Futu + OpenD source + missing recorder account fails before artifact output.
2. Explicit `lx` produces host/port `11111` and only the `lx` systemd dependency.
3. Explicit `sy` produces port `11112` and only the `sy` systemd dependency.
4. Unknown account, non-Futu account, missing endpoint, invalid port, and cross-market endpoint mismatch each fail closed.
5. Cross-market OpenD-root mismatch fails when an account-owned OpenD unit is included.
6. A sole selected Futu account remains backward compatible without the new flag.
7. `include_opend=false` still renders an explicit endpoint and no OpenD dependency.
8. A sole legacy account-less OpenD plan is accepted; a multi-Futu account-less legacy plan is rejected.
9. Local source with a recorder account is rejected and local source without it is unchanged.
10. The profile records the binding; fresh drift round-trips it without mismatch.
11. A legacy profile with one unique `127.0.0.1:11111` match reports the compatibility warning and produces new expected content; zero/multiple matches return an error and write nothing.
12. Launchd receives explicit endpoint arguments and no fake ordering primitive.
13. CLI parsing and help expose the new option, and existing recorder tests remain green.

Run in order:

```bash
./.venv/bin/python -m pytest -q tests/test_service_deploy.py
make lint
make test
```

On Linux, render into a temporary directory and run `systemd-analyze verify` against all generated unit files before any install. On macOS, record this check as deferred to the Linux pre-apply gate rather than weakening it.

### 5.7 Gate A acceptance and rollback

Accept Gate A only if every required case is asserted, lint/full tests pass, docs match the CLI, and `git diff --check` is clean. No production evidence is claimed at this point.

Source rollback is a scoped revert of this work unit. It must not touch runtime configs, research evidence, Futu state, or unrelated dirty files.

## 6. Gate B — source delivery boundary

Gate B occurs only after an explicit commit/push/merge request. It includes the files named in Gate A and the plan/review artifacts, but excludes `VERSION`, tag, GitHub Release, deployment, systemd application, Incus changes, and service restarts. Record the exact base/ref and test results.

## 7. Gate C — production rollout without OpenD restart

This gate requires separate release/upgrade and production-service authorization. It is designed to avoid reproducing the incident while the memory envelope remains unproven.

### Preconditions

- A released artifact containing Gate A exists; release publication is not inferred from source delivery.
- Capture the current release, profile hash, installed sample-unit hash, both OpenD `MainPID` values, and `ActiveEnterTimestampMonotonic` values.
- Re-read canonical US/HK account configuration and require that `lx` still resolves uniquely to `127.0.0.1:11111`. If it changed, stop and amend/review the plan instead of preserving a stale endpoint.
- From the target release, run read-only service drift against the current production profile. It must resolve the legacy endpoint uniquely to `lx`; otherwise abort before switching the release.
- `systemd-analyze verify` passes on the target rendered unit set.
- The Strategy Lab sample oneshot is not currently active.

### Apply sequence

1. Use the controlled upgrade path with `--no-restart-services`. Do not run the normal all-service restart path.
2. Allow the controlled drift reconciliation to install the explicit sampler unit/profile and reload affected timers.
3. Verify the installed sampler command and dependency fields before any manual service restart.
4. Restart only the separately approved non-OpenD long-running services that must load the new Python release. Build the allowlist from the profile, remove every service containing `opend`, show it in the change receipt, and execute it sequentially. Treat `options-monitor-trade-intake.service` as its own business-write-capable reactivation boundary: do not restart it merely because it appears in the non-OpenD allowlist.
5. Do not stop, start, or restart either OpenD in Gate C.

### Acceptance evidence

- The installed command contains `--opend-host 127.0.0.1 --opend-port 11111`.
- `Wants`/`After` names only `options-monitor-opend-lx.service` in addition to normal network ordering.
- Both OpenD `MainPID` and active-enter timestamp values are unchanged from the preflight snapshot.
- New profile binding is explicit and a second read-only drift is clean with no legacy warning.
- Target release/config identity, named non-OpenD services, timers, and read-only runtime health pass.
- No tick, notification, trade/position action, recorder write, or broker-facing mutation is triggered as a rollout test.

### Rollback

Stop the sample timer before restoring the old unit so a persistent timer cannot race the rollback. Use the controlled previous-release rollback with service restarts disabled, restore the prior generated unit/profile through the control plane, and restart only the same separately authorized non-OpenD allowlist. Require both OpenD PIDs to remain unchanged. Because the old sampler can activate both OpenDs, leave its sample timer stopped after rollback until the operator explicitly accepts that known risk or a replacement is ready.

Until Gate D passes, record a production runbook hold: upgrades must use `--no-restart-services`; an all-service restart or container reboot is not covered by Gate C.

## 8. Gate D — host-authorized Incus memory experiment

This is not a repository implementation step. `ssh liuxie-incus` enters the container and cannot mutate the host's Incus configuration; a host owner must perform and own the `limits.memory` change.

### Mandatory experiment inputs

Do not start until an operator records all fields:

```text
baseline_limits_memory=
baseline_limits_memory_enforce=
baseline_limits_memory_swap=
host_available_before=
minimum_host_reserve=
maximum_preapproved_candidate_limit=
candidate_step=             # no more than 1 GiB
safe_fallback_account=      # lx or sy
maintenance_window=
host_operator=
container_operator=
trade_intake_reactivation_authorized=  # separate because reconnect/backfill can write normal intake state
```

Do not change enforcement mode, swap policy, CPU limits, IO limits, or any Futu state. Preserve the exact original `limits.memory` value for rollback.

### Baseline artifact

Collect synchronized, timestamped samples before mutation:

- Host: expanded Incus instance config, host available memory, sibling workload headroom, and NVMe `iostat -xz`.
- Container cgroup: `memory.current`, `memory.peak`, `memory.high`, `memory.max`, `memory.events`, `memory.events.local`, `memory.pressure`, `memory.stat`, and `io.stat`.
- Container services: both OpenD PIDs, active timestamps, enabled states, sample timer next trigger, and read-only per-account endpoint/readiness results.
- Ten-minute idle slopes for `memory.events:high`, `pgscan`, `workingset_refault_file`, IO read bytes, and pressure totals. Counter decisions use deltas/slopes, never lifetime totals.

Before changing a limit, fill this decision table from the synchronized baseline. Do not invent thresholds after seeing a candidate result.

| Metric | Baseline value/slope | Pass threshold after warm-up | Immediate stop threshold |
|---|---:|---:|---:|
| Host available memory | operator fills | at or above minimum reserve | below minimum reserve |
| `memory.events:oom` / `oom_kill` | counter values | delta `0` | any positive delta |
| `memory.events:high` | events/minute | delta `0` for final ten minutes | positive delta in three consecutive 30-second samples |
| `memory.pressure full total` | delta/minute and `avg10` | operator-recorded idle envelope | above pre-registered bound in three consecutive samples |
| `pgscan` / `workingset_refault_file` | delta/minute | operator-recorded idle envelope | above pre-registered bound in three consecutive samples |
| NVMe/container read rate | idle p95 | operator-recorded idle envelope | at least `100 MB/s` for three minutes after warm-up |

The numerical pressure/refault/IO pass bounds must be written into the experiment artifact before the first candidate. Lifetime totals and the candidate results themselves cannot be used to move those bounds.

### Consumer quiescence and write boundary

Cold-starting OpenD while consumers remain connected can trigger reconnect/backfill or a scheduled tick, contaminating the experiment and potentially writing intake state or sending notifications. Before the experiment:

1. Choose a window that ends before the next mutation/notification-capable tick, auto-close, position-promotion, projection, upgrade, or Strategy Lab timer activation. If the recorded next-trigger inventory does not provide enough time for the `lx`, sequential-`sy`, parallel-start, cooldown, and rollback phases, postpone; do not broadly stop timers and then allow `Persistent=true` catch-up by accident.
2. Confirm no relevant oneshot is active. Wait for it to finish or postpone; do not kill it.
3. Stop `options-monitor-trade-intake.service` through systemd and wait for it to become inactive before stopping either OpenD. This prevents the cold-start experiment from driving normal intake writes.
4. Stop `options-monitor-strategy-lab-sample.timer` and record its next trigger and prior state. The selected window must finish before that trigger, so restoring it does not cause an immediate missed-run catch-up.
5. Do not manually run ticks, Strategy Lab actions, health paths with `ensure=True`, notifications, backfills, or broker/business writes as probes. Read-only `ensure=False`/status probes are allowed.

Restarting trade intake can reconnect, backfill, and write normal idempotent intake state. It is not part of the capacity measurement. Restore it only when `trade_intake_reactivation_authorized` is explicitly true; otherwise leave it stopped, report the availability impact, and do not claim Gate E or incident closure.

### Controlled sequence for each candidate

1. Reconfirm the consumer-quiescence conditions above and that trade intake and the sample timer are inactive.
2. Stop both OpenDs through systemd in the approved maintenance window. Do not kill processes directly and do not edit/delete their HOME state.
3. Treat the unchanged original memory limit as candidate `0` and test it first. Only if candidate `0` fails and the container has returned to the safe fallback state may the host operator raise `limits.memory` for the next candidate. Each increase is at most the pre-approved step and never exceeds the recorded cap. Verify the resulting `memory.high` and `memory.max` from inside the container before starting a process.
4. Start `lx` only. Use bounded, read-only checks for process state, port, program status, quote login, and account mapping. If 2FA/authentication is required, stop the experiment; do not create a restart loop.
5. Allow a five-minute warm-up, then require ten consecutive minutes inside the pre-registered idle envelope. If it fails, do not start `sy`.
6. Start `sy` once. Repeat the same bounded readiness and five-plus-ten-minute measurement sequence. This sequential phase is a safety screen, not a passing result.
7. If the sequential phase passes, stop `sy` and then `lx`, wait until pressure/IO returns to the recorded baseline, and start both units in one systemd request so systemd may schedule their cold starts together. Record both active-enter timestamps to prove this was a parallel rather than already-warm trial.
8. Apply the same bounded readiness, five-minute maximum warm-up, ten-minute steady window, and stop thresholds to the parallel phase. Only a candidate that passes this phase is a passing memory envelope.
9. Retain the first passing candidate. Restore the sample timer only if its recorded trigger was not missed. Restore trade intake only under its separate reactivation authorization, then proceed to Gate E.

### Immediate stop conditions

- Any `oom` or `oom_kill` increment.
- Host available memory falls below the pre-registered reserve.
- `memory.events:high`, pressure totals, `pgscan`, or refaults continue increasing through three consecutive 30-second samples after warm-up.
- NVMe reads return to an incident-like sustained plateau (the prior incident was approximately `125-130 MB/s`) rather than decaying after warm-up.
- Either endpoint maps to the wrong account, OpenD requests 2FA, or a service enters repeated restart.

On failure, stop `sy` first, wait for pressure to stabilize, then restore the original memory limit. Keep only the pre-approved safe fallback account active; do not lower the limit underneath two pressured processes. Restore the sample timer only if doing so cannot reactivate the failed account and no persistent catch-up is due. Keep trade intake stopped unless its independent reactivation/write boundary is authorized. Preserve all measurements and mark the candidate failed.

Because the original limit is candidate `0` and later candidates increase monotonically, the first candidate that passes both sequential and parallel phases is the smallest tested envelope. If no candidate within the host cap passes the parallel phase, Gate D fails. Do not compensate with an unreviewed readiness hook, IO throttle, swap change, or higher unapproved limit.

## 9. Gate E — observation and incident closure

After separately authorized trade-intake reactivation, observe for at least 24 hours and through at least one natural Strategy Lab sample. Capture:

- both account endpoint/readiness results;
- sampler result and timestamps;
- `sy` PID/active timestamp before and after the sample, proving the sampler did not activate or restart it;
- cgroup and IO counter deltas over the observation window;
- host reserve and NVMe behavior;
- service drift and target-release identity.

Pass only if the completion definition in section 3 holds. A healthy service/timer status alone is insufficient.

## 10. Conditional follow-up work, not current implementation

### Startup sequencing

Create a new plan and run planreview only if Gate D cannot prove a host-safe two-gateway cold start, or if the operator cannot allocate the required memory. That plan must own `inactive -> starting -> ready | auth_required | resource_blocked | failed`, reuse `ensure=False` read-only probes, prevent `Restart=always` from turning probe failure into a cold-start loop, and cover direct boot, dependency activation, process crash, container reboot, timeout, and 2FA. None of those semantics may be improvised in Gate A-D.

### Futu HOME/state isolation

Track shared HOME as a separate correctness/security migration, not as the performance fix. Its future plan must define per-account program-data origin, unique device identity, 2FA ownership, prohibition on old/new concurrent instances, rollback retention, and validation of both quote and trade identities. Do not copy, delete, or initialize `Device.dat` or `.com.futunn.FutuOpenD` under this plan.

## 11. Alternatives rejected

- **Remove all sampler `Wants=` and return success when OpenD is unavailable:** changes evidence semantics and can silently create sampling gaps.
- **Keep all dependencies and only raise memory:** leaves an unrelated service activation relationship and makes future resource needs unnecessarily larger.
- **Add a generic readiness gate now:** the failure and restart state machine is not needed to prove the smaller fix and can amplify cold starts if attached incorrectly.
- **Use fixed `7/8 GiB` limits:** the incident peak was constrained by `memory.high`; it is not an uncapped working-set measurement.
- **Split HOME now:** crosses Futu device/login state and can increase rather than decrease duplicate cache working sets.
- **Throttle NVMe first:** masks reclaim/refault symptoms without correcting activation scope or capacity.

## 12. Residual risks and tracking destinations

- Direct container boot still starts both enabled OpenD units. Gate D therefore includes a parallel two-unit cold-start trial; an exact full-container reboot remains unexecuted unless separately authorized and is tracked as a residual operational validation risk. If the parallel trial cannot pass, track a separately reviewed startup-sequencing work unit.
- Provider quote login/rate-limit warnings can make a sample incomplete even when the unit exits successfully. Track this in Strategy Lab evidence-quality monitoring; do not redefine it in the service renderer.
- Shared Futu state remains a correctness and authentication risk. Track it in a dedicated Futu state-isolation migration; it is not a prerequisite for this incident fix.
- An external writer or operator can still start both OpenDs outside the sampler path. The host memory proof and operational runbook, not additional application coupling, own that risk.
- Until Gate D passes, normal all-service upgrades and container reboots remain outside the proven envelope and are explicitly held.

## 13. Handoff invariant

An implementation agent receiving Gate A must not design a missing-sample policy, change OpenD lifecycle/restart behavior, tune Incus, touch Futu HOME, publish a release, or operate production. If a required test exposes a need for any of those changes, stop and return the evidence for a new plan decision.
