# Pi Agent Core Integration Contract

Status: the Assistant channel and shared Host call path use Pi Agent Core, the
legacy Python Agent runtime is removed, and the mixed Python/Node release gates
are active. Context-bounded tool loading and evidence admission are part of the
released runtime.
The current repository version is owned by `VERSION`, and published release
history is owned by `CHANGELOG.md`. Deployment and configured-provider canary
status are environment facts and must be checked through the controlled runtime
surfaces; Git history alone does not prove an environment was upgraded.

Last upstream verification: 2026-08-19. The pinned baseline is
`@earendil-works/pi-agent-core@0.84.2`, `@earendil-works/pi-ai@0.84.2`, and
`@earendil-works/pi-session-backend-sqlite-node@0.84.2`, which require Node.js
`>=22.19.0`.

The context-bounded tool-loading and evidence-admission contract in section 13
is part of the current runtime contract. Runtime enablement still depends on the
deployed version and configured provider.

This document is the implementation authority for OM's Pi Agent Core runtime.
[OM_COPILOT_V2_DESIGN.md](OM_COPILOT_V2_DESIGN.md) continues to own the product
and Scene v4 contract; it no longer describes a separate legacy model/tool
runtime.

## 1. Product Requirement

OM is evolving into an investment product whose primary interaction surface is
an Agent. Pi Agent Core replaces generic Agent infrastructure; it does not
replace OM's investment logic, financial facts, permissions, or deterministic
Control workflows.

### 1.1 User scenarios

There is one conversational Agent and two user-visible scenario families:

1. **Investment question and answer**: the user asks natural-language
   questions about positions, exposure, yield, candidates, performance,
   notifications, or missing data. The Agent selects canonical read tools and
   answers from their evidence.
2. **Project inspection and control**: the user asks OM to inspect runtime,
   configuration, jobs, or project state. A requested mutation may create a
   deterministic preview, but requires explicit confirmation before OM applies
   it and returns a readback receipt.

These are evaluation scenarios, not Scene names, routers, or hard-coded intent
branches. All non-Control text still enters the single `om_chat` Scene.

### 1.2 Product entrypoints

- `./om assistant handle` is the product entry for local and remote messages.
- `./om copilot run` and `./om copilot eval` remain diagnostic and evaluation
  surfaces. They are not a second product assistant.
- No TUI or Web UI is included in this integration.

### 1.3 Success criteria

The integration succeeds when:

- Pi `Agent` is the only generic model/tool loop used by free-form Copilot;
- Pi Session owns new conversational transcripts and context compaction;
- OM still owns sender and account scope, canonical tools, financial truth,
  Control, result admission, run governance, audit, and reply delivery;
- all five existing OM model profiles remain supported;
- same-user continuity inside one trusted OM key/path scope, cross-config
  separation, and cross-user isolation are proven without persisting plaintext
  paths in Pi memory;
- cancellation/admission has one durable winner, concurrent evidence is not
  lost, and bounded read-only recovery remains available;
- production has no hidden fallback to the retired OM Engine;
- the previous release remains a complete rollback unit.

### 1.4 Non-goals

The integration does not add:

- TUI, Web UI, remote Pi server, or WebSocket transport;
- Pi coding-agent bash, filesystem, patching, or shell tools;
- multiple Agents, subagents, planner roles, or business-specific Scenes;
- cross-user learning, autonomous prompt mutation, or model-generated policy;
- a new strategy-lab, backtest, or experiment engine; those remain separate OM
  capabilities that may later be exposed as canonical tools;
- direct model access to mutation tools;
- in-place recovery of an interrupted Pi Agent loop;
- production dual-run or automatic fallback to the legacy Engine.

## 2. Upstream Capability Decision

Use the stable `Agent` API, not `AgentHarness`.

Pi `Agent` currently provides the stateful transcript, event stream, model/tool
iteration, sequential tool execution, `transformContext`, hooks, queues, and
`abort()`. The SQLite Session backend is a separate package.

Do not base production execution on `AgentHarness` until its implementation is
verified again. In the pinned source, `prompt`, `compact`, `resume`, `abort`,
`steer`, `followUp`, and `watch` return `HarnessNotImplemented`.

Upstream references:

