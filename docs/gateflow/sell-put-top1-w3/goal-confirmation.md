# Gateflow Goal Confirmation — Sell Put Top1 W3

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w3`
- Branch: `feat/sell-put-top1-w3`
- Base: `origin/main@baa3e363`
- Product source: `docs/plans/sell-put-top1-optimization-loop-mvp-20260814.md`
- Modular sources: `docs/plans/sell-put-top1-modular-technical-implementation-plan-20260814.md`, `docs/plans/sell-put-top1-modular-implementation-control-20260814.md`

## Confirmed goal

Implement the smallest production-grade SQLite lifecycle boundary that lets one Sell Put Top1 experiment survive process restarts and enforce human authorization, account opt-in, a single forward-validation collection slot, immutable hidden-window consumption, terminal-mode competition, and deterministic terminal-artifact recovery.

W3 is state and write-authority infrastructure. It does not evaluate a hypothesis, select a research leader, read market data, capture corpus, calculate metrics, run a timer, expose CLI/Agent tools, or start a real experiment.

## Success signal

With only synthetic data and a temporary SQLite/artifact root, tests prove that:

- account opt-in defaults off and maintainer availability always has final veto;
- research and validation require separate exact-hash human confirmations;
- a validation cannot start on an overlapping 20-trading-day commitment or while another collection slot is occupied;
- the twentieth sealed decision partition atomically closes decision intake and releases the collection slot;
- completed and aborted terminal requests cannot both win for one generation;
- a crash after request commit, after file publication, or before final SQLite publication CAS recovers the same requested bytes and hashes;
- terminal intent rejects all late experiment writes, while public status reveals no hidden intermediate result and receipt remains unavailable until conclusion.

## Scope

### Included

- One compact, private SQLite schema and migration owned by `src/infrastructure/strategy_lab/experiment_store.py`.
- Current feature/experiment/auth/progress state plus append-only audit/outbox events, generation rows, and consumed hidden commitments.
- Application commands for prepare, separate authorization, research/validation start, leader lock, generation revision/seal, validation point/partition commit, abort/feature-disable termination, recovery, status, and receipt read.
- Canonical generation-terminal and aborted-experiment receipt projection with content/file hashes.
- Exact extraction of the existing deterministic JSON text renderer so requested bytes and published bytes share one owner.

### Excluded

- Corpus/recommendation-point copying, 40-day research execution, leader calculation, 20-day decision/outcome economics, outcome jobs, statistics, adoption, LLM, Prompt, CLI, Agent tools, timers, service profiles, release, deploy, runtime config, and real provider reads.
- Generic feature-flag service, repository interface, event bus, workflow engine, task queue, ORM, registry, or multi-database transaction layer.
- Corpus/validation/outcome result tables; later modules add only the tables they own.
- Any production Candidate Engine, scheduler, notification, ledger, or broker behavior change.

## Fixed boundaries

- Scope remains first-release `market=HK`, lowercase account, `strategy_family=sell_put`.
- SQLite is the single write authority. All writes use `BEGIN IMMEDIATE`; no WAL is introduced for this single-writer workload.
- Events are immutable rows. Publication completion is a new event plus a CAS update, never an update to the requested event.
- Specs and event payloads use compact canonical JSON in SQLite; no recommendation points, candidate rows, outcomes, or metric series are duplicated in W3.
- A hidden commitment is inserted only when validation actually starts. It is never deleted, so an aborted validation still consumes the full committed window; a merely proposed/locked future window does not consume dates.
- W3 models a validation point and a sealed trading-day partition separately. Multiple official points may belong to one partition; only partition seal advances the 20-day counter.
- W3 can conclude the aborted path. Normal research/validation completion payloads remain later-module policy; W3 only provides the same generic terminal projection/CAS primitive for them.

## Stop conditions

Stop and return to design/PlanReview if implementation would require:

- a new data authority beyond the single Strategy Lab SQLite store and immutable artifact files;
- hidden intermediate metrics in public status/receipt;
- a production tick dependency on the experiment store;
- a Candidate Engine, scheduler, notification, production config, service, CLI, or Agent change;
- market/provider reads or invented result policy inside the store.
