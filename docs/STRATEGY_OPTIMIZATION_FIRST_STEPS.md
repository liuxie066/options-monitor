# Strategy Optimization First Steps

This document is retained as a compatibility pointer for earlier notes. The current product and technical design is now fixed in [STRATEGY_LAB_DESIGN.md](STRATEGY_LAB_DESIGN.md).

Current boundary:

```text
Research = evidence infrastructure
Shadow Replay = counterfactual replay engine
Strategy Lab = strategy evolution product surface
```

The earlier evidence-first stages are now treated as the foundation under Strategy Lab:

- runtime config authority remains a read-only diagnostics concern.
- candidate filter / rank trace remains the source for accepted and rejected universe evidence.
- Shadow Replay remains the offline dataset, mark path, outcome, readiness, and candidate-impact engine.
- Strategy Lab is the product layer above that foundation. Its implemented surfaces include update, read-only decision-instance readiness, experiment, advisory proposal, and llm-context: evidence lifecycle data-plan, controlled hypotheses, candidate-impact evaluation, Combo Yield group-level observed-universe experiment, scorecards, dry-run proposal artifacts, and redacted local context for LLM-assisted review. Sell Put, Covered Call, and Combo Yield are separate strategy domains under one workflow; Combo Yield remains group-level and does not use the single-leg optimizer.

Strategy Lab still cannot mutate runtime config, trade state, notification behavior, Feishu, or broker-facing state. See [STRATEGY_LAB_DESIGN.md](STRATEGY_LAB_DESIGN.md) for the fixed PRD, architecture, implemented readiness / experiment CLI, target CLI, module plan, gates, and MVP acceptance criteria.
