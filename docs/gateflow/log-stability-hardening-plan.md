# Gateflow Plan — log-stability-hardening

- Gate: plan
- Work unit: `log-stability-hardening`
- Branch: `codex/log-stability-hardening`
- Status: revised after planreview; pending re-review
- Artifact: `docs/gateflow/log-stability-hardening-plan.md`
- Design input: `docs/plans/remote-disk-log-stability-plan-20260719-093026.md`
- Prior review input: `docs/reviews/plan-review-20260719-092654.md`

## Goal and evidence

Production evidence on 2026-07-19 showed two independent repository-owned log amplifiers:

1. `options-monitor-runtime-status.service` invokes raw `om-agent runtime_status`, writing a large pretty JSON envelope to journald every 15 minutes even though `./om status` already provides a compact human summary.
2. Two observed OpenD-dependent one-shot services can remain `activating` indefinitely because generated systemd units have no `RuntimeMaxSec`. Phone-verification/disconnect failures inside those invocations consequently continue emitting Futu SDK logs until manually stopped. A separate long-running trade-intake restart loop also exists, but current evidence shows that fixing it requires a distinct service exit/restart contract rather than the same one-shot guardrail.

The work unit succeeds when:

- the generated Runtime Status service uses a bounded journal-summary mode owned by the existing `./om status` facade and every tested envelope stays at or below 20 lines and 16 KiB UTF-8;
- the two observed stuck OpenD-dependent one-shot units have an explicit 10-minute `RuntimeMaxSec` safety net while tick, Runtime Status, projection verification, and long-running services remain unchanged;
- phone-verification and disconnect floods from those one-shots stop no later than the service runtime bound;
- focused tests and the relevant broader quality gates pass.

## Scope

### In scope

- generated service definitions in `src/application/service_deploy.py`;
- existing Runtime Status CLI summary path;
- focused tests and operator-facing service documentation affected by generated unit behavior.

### Non-goals

- production deployment, restart, timer changes, `/etc` edits, journald/rsyslog changes, or remote deletion;
- Incus host storage governance;
- changing Runtime Status tool JSON contracts;
- changing trade, position, notification, or Strategy Lab business semantics;
- adding a new logging stack, retry framework, error taxonomy, daemon state machine, or dependency;
- redesigning direct Futu context constructors or trade-intake restart semantics; the auth-aware trade-intake lifecycle is assigned to a separate work unit because a quote preflight cannot prove trade authentication readiness.

## Decisions

1. **Bound output at the CLI formatting boundary.** Add a `--journal-summary` mode to `./om status`. It reuses the Runtime Status envelope and existing line helpers, but emits a deliberately small diagnostic subset, summarizes warning counts, includes at most one sanitized/truncated warning or error line, and enforces a final 16 KiB UTF-8 ceiling. Default human `./om status` and structured `./om-agent runtime_status` remain unchanged.
2. **Systemd-only runtime limit.** `RuntimeMaxSec` is a systemd safety net for the observed Linux production one-shots. Launchd parity is not claimed: launchd has no direct equivalent, and adding wrapper timeouts would broaden public behavior.
3. **Limit only evidence-backed services.** Apply `RuntimeMaxSec=600` only to `options-monitor-auto-close-*.service` and `options-monitor-strategy-lab-sample.service`. Tick already owns a 600-second application timeout, Runtime Status currently completes in about three seconds, and no hang evidence exists for projection verification; all receive negative regression assertions.
4. **Do not mix trade-intake lifecycle redesign into this slice.** A quote readiness probe does not prove trade authentication, and `Restart=always` would still create an infinite restart loop. Auth-aware distinct exit status plus systemd `RestartPreventExitStatus` needs its own evidence-backed work unit.
5. **Do not hide failures.** Journal summary preserves overall/freshness/service status, warning/error counts and a bounded first detail; operators can request the unchanged full structured envelope through `om-agent`.

## Implementation slices

### Slice 1 — Bounded Runtime Status journal summary

Files:

- `src/application/runtime_status_cli.py`
- `src/interfaces/cli/observability_ops.py`
- `src/application/service_deploy.py`
- closest existing Runtime Status CLI tests and `tests/test_service_deploy.py`