- [Agent README](https://github.com/earendil-works/pi/blob/v0.84.2/packages/agent/README.md)
- [Agent implementation](https://github.com/earendil-works/pi/blob/v0.84.2/packages/agent/src/agent.ts)
- [AgentHarness implementation](https://github.com/earendil-works/pi/blob/v0.84.2/packages/agent/src/harness/agent-harness.ts)
- [SQLite Session backend](https://github.com/earendil-works/pi/tree/v0.84.2/packages/session-backends/sqlite-node)

Any dependency upgrade requires rerunning the Pi contract tests before changing
the pinned package versions.

## 3. Architecture And Ownership

```text
Feishu / WeChat / CLI
        |
        v
OM Assistant Inbound
identity, account scope, message idempotency, explicit Control
        |
        v
OM Copilot Service + Host
contract, Scene, leases, run record, cancellation, audit, admission, outbox
        |
        | om-pi-ipc.v1 over JSONL stdio
        v
per-request Node Pi Runtime
Pi Agent + selected model + Pi Session/context
        |
        | tool.call / tool.result
        v
existing Python Copilot tool adapter
canonical OM tool registry, execution, redaction, compact observation
```

There is no new Pi service layer. Python starts one Node child for one Agent
run. A measured startup or throughput problem is required before introducing a
long-lived process or pool.

### 3.1 Pi owns

- provider request and streamed model response;
- generic Agent iteration and tool-call lifecycle;
- in-run transcript state;
- steering, follow-up, abort, and lifecycle events;
- new conversation transcript persistence;
- token-aware context transformation and compaction.

### 3.2 OM owns

- authenticated channel, sender, conversation, account, market, and config
  scope;
- the `om_chat` Scene, ordered prompt fragments, runtime context slots, limits,
  and fingerprints;
- the canonical tool registry, JSON schemas, defaults, output contracts,
  execution, and redaction;
- deterministic Control preview, pending operation, confirmation, apply,
  idempotency, readback, and receipt;
- financial fact authority and final-result admission;
- session exclusion, concurrency lanes, run/cancel/recovery metadata, events,
  audit, and reply outbox.

The model cannot override an OM-owned fact or policy. Pi tool definitions are a
mechanical projection of OM definitions, never a second catalog.

## 4. Process Boundary

Python launches the child without a shell:

```text
[node_executable, <repo>/agent-runtime/main.ts]
```

The child handles exactly one `run.start` and exits after one terminal message.
stdin and stdout are UTF-8 JSONL. stdout contains protocol messages only;
diagnostic logs go to stderr.

Python passes a new, allowlisted environment rather than copying the complete
parent environment. Model credentials are resolved by the existing OM secret
boundary and exposed to the child only as `OM_PI_MODEL_API_KEY`. Credentials,
original credential names, and secret values never appear in JSONL, Session,
events, or returned errors.

The minimum environment is:

- `PATH`, locale, timezone, and certificate/proxy variables required by the
  selected provider;
- `OM_PI_MODEL_API_KEY` when the selected provider requires one;
- `OM_PI_SESSION_DB` when persistence is enabled.

The Scene timeout remains the hard wall. On expiry before an admission decision,
Python sends `run.cancel`, waits at most two seconds, then terminates the child.
After a decision write, cleanup sends no contradictory cancel and follows the
terminal/unknown-commit rules below. Raw stderr is never copied to a user
response.

## 5. `om-pi-ipc.v1` Contract

### 5.1 Common envelope

Every line is one JSON object with this closed shape:

```json
{
  "protocol": "om-pi-ipc.v1",
  "type": "run.start",
  "request_id": "req_123",
  "run_id": "run_123",
  "seq": 1,
  "payload": {}
}
```

Rules:

- all six fields are required and no additional top-level fields are accepted;
- `request_id`, `run_id`, and `type` must be non-empty strings;
- `seq` is a positive integer, starts at `1`, and increases by one independently
  in each direction;
- every message after `run.start` must match its `request_id` and `run_id`;
- malformed JSON, an unknown type, a sequence gap or duplicate, or a mismatched
  identity is a terminal `PROTOCOL_ERROR`;
- the first Python message must be `run.start`; the first Node message must be
  `run.accepted` or `run.error`;
- each side emits at most one terminal message.

### 5.2 Python to Node

#### `run.start`

Descriptions, schemas, output contracts, and the catalog hash are abbreviated
below; the top-level fields and limit keys are complete.

```json
{
  "execution_environment": "local",
  "session_id": null,
  "system_prompt": "compiled static om_chat prompt",
  "runtime_context": [
    {"role": "system", "content": "authoritative current context"}
  ],
  "user_message": "检查当前运行状态",
  "model": {
    "provider": "deepseek",
    "api_kind": "openai-completions",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "timeout_seconds": 90,
    "context_window_tokens": 24000,
    "max_output_tokens": 2048,
    "max_attempts": 2
  },
  "tools": [
    {
      "name": "tool_directory",
      "description": "activate an exact bounded business tool set",
      "input_schema": {"type": "object", "properties": {}}
    },
    {
      "name": "submit_answer",
      "description": "submit the admitted final answer",
      "input_schema": {"type": "object", "properties": {}}
    }
  ],
  "tool_loading_mode": "directory",
  "tool_catalog": [
    {
      "name": "runtime_status",
      "toolset": "diagnostics",
      "purpose": "读取当前运行健康状态与关键运行摘要。",
      "access": "read",
      "evidence_type": "diagnostic"
    }
  ],
  "catalog_hash": "sha256:...",
  "catalog_snapshot": [
    {
      "name": "runtime_status",
      "toolset": "diagnostics",
      "description": "read current runtime status",
      "input_schema": {"type": "object", "properties": {}},
      "output_contract": {},
      "access": "read",
      "purpose": "读取当前运行健康状态与关键运行摘要。",
      "evidence_type": "diagnostic"
    }
  ],
  "limits": {
    "timeout_seconds": 180,
    "max_iterations": 16,
    "max_tool_calls": 12,
    "max_consecutive_failed_tool_batches": 2,
    "final_answer_reserve_seconds": 20
  },
  "recovered_observations": [],
  "debug": null
}
```

Validation rules:

- `execution_environment` is `local`, `eval`, or `channel`;
- `session_id` is `null` for a transient run or an OM-derived identifier;
- `system_prompt` and `user_message` are non-empty strings;
- `runtime_context` accepts only `{role: "system", content: string}` and is
  never persisted as conversation history;
- provider, API kind, model, and the operator-declared safe context window must
  already have passed OM configuration validation; `api_kind` is
  `openai-responses` or `openai-completions`;
- `tools` accepts only the Host-projected initial set; input schemas must be
  JSON objects and tool names must be unique;
- `tool_loading_mode` is `eager` or `directory`; `tool_catalog` and
  `catalog_snapshot` have the same sorted business-tool names, and
  `catalog_hash` binds their frozen content;
- all five runtime limits are positive integers; the absolute context authority
  is `model.context_window_tokens`;
- `recovered_observations` contains previously sanitized, successful read-only
  observations from OM Host recovery;
- `debug` must be `null` outside `eval`. Eval requires `delay_ms` and exactly one
  of `fixture_response` or `fixture_turns`; optional history, persistence-delay,
  and compaction fixtures remain deterministic and network-free.

#### `tool.result`

```json
{
  "call_id": "call_123",
  "tool_name": "runtime_status",
  "observation": {
    "tool_name": "runtime_status",
    "ok": true,
    "status": "complete",
    "summary": "runtime_status returned read-only data"
  }
}
```

The result must match exactly one outstanding `tool.call`. The observation is
the output of `copilot.tools.compact_observation()`, not the raw tool response.
Duplicate, unknown, or mismatched call IDs are terminal protocol errors.

#### `run.cancel`

```json
{"reason": "host_cancel_requested"}
```

The Node runtime calls `Agent.abort()`. Cancellation is valid while the run is
active, waiting for a tool result, or waiting for result admission. It never
confirms or cancels a pending Control operation.

#### `run.commit` and `run.discard`

Both have the closed empty payload `{}`. After one valid `run.proposed`, Python
sends exactly one of them unless cancellation wins first. `run.commit`
authorizes persistence of the buffered Pi turn; `run.discard` forbids it.
Neither message authorizes a Control operation or a reply delivery.

For Host-store-managed product runs, the linearization point is the private
Host SQLite admission transition, not the JSONL write.
`request_cancel()` and result admission compete to move one row from `open` to
exactly one of `cancel`, `commit`, or `discard`. Python then sends only the
message selected by that durable winner. For transient diagnostics without a
Host store, the process adapter remains the single writer and its first
complete `run.cancel`, `run.commit`, or `run.discard` write is the local
linearization point. A failed protocol write after a durable `commit` claim
retains the Host's unknown-commit handling.

### 5.3 Node to Python

#### `run.accepted`

```json
{
  "runtime": "pi-agent-core",
  "runtime_version": "0.84.2",
  "session_id": null
}
```

`session_id` must equal the value accepted from `run.start`; it is `null` only
for transient runs.

#### `agent.event`

```json
{
  "event_type": "model_turn_completed",
  "data": {
    "stop_reason": "stop",
    "attempt_count": 1,
    "model_retry_count": 0,
    "usage": {"input": 10, "output": 5, "totalTokens": 15},
    "usage_total": {"input": 10, "output": 5, "totalTokens": 15}
  }
}
```

Allowed event types are:

- `agent_start`, `turn_start`, `model_turn_completed`;
- `tool_execution_start`, `tool_execution_end`, `turn_end`, `agent_end`.

This is an OM-owned normalized event contract, not a passthrough of arbitrary
upstream Pi events. Thinking text, provider payloads, credentials, raw tool
results, text deltas, and private reasoning are prohibited. V1 has no streaming
UI consumer, so only completed model turns cross the process boundary.

`model_turn_completed.data` has exactly the five fields shown above.
`attempt_count` is the non-negative number of actual provider HTTP requests for
that logical model turn; a deterministic fixture turn uses zero.
`model_retry_count` is the cumulative sum of
`max(attempt_count - 1, 0)` across completed provider calls in the run,
including an independently committed pre-run compaction. `usage` belongs to
the current assistant message and `usage_total` is the cumulative main-turn
plus committed-compaction usage. `turn_end.data` remains exactly
`{stop_reason, usage}`. All numeric fields are finite and non-negative.

#### `tool.call`

```json
{
  "call_id": "call_123",
  "tool_name": "runtime_status",
  "arguments": {"config_key": "us"}
}
```

Python rejects any tool outside the current Host allowlist before calling the
existing payload builder and executor. V1 permits only one outstanding call
because Pi tool execution is configured as `sequential`.

#### `run.proposed`

```json
{
  "status": "answered",
  "text": "结论文本",
  "control_request": null,
  "termination_reason": "stop",
  "usage": {"input": 10, "output": 5, "totalTokens": 15}
}
```

This is not terminal and is not permission to persist or deliver. Python runs
the Host's result admission callback and answers with exactly its durable
winner: `run.cancel`, `run.commit`, or `run.discard`.

#### `run.final`

```json
{
  "status": "answered",
  "text": "结论文本",
  "control_request": null,
  "termination_reason": "stop",
  "usage": {"input": 10, "output": 5, "totalTokens": 15},
  "committed": true
}
```

`status` is `answered`, `control_requested`, or `cancelled`. Python still maps
this payload into `AppResult`; an answered or control result is delivered only
when `committed` matches the Host's prior admission decision. Cancelled results
set `committed:false` and do not use the proposal handshake.

#### `run.error`

```json
{
  "code": "MODEL_ERROR",
  "stage": "model",
  "message": "safe public summary",
  "retryable": true
}
```

Allowed Node error codes are `PROTOCOL_ERROR`, `CONFIG_ERROR`, `MODEL_ERROR`,
`SESSION_ERROR`, `TOOL_BRIDGE_ERROR`, `BUDGET_EXHAUSTED`, and `INTERNAL_ERROR`.
Python additionally creates `PI_PROCESS_TIMEOUT`, `PI_PROCESS_EXITED`, and
`PI_RUNTIME_UNAVAILABLE` when the child cannot provide a terminal envelope,
and `CANCELLED` when a cancelled child must be stopped forcibly.

### 5.4 State machine

```text
spawned
  -> run.start
  -> accepted
  -> active <-> awaiting_tool_result
  -> awaiting_admission -> run.commit | run.discard | run.cancel
  -> run.final | run.error
  -> exited
```

- `run.cancel` is accepted in `active`, `awaiting_tool_result`, and
  `awaiting_admission`;
- only `tool.result` or `run.cancel` is accepted while awaiting a tool;
- only `run.commit`, `run.discard`, or `run.cancel` is accepted while awaiting
  admission;
- for product runs, the first Host SQLite compare-and-set from private state
  `open` wins; for transient runs, the first complete adapter write wins; no
  second message is sent for that state;
- Node stops accepting input after a terminal message;
- Python treats EOF before a terminal message as `PI_PROCESS_EXITED`;
- a valid terminal envelope remains authoritative when the child is silent but
  does not exit during cleanup: `run.final` exits zero, while a flushed
  `run.error` may exit non-zero; neither is replaced by
  `PI_PROCESS_EXITED` merely because cleanup must kill the child;
- Python waits for child exit before returning and treats any stdout record
  after a terminal message as `PROTOCOL_ERROR`.

## 6. Memory And Context

### 6.1 Session identity

Channel sessions must include the sender. The existing
`channel:conversation_id` key can mix users in one group conversation and must
not be reused for Pi memory.

The new identifier is:

```text
"om_" + sha256(
  "om-pi-session-v1\0" + channel + "\0" + sender_id + "\0"
  + conversation_id + "\0" + authority_scope
).hexdigest()
```

OM resolves the authority before acquiring the Host lease or opening Pi
Session storage. Exactly one public data-scope input is accepted:

- `config_key`: normalize and validate it through the existing runtime-config
  rules; `authority_scope` is `key:<normalized-key>`;
- `config_path`: resolve it with the existing
  `resolve_runtime_config_path(config_path=...)` boundary, canonicalize the
  resolved path, pass that canonical path only as Host-owned fixed tool input,
  and set `authority_scope` to `path:` plus the SHA-256 of the canonical path;
- both key and path: fail closed with `CONFIG_ERROR` rather than relying on the
  resolver's path precedence;
- neither: the existing Assistant runtime must supply its configured default
  key before Copilot handoff; if it cannot, the channel request fails before
  lease acquisition and Node spawn.

Canonical aliases and symlinks to the same resolved path produce the same
scope; different canonical paths remain isolated. Raw paths never enter the Pi
Session ID, transcript, model-visible runtime context, or Host lease key. The
model cannot choose or modify either scope input. In V1 one resolved config is
the account/market memory partition. If a future config can switch among
independently authorized accounts, the resolved account identity must replace
this value before that behavior ships.

Local diagnostic sessions hash
`"local\0" + authority_scope + "\0" + explicit_session_key`. A local run
without an explicit session key is transient.

### 6.2 Storage ownership

- Channel default: `<runtime-root>/output_shared/state/pi_sessions.sqlite3`.
- If a caller supplies a Host database, the Pi database is
  `host_db.with_name("pi_sessions.sqlite3")`.
- Pi SQLite stores transcript and compaction entries only.
- `CopilotHostStore` keeps run state, events, cancellation, recovery metadata,
  lanes, audit, and reply outbox.
- Existing `copilot_sessions.messages_json`, `turns_json`, and `memory_json`
  become legacy read-only data after cutover. They are not dual-written or
  imported because the old channel key does not prove sender ownership.

The product must state that conversational memory starts fresh at cutover.
Historical run and inbound audit records remain available to operators.

### 6.3 Durable turn commit

Pi Session appends entries individually, so an OM turn needs one small commit
convention. The runtime buffers the new Pi messages until `turn_end`, appends
them in order, then appends a custom entry:

```json
{
  "customType": "om.turn.commit.v1",
  "data": {"run_id": "run_123", "kind": "turn"}
}
```

A successful pre-run compaction uses the same marker with
`kind: "compaction"`. It is an independent maintenance checkpoint over already
committed history and is written before the current prompt; it is not governed
by the later `run.commit`/`run.discard` decision for the current turn. Before
every run, Node finds the latest valid commit marker on the `main` lane and
calls `Session.moveLane("main", committed_leaf_id)`; when no marker exists, it
moves the lane to `null`. This abandons any entries written after the last
marker by a crashed child before new messages are appended. Merely truncating
context at the latest marker is insufficient because a later successful turn
could otherwise be parented on top of the old partial tail and make that tail
reachable again.

After the rewind, `findEntriesOnBranch` is called with
`{order: "oldestFirst"}` and only the resulting committed branch is passed to
`buildSessionContext()`. A persisted compaction entry is followed by its commit
marker before it can become the active branch. The custom marker is durability
metadata only and never becomes a model message.

The existing Host per-session lease remains the outer exclusion boundary. The
Pi repository writer lease is an additional storage guard. A cooperative child
exit must release it. A forcibly killed child cannot run dispose; its lease is
allowed to remain until the configured 30-second TTL and can be fenced by the
next writer only after expiry. Python never deletes a Pi writer row directly.

### 6.4 Context construction

For each run, Node constructs model context in this order:

1. compiled static `om_chat` system prompt;
2. current OM runtime context and pending-Control snapshot;
3. optional recovered read-only observations;
4. committed Pi Session branch context;
5. current user message.

Pi exposes one `Context.systemPrompt`, not system-role transcript messages.
Node therefore builds one effective system prompt by concatenating item 1 with
bounded, tagged blocks for items 2 and 3. It then loads item 4 into
`Agent.state.messages` and calls `Agent.prompt()` with item 5. The effective
system prompt is rebuilt every run and never persisted. Current tool facts and
pending operations always outrank remembered conversation text.

Use Pi's exported `estimateContextTokens`, `estimateTokens`, `shouldCompact`,
`prepareCompaction`, and `compact` functions. The adapter decides when to call
them and persists the returned compaction entry; it must not implement another
summarization algorithm. OM supplies fixed financial-conversation compaction
instructions because Pi's default summary format is coding-oriented: preserve
the user's goals and preferences, timestamp historical claims, preserve
unresolved questions and Control state references, never promote remembered
facts into current financial facts, and omit file-operation guidance.

Compaction failure leaves the last commit marker active. If the unmodified
context still fits, the run continues without a new checkpoint; if it does not
fit, the run fails explicitly with `SESSION_ERROR` and does not call the main
model with a truncated or guessed context.

## 7. Tool And Control Bridge

### 7.1 Tool projection

Python continues to build tool descriptions through
`src/application/copilot/tools.py`. Only `name`, `description`, and
`input_schema` cross to Pi. Pi tools are created mechanically from those JSON
schemas and use `executionMode: "sequential"`.

No Pi built-in tools are enabled.

### 7.2 Tool execution

For each `tool.call`, Python performs the existing sequence:

```text
Host allowlist
-> build_tool_payload()
-> call_read_tool()
-> compact_observation()
-> tool.result
```

Tool argument errors remain recoverable observations so the Agent may repair
them. Policy violations, unknown tools, call-ID mismatches, and cancellation
fail closed.

### 7.3 Control preview

`request_control_preview` remains generated from the current deterministic
capability catalog. It is the only model-visible non-read surface and returns a
validated `control_request`; it never applies a write.

The existing Assistant path remains:

```text
Pi control request
-> inbound_service validation
-> deterministic preview and pending operation
-> explicit user confirm or cancel
-> deterministic apply
-> readback receipt
```

Control confirmation, cancellation, apply, and write tools never enter Pi.

## 8. Provider Mapping

OM configuration remains the public source. Python validates it and sends a
secret-free model description to Node.

| OM provider | `api_kind` | Pi API/provider behavior | Existing default base URL |
|---|---|---|---|
| `openai` | `openai-responses` | OpenAI Responses | `https://api.openai.com/v1` after normalization |
| `deepseek` | `openai-completions` | OpenAI-compatible chat completions | `https://api.deepseek.com` |
| `kimi` | `openai-completions` | Moonshot OpenAI-compatible chat completions | `https://api.moonshot.ai/v1` |
| `kimi-code` | `openai-completions` | OM custom OpenAI-compatible chat completions mapping | `https://api.kimi.com/coding/v1` |
| `ollama` | `openai-completions` | OpenAI-compatible chat completions without required key | `http://127.0.0.1:11434/v1` |

Preserve `model`, `base_url`, `timeout_seconds`, `context_window_tokens`, and
`max_output_tokens`. A directly supplied legacy runtime `max_attempts` remains
accepted after strict validation from 1 through 3; when absent it remains 2.
The current authoring model profiles do not expose that internal retry value,
so there is no retry CLI or profile field. Pi's built-in
`kimiCodingProvider()` uses Anthropic Messages at
`https://api.kimi.com/coding`, which is not OM's current `kimi-code` contract;
using it would be an unrelated breaking change. Do not load every Pi builtin
model or provider. Build one selected model/provider mapping per run and keep
OM's five public profiles stable. Any provider-mapping change requires
fixture-backed payload/tool contract coverage for all five profiles. Live
provider canaries are separate, explicitly authorized acceptance work.


## 9. Current Owner Map

| Responsibility | Canonical owner |
|---|---|
| Product facade and Scene contract | `src/application/copilot/service.py`, `src/application/copilot/scene.py`, and `src/application/copilot/om_chat.scene.json` |
| Host run governance, cancellation, durable admission, audit, and finalization | `src/application/copilot/host.py`, `src/application/copilot/host_store.py`, `src/application/copilot/result_admission.py`, and `src/application/copilot/event_store.py` |
| Assistant channel identity, idempotency, and deterministic Control handoff | `src/application/copilot/channel_facade.py`, `src/application/assistant/inbound_service.py`, and `src/application/copilot/control_handoff.py` |
| Canonical tool metadata, projection, execution, and permissions | `src/application/agent_tool_registry.py`, `src/application/agent_tools/`, and `src/application/copilot/tools.py` |
| Provider and model-profile validation | `src/application/copilot/model_config.py` |
| Python/Node process boundary | `src/infrastructure/pi_agent_process.py` and `agent-runtime/main.ts` |
| Runtime dependency lock | `agent-runtime/package.json` and `agent-runtime/package-lock.json` |

The retired Python Engine, model client, conversation memory, and generic Agent
modules do not remain as fallback owners. New behavior belongs at one of the
boundaries above; do not add a second tool registry, transcript store, model
loop, or Control path.

## 10. Operations And Rollback

- Installation and upgrade checks must validate the pinned Node runtime and the
  locked `agent-runtime` dependencies.
- Source version and release history are owned by `VERSION` and `CHANGELOG.md`;
  deployment and configured-provider status must be verified from the target
  runtime.
- Provider canaries, release publication, and environment upgrade are separate,
  explicitly authorized operations.
- Cutover is atomic. There is no production dual-run or automatic fallback to
  the retired Python Engine.
- Rollback restores the previous complete release, including its Python and
  Node artifacts; it does not mix runtime components across releases.

## 11. Final Acceptance Matrix

| Area | Required evidence |
|---|---|
| Entrypoint | free text through `./om assistant handle` reaches the single Pi-backed `om_chat` path |
| Investment Q&A | current facts come only from canonical tools and missing data stays explicit |
| Inspection | runtime/config/job questions use the same Agent and read tools |
| Control | model can request preview only; confirm/apply/readback remain deterministic |
| Memory | same sender/conversation/canonical key-or-path scope continues; different scopes or senders cannot share memory, and plaintext paths are absent |
| Context | effective input capacity is `model.context_window_tokens - model.max_output_tokens`; the Runtime owns the 70% compact trigger, 75% hard gate, and 50% post-compact target; pre-run compaction is independently durable and current-turn admission preserves complete message/tool groups |
| Tools | model-visible list equals the Host projection; no Pi builtin write/shell/file tool exists; one lock preserves every concurrent lifecycle/tool event and metric; abandoned reads cannot exceed one worker or write late Host events |
| Providers | OpenAI, DeepSeek, Kimi, Kimi Code, and Ollama contract tests pass |
| Cancellation | two independent Host connections racing cancel against commit/discard accept exactly one durable winner, reflected consistently in CLI, Session, Host result, and outbox |
| Recovery | only bounded successful read observations are reused; no Control action is replayed; forced-kill Pi lease is busy only until fenced TTL takeover |
| Audit | prompt/tool fingerprints, normalized events, metrics, and final state remain available without secrets or reasoning text |
| Delivery | a validated terminal survives child cleanup and final channel reply remains idempotent through the existing outbox |
| Operations | install, upgrade, verification, and release-level rollback are proven |
| Cleanup | legacy Engine/model/memory modules and their callers are removed; no production fallback remains |

## 12. Decisions That Are Closed

- Keep Python for OM business and governance; add Node only for Pi runtime.
- Use JSONL stdio and one child per request in V1.
- Use Pi `Agent`; do not use the incomplete `AgentHarness`.
- Use one `om_chat` Scene and one canonical OM tool registry.
- Keep tool execution sequential in V1.
- Store new transcript data in Pi SQLite and start memory fresh at cutover.
- Partition Host leases and Pi Sessions by authenticated identity plus trusted
  normalized-key or canonical-path-hash authority scope; pass canonical paths
  only as Host-only fixed tool input.
- Require an operator-declared safe context window; never guess it from a model
  name.
- Use one process-wide read-worker slot and accept bounded retryable busy after
  forced process/Session failure rather than adding a worker pool or lease
  deletion path.
- Use one run-local lock for all Host event/cache mutations; do not add a new
  event framework until measured contention requires it.
- Use one private Host SQLite admission CAS as the product-run winner; JSONL is
  delivery, not the durable cancel/admission authority.
- Commit pre-run compaction independently; current-turn admission owns only the
  new user/assistant/tool suffix.
- Keep current Host run/audit/outbox governance.
- Preserve all five existing provider profiles.
- Do not add TUI, Web, multi-agent, cross-user learning, or direct mutation
  tools.
- Cut over atomically and roll back by release, never by hidden legacy fallback.

No product or architecture decision remains open for the base runtime.
Implementation may correct a verified upstream signature or repository fact, but changing an
ownership boundary, product scope, protocol guarantee, or cutover strategy
requires updating this document before code.

## 13. S8 Context-Bounded Tool Loading And Evidence Admission

Status: implemented source contract. Release, deployment, and provider
enablement remain environment facts and must be checked independently.

### 13.1 Problem statement and production evidence

The current Host sends every scene-visible tool description and input schema to
Pi at request start. Fixed system instructions and the full canonical tool set
therefore consume input capacity even when a request needs no tool or only one
tool. Large model projections can then consume the remaining capacity and
leave too little room for a complete answer.

The motivating incident was not a Feishu rendering defect. For production run
`run_6908584d4259`, the request for monthly option income produced one compact
`option_performance_report` observation of 5,435 characters and one
`analysis_catalog` observation of 96,219 characters. The provider stopped for
length after emitting one token, `我`, and the existing admission path accepted
that non-empty text as an answer. The incident exposed four separate contract
gaps:

1. every business schema was paid for before the model selected a tool;
2. a generic result projection could expose far more evidence than the answer
   needed;
3. context compaction did not own a hard, pre-provider input gate;
4. final-answer admission checked structure but not evidence coverage or
   truncation.

S8 addresses the owning boundaries rather than adding a larger prompt, a
second router model, or another result cache.

### 13.2 Goals, success criteria, and non-goals

S8 succeeds when:

- a conceptual request can be answered without loading any business schema;
- a factual request starts from a compact Host-authoritative catalog and the Pi
  main model activates only the canonical tools it needs;
- no active turn exposes more than two business toolsets or six business tools;
- tool results state their evidence coverage, freshness, and page/query scope;
- a complete claim cannot be admitted from partial or unknown-completeness
  evidence;
- eligible immutable collection owners support variable page sizes without
  duplicates inside one bounded snapshot; mutable collections fail closed or
  require narrowing instead of pretending to provide stable continuation;
- the Node Pi Runtime owns the final assembled-context calculation, automatic
  trigger, and pre-provider hard gate, while Pi Core supplies the compaction
  primitives;
- every ordinary answer passes one structured evidence-admission tool before
  the existing Host final-result admission commits it;
- the fixed prompt plus resident schemas do not exceed the current baseline;
- eager tool loading remains a temporary, explicit rollback mode until the
  directory path meets its measured removal gate.

S8 does not add:

- a semantic Host router, keyword intent classifier, planner model, second
  Agent, or second compaction model;
- a parallel tool directory, duplicate output-contract registry, or generic
  raw-result cache;
- a parallel active-schema store, compaction implementation, or caller-specific
  token estimator;
- model-selected toolsets, free-text authorization reasons, or model authority
  to widen the Host allowlist;
- offset pagination, model-maintained `seen_ids`, or cross-snapshot deduplication;
- hidden result completion, automatic fresh-snapshot refill, or automatic
  fallback from directory mode to eager mode;
- direct model mutation, confirmation, cancellation, or apply authority.

### 13.3 Ownership model

The single `om_chat` Scene and the existing canonical registry remain. The
request path has four owners:

| Concern | Owner | Required behavior |
|---|---|---|
| Scene and authority scope | Python Service and Host | derive the authorized tool universe from entry contract, scene, config, channel capability, and canonical registry |
| Tool choice | Pi main model | decide whether evidence is required and select exact canonical tools from the compact catalog |
| Schema activation and final context | Node Pi Runtime | atomically replace active schemas, assemble the exact provider input, compact, and enforce the hard gate |
| Business truth and result admission | canonical tool plus Python Host | execute the tool, project bounded evidence, validate claims, commit the existing durable run winner, and deliver the approved text |

The Host never interprets the user's business intent. It already knows the
maximum authorized universe before the model runs because the entry contract
fixes the Scene and identity scope, the Scene fixes allowed toolsets, runtime
configuration may narrow optional toolsets, and the canonical registry owns
enabled tool definitions and read/Control classification. The model can narrow
that universe; it cannot broaden it.

Tool need is based on evidence requirements, not keywords:

- general explanations and timeless conceptual guidance may be answered from
  model knowledge without a business tool;
- current, account-specific, runtime, financial, quantitative, or historical
  OM facts require canonical evidence;
- when completeness or freshness is unknown, the answer must state the gap or
  ask for a narrower scope rather than infer the missing facts.

### 13.4 Canonical compact catalog

The compact catalog is a projection of the existing canonical registry, not a
new registry or model tool. Each scene-visible canonical definition must expose
only the metadata that the main model needs for selection:

```json
{
  "name": "option_positions_read",
  "toolset": "positions",
  "purpose": "查询授权账户的期权持仓及其生命周期状态",
  "access": "read",
  "evidence_type": "collection"
}
```

Metadata ownership is closed:

- `AgentTool.catalog_summary` owns the required single-line `purpose`;
- the existing registry/module grouping derives `toolset`;
- existing read/Control authority derives `access`;
- the canonical output contract owns `evidence_type`;
- the Host derives `name` from the canonical tool definition.

`evidence_type` is a static catalog value from
`point|collection|aggregate|diagnostic|mixed`. A payload-dependent tool uses
`mixed` when its actions have different result shapes. Its resolver may refine
coverage, freshness, and result shape after arguments are known, but it may not
change the catalog value. This keeps directory selection deterministic without
adding a second metadata source.

There is no catalog YAML/JSON file and no `list_tools` call. In directory mode,
missing or invalid catalog metadata for any scene-visible tool fails scene
preparation before the provider call. The Host must not omit the tool, copy its
full description into the catalog, or silently switch the request to eager
mode. Eager mode retains its own compatibility behavior while it exists.

At request start the Host freezes an immutable snapshot containing:

- the authorized canonical tool names;
- compact catalog entries;
- full descriptions and input schemas;
- output-contract versions;
- read/Control classification;
- `catalog_hash`, calculated from a deterministic serialization of the above.

Registry, deployment, or configuration changes after request start affect only
the next external user request.

### 13.5 Preserved startup protocol and S8 `run.start` delta

S8 preserves the `om-pi-ipc.v1` JSONL envelope, one-child-per-request startup,
and the existing first-message `run.start` protocol. It does not add a separate
bootstrap process, directory round trip, or model-visible catalog tool call.
Python and Node change the closed `run.start` payload atomically in the same
release.

The payload adds:

```json
{
  "tool_loading_mode": "directory",
  "tool_catalog": [
    {
      "name": "option_positions_read",
      "toolset": "positions",
      "purpose": "查询授权账户的期权持仓及其生命周期状态",
      "access": "read",
      "evidence_type": "collection"
    }
  ],
  "catalog_snapshot": [
    {
      "name": "option_positions_read",
      "toolset": "positions",
      "description": "读取授权账户的期权持仓",
      "input_schema": {"type": "object", "properties": {}},
      "output_contract": {},
      "access": "read",
      "purpose": "查询授权账户的期权持仓及其生命周期状态",
      "evidence_type": "collection"
    }
  ],
  "catalog_hash": "sha256:..."
}
```

The absolute context window has one authority: the operator-declared
`model.context_window_tokens` already carried by `run.start`. S8 removes the
semantically duplicate `limits.max_context_tokens`; the Runtime no longer takes
the minimum of two independently configured windows. `max_output_tokens`
remains the provider-output reservation. For the adopted DeepSeek V4 Flash
profile, the declared context window is 128,000 tokens; this value is a model
profile fact, not a Scene-specific budget.

The same atomic payload migration also removes the unused Scene
`runtime.max_context_chars` and `runtime.max_context_tokens` fields, their
`SceneManifest.limits` projections, the Host `max_context_tokens` process
limit, and the corresponding Node closed-payload field. Python and Node do not
support mixed old/new payload shapes inside one release. Source and fixture
search must prove that no Scene or process limit still carries an absolute
context cap.

The 70% compact trigger, 75% hard input gate, and 50% post-compact target are
fixed constants owned by the Node Pi Runtime, not operator settings or
`run.start` fields. The Runtime records the effective values in structured
metrics. The Python Host neither supplies nor independently calculates them;
the closed payload does not accept a `context_policy` field. The Runtime
performs the only final calculation because it alone knows the exact system
context, compact catalog, active schemas, transcript, tool groups, recovered
context, and current message sent to the provider.

Initial tool exposure is:

| Mode | Business schemas in `run.start.tools` | Resident internal tools | Catalog context |
|---|---:|---|---|
| `eager` | all authorized business schemas | `submit_answer`; authorized `request_control_preview` when present | optional for metrics, not model selection |
| `directory` | none | `tool_directory`, `submit_answer`; authorized `request_control_preview` when present | required Host-authoritative system context |

These resident tools are protocol/Control tools, not canonical business tools.
They do not appear as selectable catalog entries and do not count toward
business tool or toolset limits. The Node Runtime renders the compact catalog
as non-persistent Host-authoritative system context; it is not compiled into
the static prompt or copied into the user message.

### 13.6 Directory activation protocol

The resident `tool_directory` exposes exactly one operation:

```json
{
  "name": "tool_directory",
  "input": {
    "catalog_hash": "sha256:...",
    "tool_names": ["option_positions_read", "query_cash_headroom"]
  }
}
```

The model does not send toolsets or a free-text reason. The Python Host checks:

1. `catalog_hash` equals the immutable request snapshot;
2. every name belongs to the request allowlist;
3. the exact selection spans at most two business toolsets;
4. the selection contains at most six business tools.

On success, `tool.result` carries two separate views. `observation` is the small
model-visible acknowledgement. The optional private `tool_activation` field is
consumed only by the Node Runtime:

```json
{
  "call_id": "call_123",
  "tool_name": "tool_directory",
  "observation": {
    "ok": true,
    "status": "activated",
    "active_tool_names": ["option_positions_read", "query_cash_headroom"]
  },
  "tool_activation": {
    "catalog_hash": "sha256:...",
    "schema_hash": "sha256:...",
    "tools": [
      {
        "name": "option_positions_read",
        "description": "...",
        "input_schema": {"type": "object", "properties": {}}
      }
    ]
  }
}
```

The full descriptions and schemas never enter the model-visible activation
observation. The existing unique `call_id` correlates the activation; no second
activation identifier is added. The Runtime validates the catalog and schema
hashes, exact tool names, and schema shape, then calls Pi
`prepareNextTurnWithContext` to replace the active business tool set atomically.
A validation or Pi schema-application failure leaves the previous set
unchanged. Runtime application failure is terminal; it does not ask the model
to repair a half-applied set. The replacement Pi context is the only
provider-visible active-schema state; the Runtime retains only the frozen Host
universe, hashes, and bounded repair/change counters around it.

Activation is an exact replacement, not an addition:

- the Node Runtime requires `tool_directory` to be the sole call in its batch,
  so no tool executes against a schema set while that set is being replaced;
- the same set is an idempotent no-op and consumes no successful-change budget;
- a different valid set replaces every active business schema and consumes one
  successful change;
- an invalid selection leaves the active set unchanged and consumes the one
  protocol repair; a second invalid selection is terminal;
- a request permits at most two successful set changes;
- phase switching may replace schemas but retains already collected business
  evidence for the current request;
- a new external user message resets directory mode to no business schemas.

`request_control_preview` remains the only model-facing Control operation. The
Host includes its existing small schema as a resident tool only when the
channel supplies authorized Control specs, and it validates the selected spec
and arguments. It is never selected, added, or removed through
`tool_directory`. Whether the current message explicitly asks for an action is
a Pi selection rule, not a second Host intent classifier. A mistaken selection
can create only the existing no-write preview; explicit confirmation, apply,
and readback remain deterministic outside Pi. The tool is excluded from the
business count and must remain the sole call in its batch. Pi never receives
mutation, confirm, cancel, or apply tools.

### 13.7 Repair budgets and terminal failures

Repairs are request-local and independent by failure class, but share the
existing global model-turn, wall-time, token, tool-call, failed-batch, context,
and final-answer-reserve budgets:

| Budget | Used for | Maximum |
|---|---|---:|
| Protocol repair | invalid hash, unauthorized tool, tool/toolset count violation, or malformed activation | 1 |
| Plain-final repair | the first ordinary assistant final that omits `submit_answer` | 1 |
| Submission repair | the first invalid assistant batch containing `submit_answer`, or the first canonical retryable Host admission rejection | 1 |

Among Host results, only `status=rejected`, `retryable=true`, with a non-empty
`reason` consumes submission repair. Cancellation, infrastructure failure, and
other non-retryable results never become model repair. An invalid mixed batch
consumes every repair category represented by its calls; for example, a batch
containing both `tool_directory` and `submit_answer` consumes both protocol and
submission repair. A second failure in either answer class is terminal even if
the other class remains unused. Before either answer repair continues the
provider loop, the Runtime checks the remaining model iterations, tool calls,
consecutive failed-tool-batch budget, and final-answer time reserve. Failure
uses the existing `BUDGET_EXHAUSTED` path and never enters forced final.

Compaction failure, context hard-gate failure, private schema validation
failure, Pi atomic-apply failure, timeout, cancellation, and child failure are
terminal. They consume no model repair because the model cannot safely correct
them.

### 13.8 Bounded observation and coverage contract

The canonical tool remains the truth owner. The Python Host may hold the full
canonical result transiently only long enough to validate it and build the
model projection. It does not persist an extra raw copy.

S8 extends the existing `compact_observation()` path; it does not introduce a
second projection framework. Every scene-visible output contract must declare:

- `evidence_type`;
- a deterministic bounded slice or summary rule;
- coverage calculation;
- freshness policy or explicit `not_applicable`;
- for collections, requested-page and full-query semantics;
- any cursor TTL and stable sort requirements.

The model projection carries a closed coverage envelope:

```json
{
  "coverage": {
    "status": "complete",
    "complete_for": "requested_page",
    "total_count": 143,
    "included_count": 20,
    "omitted_count": 123,
    "scope": {
      "account": "lx",
      "market": "US",
      "requested_limit": 20
    },
    "as_of": "2026-08-22T09:30:00+08:00",
    "has_more": true
  }
}
```

`coverage.status` is `complete`, `partial`, or `unknown`. Completeness is always
relative to the declared `complete_for` scope: `point`, `requested_page`, or
`full_query`. A full requested page may be complete for that page while still
being incomplete for the full collection. Missing resolver metadata, resolver
failure, or a contract that cannot determine coverage/freshness produces
`unknown`; that evidence may support diagnosis but cannot support an exhaustive
claim.

`included_count` is required for a collection projection. `total_count` and
`omitted_count` may be `null` when the owner cannot determine a total without an
unbounded or materially more expensive query. A `null` total is never inferred
from page size and cannot support `complete_for=full_query`; the Host banner
states that the total is unknown.

The projection budget is a soft target of approximately 4,000 tokens per tool
result and 20,000 tokens of active evidence per request. Those values never
authorize truncating a complete claim. If deterministic complete evidence
cannot fit, the result becomes `needs_narrowing` or the answer fails closed.
Generic “first 20 items” preview behavior cannot support a `full_query` claim.

When more detail is needed, the Pi main model calls the same canonical tool
with narrower arguments. S8 does not add `__read_observation__`, a raw-result
page cache, or a generic result-recall tool. Audit persists only the redacted
model view, counts, coverage/freshness metadata, and content hash.

All scene-visible output contracts must migrate and pass CI before the evidence
gate can be enabled, including tools that are uncommon in sampled traffic. CI
scans the complete scene allowlist; there is no default global TTL or hidden
allowlist omission.

### 13.9 Stable collection pagination and snapshots

S8 does not add continuation to every collection. Each payload-resolved output
contract declares `pagination.mode=none|keyset`. `keyset` is permitted only
when the canonical owner can prove all of these invariants for the cursor TTL:

- row identity is unique and stable;
- membership and every sort-key field are immutable;
- the first call can record a replayable upper boundary or version;
- later calls can apply the same authority scope, filters, boundary, and order.

A mutable position projection, current runtime view, or newly materialized
analysis view defaults to `none`. If its bounded page omits rows, the result is
`needs_narrowing`; S8 does not add a generic snapshot store to make it pageable.
The transaction-detail use case must use a canonical ledger/event owner whose
contract proves the invariants above; it must not page through
`analysis_query`. Offset paging and a model-supplied list of seen IDs remain
forbidden.

An eligible keyset order includes a unique tie-breaker, for example:

```text
occurred_at DESC, trade_event_id DESC
```

The opaque cursor binds:

- cursor version and canonical tool name;
- the normalized membership-and-order query;
- account, market, and other authority scope;
- stable sort definition;
- snapshot boundary;
- last returned sort key;
- issued-at and expires-at timestamps.

The signed normalized query excludes `limit` and `include_total`. Therefore an
eligible stream can serve 10 items, then 20, then 10 without repeating items
inside its bounded snapshot.
The cursor is stateless and HMAC-signed. No cursor database or raw-row cache is
added. The Pi Session may retain the opaque cursor needed for continuation;
audit stores only its hash.

Cursor signing uses a domain-separated child key derived by the Python tool
adapter from the existing `inbound.operation_hmac_key`. The derivation is
HMAC-SHA256 with the fixed byte label
`options-monitor/copilot/trade-event-cursor/v1`; the resulting child key is
passed to the canonical ledger facade. Neither key enters Node/model context.
S8 adds no credential, keyring, transparent rotation, or upgrader secret
workflow. Rotating the inbound master key invalidates outstanding cursors, and
the user must start a new query. An `events` request fails closed and explicitly
when its runtime consumer cannot resolve the inbound key.

Cursor TTL belongs to each canonical output contract. Expired, invalid,
wrong-signature, wrong-tool, wrong-query, or wrong-authority cursors fail
explicitly and never start a new snapshot automatically. A new snapshot can
contain records already seen in the old snapshot; OM tells the user that
duplicates are possible and does not maintain cross-snapshot `seen_ids`.

`limit` is a maximum, not a fill guarantee. If the user asks “再来二十条” but
only ten rows remain in the original snapshot, the tool returns:

```json
{
  "requested_limit": 20,
  "returned_count": 10,
  "snapshot_exhausted": true,
  "has_more": false,
  "next_cursor": null
}
```

The answer states that only ten remain. It must not cross into a fresh snapshot
to fill the other ten. The user may explicitly ask to query the latest data;
that starts a new `stream_id` and may repeat earlier records.

Snapshot validity and current-fact freshness are separate:

- “再来” requires a still-valid cursor and continues the original `as_of`;
- “查询最新” starts a new snapshot;
- an old valid snapshot may be continued, but it cannot support a claim about
  the current total;
- historical continuation must display its `as_of` time.

Across a new external message, continuation may reuse only an exact opaque
cursor and its bounded metadata from a still-retained complete canonical
business observation. A model-generated compaction summary is never cursor
authority. If compaction has removed that observation, continuation fails with
guidance to start a new query; the new snapshot may repeat earlier rows. Raw
rows are never recovered as authoritative evidence. The Host and canonical
tool revalidate every retained cursor on use.

#### Canonical trade-event stream

The first concrete keyset owner is the canonical ledger `trade_events`
collection exposed through the existing
`option_positions_read(action=events)` surface. S8 does not add a second trade
reader. Omitting `position_effect` returns every event admitted by the
normalized query; `position_effect=close` selects the existing application
meaning of close, which includes canonical `close`, `expire_close`,
`assignment`, and `exercise` events. The existing account, broker, symbol,
option type, strike, and expiration selectors remain part of the normalized
query. `config_key` also binds the result to the canonical row market; a US
request must never return an HK event or vice versa.

The ledger schema adds three query-owned projections beside the immutable event
payload:

- `ingest_seq`, a globally unique, monotonically increasing integer that never
  changes after insert;
- `market`, derived from the canonical contract symbol identity;
- `position_effect`, derived from the canonical event type.

These fields are written in the same canonical ledger transaction as the
event. Existing rows receive a deterministic one-time backfill before keyset
mode can be enabled. Sequence allocation remains inside the SQLite write
transaction and is protected by a uniqueness constraint; callers never read a
maximum and allocate the next value outside that transaction. The named
`ingest_seq` is the public snapshot primitive. SQLite's hidden `rowid` is not a
cursor or migration contract.

The cursor freezes collection membership, filters, and order; it does not
freeze every non-query byte in `event_json`. After pagination schema activation,
SQLite rejects deletion and any update to `event_id`, `trade_time_ms`, account,
market, position effect, or the broker/symbol/option-type/strike/expiration/
event-type fields used by this query. Existing controlled enrichment may still
update non-query evidence such as cash-conversion metadata in `raw_payload`.
Python's canonical event encoder remains the JSON contract owner; SQLite checks
only the scalar pagination projections and these immutability boundaries. No
global mutation revision or duplicate canonical JSON validator is added.

The first page opens one SQLite read transaction, records
`snapshot_max_ingest_seq = MAX(ingest_seq)`, and reads the page under the same
transaction. This value is the current maximum inserted sequence, not a row
count, event time, requested limit, or model-selected value. Later inserts,
including late-arriving events whose business time is old, receive a greater
sequence and remain outside that snapshot. OM does not copy rows, retain a
long-lived read transaction, or create a snapshot cache.

The visible order is fixed for S8:

```text
trade_time_ms DESC, event_id DESC
```

`event_id` is the unique tie-breaker. `ingest_seq` controls snapshot membership
only; it does not replace business-time ordering. A continuation applies the
same normalized filters and snapshot fence plus the keyset predicate below:

```text
ingest_seq <= snapshot_max_ingest_seq
AND (trade_time_ms, event_id) < (last_trade_time_ms, last_event_id)
```

The `events` action defaults to 10 rows and accepts at most 20. A continuation
may change `limit`, so streams such as 5 -> 20 -> 10 remain valid; values above
20 fail validation and are never silently clamped. The owner reads at most
`limit + 1` matching rows, returns at most `limit`, and uses the extra row only
to derive `has_more`. The action-specific bound does not change limits for the
other `option_positions_read` actions.

The event output contract contains:

```json
{
  "rows": [],
  "requested_limit": 10,
  "returned_count": 0,
  "total_count": null,
  "stream_id": "opaque-stream-id",
  "as_of": "2026-08-22T00:00:00Z",
  "has_more": false,
  "snapshot_exhausted": true,
  "next_cursor": null
}
```

`cursor` and `include_total` are `events`-only inputs. `include_total` defaults
to false and, like `limit`, is excluded from the signed normalized query because it
does not change row membership or order. When the user explicitly requests an
exact total, `include_total=true` performs a canonical aggregate over the same
filters and snapshot fence. An exact `total_count` proves only the aggregate;
it does not make a partial `rows` page complete. Ordinary pages leave
`total_count=null` and rely on `has_more` and `snapshot_exhausted`.

The event cursor TTL is 30 minutes. The cursor additionally binds the fixed
order, `snapshot_max_ingest_seq`, last `(trade_time_ms, event_id)`, normalized
query, authority scope, `stream_id`, and `as_of` through the section 13.9 HMAC
contract. A continuation may supply a different `limit` or `include_total`, but
any repeated filter must normalize to the signed query; conflicts fail
explicitly. Expiry never starts a new stream.

An unbounded request for all detail rows is not represented by a larger limit
and must not cause Pi to drain the cursor into model context. OM returns
`needs_narrowing` guidance so the user can add account, market, date, or other
selectors, or it uses an explicit canonical aggregate when that answers the
question. S8 does not add an automatic file export, retention policy, or
cleanup workflow.

The ledger query must apply authority, filters, snapshot membership, order,
and keyset before the page limit in SQLite. It must not call
`list_trade_events()`, deserialize the complete collection, or perform final
page filtering in Python. Query-critical projections and index paths must
cover both the unfiltered event stream and the `position_effect`-filtered
stream. With a properly indexed `ingest_seq`, acquiring the maximum and reading
a page remain bounded by the index and page size rather than total row count;
one million rows do not justify a raw-result cache, offset paging, or database
partitioning by themselves.

### 13.10 Evidence identity, freshness, and request scope

Observation IDs are globally unique opaque values, not run-local counters such
as `obs_1`. The Host maintains a request-local evidence registry that binds an
ID to tool, arguments hash, output-contract version, coverage, freshness,
`as_of`, and redacted content hash.

Claims may reference only observations registered during the current external
user request. The current uncommitted turn is never compacted, so its business
tool groups and observation references remain intact. The Host registry, not a
compaction summary, preserves their claim validity. A new external message
invalidates old observations as fresh claim evidence even though their compact
transcript may remain useful as conversation context. Only signed
cursor/snapshot metadata may cross requests for continuation.

When a follow-up needs an old page fact or aggregate, the model calls the
canonical tool again for the bounded range. A compact summary is not promoted
to current authority. Freshness belongs to the output contract:

- current facts without a valid time are `unknown`;
- historical or immutable facts may declare `not_applicable` or a contract-
  specific policy;
- compaction and Session recovery never refresh `as_of`;
- stale evidence must be refreshed through the canonical tool or disclosed as
  an evidence gap.

### 13.11 Structured final-answer admission

S8 adds one always-resident internal tool, `submit_answer`. It is a structured
evidence protocol, not a second model or keyword-based semantic verifier:

```json
{
  "mode": "evidence",
  "status": "complete",
  "answer_markdown": "截至 2026-08-22……",
  "claims": [
    {
      "text": "当前授权账户有 10 个未平仓期权头寸",
      "kind": "current_fact",
      "observation_ids": ["obv_019..."],
      "required_scope": "full_query"
    }
  ]
}
```

The closed schema is:

- `mode`: `conceptual` or `evidence`;
- `status`: `complete`, `partial`, `needs_narrowing`, or
  `insufficient_evidence`;
- `answer_markdown`: the proposed user-visible answer;
- `claims[]`: `text`, `kind`, `observation_ids`, and `required_scope`;
- claim `kind`: `current_fact`, `historical_fact`, `derived_fact`, or
  `judgment`;
- `required_scope`: `point`, `requested_page`, or `full_query`.

Conceptual mode may have no claims. Evidence mode requires at least one claim.
The Node Runtime requires `submit_answer` to be the sole call in its batch and
does not dispatch an invalid batch to the Host. The model does not submit
coverage counts, freshness, or `as_of`; the Host derives them from the current
request evidence registry. Financial facts,
current or historical OM facts, numeric results, derived facts, and
evidence-based judgments must be declared as claims. General explanation and
advice may remain unclaimed prose, but the verifier makes no semantic guarantee
for arbitrary free text.

The Host verifier deterministically checks:

1. every observation exists in the current request registry;
2. the observation belongs to an authorized successful read;
3. coverage supports the claim's required scope;
4. freshness and `as_of` support the claim kind;
5. `partial`, `unknown`, and `needs_narrowing` states are represented honestly;
6. answer size and Markdown satisfy the existing public result contract.

On acceptance, the Host stores the exact approved Markdown and a private
admission fingerprint. The Runtime terminates the Pi loop atomically without
another provider call and emits the existing `run.proposed` answered path. The
Python Host compares the proposal with the retained approved candidate before
the existing durable admission CAS and outbox flow.

On rejection, the tool returns only a compact structured reason. The Node
Runtime owns separate one-shot plain-final and submission-repair counters. The
first plain assistant final that bypasses `submit_answer` appends the synthetic
repair prompt. The first invalid submission batch, or the first Host result
with `status=rejected`, `retryable=true`, and a non-empty `reason`, consumes
submission repair; the compact rejection observation is the instruction for
the next turn. An invalid mixed batch consumes every represented repair class,
not only the first class detected. The answer-admission `repair_count` is the
sum of the two counters.

A second failure in the same answer class ends with
`ANSWER_ADMISSION_FAILED`; it produces no proposal and persists no current-turn
message. Both the initial user prompt and the synthetic repair prompt preserve
the real terminal outcome: an inbound Host error, Runtime failure, or
cancellation is handled before a generic prompt or answer-admission failure.
An exact canonical cancellation returned by a Host callback is normalized by
the Python adapter and sent as the existing `run.cancel`; it is never forwarded
as `tool.result` and never triggers submission repair. Other unexpected
non-retryable submit failures remain terminal tool-bridge errors. No extra Host
callback or fallback-answer IPC is added. Control proposals continue through
`request_control_preview` and the existing Control terminal path, not
`submit_answer`.

The Host, not the model, forces coverage banners:

- `complete`: normal answer, with `as_of` when the contract requires it;
- `partial`: “部分数据” plus included, total/unknown total, omitted, and scope;
- `unknown`: completeness is unknown and exhaustive wording is prohibited;
- `needs_narrowing`: fixed guidance to narrow account, time, symbol, or result
  range.

The model cannot remove or rephrase these safety banners.

The accepted terminal is carried by the existing `tool.result` message with one
new mutually exclusive private field:

```json
{
  "call_id": "call_456",
  "tool_name": "submit_answer",
  "observation": {"ok": true, "status": "answer_accepted"},
  "approved_answer": {
    "status": "complete",
    "text": "截至 2026-08-22……",
    "text_sha256": "sha256:..."
  }
}
```

`tool_activation`, `approved_answer`, and `control_request` cannot coexist in
one result. A rejection returns only an error observation. Acceptance returns
`approved_answer`; the Node bridge terminates the successful path only when
`approved_answer` is present, retains the exact text/hash, and makes no further
provider call. A second answer-admission failure terminates through the error
path above, not through a fabricated `approved_answer`.

The answer state machine is:

```text
running -> submit_pending                              (submit call)
running -> plain_repair_pending                        (first plain final)
plain_repair_pending -> running                        (synthetic prompt)
submit_pending -> submission_repair_pending            (first repairable failure)
submission_repair_pending -> running                   (structured rejection)
submit_pending -> answer_ready                         (accepted `approved_answer`)
answer_ready -> proposed -> committed | discarded | cancelled
second same-class answer failure -> failed              (no proposal or commit)
Host callback cancellation -> Python `run.cancel` -> cancelled
```

The two answer-repair transitions may occur once each and in either order,
subject to the same global limits.

At the Copilot Host boundary, `option_performance_report` has one narrow
current-message authorization fence with two branches. If the entire immutable
current message matches the affirmative form
`截至 YYYY-MM-DD 的 M月期权收益率`, with only the existing optional whitespace,
`总计`, `不分账号`, comma, and terminal punctuation variants, the Host requires
canonical `period=mtd` and an equal `as_of_date`; the date must be
Gregorian-valid and its month must match the stated month. Any mismatch,
including non-MTD or a missing date, is `INPUT_ERROR`. For every other message,
the stale/ambiguous fence runs only when the canonical payload is MTD with a
non-empty `as_of_date`: no cutoff indicator removes the model-supplied date,
while a recognized cutoff indicator rejects before the business read. Thus
non-affirmative non-MTD calls and MTD calls without `as_of_date` do not enter
the stale/ambiguous fence. Neither branch selects a tool or period, inspects
Session history, or changes direct Tool Gateway behavior.

Before `proposed`, cancellation wins and the current turn is not persisted.
After `proposed`, the existing Host admission CAS remains the sole winner. The
Runtime builds the canonical assistant message by replacing the accepted
`submit_answer` tool-call message content with the approved text while retaining
that message's model, timestamp, and usage; it sets `stopReason=stop`. The
proposal text and hash must equal the Host-retained candidate before commit.
Discard and cancel write no current-turn message. Independently committed
pre-run compaction remains durable under the current checkpoint rule.

### 13.12 Pi Session normalization

Directory activation and answer submission are request-control protocol, not
durable conversational content. Before committing the current turn, the Node
Runtime normalizes complete message/tool groups so that:

- `tool_directory` calls, private activation results, rejected `submit_answer`
  attempts, synthetic repair prompts, and accepted `submit_answer` call/result
  groups are omitted from the durable Pi Session suffix;
- the accepted answer is persisted once as a canonical assistant text message;
- canonical business tool call/result groups remain complete and may support
  conversational continuity, but their observation IDs cannot satisfy claims
  in a later external request;
- no partial tool group is ever retained or deleted independently.

This prevents internal protocol noise and duplicate answer text from consuming
future context while preserving the user's natural conversation. Pagination
cursor metadata may remain in a bounded canonical business observation under
the rules in section 13.9.

### 13.13 Context budget and Pi compaction primitives

S8 uses Pi's exported `estimateContextTokens`, `estimateTokens`,
`prepareCompaction`, and `compact` primitives with the same active model. The
Node Pi Runtime owns the automatic trigger, hard gates, and durable compaction
checkpoint; it does not call the unimplemented `AgentHarness.compact()` API.
DeepSeek V4 Flash does not use a second summarizer, Host summary model, or
OM-specific conversation-pruning algorithm.

The effective input capacity is:

```text
effective_input_capacity = model.context_window_tokens - max_output_tokens
```

Compaction remains a pre-run Session maintenance action. It operates only on
the committed Session prefix and runs at most once before the Pi `Agent` starts.
The current external-request suffix is never partially committed or summarized.

The pre-run sequence is:

1. assemble and estimate the first main-call candidate from static prompt,
   dynamic catalog, runtime context, initial schemas, committed Session, current
   user message, and recovered metadata;
2. when that candidate is at or above 70% and the committed Session has an
   eligible prefix, prepare Pi compaction from that prefix only;
3. estimate the separate compaction provider input and reject it above the same
   75% hard gate before calling the provider;
4. after successful Pi compact, commit the compaction entry and marker using
   the existing independent checkpoint, reload the committed prefix, then
   reassemble the first main-call candidate; the target is at most 50%;
5. if the candidate is above 75% after the one compact, or no safe compact call
   can be made, return `BUDGET_EXHAUSTED` before starting the Agent.

Once the Agent starts, the Runtime reassembles and estimates the exact candidate
before every main provider call, including after activation, tool results, or
answer repair. It does not compact the open suffix. At or above 70% it records a
warning; above 75% it returns `BUDGET_EXHAUSTED` before the call. Bounded tool
projection and narrowing keep current evidence below that gate. There is no
mid-turn Session write, second compaction loop, or custom pruning algorithm.

Every trigger, hard gate, post-compact check, activation turn, and answer-repair
turn calls one Runtime-owned `estimateProviderInputTokens()` boundary. It uses
Pi `estimateContextTokens()` to locate the last valid provider usage and keeps
the reported input/output/cache total authoritative. Only the unreported
trailing messages and newly assembled fixed input are estimated. When no valid
usage exists, the same estimator covers the complete candidate.

Pi's structural `estimateTokens()` remains the baseline, but its character
heuristic can underestimate Chinese input. For each unreported serialized
component the single estimator therefore uses:

```text
conservative_tokens = ceil(
  max(pi_estimate_tokens, ascii_characters / 4 + non_ascii_characters) * 1.10
)
```

The 10% covers serialized structural overhead. There is no separate estimator
for compaction, main calls, activation, or repair, and no caller may combine
provider usage with a second full-history estimate.

S8 does not add a DeepSeek-specific tokenizer or auto-tune thresholds from
traffic. Compaction summaries preserve user goals and preferences, material
timestamps, unresolved work, and Control references from committed history.
They never become authority for facts or opaque cursor bytes. Historical facts
remain historical and are never promoted to current truth. Current-request
evidence stays in the untouched open suffix and Host evidence registry.

A failed or empty compaction does not modify the committed Session. The bounded
context contract fails the request closed even if the old
context might appear to fit; there is no Session reset, outer provider retry,
or alternate summarizer. This is an explicit S8 contract change.

### 13.14 Prompt, observability, and privacy

The static prompt change is semantic-equivalent cleanup only: insert the new
directory, evidence, and finalization rules; remove rules they supersede; and
do not aggressively rewrite unrelated wording in this work unit. The compact
catalog remains dynamic system context. The compiled static prompt must not
exceed its pre-S8 character and conservative-token baseline. The two small
resident internal schemas are measured separately and must remain materially
smaller than the removed eager business schema set.

Structured metrics record:

- fixed prompt, catalog, active schemas, history, current message, recovered
  context, tool-result, and repair token estimates;
- catalog hash, entry count, characters, and token estimate;
- activation ID, selected names/toolsets, schema hash, no-op/change/rejection,
  and repair count;
- observation coverage, narrowing reason, freshness state, and redacted hash;
- requested/returned page size, stream ID, snapshot status, and cursor hash;
- compact trigger, before/after estimates, retained groups, latency, result,
  and failure;
- 70/75/50 threshold decisions and pre-provider budget rejection;
- provider-reported usage, retries, status, and latency;
- answer-admission status, referenced observation IDs, repair count, and final
  Host banner.

Metrics and audit must not store raw prompts, full canonical responses, full
cursors, secrets, reasoning text, or private activation schemas. Existing Host
scene/tool fingerprints remain and are extended rather than replaced.

### 13.15 Rollout, rollback, and eager removal

One source version implements the complete S8 common contract. A single
temporary setting, `tool_loading_mode=eager|directory`, controls only business
schema loading. It does not disable output contracts, projection bounds,
cursor semantics, budget gates, compaction, metrics, or `submit_answer`.

Implementation stays in one source version but is reviewed through small,
ordered work units. Each work unit must pass its focused tests before the next
one starts:

1. add canonical catalog/output metadata and a CI inventory that walks the
   existing scene allowlist directly; do not maintain a second readiness file;
2. migrate bounded result projection one canonical tool family at a time;
   default collection tools to `pagination.mode=none` unless the owning module
   proves the section 13.9 keyset invariants;
3. add the request-local evidence registry and closed `submit_answer` admission
   state machine;
4. replace the duplicate context limits, then add exact-candidate estimation
   and committed-prefix pre-run compaction;
5. add directory activation, atomic schema replacement, and Session
   normalization;
6. run the aggregate eager-mode integration/regression suite, deploy the one
   release in `eager`, then explicitly switch the environment to `directory`
   only after every configured provider profile passes the compatibility gate.

These are review and test boundaries, not new runtime modes, feature flags,
releases, registries, or services. The only runtime switch remains
`tool_loading_mode`.

No remote sample is required to authorize the initial explicit directory
switch. Once enabled, a directory-specific failure raises an alert and requires
a human configuration change back to eager mode. There is no per-request
fallback and no automatic production configuration mutation. A failure in a
common evidence, admission, compact, or budget path requires version rollback;
eager mode does not bypass it.

Removing eager mode is a later explicit source and release decision. It is
allowed only when all of these are proven:

- directory mode has been stable for at least 14 days;
- at least 30 real evidence/tool requests have completed; conceptual-only
  requests do not count;
- no allowlist escape, catalog/schema hash mismatch, or half-applied activation
  has occurred;
- no confirmed partial or unknown result has been admitted as complete;
- among directory-mode requests that attempt at least one activation, at least
  99% reach a valid activation before terminal failure; conceptual requests
  and requests that require no activation are excluded from the denominator;
- no fixed prompt/schema context overflow has occurred;
- median business-schema tokens are at least 50% lower than the measured eager
  baseline;
- failure rate and P95 latency have an explicit human sign-off against an eager
  baseline measured for the same model, provider profile, scene, and reporting
  window. S8 does not invent an automatic latency threshold.

The criteria never delete eager mode automatically. If evidence is missing or
ambiguous, eager remains available.

### 13.16 Module change map

| Module | S8 responsibility |
|---|---|
| `src/application/agent_tool_registry.py` and canonical `TOOLS` definitions | add/validate `catalog_summary`; derive toolset/access; retain the one canonical registry |
| canonical output contracts | declare evidence type, deterministic projection, coverage, freshness, page/query scope, cursor TTL, and stable order |
| `src/application/copilot/scene.py` | build the frozen authorized universe and loading mode without interpreting user intent; remove Scene-owned absolute context caps |
| `src/application/copilot/host.py` | freeze catalog/schema snapshot, serve private activations, maintain current-request evidence registry, audit metrics, and preserve final durable admission |
| `src/application/copilot/tools.py` | extend `compact_observation()` with deterministic coverage/freshness projection and eliminate exhaustive claims from generic previews |
| `src/application/copilot/result_admission.py` | validate `submit_answer` claims against Host evidence and append non-removable coverage banners; retain existing final result checks |
| `src/application/ledger/repository.py`, `queries.py`, and `api.py` | migrate and query the canonical trade-event stream; own `ingest_seq`, normalized market/effect projections, snapshot fencing, keyset SQL, cursor validation/encoding, and the public ledger facade |
| `src/application/agent_tools/operations_impl.py` and `positions.py` | keep the existing `option_positions_read(action=events)` entry; expose the events-only cursor/count inputs and output contract; call only the public ledger facade and never load all events |
| `src/infrastructure/pi_agent_process.py` | serialize the revised closed `run.start` and private activation/admission fields without exposing secrets |
| `agent-runtime/main.ts` | render dynamic catalog context, own internal tools, atomically replace schemas, enforce budgets/compaction, terminate approved answers, and normalize Session turns |
| `src/application/agent_tools/operations_impl.py`, `secret_store/registry.py`, and `service_deploy.py` | derive the cursor child key from the existing inbound HMAC secret; keep the retired cursor env name unset without registering or binding a second credential |

No new business router, result store, cursor database, or output-contract
registry is permitted by this design.

### 13.17 Verification and acceptance matrix

| Area | Required deterministic evidence |
|---|---|
| Catalog ownership | every scene-visible tool has valid canonical summary/evidence metadata; hash is deterministic; missing metadata fails scene preparation |
| Contract inventory | CI walks the canonical scene allowlist and fails any visible tool without an output contract, coverage/freshness resolver, and explicit pagination mode; no manual readiness matrix exists |
| Intent boundary | conceptual fixture reaches `submit_answer` without a business tool; factual fixtures select tools through the Pi main model; Host has no general keyword router, and its narrow MTD cutoff fence does not select a tool or period |
| Activation | exact names only, two-toolset/six-tool maximum, hash/allowlist enforcement, idempotent no-op, two successful replacements, one repair, and atomic apply failure all pass |
| Control | Host rejects unauthorized preview schemas by channel/spec/arguments; a model fixture verifies explicit-action instructions before preview selection; sole-call and deterministic confirm/apply/readback behavior are unchanged |
| Projection | every allowlisted output contract reports coverage/freshness; `total_count`/`omitted_count` may be null but then cannot support a complete/full-query claim |
| Pagination | a temporary canonical-ledger fixture proves 5 -> 5 and 10 -> 20 -> 10 non-overlap, same-time tie ordering, exact end-of-snapshot behavior, explicit totals, and variable limits within one boundary; inserts with newer or late historical business times after the first page remain excluded; mutable/non-proven collections expose no continuation cursor and return bounded evidence or `needs_narrowing`; expired, mismatched, or compacted-away cursors never refresh implicitly |
| Pagination scale | CI query-plan fixtures prove account/market/effect filters, snapshot fence, stable order, and keyset execute at the SQLite owner without full-collection deserialization or a temporary sort; a non-default one-million-row local benchmark proves per-page memory is bounded by page size and later-page cost does not grow linearly with cursor depth |
| Cursor secret | an inbound-only runtime fixture pages successfully with the fixed domain derivation; no dedicated cursor credential is registered or bound; a missing inbound key makes `events` fail explicitly; neither master nor child key appears in Node/model/metrics or upgrader state, and an intentional inbound-key change deterministically invalidates old cursors |
| Evidence scope | observation IDs are globally unique and valid only in the current external request; pre-run committed-prefix compaction cannot grant old IDs current authority; old-page follow-up re-calls the canonical tool |
| Answer admission | conceptual/evidence modes, every claim kind/scope, mutually exclusive private result fields, one plain-final repair and one submission repair in either order, mixed-batch consumption of every represented class, second same-class safe failure with no commit, shared-budget exhaustion before either repair, canonical retryable rejection only, callback cancellation through Python `run.cancel`, identical terminal arbitration after both prompts, cancel/propose race, and exact approved-text/hash comparison pass |
| MTD cutoff scope | the real Copilot Host callback proves exact affirmative MTD/date attestation, no-indicator stale-date removal, and ambiguous-indicator rejection before any business read; outside the exact affirmative branch, non-MTD and MTD without `as_of_date` remain unchanged; direct Tool Gateway behavior remains unchanged |
| Context | 69/70/75 percent boundaries, committed-prefix-only pre-run compact, untouched open suffix, same-model compact, <=50 percent target, failed compact rollback, and exact pre-provider rejection before every main call pass at 128k and smaller fixtures |
| Session | internal directory/finalizer groups and repair prompts are absent; canonical assistant answer appears once; business tool groups remain complete |
| Compatibility | eager mode keeps all business schemas while using the same projection, cursor, budget, compact, metrics, and answer-admission contract |
| Provider profiles | for every configured model/provider profile, a follow-up turn whose durable history contains now-deactivated canonical tool call/result groups succeeds while the active schema set is replaced; any failure blocks directory enablement with no automatic eager fallback |
| Context migration | source and fixture search finds no Scene/process absolute context cap; the closed Python/Node payload uses only `model.context_window_tokens`; a 128k model with a formerly smaller Scene fixture is admitted by the single authority |
| Prompt budget | static prompt chars/tokens do not exceed baseline; directory-mode fixed plus active-schema tokens are measured against eager baseline |
| Audit/privacy | required structured metrics exist; raw prompts/results/cursors/private schemas/secrets/reasoning are absent |
| Regression | focused Pi contract tests, Copilot Host/Service tests, agent plugin contract/smoke, full Python suite, locked Node tests, and clean-archive smoke pass |

The historical one-character incident is a required regression fixture: a very
large tool result must become bounded/partial or `needs_narrowing`, the 75%
gate must prevent an impossible provider call, and a one-token length-stopped
completion must never become an admitted answer.

### 13.18 Closed S8 decisions

- Use catalog-first loading, not directory-first enumeration or an eager-only
  prompt.
- Keep the single Pi main model responsible for exact tool selection.
- Keep Host authority deterministic and intent-blind. Its only current-message
  input attestation is the Copilot-only two-branch fence defined in 13.11:
  attest canonical MTD and an equal `as_of_date` for an exact affirmative
  match; for every other message, act only on canonical MTD payloads that
  actually contain `as_of_date`, removing an old cutoff when there is no
  indicator and rejecting an ambiguous indicator. Outside the exact
  affirmative branch, non-MTD and MTD without `as_of_date` remain unchanged;
  direct Tool Gateway calls remain outside the fence.
- Project the compact catalog from canonical metadata; do not duplicate it.
- Preserve `run.start`; add the catalog and policy fields there, and keep only
  one absolute context-window authority.
- Apply active schemas atomically and replace rather than accumulate them.
- Bound the request to two toolsets, six business tools, two successful schema
  changes, one protocol repair, one plain-final repair, and one submission
  repair. The answer repairs share global limits, and a mixed batch consumes
  every represented repair class.
- Extend `compact_observation()` and canonical output contracts; do not add a
  raw-result cache or generic recall tool.
- Let only eligible immutable collection owners expose signed stateless keyset
  cursors with replayable boundaries; mutable or unproven collections narrow or
  stop instead of inventing snapshot semantics, and no path auto-refreshes to
  fill a page.
- Make the existing `option_positions_read(action=events)` canonical event
  stream the first concrete keyset owner: use explicit `ingest_seq` snapshot
  membership, fixed `trade_time_ms DESC, event_id DESC` order, a default of 10,
  a maximum of 20, and a 30-minute cursor TTL; do not add a second trade tool.
- Compute an exact event total only for an explicit request. Unbounded detail
  requests narrow or use an aggregate; they never drain pages into Pi context
  or add an automatic export workflow.
- Scope factual evidence to one external request; an exact cursor may cross
  requests only while its complete canonical observation remains retained, and
  a compaction summary never authorizes continuation.
- Use Pi compact with the active model only as committed-prefix pre-run
  maintenance at 70/75/50 thresholds; never compact the open turn, and enforce
  the hard gate before every provider call.
- Require `submit_answer` for ordinary answers and keep Control on its separate
  deterministic path.
- Among Host results, repair only canonical retryable admission rejection;
  route an exact Host callback cancellation through Python `run.cancel`, and
  preserve the real terminal outcome after both the initial and synthetic
  prompts.
- Guarantee evidence structure and declared scope, not semantic truth for
  arbitrary prose.
- Derive one domain-separated cursor child key from the existing inbound HMAC
  secret at the Python tool adapter; do not add another credential, keyring,
  transparent rotation, cursor store, or upgrader ownership. An intentional
  inbound-key change invalidates outstanding cursors.
- Freeze trade-event membership, filters, and order rather than every evidence
  byte: reject deletes and query-critical updates, allow controlled non-query
  enrichment, and do not add a global mutation revision.
- Roll out common safeguards in eager mode first, switch directory mode
  explicitly after offline and configured-provider gates, and remove eager only
  through a later evidence-backed release decision.

No S8 product or architecture decision remains open. Verified source or
upstream API details may refine field spelling during implementation, but any
change to authority, budgets, evidence scope, cursor semantics, compaction
failure behavior, rollout boundaries, or final-answer admission requires a
design update before code.
