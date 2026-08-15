# Gateflow Goal Confirmation — Sell Put Top1 HK Terminal Fee Contract

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-hk-terminal-fee-contract`
- Branch: `feat/sell-put-top1-hk-terminal-fee-contract`
- Base: `origin/main@8528de6b59f89b815c9b481a69bfa6055333b93a`
- Design documents:
  - `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
  - `docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`
  - `docs/plans/sell-put-top1-modular-implementation-control-20260814.md`
  - `docs/performance/sell-put-top1-capability-preflight-20260814.md`
- Confirmation: user confirmed this independent prerequisite work unit on 2026-08-15.
- Artifact path: `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/goal-confirmation.md`

## Goal and motivation

Lock one versioned, deterministic HK option terminal-fee source contract before W1B implements experiment economics. Correct the existing production projections that currently treat an incomplete HK assignment stock-fee estimate as usable net economics.

## Success signals

- One domain calculator covers HK assignment, exercise, and expired-worthless terminal fees from the official Futu schedule.
- Assignment and exercise reuse the existing HK stock settlement arithmetic; no second fee engine or duplicated formula is introduced.
- Actual broker fee evidence remains authoritative.
- Without an explicit account fee-plan binding, the calculator returns `amount=None`, consumers remain incomplete, and net PnL/efficiency is not produced.
- Invalid numeric inputs cannot be silently truncated or converted into a plausible fee.
- Focused and adjacent regressions, Ruff, dependency checks, and Kimi DeepReview pass with no unresolved finding.

## Scope boundary

### Included

- Versioned pure HK terminal-fee calculation in `domain/domain/fee_calc.py`.
- Assignment/expired-worthless consumption and fail-closed net-output behavior in the two existing domain projections.
- Focused tests and the existing preflight evidence correction.

### Excluded

- Fetching or persisting the real `lx` fee-plan receipt.
- Futu/OpenD calls, order-fee intake, config, SQLite, CLI, Agent tools, services, notifications, trades, ledger writes, release, deployment, or a real experiment.
- US terminal-fee support and end-to-end exercise event projection.
- W1B ExperimentSpec, economics, statistics, or any later Strategy Lab module.

## Readiness decision

This work unit can lock the source fee contract required by W1B. It does not make `W0R` or a real pilot green: `lx` still lacks an auditable `commission_free/platform_fee/fee_plan_ref` receipt, and other provider-capability gaps remain.

## Inherited draft

The seven fee-related files were moved into this isolated worktree from an uncommitted draft and retained in a named stash. They are review input, not accepted implementation. No unrelated root-worktree change is included.

## Blocking open questions

None.

## Decision

`goal-confirmation-pass`; next gate: `plan`.
