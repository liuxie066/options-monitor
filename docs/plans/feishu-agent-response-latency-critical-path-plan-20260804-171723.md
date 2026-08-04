# Feishu Agent Response Latency — Critical-Path Fix Plan

## Summary

The production latency is dominated by an optional conversation-memory LLM call that runs before the durable Copilot run starts. This work unit removes model-driven memory compaction from the online request path. Existing structured memory remains readable and injectable, while recent messages continue to provide bounded context.

Success has two independent gates:

- root-cause gate: no memory-compaction model request occurs before the main run, and `inbound_command_audit.created_at -> copilot_runs.started_at` has P95 below 1 second and maximum below 2 seconds;
- user SLO: ordinary Feishu requests with one model turn and no tools have end-to-end P95 at or below 8 seconds.

Failure of the user SLO does not invalidate the critical-path fix when the root-cause gate passes; it is routed to main-model or transport diagnosis.

## Implementation Changes

- Replace `prepare_contract_with_memory(..., model_runner=...)` with a read-only memory preparation function that loads existing `memory_json` and injects its pinned state and recent episodes. It must not accept or call a model runner.
- Remove the online compaction trigger, compaction prompt/parser, and now-unused normalization helpers. Existing `memory_json`, raw turns, recent-message limits, and stored episodes are preserved without migration or rewrite.
- Keep the current request ordering: prepare the contract from existing memory, start the normal Copilot run, record the completed turn, and deliver through the existing Feishu reply outbox.
- Update the Copilot design document to state that online LLM memory compaction is disabled; structured memory may remain stale and canonical tools remain authoritative for current facts. Remove `memory_compact` from the active concurrency contract.
- Do not add a worker, queue, job table, turn sequence, retry state machine, configuration key, public command, runtime-status field, Feishu protocol change, or WeChat behavior.

Primary implementation ownership remains in conversation-memory preparation and the local Copilot harness. Feishu WS, reply delivery, model-provider settings, and session storage schemas are outside this work unit unless a focused regression test exposes an existing compatibility defect.

## Tests and Acceptance

- Replace the old automatic-compaction tests with tests proving that a session with more than eight uncompacted turns makes exactly one model request: the normal Agent request.
- Verify that valid existing pinned state and episodes are injected immediately before the current user message, and that missing or malformed stored memory fails open without changing the contract or calling a model.
- Verify that raw turns and `memory_json` are unchanged by request preparation and that normal turn recording still occurs after a successful answer.
- Run the focused conversation-memory, Copilot harness, inbound assistant, and Feishu WS tests, followed by the full test suite.
- After a separately authorized production upgrade, observe at least 20 naturally occurring one-turn/no-tool Feishu requests. Correlate audit and run rows through `copilot_runs.request_id = inbound_command_audit.command_id`; calculate the root-cause gate from those durable timestamps and the user SLO through reply-outbox delivery plus the existing Feishu total-duration log.
- Do not generate test messages, restart services, clear sessions, or rewrite production memory as part of diagnosis or passive acceptance.

## Delivery and Rollback

- Implement from a clean worktree based on current `origin/main`; preserve the unrelated dirty Futu branch.
- Source validation/merge, release publication, and production upgrade remain separate authorization boundaries.
- This work unit has no schema or business-data migration. Rollback is application-version rollback only; existing session memory remains compatible in both directions.

## Deferred Work and Assumptions

- Long-term memory will stop advancing after this change. This is accepted because production memory is already stale, recent context remains bounded, and the design defines memory as non-authoritative context.
- Reintroducing automatic compaction requires a separate plan only if an offline follow-up evaluation demonstrates material context loss beyond the recent-message window.
- That future plan must first prove foreground isolation: concurrent background compaction may increase ordinary-request P95 by no more than 500 ms. It must also replace the sliding-array count cursor with a stable identity before compaction can write again.
- Current production continues using DeepSeek V4 Flash. This work unit does not change thinking mode, provider retry settings, prompts for the main Agent, or Feishu delivery behavior.
