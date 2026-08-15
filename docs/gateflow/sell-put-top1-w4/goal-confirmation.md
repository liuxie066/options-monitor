# Gateflow Goal Confirmation — Sell Put Top1 W4

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w4`
- Confirmed by user: 2026-08-15
- Base: `origin/main@baa681628f0b62a43f48edd51e2e5fb4f4fafa8e`
- Artifact path: `docs/gateflow/sell-put-top1-w4/goal-confirmation.md`
- Decision: accepted

## Goal and motivation

Build the minimal durable Corpus boundary needed by the Sell Put Top1 loop. W4 must preserve every official scheduled recommendation denominator and the accepted-only ranking projection long enough for a later 40-day research run, even after normal `output_runs` retention removes the producer source.

## Success signals

- A write-once `corpus_day_expectation.v1` is sealed before the first official target and contains the complete target/point-ID denominator.
- Effective feature off means no new Corpus artifact or SQLite row.
- A clean M2 point produces one minimal immutable W1A ranking projection; same facts are idempotent and disagreement is terminal conflict without overwrite.
- Late expectation, schedule drift, point/snapshot/projection gap, or conflict makes the affected day ineligible.
- Dataset freeze checks exactly the latest caller-certified mature 40-date calendar window; it never skips a bad date or searches an older window.
- A frozen projection still reranks exactly after the source `output_runs` tree is gone.

## Scope boundary

W4 owns expectation sealing, point capture, compact Corpus indexes/status, and deterministic 40-day dataset freezing. Trading-calendar order and the latest mature date are explicit caller-provided facts bound by hashes; W4 performs no provider lookup.

The following remain later work units:

- W5: 40-day counterfactual research, historical close/fee receipts, statistics, and leader selection.
- W6: independent 20-day hidden validation, fill observation, and expiry outcomes.
- W7: timer/service orchestration and installed readiness.
- W8: LLM prompt/advisory behavior.

No production config, CLI, service, Candidate Engine, notification, broker, experiment transition, release, deploy, or real pilot is changed in W4.

## Direct code evidence

- M2 already publishes and validates `recommendation_point.v1` in `src/application/recommendation_point.py`.
- M1A already builds, validates, and reranks `sell_put_ranking_projection.v1` in `src/application/strategy_lab/top1/ranking.py` without reading its source artifact.
- M3 already owns feature intent and the only Strategy Lab SQLite write authority in `src/infrastructure/strategy_lab/experiment_store.py`, but schema v1 has no Corpus tables.
- `src/application/scan_scheduler.py` already owns official scan-target calculation; W4 must reuse it rather than copy schedule rules.
- `publish_exact_text()` already supplies private, write-once exact-byte artifact publication.
- No `src/application/strategy_lab/top1/corpus.py` exists on the confirmed base.

## Parsimony decision

Reuse the existing scheduler, point/snapshot validators, projection builder, exact-byte publisher, and single SQLite store. Do not add a repository interface, event bus, workflow engine, outbox table, provider abstraction, generic dataset framework, or separate readiness module in W4.

## Blocking open questions

None. The user confirmed both this boundary and creation of the isolated `feat/sell-put-top1-w4` branch.

## Next gate

`plan`