Changes:

1. Add `--journal-summary` to `om status`; default summary and `--json` behavior remain unchanged and mutually unambiguous.
2. Add `format_runtime_status_journal_summary(envelope, max_bytes=16384)`. Reuse existing extraction/helpers, emit no more than 20 lines, include counts plus at most one bounded warning/error detail, replace embedded newlines, and enforce the final UTF-8 byte ceiling without splitting a multibyte character.
3. Render Runtime Status systemd and launchd commands as `om status --profile-path <...> --journal-summary`; do not suppress stderr or create a new log file.
4. Test normal, many-warning, long-warning, embedded-newline, non-ASCII and error envelopes. Assert `len(text.splitlines()) <= 20` and `len(text.encode("utf-8")) <= 16384` for each. Assert default formatter remains full/unmodified.

Validation:

```bash
./.venv/bin/python -m pytest tests/test_service_deploy.py <existing-runtime-status-cli-test-path>
```

Residual risk:

- The bounded journal view intentionally omits most warning details; full detail remains available through the unchanged structured tool and default human status command.

### Slice 2 — Runtime bounds for observed stuck systemd one-shots

Files:

- `src/application/service_deploy.py`
- `tests/test_service_deploy.py`
- operator docs selected below

Changes:

1. Add optional `runtime_max_sec` to `_systemd_unit`; validate it is positive and render `RuntimeMaxSec=<seconds>` only when supplied.
2. Supply `600` only for generated `auto-close-*` services and `strategy-lab-sample.service`.
3. Add positive assertions for those services and negative assertions for tick, Runtime Status, projection verify, trade-intake, Feishu/WeChat listeners, and OpenD daemons.
4. Do not change launchd program lifecycle.

Validation:

```bash
./.venv/bin/python -m pytest tests/test_service_deploy.py
```

Residual risks:

- Futu SDK can continue logging until the 10-minute bound; this slice guarantees termination, not immediate auth classification.
- Trade-intake auth-specific restart suppression is assigned to a later dedicated work unit with a distinct exit/restart contract.
- launchd hard timeout parity is assigned to a later work unit if macOS evidence warrants.

### Slice 3 — Aggregate verification and documentation

Files:

- operator docs that describe service generation/deployment (select the narrow existing owner during implementation);
- Gateflow implementation/review artifacts under `docs/gateflow/` and `docs/reviews/`.

Validation:

```bash
./.venv/bin/python -m pytest \
  tests/test_service_deploy.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py
./.venv/bin/python -m compileall -q src domain
```

Additionally render a systemd bundle in a temporary directory and assert:

- Runtime Status `ExecStart` uses `om status --journal-summary`;
- only auto-close and Strategy Lab sample contain `RuntimeMaxSec=600`;
- no generated unit is applied to `/etc`.

## Review and commit sequence

1. Run `planreview` against this artifact; fix all accepted findings and re-review.
2. Commit accepted plan only: `gateflow: accept plan for log-stability-hardening`.
3. Implement each approved slice separately.
4. After each slice, run focused validation, `deepreview`, fix/re-review, then commit:
   - `gateflow: accept log-stability-hardening slice-1`
   - `gateflow: accept log-stability-hardening slice-2`
   - `gateflow: accept log-stability-hardening slice-3`
5. Run aggregate `deepreview`, fix/re-review, and commit accepted aggregate evidence.
6. Push and open a draft PR only after all local gates pass. Production rollout remains excluded and requires a separate CEO approval gate.

## Docs decision

Generated service behavior is operator-visible, so documentation must state:

- Runtime Status service logs compact summary while structured `om-agent` output remains available;
- only auto-close and Strategy Lab sample systemd one-shots have a 10-minute safety bound;
- production deployment/restart is not part of this code work unit.

## Residual risk classification

- Shared Incus host disk usage and host-level retention: assigned to infrastructure work unit.
- Production service rollout and post-deploy log-rate validation: requires explicit CEO decision after draft PR/release readiness.
- launchd hard timeout parity: assigned to later work unit if evidence warrants.
- trade-intake auth-aware restart suppression and unrelated direct Futu constructors: assigned to a later work unit unless new production evidence changes scope.
