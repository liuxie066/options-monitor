# Futu quote/broker capability boundary — implementation verification

- Date: 2026-08-04
- Branch: `feat/futu-quote-broker-boundary`
- Plan: `docs/plans/futu-quote-broker-capability-boundary-plan-20260804.md` revision 2
- Scope: source, tests, generated dependency graph, and operator documentation only

## Read-only P0 evidence

- Production services were read through systemd without restart or configuration mutation.
- `options-monitor-opend-lx.service` was active with cgroup PIDs `12550` and `12562`; `127.0.0.1:11111` was owned by FutuOpenD PID `12562`.
- `options-monitor-opend-sy.service` was active with cgroup PIDs `12551` and `12568`; `127.0.0.1:11112` was owned by FutuOpenD PID `12568`.
- The two broker endpoints therefore mapped to distinct OpenD processes at observation time. The shared quote endpoint may coincide with the lx broker endpoint under the approved initial topology.
- Current host IO pressure was zero over the sampled PSI windows. This is not comparable to the original incident window and is not evidence that the reclaim/refault incident is closed.

## Source verification

- Local US/HK snapshots resolve one canonical quote endpoint at `127.0.0.1:11111`.
- The additive `futu_routing_audit.v1` passes for those snapshots and emits only masked account IDs. It reports the expected sole-Futu legacy projection warning.
- The public `om config validate --related-config-path` route could not be demonstrated against the checked-in snapshots because their existing generated-source freshness hashes are stale. No runtime config was rebuilt or changed to bypass that independent readiness gate.
- `python3.12 scripts/generate_dependency_graph.py --check`: pass, 579 production modules, zero cycles.
- `python3.12 -m ruff check .`: pass.
- `git diff --check`: pass.
- Full pytest suite: `4290 passed, 10 skipped`; six pre-existing deprecation warnings.

## Boundary confirmation

- No `config.yaml`, generated runtime config, secret, `VERSION`, changelog, service, OpenD, Incus, broker data, Feishu data, or notification state was modified.
- No release, remote upgrade, service restart, or production apply was performed.
- Resource relief still requires a separately authorized matched-window production experiment.
