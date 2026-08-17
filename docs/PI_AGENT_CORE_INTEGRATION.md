# Pi Agent Core Integration PRD And Development Design

Status: revised after PlanReview; implementation has not started; focused
re-review is required before S1.

Last upstream verification: 2026-08-16. The pinned baseline is
`@earendil-works/pi-agent-core@0.84.2` and
`@earendil-works/pi-session-backend-sqlite-node@0.84.2`, which require Node.js
`>=22.19.0`.

This document is the single planning authority for replacing OM's generic
Copilot runtime with Pi Agent Core. The current production architecture remains
documented in [OM_COPILOT_V2_DESIGN.md](OM_COPILOT_V2_DESIGN.md) until the
cutover is complete.

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

### 1.5 PlanReview feedback disposition

The 2026-08-16 focused review failed the prior draft on six implementability
gaps. This revision accepts all six and closes them at their existing owners:

| Finding | Closed design decision |
|---|---|
| terminal cleanup could overwrite a valid result | a validated terminal is authoritative; cleanup only reaps the child |
| cancellation raced with admission | one private Host SQLite CAS selects cancel/commit/discard before protocol delivery |
| abandoned tool workers could grow without bound | one process-wide read-worker slot; no queue and no second worker |
| forced kill could not release Pi's writer lease | Host lease releases immediately; Pi lease expires and is fenced after its TTL |
| Session history crossed config scopes | trusted normalized config scope is part of the Session/lease hash |
| Scene cap impersonated model capability | every active model profile declares a validated safe context window |

The unused V1 `message_delta` protocol surface is deleted because this release
has no streaming UI consumer. Strict IPC validation, seven development slices,
the run-local read reuse guard, lockfile install with `--ignore-scripts`, and
the no-container process boundary remain: the review found concrete value for
the first three and no evidence that weakening npm isolation or adding a
container is required for this scope.

The focused re-review found four remaining ownership gaps. This revision closes
them without adding a second runtime or a generic event framework:

| Finding | Closed design decision |
|---|---|
| external cancellation raced with result admission | one private Host SQLite compare-and-set chooses `cancel`, `commit`, or `discard` |
| tool-worker events raced with lifecycle events | one run-local lock serializes every Host event/cache mutation and gate close |
| public `config_path` collapsed to the `default` memory scope | the existing resolver supplies a Host-only canonical path and its hash becomes the authority identity |
| compaction had two commit owners | pre-run compaction is an independent checkpoint; admission governs only the current-turn suffix |

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

```json
{
  "execution_environment": "eval",
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
      "name": "runtime_status",
      "description": "...",
      "input_schema": {"type": "object", "properties": {}}
    }
  ],
  "limits": {
    "timeout_seconds": 180,
    "max_iterations": 16,
    "max_tool_calls": 12,
    "max_context_tokens": 24000,
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
- `tools` accepts only the Host-projected allowlist; input schemas must be JSON
  objects and tool names must be unique;
- all six limits are positive integers and may only reduce Scene limits;
- `recovered_observations` contains previously sanitized, successful read-only
  observations from OM Host recovery;
- `debug` must be `null` outside `eval`. Slice 1 permits
  `{"fixture_response":"text","delay_ms":0}` to drive an actual Pi `Agent`
  through a deterministic fake stream without network access.

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
Host SQLite transition described in section 15.4, not the JSONL write.
`request_cancel()` and result admission compete to move one row from `open` to
exactly one of `cancel`, `commit`, or `discard`. Python then sends only the
message selected by that durable winner. For transient diagnostics without a
Host store, the process adapter remains the single writer and its first
complete `run.cancel`, `run.commit`, or `run.discard` write is the local
linearization point. A failed protocol write after a durable `commit` claim
retains the unknown-commit handling in section 15.4.

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
    "usage": {"input": 10, "output": 5, "totalTokens": 15}
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

Use Pi's exported `estimateContextTokens`, `shouldCompact`,
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

Preserve `model`, `base_url`, `timeout_seconds`, `context_window_tokens`,
`max_output_tokens`, and `max_attempts`. Pi's built-in
`kimiCodingProvider()` uses Anthropic Messages at
`https://api.kimi.com/coding`, which is not OM's current `kimi-code` contract;
using it would be an unrelated breaking change. Do not load every Pi builtin
model or provider. Build one selected model/provider mapping per run and keep
OM's five public profiles stable. A provider slice is incomplete until a
fixture-backed contract test proves payload/tool mapping for all five profiles.
Live provider canaries are separate, explicitly authorized acceptance work.

## 9. Module Change Map

### 9.1 Keep as authority

- `src/application/copilot/service.py`
- `src/application/copilot/scene.py` and `om_chat.scene.json`
- `src/application/copilot/tools.py`
- `src/application/copilot/control_handoff.py`
- `src/application/copilot/result_admission.py`
- `src/application/copilot/event_store.py`
- `src/application/agent_tool_registry.py` and `agent_tools/`
- `src/application/assistant/inbound_service.py` and deterministic Control

### 9.2 Change

- `channel_facade.py`, `contracts.py`, `service.py`, and `om_chat.scene.json`:
  preserve key/path scope as fixed Host tool input, derive its opaque Session
  identity, and stop loading or writing legacy transcript memory.
- `host.py`: replace `run_engine()` with the Pi process call while retaining
  contract validation, Scene/tool projection, event logging, cancellation,
  admission, and finalization.
- `host_store.py`: retain governance tables, add the private admission CAS, and
  stop writing legacy conversation fields after cutover.
- `local_harness.py`: resolve model configuration and delegate diagnostic runs
  to the same Pi process boundary.
- `model_config.py`: retain OM configuration/secret validation without
  importing the retired `CopilotModelSettings` type.
- assistant model profile/config/CLI surfaces: require, preserve, display, and
  validate the declared safe context window.
- install, update, setup-check, release CI, and runtime diagnostics: verify
  Node and install/check the locked runtime package.

### 9.3 Add

- `agent-runtime/package.json` and `package-lock.json`;
- `agent-runtime/main.ts` as the single initial Node runtime file;
- `src/infrastructure/pi_agent_process.py` as the stdlib subprocess/JSONL
  adapter;
- `tests/test_pi_agent_process.py` as the focused protocol/process test file.

Do not add an interface with one implementation, a server framework, a second
tool registry, or a TypeScript build framework. Node 22 runs the restricted
erasable-TypeScript entry directly; split `main.ts` only after measured size or
ownership pressure justifies it.

### 9.4 Delete after atomic cutover

- `src/application/copilot/engine.py`
- `src/application/copilot/model_client.py`
- `src/application/copilot/conversation_memory.py`
- `src/application/copilot/agent.py`

Deletion happens only after all callers and architecture guards point to Pi and
the full acceptance gate passes. Database columns are left in place initially;
dropping unused historical columns has no product value during this migration.

## 10. Delivery Slices

Each slice is independently testable. A later slice does not start until the
current slice's exit gate passes.

| Slice | Scope | Exit gate |
|---|---|---|
| S1 | Pi package, JSONL process boundary, actual `Agent` with deterministic fixture stream | focused protocol tests pass without network, tools, or persistent state |
| S2 | read-only OM tool bridge with deterministic eval fixtures | schema/allowlist/error/cancellation tests pass; no write tool is visible |
| S3 | sender-and-config-scoped Pi Session, durable turn commit, context loading and compaction | continuity, key/path/user isolation, crash-lease, partial-write, and independent compaction-checkpoint tests pass |
| S4 | model context capability plus five provider mappings and error/usage normalization | config/CLI migration and all loopback provider contract fixtures pass; live canaries remain a separate release action |
| S5 | Host run events, durable cancel/admission CAS, serialized evidence writes, bounded read-only recovery, progress | concurrent winner/event tests and current run-control regressions pass |
| S6 | trusted key/path scope, `request_control_preview`, and `./om assistant handle` channel cutover | scope isolation, preview/confirm/apply separation, and channel idempotency pass |
| S7 | packaging, release gates, atomic cutover, legacy runtime deletion | full tests, setup check, release check, rollback rehearsal, and answer-quality acceptance pass |

Sections 11 through 17 are the executable specifications for S1 through S7.
They are one implementation plan, not seven independently released product
modes. Production remains on the legacy call path until the complete S7 release
passes the atomic cutover gate.

## 11. S1 Executable Development Specification

### 11.1 Objective

Prove that Python can safely run one real Pi `Agent` in a Node child and receive
an ordered terminal result through `om-pi-ipc.v1`, without a real provider,
tool, Session, channel, or production state.

### 11.2 Files

Create only:

```text
agent-runtime/package.json
agent-runtime/package-lock.json
agent-runtime/main.ts
src/infrastructure/pi_agent_process.py
tests/test_pi_agent_process.py
```

No public CLI or application module changes in S1.

### 11.3 Node package

Pin exact runtime versions:

```json
{
  "private": true,
  "type": "module",
  "engines": {"node": ">=22.19.0"},
  "dependencies": {
    "@earendil-works/pi-agent-core": "0.84.2",
    "@earendil-works/pi-ai": "0.84.2"
  }
}
```

Add the pinned SQLite backend only in S3, when Session persistence first becomes
executable code.

Use Node's built-in test/runtime facilities and the committed npm lockfile. Do
not add a bundler, test framework, server, or logging dependency.

### 11.4 Python call surface

`src.infrastructure.pi_agent_process.run_pi_agent()` is a function, not a new
service/interface hierarchy:

```python
def run_pi_agent(
    start_payload: dict[str, Any],
    *,
    request_id: str,
    run_id: str,
    timeout_seconds: int,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_tool_call: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    on_proposed: Callable[
        [dict[str, Any]], Literal["commit", "discard", "cancel"]
    ] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    runtime_entry: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    ...
```

Required behavior:

- validate the start payload before spawning;
- invoke Node as an argv list without `shell=True`;
- send one `run.start` envelope;
- validate every child envelope and monotonic sequence;
- forward normalized `agent.event` payloads to `on_event`;
- in S1, reject any `tool.call` as a local `TOOL_BRIDGE_ERROR` (no tools in
  S1; `on_tool_call` is wired in S2);
- forward `run.proposed` to `on_proposed` and send exactly the returned closed
  decision; the production Host callback performs the durable compare-and-set
  and never exposes an unclaimed Boolean decision;
- a missing/failing callback is a transient/test-only safety path: after the
  local cancellation check it sends `run.cancel` or `run.discard`, admits no
  result, and returns a safe local error;
- poll `is_cancelled` only before an admission decision or terminal and send at
  most one `run.cancel`;
- enforce the hard deadline and terminate the child after the two-second grace;
- return the closed success/error union defined below;
- convert `run.error`, premature EOF, non-zero exit, and timeout into a safe
  result dictionary with the defined error code;
- close pipes and reap the child on every path.

Use Python stdlib `subprocess`, `selectors`, `json`, and `time`; do not add a
Python dependency.

### 11.5 S1 process result contract

`run_pi_agent()` does not raise an expected child, protocol, cancellation, or
deadline failure into the application layer. It returns exactly one of these
closed shapes:

```json
{"ok":true,"result":{"status":"answered","text":"...","control_request":null,"termination_reason":"stop","usage":{},"committed":true}}
```

```json
{"ok":false,"error":{"code":"PI_PROCESS_TIMEOUT","stage":"deadline","message":"Pi Agent timed out","retryable":true}}
```

`result` is the validated `run.final` payload. `error` is either the validated
`run.error` payload or a Python-created safe error. There are no sibling keys,
raw exceptions, child stderr, commands, environment values, or partial model
text in the return value. A cooperative cancellation is a successful terminal
result with `status: "cancelled"`; a child killed after ignoring cancellation
returns `CANCELLED` and is mapped to the same application status in S5.

This narrow union is the infrastructure boundary. Conversion to `AppResult`
belongs to the Host integration slice, not this module.

### 11.6 Node runtime detailed design

`agent-runtime/main.ts` remains one file. Use only imports from
`node:process`, `@earendil-works/pi-agent-core`, and
`@earendil-works/pi-ai`; package metadata may be imported through the Agent
package's exported `package.json`. Do not add a schema, logging, or stream
library.

The implementation has these module constants:

```text
PROTOCOL = "om-pi-ipc.v1"
MAX_LINE_BYTES = 1_048_576
MAX_SAFE_MESSAGE_CHARS = 240
MAX_FIXTURE_DELAY_MS = 300_000
```

The file is divided into plain functions, not classes:

| Function | Responsibility |
|---|---|
| `readJsonLines(input)` | async generator over binary stdin; split on LF, require strict UTF-8 and a final LF, reject an empty or over-limit line |
| `parseEnvelope(line, expectedSeq, identity, allowedTypes)` | parse a closed envelope; validate protocol, IDs, positive contiguous sequence, type, and object payload |
| `validateStart(payload)` | validate the complete S1 `run.start` payload before any Agent work |
| `emit(type, payload)` | serialize one closed envelope, increment the Node sequence, and await stdout drain when required |
| `safeError(code, stage, message, retryable)` | construct only an allowlisted, length-bounded public error payload |
| `createFixtureStream(model, options, debug)` | return a Pi `AssistantMessageEventStream` immediately and produce a deterministic response without network access |
| `normalizeAgentEvent(event)` | return one normalized `agent.event` payload or `null`; never pass through a Pi object |
| `extractTerminal(agent, cancelRequested)` | inspect the last assistant message and return a final or safe error decision |
| `pumpInbound(lines, identity, agent, state)` | consume post-start input; accept cancellation and the one admission decision expected by current state, and call `agent.abort()` once |
| `main()` | own the one-run lifecycle, terminal emission, stdin shutdown, and exit code |

`readJsonLines()` buffers `Buffer` chunks and searches for byte `0x0a`; it does
not use `readline`, because `readline` cannot enforce the byte ceiling before a
newline arrives. A strict `TextDecoder("utf-8", {fatal: true})` rejects invalid
input instead of silently inserting replacement characters. CRLF is accepted
by removing one trailing `0x0d`; blank records are not accepted.

`validateStart()` enforces the full contract from section 5 and these S1
restrictions:

- `execution_environment` is `eval`;
- `session_id` is `null`;
- `tools` and `recovered_observations` are empty arrays;
- `debug` has exactly `fixture_response` and `delay_ms`;
- `fixture_response` is a string and `delay_ms` is an integer from `0` through
  `300000`;
- `system_prompt` and `user_message` are non-empty;
- all top-level payload keys are known and every nested value used by the
  common contract has the declared type.

Python also requires non-empty `request_id` and `run_id`, a positive integer
function `timeout_seconds`, equality between that argument and
`limits.timeout_seconds`, and `model.timeout_seconds <= limits.timeout_seconds`.

The S1 `run.start` payload is closed and every field is required:

| Field | Accepted S1 value |
|---|---|
| `execution_environment` | literal `"eval"` |
| `session_id` | `null` |
| `system_prompt` | non-empty string |
| `runtime_context` | array of closed `{"role":"system","content": non-empty string}` objects |
| `user_message` | non-empty string |
| `model` | closed object with non-empty string `provider`, `model`, and `base_url`, allowed `api_kind`, plus positive integer `timeout_seconds`, `context_window_tokens`, `max_output_tokens`, and `max_attempts` |
| `tools` | empty array |
| `limits` | closed object with positive integer `timeout_seconds`, `max_iterations`, `max_tool_calls`, `max_context_tokens`, `max_consecutive_failed_tool_batches`, and `final_answer_reserve_seconds` |
| `recovered_observations` | empty array |
| `debug` | closed object with string `fixture_response` and bounded integer `delay_ms` |

`base_url` must parse as an absolute `http` or `https` URL even though the S1
fixture never uses it. Boolean values are not accepted as integers on either
side. Strings are not silently trimmed or coerced; callers supply canonical
values.

This validation is deliberately duplicated at the process boundary: Python
rejects an invalid caller before spawn, while Node refuses an invalid or
compromised peer before Agent execution.

After `run.accepted`, `main()` creates one real Pi `Agent`:

```text
initialState.systemPrompt = effectiveSystemPrompt(start.system_prompt, start.runtime_context, [])
initialState.tools = []
streamFn = createFixtureStream
toolExecution = "sequential"
```

The fixture does not need a second model registry. Pi's supplied model argument
provides the `api`, provider, and model identifiers for the synthetic assistant
message. The stream producer performs exactly this protocol:

```text
start(empty pending assistant message)
text_start
wait delay_ms or abort signal
  success -> one text_delta -> text_end -> done(stop, final message)
  abort   -> error(aborted, fixed safe error message)
```

It uses `createAssistantMessageEventStream()` and catches its own asynchronous
producer failure, converting it to an in-band `error` event. The `streamFn`
never rejects; that is a Pi `StreamFn` requirement. All fixture usage and cost
counters are zero.

Subscribe before calling `agent.prompt()`. The subscription awaits `emit()`,
so Pi event order and stdout order remain identical. Normalize only:

| Pi event | `om-pi-ipc.v1` event |
|---|---|
| `agent_start` | `agent_start`, `{}` |
| `turn_start` | `turn_start`, `{}` |
| assistant `message_end` | `model_turn_completed`, with normalized stop reason and usage |
| `tool_execution_start` / `tool_execution_end` | reserved for S2; impossible in S1 because tools are empty |
| `turn_end` | `turn_end`, with normalized stop reason and usage |
| `agent_end` | `agent_end`, `{}` |

Ignore user message start/end, assistant message start/update, `text_start`,
text deltas, `text_end`, thinking deltas, and all other upstream fields. Usage
contains only non-negative finite `input`, `output`, `cacheRead`,
`cacheWrite`, and `totalTokens` numbers. No partial answer, cost, reasoning,
raw provider, diagnostics, or error text crosses the event boundary.

After subscribing, `main()` invokes `agent.prompt()` and retains its promise;
this synchronously creates Pi's active run before control input can be handled.
It then starts `pumpInbound()` and awaits the prompt promise. EOF while the
Agent is active is a protocol failure. A valid `run.cancel` sets a Boolean once
and calls `agent.abort()` once; a duplicate cancel is a protocol error. If the
pump finds an error, it stores that safe error and aborts the Agent so `main()`
can emit one terminal `run.error`. The pump is not awaited after a terminal
decision; `main()` destroys stdin so the child has no remaining read handle.

After `agent.prompt()` settles, terminal precedence is:

1. inbound protocol failure -> `run.error`;
2. final assistant `stopReason == "aborted"` plus accepted cancel ->
   `run.final(status="cancelled", committed=false)`;
3. final assistant `stopReason == "error"` -> `run.error(MODEL_ERROR)`;
4. missing assistant message or empty text for `stop`/`length` ->
   `run.error(MODEL_ERROR)`;
5. otherwise emit `run.proposed(status="answered")` and wait for the first
   admission-state input: `run.cancel` discards the buffered candidate and
   emits `run.final(status="cancelled", committed=false)`; `run.commit` or
   `run.discard` emits the answered final with the matching Boolean.

Final text concatenates only assistant `text` blocks. Thinking and tool-call
blocks are never rendered. `run.final` exits `0`; `run.error` is flushed and
exits `1`. An error before a valid start identity is known writes no envelope,
writes only a fixed diagnostic to stderr, and exits `2`; a caller with a known
run maps that failed handshake to `PI_RUNTIME_UNAVAILABLE` or
`PI_PROCESS_EXITED` as defined below.

No filesystem read, Session open, network call, tool execution, or dynamic
module path is permitted in S1.

The upstream behavior this design relies on is version-pinned:

- [`Agent` subscribes with awaited listeners and exposes `prompt()`, `abort()`,
  and state](https://github.com/earendil-works/pi/blob/v0.84.2/packages/agent/src/agent.ts);
- [the loop emits assistant completion before `turn_end` and `agent_end`](https://github.com/earendil-works/pi/blob/v0.84.2/packages/agent/src/agent-loop.ts);
- [the AI stream requires `start` and a terminal `done` or `error`](https://github.com/earendil-works/pi/blob/v0.84.2/packages/ai/src/types.ts);
- [`createAssistantMessageEventStream()` terminates on `done` or `error`](https://github.com/earendil-works/pi/blob/v0.84.2/packages/ai/src/utils/event-stream.ts).

### 11.7 Python process adapter detailed design

`src/infrastructure/pi_agent_process.py` contains the public function and a
small set of private functions. It does not define a service, repository,
protocol class, or custom exception hierarchy.

| Function | Responsibility |
|---|---|
| `run_pi_agent(...)` | validate, start the child, drive the selector loop, return the closed result union, and always reap |
| `_validate_start_payload(payload)` | enforce the same S1 restrictions as Node before spawn |
| `_runtime_command(runtime_entry, environ)` | find Node, verify `>=22.19.0`, resolve the default entry, and return an argv list |
| `_child_env(environ)` | copy only allowlisted names into a new environment mapping |
| `_encode_envelope(type, payload, identity, seq)` | produce one UTF-8 JSONL record and enforce the outbound size ceiling |
| `_consume_stdout(buffer, chunk, state)` | split complete lines, strictly decode/parse/validate them, and retain an incomplete tail |
| `_safe_failure(code, stage, message, retryable)` | build the `{"ok":false,"error":...}` branch without exception text |
| `_stop_child(process)` | terminate, wait one second, kill if required, and wait again |

`runtime_entry=None` resolves to `<repo>/agent-runtime/main.ts` from this
module's path. An injected entry is accepted only for focused tests. The argv is
`[resolved_node, resolved_entry]`, with `cwd` set to the repository root and
`shell=False`.

The monotonic hard deadline starts after input validation and before runtime
resolution, so version checks and spawn time count against the same Scene wall.
If `is_cancelled` is already true before spawn, return `CANCELLED` without
starting Node. A cancellation-check exception returns a fixed
`INTERNAL_ERROR`.

`_runtime_command()` resolves `node` through the supplied environment's `PATH`
and runs `[node, "--version"]` with a two-second timeout bounded by the
remaining hard deadline. Missing executable, unparseable output, a version
below `22.19.0`, or a missing runtime entry returns `PI_RUNTIME_UNAVAILABLE`
before the Agent child is started. This extra process is accepted in V1 because
it turns an otherwise opaque TypeScript startup failure into a stable
diagnostic; remove or cache it only if startup profiling shows material cost.

`environ` is the complete source environment, not an overlay on `os.environ`.
When it is `None`, `os.environ` is the source. `_child_env()` copies only:

```text
PATH LANG LC_ALL TZ
SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS
HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
OM_PI_MODEL_API_KEY OM_PI_SESSION_DB
```

S1 never requires the last two names. Empty values are omitted. No other OM,
broker, Feishu, shell, or user environment value reaches Node.

Spawn with binary `stdin/stdout/stderr`, `bufsize=0`, and no shell. Send
`run.start` as Python sequence `1`, then register stdout and stderr with
`selectors.DefaultSelector`. Drain stderr and discard its bytes; this prevents
a full pipe from blocking the child without allowing raw diagnostics into an
application result or log.

The event loop polls at most every `100ms` and uses `time.monotonic()` for all
deadlines:

1. check `is_cancelled` while no admission decision or terminal has been
   written; when first true, send `run.cancel` with the next Python sequence
   and set a two-second cancellation grace deadline;
2. select until the nearest poll, hard deadline, cancellation deadline, or
   post-terminal exit deadline;
3. read ready pipes as bytes; split stdout on LF and cap every line and
   incomplete buffer at `1_048_576` bytes;
4. validate strict UTF-8, JSON object shape, Node sequence, identity, allowed
   type, and payload before invoking any callback;
5. forward `agent.event.payload` to `on_event` in sequence;
6. for `tool.call`, require `on_tool_call`, call it synchronously, validate its
   dictionary result, and send one `tool.result`; without a callback return a
   local `TOOL_BRIDGE_ERROR`;
7. for `run.proposed`, require one closed `on_proposed` decision and send only
   its matching `run.cancel`, `run.commit`, or `run.discard`; for product runs
   the callback has already claimed that winner in Host SQLite, and the final
   `committed` value must match it; the missing/failing transient path performs
   the local cancellation check, then sends cancel or discard before returning
   a safe local error;
8. after a terminal envelope, close stdin but continue draining until child
   exit; any later stdout record is a test-visible `PROTOCOL_ERROR`;
9. require exit `0` when a child exits normally after `run.final`; preserve a
   valid terminal during forced silent-child cleanup, and preserve a valid
   `run.error` when a normal child exits non-zero.

Payload validation after the envelope is also closed:

- `run.accepted` must report runtime `pi-agent-core`, exact pinned version
  `0.84.2`, and `session_id: null`;
- `agent.event` must match one row of the S1 normalization table, with no
  additional payload or data fields;
- S1 `run.proposed` permits only an answered candidate and uses the same fields
  as `run.final` except `committed`;
- S1 `run.final` permits only `answered` or `cancelled`, requires string `text`,
  `control_request: null`, normalized usage, and Boolean `committed`; answered
  termination is `stop` or `length` and must match the admission decision,
  while cancelled termination is `aborted`, has empty text, and is uncommitted;
- `run.error` must pass the code, stage, message, and retryability allowlists in
  section 11.8;
- `tool.call` must have only non-empty `call_id`, non-empty `tool_name`, and an
  object `arguments`.

Receiving a second `run.accepted`, a lifecycle event before acceptance, a
second proposal, two terminal records, a final before admission, or a terminal
record while a tool callback is outstanding is `PROTOCOL_ERROR`.

An `on_event` or `on_tool_call` exception aborts and reaps the child, then
returns a fixed `INTERNAL_ERROR` or `TOOL_BRIDGE_ERROR`; an `on_proposed`
exception is allowed only outside the production Host callback, performs the
local cancel/discard safety path exactly once, and returns `INTERNAL_ERROR`
after cleanup. Callback exception text is not returned. S1 does not run
callbacks concurrently until S2 introduces its one bounded read worker.

If the hard deadline expires before an admission decision is written, send
`run.cancel` if it was not already sent, wait no more than two seconds, then
terminate/kill and return `PI_PROCESS_TIMEOUT`. After commit/discard has been
written, timeout cleanup sends no contradictory cancel; a missing final after
commit retains the unknown-commit handling in section 15.4. If an explicit
Host cancellation was observed first, forced cleanup returns `CANCELLED`.
`_stop_child()` is also called from a `finally` block whenever the child is
still alive. All selector registrations, pipes, and the child are closed or
reaped on every return path.

After a terminal record, the child gets one second to exit. If it remains
silent and alive, Python terminates/kills and reaps it but returns the already
validated `run.final` or `run.error` unchanged. A fixed safe warning may be
logged; the closed process result gains no diagnostic field. Any additional
stdout record before cleanup remains `PROTOCOL_ERROR`.

### 11.8 Error ownership and mapping

| Condition | Owner | Returned code | Stage | Retryable |
|---|---|---|---|---|
| invalid caller payload | Python | `CONFIG_ERROR` | `config` | false |
| Node missing/too old or entry missing | Python | `PI_RUNTIME_UNAVAILABLE` | `spawn` | false |
| invalid child UTF-8/JSON/envelope/sequence/identity/type | Python | `PROTOCOL_ERROR` | `protocol` | false |
| EOF or write failure before terminal | Python | `PI_PROCESS_EXITED` | `process` | true |
| hard deadline | Python | `PI_PROCESS_TIMEOUT` | `deadline` | true |
| explicit cancellation not completed in grace | Python | `CANCELLED` | `cancel` | false |
| unexpected tool call or callback failure | Python | `TOOL_BRIDGE_ERROR` | `tool` | false |
| process-wide read worker already occupied | Python | `TOOL_BRIDGE_ERROR` | `tool` | true |
| invalid start after identity is known | Node | `PROTOCOL_ERROR` | `protocol` | false |
| fixture used outside eval/invalid runtime config | Node | `CONFIG_ERROR` | `config` | false |
| active Pi writer lease | Node | `SESSION_ERROR` | `session` | true |
| Session path/schema/open/compaction failure | Node | `SESSION_ERROR` | `session` | false |
| Pi final assistant error/empty answer | Node | `MODEL_ERROR` | `model` | true only for model/runtime failures classified retryable |
| unexpected Node defect | Node | `INTERNAL_ERROR` | `runtime` | false |

Python accepts only the Node error codes listed in section 5.3, stages
`protocol`, `config`, `model`, `session`, `tool`, `budget`, and `runtime`, a
Boolean `retryable`, and a single-line message of at most 240 characters.
Anything else becomes `PROTOCOL_ERROR`. Node never copies `Error.message`,
provider bodies, stderr, arguments, prompts, or environment values into the
safe message.

### 11.9 Required event traces

A successful fixture has this exact observable order; the two directions keep
independent sequence numbers:

```text
P1 run.start
N1 run.accepted
N2 agent.event agent_start
N3 agent.event turn_start
N4 agent.event model_turn_completed(stop)
N5 agent.event turn_end(stop)
N6 agent.event agent_end
N7 run.proposed(answered)
P2 run.commit
N8 run.final(answered, committed=true)
child exit 0
```

An empty fixture string returns `run.error(MODEL_ERROR)`. A cooperative
cancellation after `turn_start` is:

```text
P1 run.start
N1 run.accepted
N2 agent.event agent_start
N3 agent.event turn_start
P2 run.cancel
N4 agent.event model_turn_completed(aborted)
N5 agent.event turn_end(aborted)
N6 agent.event agent_end
N7 run.final(cancelled, committed=false)
child exit 0
```

No accepted S1 trace contains `tool.call`, a tool event, Session activity, or a
network request.

### 11.10 Focused tests

`tests/test_pi_agent_process.py` must prove:

1. a valid eval fixture uses the real Pi `Agent`, emits ordered lifecycle
   events, commits through the proposal callback, and returns the exact fixture
   text; a discarded proposal returns `committed:false`;
2. Unicode and embedded newlines remain one valid JSONL record;
3. malformed child JSON fails as `PROTOCOL_ERROR` without hanging;
4. a mismatched run ID or duplicate/gapped sequence fails closed;
5. premature EOF maps to `PI_PROCESS_EXITED`;
6. a delayed fixture can be cancelled and the child is reaped;
7. a never-finishing fixture reaches `PI_PROCESS_TIMEOUT` and the child is
   reaped;
8. oversized lines, invalid UTF-8, trailing partial records, output after a
   terminal record, and a stderr stream larger than the pipe buffer fail or
   drain without hanging;
9. a missing/old Node runtime and callback failures use the exact safe mapping;
10. cancellation before/during admission sends no commit/discard, while a
    cancel observed after a complete decision write cannot change that result;
11. a child that emits a valid terminal and then hangs is forcibly reaped
    without changing the validated result;
12. no test requires network access, model credentials, OM runtime data, or a
   writable production path.

The happy-path and cancellation tests use `agent-runtime/main.ts` and therefore
prove the real Pi `Agent`. Malformed-child tests may inject one minimal
temporary runtime entry through `tmp_path`; this is the only reason
`runtime_entry` is injectable. One parameterized protocol test covers malformed
records instead of creating a test per parser branch.

Tests collect `on_event` payloads in a list and assert the complete order above.
They also compare `Popen.poll()` after each terminal path so "returned" cannot
hide an unreaped child. The valid fixture child receives no credential or
provider configuration and succeeds only through the injected stream function.

### 11.11 S1 commands and definition of done

```bash
npm ci --ignore-scripts --prefix agent-runtime
./.venv/bin/python -m pytest -q tests/test_pi_agent_process.py
```

S1 is complete only when:

- the pinned packages install from the lockfile;
- Node version failure is explicit and safe;
- the implementation matches the two required event traces message-for-message
  at the JSONL boundary;
- all focused tests pass repeatedly;
- `git diff --check` passes;
- no existing Python import or CLI behavior changed;
- the diff contains no provider call, tool bridge, Session database, channel
  wiring, release change, or legacy deletion.

## 12. S2 Read-Only Tool Bridge Detailed Design

### 12.1 Objective and boundary

S2 proves that a real Pi `Agent` can select and call OM's canonical read tools
through the process boundary. It does not move a production entrypoint yet.
`./om copilot eval` remains on its current implementation until the shared Host
call site changes in S5; this avoids creating a temporary second routing mode.

S2 changes only the three S1 implementation files and their focused test:

```text
agent-runtime/main.ts
src/infrastructure/pi_agent_process.py
tests/test_pi_agent_process.py
```

No tool catalog, executor, Scene, channel, or Control module is duplicated.

### 12.2 Eval fixture and Pi tool construction

For deterministic tests, the eval-only `debug` object gains `fixture_turns`, an
array of closed objects. Each fixture turn contains exactly one of:

```json
{"text": "最终回答"}
{"tool_calls": [{"call_id": "call_1", "tool_name": "runtime_status", "arguments": {}}]}
```

The fixture stream consumes one turn per provider request. It must still emit a
valid Pi assistant stream and never bypass `Agent.prompt()`, schema validation,
tool hooks, or lifecycle events. `debug` remains forbidden outside `eval` and
is not a production provider abstraction.

`main.ts` maps each `run.start.tools[]` item to one `AgentTool`:

| Pi field | Source |
|---|---|
| `name` | Host-projected tool name |
| `label` | same name; no second display catalog |
| `description` | Host-projected description |
| `parameters` | Host-projected JSON Schema, structurally treated as Pi `TSchema` |
| `executionMode` | literal `"sequential"` |
| `execute` | send `tool.call`, await the matching `tool.result` |

The Agent also sets global `toolExecution: "sequential"`. Pi performs JSON
Schema validation before `execute`; Python repeats the Host allowlist and
canonical payload validation because the process boundary is untrusted. No
`prepareArguments`, update callback, built-in Pi tool, or second schema package
is introduced.

### 12.3 Outstanding-call state and result encoding

Node holds at most one pending record:

```text
call_id -> {tool_name, resolve, reject, abort_listener}
```

`execute()` fails before writing when another call is pending. It sends the
closed `tool.call`, then waits for the matching `tool.result` or Agent abort.
An accepted result becomes:

```text
content = [{type: "text", text: stable JSON of compact observation}]
details = {observation: compact observation}
terminate = false
```

`observation.ok == false` is promoted to `isError: true` by a global
`afterToolCall` hook that returns `{ isError: true }` when
`details.observation.ok === false`; this preserves the compact content and
details while letting the model repair invalid arguments. `execute()` returns
the same normal result for `ok: true` and `ok: false` and never throws for a
valid observation: throwing would make Pi replace `content` with the thrown
message, breaking the compact-observation guarantee. Raw executor output never
enters content, details, Session, or IPC. Cancellation rejects the pending
promise, removes its listener, and prevents a later result from being accepted.

Python changes `run_pi_agent(..., on_tool_call=...)` from a synchronous callback
inside the selector loop to one daemon worker for the single outstanding read.
The process adapter owns one module-level active-worker slot protected by a
`threading.Lock`; it does not create a pool or queue additional callbacks.
Before starting a worker, the selector claims the empty slot. If another live
worker owns it, the run fails closed with retryable `TOOL_BRIDGE_ERROR` and
starts no second thread.

The worker reports through a stdlib queue; only the selector thread writes
JSONL. It releases the active slot in `finally` after the callback actually
returns. A timed-out read cannot be forcibly killed in Python: the adapter
marks it abandoned, discards its late value, and leaves it holding the sole
slot until real exit. This deliberately caps V1 at one process-wide read
worker. Only measured concurrency pressure justifies a later bounded
multi-worker or killable tool-process design.

The Host callback used from S5 performs exactly:

```text
verify manifest allowlist and pure_read capability
-> tools.build_tool_payload(tool_name, arguments, contract)
-> tools.call_read_tool(tool_name, payload, allowed_tools)
-> tools.compact_observation(tool_name, result)
```

`execute()` throws only for bridge failures: an unknown/non-read tool, callback
exception, protocol mismatch, or invalid callback return. These are terminal
`TOOL_BRIDGE_ERROR`; the run fails closed without committing a successful turn,
and callback exception text is discarded rather than becoming a model-visible
compact observation.

### 12.4 Budget behavior

Pi supplies the loop; OM supplies only its existing Scene ceilings. `main.ts`
tracks assistant turns, finalized tool calls, and consecutive all-failed tool
batches from Pi events. `prepareNextTurnWithContext()` replaces
`context.tools` with an empty array for one forced-final turn when continuing
would exceed any of:

- `max_iterations`;
- `max_tool_calls`;
- `max_consecutive_failed_tool_batches`;
- the Scene's final-answer time reserve.

`shouldStopAfterTurn()` stops after that forced-final turn. If no non-empty
assistant text is produced, the terminal result is
`BUDGET_EXHAUSTED`; it is never reported as a successful empty answer. The
hard timeout remains Python-owned and calls `Agent.abort()` before process
cleanup. S2 does not recreate duplicate-call caching, semantic routing, or a
planner around Pi.

The two closed `run.start.limits` fields
`max_consecutive_failed_tool_batches` and `final_answer_reserve_seconds` become
active in S2 and are copied from the current Scene contract. Callers may
reduce, never raise, those values.

### 12.5 S2 event normalization

Tool events are normalized without arguments or observations:

| Pi event | IPC data |
|---|---|
| `tool_execution_start` | `call_id`, `tool_name` |
| `tool_execution_end` | `call_id`, `tool_name`, `ok` |
| failed `turn_end` tool batch | stop reason and aggregate counts only |

The full tool call and compact observation travel only in `tool.call` and
`tool.result`. Text/thinking deltas and raw Pi details stay suppressed.

### 12.6 S2 focused acceptance

Extend `tests/test_pi_agent_process.py` to prove:

1. a fixture tool call crosses to Python, executes the callback once, returns
   to Pi, and leads to the next fixture answer;
2. invalid arguments are rejected by Pi schema validation without invoking the
   callback;
3. Python rejects a tool outside the current allowlist even when a malicious
   child requests it;
4. two tool calls in one model message execute in source order and never overlap;
5. compact `ok:false` results are visible to the next fixture turn as error tool
   results;
6. cancellation or timeout while waiting for a tool reaps Node, returns once,
   discards the late worker value, and leaves the live worker holding the one
   process-wide slot;
7. call-ID/name mismatch, duplicate result, second outstanding call, and result
   after cancel fail as `PROTOCOL_ERROR`;
8. iteration, tool, failed-batch, and final-answer-reserve limits allow at most
   one tool-free final turn and otherwise return `BUDGET_EXHAUSTED`;
9. serialized content contains only the compact observation and no raw tool
   payload, secret, path, or exception text;
10. a never-returning callback followed by repeated new runs creates no second
    worker; later calls fail retryably until the original worker exits.

S2 exits when the focused test file passes without network access and a source
search proves `main.ts` enables no Pi built-in tool. No CLI behavior changes in
this slice.

## 13. S3 Session, Memory, And Context Detailed Design

### 13.1 Objective and files

S3 gives new Pi-backed conversations durable, sender-and-config-isolated
history and token-aware compaction. It does not migrate legacy memory, add user
profiles, or introduce cross-user learning.

Modify only:

```text
agent-runtime/package.json
agent-runtime/package-lock.json
agent-runtime/main.ts
src/infrastructure/pi_agent_process.py
tests/test_pi_agent_process.py
```

Add the pinned
`@earendil-works/pi-session-backend-sqlite-node@0.84.2` package. Session logic
stays in `main.ts` until its measured size makes a split useful.

### 13.2 Session open and lease lifecycle

When `session_id` is non-null, Node creates:

```text
await using repository = new SqliteSessionRepository({
  env: new NodeExecutionEnv({cwd: repositoryRoot}),
  sqlite: createNodeSqliteFactory(),
  databasePath: OM_PI_SESSION_DB,
  writerLease: {ttlMs: 30_000, heartbeatIntervalMs: 10_000}
})
```

`NodeExecutionEnv` is imported from
`@earendil-works/pi-agent-core/node`; no OM filesystem adapter is added.
`repositoryRoot` is the absolute child working directory set by Python. Node
calls `repository.list()`, finds metadata whose
`id` equals `session_id`, then calls `repository.open(metadata)`; when absent it
calls `repository.create({id: session_id, cwd: repositoryRoot,
metadata:{schema:"om-pi-session.v1"}})`. The V1 catalog lookup is O(n); replace
it only if measured session volume makes it material.

The runtime uses only lane `main`. Normal repository dispose releases the
Session storage and SQLite writer lease. A missing/unwritable database,
duplicate/raced create, unknown session metadata, or schema/open failure
returns non-retryable safe `SESSION_ERROR` before the main model call. An
active-writer conflict returns retryable `SESSION_ERROR` with a fixed
`session is temporarily busy` message; no upstream path or lease owner leaks.
Transient sessions do not open the repository.

The existing `CopilotHostStore.acquire_session_run()` lease remains the outer
application lock. The Pi writer lease protects storage integrity if another
process bypasses that Host lock; it is not used as a new concurrency scheduler.

### 13.3 Rewind and committed context load

The load sequence is fixed:

1. open/create the Session and locate lane `main`;
2. read branch entries oldest-first and find the last valid
   `om.turn.commit.v1` marker;
3. call `Session.moveLane("main", marker.id)` or move to `null` when none exists;
4. read the rewound branch again with `{order: "oldestFirst"}`;
5. call Pi `buildSessionContext(entries)`;
6. use only its `messages` as persisted conversation history.

Malformed custom markers are ignored as uncommitted data; duplicate commit
markers are harmless, and the last reachable valid marker wins. The current
system prompt, runtime context, recovered observations, and current user
message are then added ephemerally in the order defined by section 6.4.

The runtime does not persist system prompts, current holdings, pending-Control
snapshots, recovered observations, provider errors, or incomplete turns.

### 13.4 Successful turn persistence

Immediately before `Agent.prompt()`, Node records the current
`agent.state.messages.length`. After the prompt promise settles, it buffers the
suffix from that index and validates complete user/assistant/tool-result
groups. It writes that suffix only after all tool executions have completed,
the terminal outcome is `answered` or `control_requested`, and Python later
sends `run.commit`:

1. current user message;
2. each assistant and tool-result message emitted by Pi in source order;
3. `Session.appendCustomEntry("om.turn.commit.v1",
   {run_id, kind:"turn"})`.

The current user message is written at commit time, not before the model call.
`run.discard`, cancellation, model error, timeout, or protocol failure writes no
turn entry. A crash during the committed append sequence leaves a tail which
the next run rewinds away. Host audit remains the record of failed attempts.

Compact observations inside tool-result messages are already redacted by
Python. The Session receives no Host event or outbox data.

### 13.5 Compaction policy

Before prompting, Node estimates committed history with
`estimateContextTokens()`, the effective system prompt with Pi AI's
`estimateTextTokens()`, and the current user message with `estimateTokens()`.
It computes:

```text
context_window = min(model.contextWindow, limits.max_context_tokens)
fixed_input_tokens = estimateTextTokens(effective_system_prompt)
                   + estimateTokens(current_user_message)
history_tokens = estimateContextTokens(committed_history).tokens
context_tokens = fixed_input_tokens + history_tokens
reserve_tokens = model.maxTokens
usable_history_tokens = context_window - reserve_tokens - fixed_input_tokens
keep_recent_tokens = max(2_000, floor(usable_history_tokens / 2))
```

Configuration is invalid when `usable_history_tokens <= 2_000`.
`shouldCompact(context_tokens, context_window, settings)` decides whether
compaction is needed, with `settings.reserveTokens = reserve_tokens` and
`settings.keepRecentTokens = keep_recent_tokens`. When true, Node calls
`prepareCompaction(entries, settings)` then `compact(...)` using the same
selected model, API key, timeout signal, and provider request ceilings as the
main run. Pi's outer compaction retry is disabled so it cannot multiply OM's
provider attempt limit. OM's fixed custom instructions are those in section
6.4.

On success, Node constructs
`{type:"compaction", ...compactResult.value}` and calls
`Session.appendEntry(compactionEntry, "main")`, then appends
`Session.appendCustomEntry("om.turn.commit.v1",
{run_id, kind:"compaction"})`. It reloads through
`buildSessionContext()` and rechecks the token estimate before prompting. If
the compacted total still exceeds `context_window - reserve_tokens`, the run
returns `SESSION_ERROR`; it does not run a second compaction loop.

This marker commits only the maintenance rewrite of history that was already
committed before the current request. It remains valid if the later current
turn is rejected, cancelled, times out, or crashes. `run.commit` and
`run.discard` never append, remove, or reinterpret this pre-run checkpoint;
they govern only the current user/assistant/tool-result suffix from section
13.4.

On summarization failure, no compaction commit is written. The old committed
branch remains active. The run may continue only if the old context plus
current request still fits; otherwise it returns `SESSION_ERROR` with stage
`session`. No character trimming, oldest-message deletion, or second
summarizer exists.

### 13.6 Session identity and channel handoff

`pi_agent_process.py` owns one helper,
`derive_pi_session_id(channel, sender, conversation, authority_scope)`,
implementing the hash in section 6.1. It rejects empty channel, sender,
conversation, or authority scope parts. S3 tests call it directly;
`channel_facade.py` starts using it only in S6, so production memory does not
split before the atomic cutover.

Local diagnostics persist only with an explicit session key. Eval fixtures are
transient by default. Session database paths are passed by Python through the
allowlisted `OM_PI_SESSION_DB` environment variable and never accepted from a
model or tool argument.

### 13.7 S3 focused acceptance

Focused tests use a temporary SQLite file and prove:

1. two successful turns in one session expose the first committed transcript
   to the second Pi fixture;
2. two sender hashes in the same conversation cannot read each other;
3. one sender/conversation with different trusted key or canonical-path scopes
   cannot read the other scope's assistant or tool-result history;
4. transient and eval-default runs create no database;
5. a child killed after each individual append point leaves no partial message
   in the next model context;
6. a later successful turn branches from the last commit marker, not from an
   abandoned partial tail;
7. unordered/newest-first reads cannot accidentally be passed to
   `buildSessionContext()`;
8. compaction persists summary, retained tail, usage, then a commit marker and
   preserves the current request/tool-call groups;
9. after successful pre-run compaction, admission rejection, cancellation, or
   child crash keeps that compaction marker but leaves no current-turn message;
10. failed compaction keeps the previous committed branch and fails closed only
   when the full context cannot fit;
11. runtime context, recovered observations, errors, and Control snapshots do
   not appear in later Session reads;
12. cooperative success, error, and cancellation release both Host and Pi
    writer leases immediately;
13. forced child termination releases the Host lease but the Pi lease rejects a
    second writer before TTL, permits fenced takeover after TTL, and never
    requires Python to delete the writer row.

S3 exits when these tests pass repeatedly against the real pinned SQLite
backend and inspecting the database shows only Pi entries plus the OM commit
marker.

## 14. S4 Provider Integration Detailed Design

### 14.1 Objective and files

S4 replaces OM's hand-written provider clients with Pi AI provider streams
while keeping provider selection and secret contracts stable. It adds one
required, operator-declared safe context-window capability to the existing
model profile. It does not add provider discovery, OAuth login, live model
catalogs, or a second config file.

Modify:

```text
agent-runtime/main.ts
src/infrastructure/pi_agent_process.py
src/application/copilot/model_config.py
src/application/copilot/local_harness.py
src/application/assistant/llm_model_profiles.py
src/application/config_validator.py
src/application/config_yaml.py
src/interfaces/cli/assistant_ops.py
configs/system.json
configs/examples/config.yaml.example
tests/test_pi_agent_process.py
tests/test_config_yaml.py
tests/test_validate_config_notifications.py
tests/test_cli_operator_commands.py
```

`src/application/llm_provider_registry.py` remains the public provider catalog.
It changes only if a test exposes a missing existing fact; Pi-specific names do
not enter it.

### 14.2 Python model normalization and secret handoff

Move the small immutable settings value from the retiring `model_client.py` to
`model_config.py` and rename it `PiModelSettings`. It contains exactly:

```text
provider, api_kind, model, base_url,
api_key_env, credential_name,
timeout_seconds, context_window_tokens, max_output_tokens, max_attempts
```

`PiModelSettings.from_config(raw)` reuses `require_provider_spec()`, the current
bounds, default base URLs, and credential names. For OpenAI, an empty OM base
URL is normalized to `https://api.openai.com/v1` only in the secret-free
`run.start.model`; the user-facing config remains unchanged. Python maps the
registry's `responses` to `openai-responses` and `chat_completions` to
`openai-completions`. Node accepts only the provider/API pairs in section 8.

`context_window_tokens` is required for every authoring profile that can become
active and is copied unchanged into generated `assistant.llm`. It is an
OM-verified safe capability, so it may be lower than the provider's advertised
maximum. It must be an integer from `4_096` through `2_000_000` and greater
than `max_output_tokens + 2_000`. Missing values fail configuration and setup
before Node spawn; OM never guesses from a model name or silently substitutes
the Scene limit. Repository examples use the existing conservative
`24_000`-token policy cap.

The migration is explicit: add `assistant.llm.context_window_tokens: 24000` to
`configs/system.json`, add `context_window_tokens: 24000` to every shipped
`assistant.models.<profile>` example, include the field in
`LlmModelProfile.llm_config()` and its public non-secret payload, and preserve
it through `build-assistant` into resolved runtime config. Existing user YAML
profiles are not silently defaulted: an enabled Copilot profile must be edited
and rebuilt before setup/preflight passes.

`./om assistant model add` gains required
`--context-window-tokens`; it passes the value directly through
`add_model_profile_to_config()` and the same validator. `model list/current`
show the declared value. No provider-specific default table or discovery call
is added.

`model_api_key_configured()` validates through `PiModelSettings` instead of
importing `CopilotModelSettings`. A new private `_resolve_model_api_key()` in
`model_config.py` reuses `resolve_secret()` and returns the credential only to
`local_harness.py`. That value is passed to `run_pi_agent()` as
`OM_PI_MODEL_API_KEY`; it is never inserted into the payload, contract,
decision trace, event, or exception text. Ollama resolves no user secret.

`local_harness._resolve_model_runner()` becomes `_resolve_pi_model()`. Existing
precedence remains:

```text
eval model_turn_json
or explicit model_config_json
or assistant_config_path
or MODEL_REQUIRED
```

The eval script is converted mechanically to S2 `debug.fixture_turns`. The two
real configuration sources return `(PiModelSettings, api_key, error)` and do
not construct a Python model callable.

### 14.3 Selected Pi model/provider

For each run, `main.ts` constructs one `Model` and one provider, adds that
provider to `createModels()`, and uses `models.streamSimple()` as the Agent
stream. It does not initialize the built-in global provider catalog.

The generated model has:

```text
id/name           = OM model
provider          = OM provider id
api               = openai-responses or openai-completions
baseUrl           = normalized OM base URL
reasoning         = false
input             = ["text"]
cost              = all zero (OM does not claim unverified price metadata)
contextWindow     = model.context_window_tokens
maxTokens         = model.max_output_tokens
```

`reasoning=false` preserves the current product behavior: private reasoning is
not requested, streamed, stored, or exposed. Image input remains out of scope.

Provider construction is exact:

| OM profile | Pi API implementation | Request options |
|---|---|---|
| `openai` | `openAIResponsesApi()` | `temperature: 0` |
| `deepseek` | `openAICompletionsApi()` | `temperature: 0`, `samplingParams.thinking={type:"disabled"}` |
| `kimi` | `openAICompletionsApi()` | omit temperature and thinking |
| `kimi-code` | `openAICompletionsApi()` | omit temperature and thinking |
| `ollama` | `openAICompletionsApi()` | `temperature: 0`, internal placeholder key `ollama` |

For non-OpenAI completions mappings, set compatibility overrides
`supportsStore:false`, `supportsDeveloperRole:false`, and
`supportsReasoningEffort:false`; retain Pi's URL-derived defaults for the other
wire details unless the contract fixture proves an existing provider needs an
override. Kimi Code deliberately does not use Pi's built-in
`kimiCodingProvider()`, whose Anthropic Messages endpoint differs from OM's
existing API contract.

The one selected provider wraps its Pi API implementation and injects, in both
`stream` and `streamSimple`, on every provider request:

```text
apiKey, signal, timeoutMs,
maxTokens = model.max_output_tokens,
maxRetries = model.max_attempts - 1,
maxRetryDelayMs bounded by the remaining Scene deadline,
temperature and samplingParams from the table
```

Pi's OpenAI adapters disable SDK retries and apply this explicit retry count,
so `max_attempts` remains the total provider-attempt ceiling. The same bounded
provider wrapper is used by main Agent turns and compaction. Pi's additional
outer compaction retry is disabled, preventing nested retries from exceeding
that ceiling. Retry callbacks emit only attempt counts and safe categories,
never provider error text.

### 14.4 Provider response and error normalization

Pi owns native tool-call parsing and streaming. Node counts usage once per
completed assistant message and reports only non-negative finite token fields.
`run.final.usage` is the sum of main turns plus committed compaction calls;
Host events distinguish `usage` for a turn from `usage_total` for the run.
Cost and reasoning tokens are not emitted in V1.

Final Pi stop reasons map as follows:

| Pi stop reason | OM process outcome |
|---|---|
| `stop` | answered when final text is non-empty |
| `length` | one Pi continuation while budget remains; otherwise the accumulated non-empty text is answered with `termination_reason:length` |
| `toolUse` | normal loop continuation |
| `aborted` | cancelled only after an accepted Host cancel; otherwise model error |
| `error` | `MODEL_ERROR` |
| `deferred` | `MODEL_ERROR`; deferred mode is not enabled |

Use Pi's `isRetryableAssistantError()` only to set the safe retryable Boolean.
The public message is fixed by category (`model authentication failed`, `model
request failed`, `model response was invalid`) and never copies
`errorMessage`, HTTP bodies, headers, URLs with credentials, or stderr.

### 14.5 S4 provider contract acceptance

Extend the focused test with a stdlib loopback HTTP server. No external network
or real key is used. For each profile, assert the actual Pi request path and
body:

1. OpenAI uses `/responses`, instructions/input, Responses function tools, and
   the configured output ceiling;
2. DeepSeek uses `/chat/completions`, system-role messages, disabled thinking,
   and native function tools;
3. Kimi omits temperature/thinking and preserves its base URL;
4. Kimi Code uses `/coding/v1/chat/completions`, not Anthropic Messages or
   `/coding`;
5. Ollama works without a configured user key and sends temperature zero;
6. all profiles convert assistant tool calls and tool results back into the
   next provider request correctly;
7. timeout, retryable 429/5xx, non-retryable auth failure, malformed stream,
   abort, and retry exhaustion map to the closed safe result;
8. `max_attempts` is the observed request ceiling and usage is counted once;
9. missing/invalid `context_window_tokens` fails before spawn; valid values
   below, equal to, and above the Scene cap produce
   `min(model.contextWindow, limits.max_context_tokens)` as the effective
   context budget;
10. `assistant model add` requires and round-trips the declared capability, and
    build-assistant preserves it in resolved `assistant.llm`;
11. captured payloads and emitted events contain no `OM_PI_MODEL_API_KEY` value.

Live provider calls are not part of automated S4. One read-only live canary per
configured production provider is a separate, explicit acceptance action
immediately before release.

## 15. S5 Host Governance And Recovery Detailed Design

### 15.1 Objective and call-site replacement

S5 replaces the one shared generic execution call in
`host.run_contract()` while preserving OM's Host authority. This is the only
application call-site switch; local diagnostics and channels both already
flow through `run_prepared_contract()` and `run_contract()`.

Modify:

```text
src/application/copilot/host.py
src/application/copilot/local_harness.py
src/application/copilot/host_store.py
src/application/copilot/event_store.py
src/interfaces/cli/copilot_ops.py
src/infrastructure/pi_agent_process.py
tests/test_copilot_phase1.py
tests/test_copilot_conversation_memory.py
tests/test_pi_agent_process.py
```

No public command changes. `engine.py`, `model_client.py`, and
`conversation_memory.py` remain present but unreachable until S7 deletes them.
There is no environment flag or fallback selecting them.

### 15.2 `local_harness` and `host.run_contract`

`run_prepared_contract()` keeps its public arguments, optional toolset loading,
and error wording. It stops calling `prepare_contract_with_existing_memory()`
and passes these resolved values to `run_contract()`:

```text
PiModelSettings or eval debug fixture
resolved API key (process environment only)
opaque session_key
control preview specs
resume source and recovered observations
```

`run_contract()` keeps contract validation, `start_run()`, Scene construction,
tool projection, cancellation checks, result admission, final event, and
`finish_run()`. It removes the `ModelRunner` and `run_engine()` imports and
builds the closed `run.start` payload from the manifest:

| Payload field | Host source |
|---|---|
| system prompt | compiled Scene system messages |
| runtime context | current non-static system context from the ExecutionContract |
| user message | `contract.input.user_message` |
| model | validated `PiModelSettings` without credential fields |
| tools | `_manifest_with_tool_descriptions()` projection |
| limits | Scene limits, only reduced by request limits |
| recovered observations | bounded successful Host recovery observations |
| session id | derived sender-and-config/local-and-config scoped key |

The derived Host/runtime sibling database path is passed only as the
allowlisted `OM_PI_SESSION_DB` child environment value, never in `run.start`.

Host validates `manifest.messages` as one leading static system message, zero
or more ephemeral system messages, and exactly one final user message equal to
`contract.input.user_message`. The leading content becomes `system_prompt`,
the other system contents become `runtime_context`, and the user content
becomes `user_message`. Any assistant/tool/extra user message is a Scene
preparation failure after S6 removes legacy history loading.

Static prompt, runtime context, and prior Session history are sent in separate
fields; the Host must not also copy legacy history into `manifest.messages`.
Prompt/tool/schema/provenance hashes continue to come from the Scene manifest.

### 15.3 Tool callback and run-local evidence

Define one private closure inside `run_contract()` for
`run_pi_agent(on_tool_call=...)`. It rechecks cancellation, the manifest
allowlist, and `pure_read`; builds the canonical payload; records `tool_call`;
executes the current tool adapter; compacts the result; assigns a visible
reference; records `tool_result`; and returns that compact observation.

Because the process adapter may abandon its single daemon worker, Host creates
one `threading.Lock`, a run-local `tool_events_open` Boolean, and one private
`_record_event()` closure. Every `event_log.record()`/Host event write from
`run_pi_agent(on_event=...)`, the tool worker, cancellation, and finalization
uses that same lock. Cache lookup/mutation and gate close use it too. The tool
callback validates and records `tool_call` while holding the lock, releases it
for the actual read, then reacquires it and records cache/result only if the
gate is still open. The lock is never held across tool I/O or child-process
waiting.

Immediately after `run_pi_agent()` returns, and before `_finalize()`, the main
Host path closes the gate under the same lock. By then the selector-side
`on_event` calls are complete; an abandoned worker can still finish its read,
but it cannot append an event or mutate run-local evidence. This serializes the
existing JSON read-modify-write store without adding an event table, queue, or
worker manager.

A run-local dictionary keyed by stable JSON of `(tool_name, canonical_payload)`
reuses a prior successful compact observation and records
`tool_result_reused`. It is only a read-cost/idempotency guard, not memory or a
tool framework. Failed calls may be retried by Pi within the Scene failure and
call ceilings. The cache dies with `run_contract()`.

The legacy private `__read_observation__` paging tool is retired: Node never
receives raw responses to page. A tool that needs paging must expose it through
its canonical public input/output contract. The Engine's keyword-based
`fresh_evidence_recheck` is also retired; the single Scene prompt, current-run
tool evidence, and answer-quality gate own that behavior. It is not rebuilt as
another semantic router around Pi.

### 15.4 Proposed-result commit handshake

Pi Session must not remember a result OM rejects. Before any successful
`answered` or `control_requested` outcome is persisted, Node sends non-terminal
`run.proposed` with the candidate `run.final` payload. Python calls an
`on_proposed` callback supplied by Host. S5 adds one private
`copilot_runs.admission_state` column with the closed values
`open|cancel|commit|discard`, default `open`, plus the idempotent migration in
`CopilotHostStore` initialization. It is operator-only state and does not alter
the public run status contract.

`CopilotHostStore.request_cancel(run_id)` and the new
`claim_admission_decision(run_id, desired)` both use `BEGIN IMMEDIATE` and a
conditional update from `admission_state='open'`. `desired` is only `commit` or
`discard`; `request_cancel()` claims `cancel` only while the public run status
is active. Exactly one call can return accepted. A cancel after commit/discard,
or a decision after cancel, returns the already selected state without
overwriting it. Finalization atomically closes any still-`open` terminal failure
as `discard`, without turning it into an admitted result. The public cancel
command reports failure as “该运行不存在、已作出准入决定或已进入终态”, rather
than claiming cancellation succeeded.

The method contracts are deliberately small: `request_cancel()` keeps its
Boolean public result, while
`claim_admission_decision(run_id, desired) -> Literal["commit", "discard",
"cancel"]` returns the stored winner after the transaction. A missing or
already terminal row raises the existing bounded Host-store error; the Host
callback converts it to the safe discard/error path and never returns an
unclaimed accepted result.

The Host callback then:

1. construct the candidate `AppResult` with the current events;
2. call `admit_result_with_decision()`;
3. atomically claim `commit` for accepted or `discard` for rejected;
4. if the claim returns `cancel`, clear/ignore the candidate and return
   `cancel`;
5. otherwise retain the candidate and matching decision in the Host closure
   and return the claimed `commit` or `discard`.

The production callback catches admission exceptions before returning, claims
`discard` when the row remains open, and returns the resulting closed state;
it never exposes a Boolean or an unclaimed exception to the adapter.

Node accepts only one of those messages while in `awaiting_admission`. On
`run.commit`, it persists the buffered current-turn suffix and its turn marker,
then emits `run.final` with `committed:true`. On `run.discard`, it writes no
current-turn Session entry and emits the same candidate with
`committed:false`. A pre-run compaction marker from section 13.5 is already an
independent committed checkpoint and is not rolled back. Python verifies the
flag matches its durable decision; Host returns the retained admitted result,
not an untrusted reconstruction.

Runs cancelled before proposal and failed runs do not propose or commit. If the
protocol write fails after Host claimed `commit`, or the child exits after
Python sends `run.commit`
but before `run.final`, Python cannot prove whether the final commit marker
reached SQLite. It returns `PI_PROCESS_EXITED`, records private
`session_commit_outcome:"unknown"`, queues no reply, and marks the run
non-resumable. The process result union stays unchanged: Host infers this
private marker from its retained accepted proposal plus the missing
`run.final`; it is not a new IPC error field. The next independent inbound turn
opens the same Session: S3 keeps a complete committed turn or rewinds an
incomplete tail. OM does not add a query mode or replay tools merely to resolve
this rare transport ambiguity. A failure before Host claims `commit` remains
normally resumable and is rewound by S3.

The protocol additions are:

```text
Node -> Python: run.proposed
Python -> Node: run.commit | run.discard
run.final: adds required Boolean committed
```

For product runs, Host SQLite is the linearization point and the process
adapter merely delivers the selected winner to Node. For transient diagnostics
without a Host store, the process adapter remains the local single writer. A
cancel winner makes Node discard the buffered turn and emit cancelled with
`committed:false`; a commit/discard winner makes later cancel return not-ready.
Any other message, duplicate decision, or mismatched final flag is
`PROTOCOL_ERROR`.

### 15.5 Event and metric mapping

The Host records canonical events, not the entire Pi stream:

| Normalized IPC event/callback | Host event |
|---|---|
| `agent_start` | `agent_started` |
| `turn_start` | `model_turn_started` with iteration number |
| `model_turn_completed` | existing `model_turn_completed` with stop reason, attempts, retry count, usage, usage total |
| Python tool callback start | existing `tool_call` with canonical safe input |
| Python tool callback finish | existing `tool_result` or `tool_result_reused` |
| compaction committed | `context_compacted` with counts/tokens only |
| forced-final activation | `agent_budget_fallback` |
| accepted cancellation | `run_cancelled` |
| `agent_end` | `agent_terminated` with reason and totals |

Text deltas, raw Pi tool events, thinking, provider payloads, and Session
entries are not appended to `copilot_runs.events_json`. `event_store.py` adds a
public label only for events already user-visible; internal lifecycle events
remain operator-only.

`host_store.append_event()` keeps its existing status state machine and changes
metric extraction only where field names differ. Tool-call count increments on
canonical `tool_result`, not on Pi lifecycle events. Retry count is the maximum
reported cumulative count; usage is the latest `usage_total`. Existing run,
events, progress, cancel, and reply inspection commands therefore keep their
public shapes.

### 15.6 Safe error mapping

Process-specific codes remain private in the Host event/decision trace. The
public `AppResult.error.code` maps to the existing allowlist:

| Process result | Public OM code |
|---|---|
| `CONFIG_ERROR` | `CONFIG_ERROR` |
| `PI_RUNTIME_UNAVAILABLE` | `DEPENDENCY_MISSING` |
| `MODEL_ERROR`, `PI_PROCESS_TIMEOUT`, `PI_PROCESS_EXITED` | `MODEL_ERROR` |
| `TOOL_BRIDGE_ERROR` | `TOOL_ERROR` |
| `BUDGET_EXHAUSTED` | `BUDGET_EXHAUSTED` |
| `CANCELLED` | `CANCELLED` |
| `SESSION_ERROR`, `PROTOCOL_ERROR`, `INTERNAL_ERROR` | `INTERNAL_ERROR` |

`contracts.py` does not expose Pi process codes. The private source code and
stage are recorded as bounded non-secret diagnostics. Every path still calls
`_finalize()` exactly once.

A retryable Pi active-writer conflict remains public `INTERNAL_ERROR` to avoid
adding a new application code, but uses the fixed safe response
`会话暂时繁忙，请稍后重试` and retains `retryable:true` only in the private process
trace. Other Session failures remain non-retryable.

### 15.7 Cancellation and bounded recovery

For a Host-store-managed run, a true caller checker first calls
`host_store.request_cancel(run_id)`; cancellation becomes observable only when
the resulting private state is `cancel`. An external cancel is likewise
accepted only when `request_cancel()` wins the `open -> cancel`
compare-and-set. The first observed cancel winner makes the process adapter
send one `run.cancel`;
Node calls `Agent.abort()`, no new provider request or tool callback may start,
and late read-tool output is discarded. After Host has claimed commit/discard,
the cancel command returns not-ready and the adapter sends no contradictory
cancel for that run. Force termination follows the S1 deadline rules.

After a forced child kill, the Host run/session lease is released normally but
the same Pi Session may return retryable busy for at most the configured
30-second writer-lease TTL. OM does not poll automatically or delete the Pi
lease row; the next normal request may retry after expiry and fenced takeover.

`./om copilot resume RUN_ID` keeps using `CopilotHostStore.resume_source()`:

- only failed/interrupted read-only runs within the current attempt ceiling are
  resumable;
- a run with `session_commit_outcome:"unknown"` is rejected as non-resumable;
- `_successful_observations()` extracts only prior canonical `tool_result`
  events whose compact observation is `ok:true`;
- observations are bounded, redacted again, and injected as an ephemeral
  system context block;
- they are not reconstructed as orphan Pi tool-result messages and are not
  persisted;
- no provider/tool call, Control request, confirmation, or reply is replayed;
- the Pi Session lane is first rewound to its last complete commit.

There is no cross-process Session inspection path in V1. Operators diagnose an
unknown commit outcome from the bounded private event; subsequent normal turns
remain safe because Session rewind is marker-based.

### 15.8 S5 regression gate

Adapt the existing Host tests rather than maintaining parallel Engine
expectations. Prove:

1. local eval, local configured, and channel-prepared requests all reach the
   same `run_contract()` Pi boundary;
2. contract/Scene rejection happens before child spawn and every later path
   finishes the Host run once;
3. canonical tool inputs/results, progress labels, metrics, usage, retry count,
   cancellation, and event visibility retain their public contracts;
4. result admission sends commit/discard correctly and rejected text never
   enters Session;
5. two independent SQLite connections and barriers race cancel against
   commit/discard before, during, and after proposal; exactly one transition is
   accepted, and CLI response, Session marker, Host result, and outbox agree;
6. a barrier races worker completion with lifecycle/cancel events repeatedly;
   no event or metric is lost/duplicated, usage and tool counts both survive,
   and no Host mutation occurs after gate close/finalization;
7. a terminal-then-hanging child is reaped without replacing its validated
   result, and an abandoned tool callback cannot append Host events after
   `_finalize()` or create another worker;
8. process-error mapping exposes no Pi stderr/provider text;
9. resume reuses bounded successful reads without replay and rejects an unknown
   Session commit outcome;
10. stale-running interruption, session/lane leases, outbox state, and list/run
   inspection remain unchanged;
11. no import or call from `host.py` or `local_harness.py` reaches `engine`,
   `model_client`, or `conversation_memory`.

S5 exits when focused Copilot/Host tests and the Pi process tests pass. The code
is not released independently; S6 and S7 complete channel and operational
acceptance first.

## 16. S6 Control And Channel Cutover Detailed Design

### 16.1 Objective and files

S6 connects the existing Assistant product entry to the Pi-backed Host and
preserves deterministic Control separation. It adds no new entrypoint or UI.

Modify:

```text
agent-runtime/main.ts
src/infrastructure/pi_agent_process.py
src/application/copilot/contracts.py
src/application/copilot/service.py
src/application/copilot/om_chat.scene.json
src/application/copilot/channel_facade.py
src/application/assistant/inbound_service.py
tests/test_copilot_phase1.py
tests/test_copilot_conversation_memory.py
tests/test_inbound_control.py
```

`./om assistant handle` remains the product entry. `./om copilot run/eval` stay
operator diagnostics over the same Host, not a second assistant.
`control_handoff.py` remains unchanged and is reused as the deterministic
preview parser/validator.

### 16.2 Sender-and-config-scoped channel session

Replace `_channel_session_key()` with the strict derivation in section 6.1. A
channel request must have an authenticated non-empty `channel` and `sender_id`
and exactly one trusted data-scope input after Assistant default resolution;
otherwise it returns `CHANNEL_NOT_READY` before acquiring a lease or spawning
Node. Direct conversations without a channel conversation ID use
`"sender:" + sender_id` as the conversation component.

`inbound_service._run_copilot()` passes both public fields instead of dropping
`request.config_path`. `run_channel_request()` and `_channel_request()` accept
both. They reject key/path conflict, normalize a key, or canonicalize an
explicit path through existing `resolve_runtime_config_path()`. `CopilotScope`
and `prepare_contract()` carry the resolved path as `config_path`; the Scene
declares it as `host_only_tool_scope`, so `build_tool_payload()` can place it
only into canonical tool fields while `_runtime_context()` omits it from model
text. No new resolver or tool-payload path is introduced.

The opaque `authority_scope` is `key:<normalized-key>` or
`path:<sha256(canonical-path)>`. Path identity is computed only after trusted
resolution; the raw public string is never hashed directly. The same canonical
path reached through relative, absolute, or symlink aliases continues one
Session, while distinct paths cannot share a lease or transcript.

The opaque Host lease key and Pi Session ID are the same hash from section 6.1.
The database path is the sibling `pi_sessions.sqlite3` of the configured Host
database, or the runtime-root default. No raw sender or conversation ID is
written to the Pi database.

`run_channel_request()` removes:

- `session_messages()` legacy history reads;
- `record_session_turn()` legacy transcript writes;
- `_tool_uses()` and memory warning/error summaries;
- `record_channel_turn()` as a conversation-memory side channel.

`_context_messages()` now creates only the current authoritative pending
Control snapshot. Pi Session supplies committed conversation history inside
Node. Explicit Control receipts remain authoritative in the inbound operation
and audit stores; they are not copied into conversational memory. A later Agent
question about operation status must use the current pending snapshot or the
canonical project-inspection tools, never remembered prose.

The existing session lease and `chat_read` lane remain around the whole Host
run, including admission and Session commit.

### 16.3 Control tool projection and bridge result

`request_control_preview` is projected only when all are true:

```text
execution_environment == "channel"
and control_preview_specs is non-empty
and the authenticated sender passed inbound authorization
```

It uses the current `control_preview_tool_description()` schema. Python handles
its `tool.call` separately from read tools:

1. revalidate the name is the one allowed preview surface;
2. call `build_control_preview_request(arguments, user_message, specs)`;
3. return `tool.result` with a compact success observation and optional closed
   `control_request`;
4. never call `execute_explicit_control()` from the tool callback.

For a valid control result, the Node `AgentTool` returns `terminate:true`, stores
the validated request in run state, and emits a standard safe tool-result
message. Because it is the only call in the batch, Pi stops before another
provider request and Node proposes `status:"control_requested"`, `text:""`,
and `termination_reason:"control_preview_requested"`. The later deterministic
inbound preview result, not model prose, supplies the user-visible response.

The S6 `tool.result` payload therefore permits:

```json
{
  "call_id": "call_1",
  "tool_name": "request_control_preview",
  "observation": {"ok": true, "status": "preview_requested"},
  "control_request": {
    "intent_name": "...",
    "arguments": {},
    "source": "copilot_control_preview",
    "confidence": 1.0
  }
}
```

`control_request` is forbidden for every other tool and must match the closed
capability schema. It contains no operation ID or proof of execution.

### 16.4 Mixed-batch and mutation safety

Pi's `terminate:true` stops automatically only when every result in a tool batch
terminates. `beforeToolCall` therefore inspects the complete assistant message:

- a batch containing one control call and no other call is allowed;
- a batch containing control plus any other call, or multiple control calls,
  blocks every call in that batch with a recoverable `INVALID_ACTION` tool
  result and does not invoke Python;
- a read-only batch without control follows normal sequential execution.

The model gets one opportunity within existing budgets to repair a blocked
mixed batch. Confirm, cancel, apply, raw write tools, pending-operation mutation,
and readback receipt functions are never projected into Pi. A malicious Node
request for them is rejected again by Python's manifest allowlist.

### 16.5 Existing inbound flow remains authoritative

`inbound_service` keeps its current order:

```text
message-id idempotency
-> explicit command parser
-> permission/confirm/cancel parser
-> free text Pi Agent
```

For `control_requested`, `_control_command_from_copilot()` revalidates the
current capability catalog, then the existing `execute_explicit_control()`
creates the deterministic preview and pending operation. The response is
audited and delivered through the existing idempotent path. Later explicit
confirm/cancel still bypasses Pi; apply still requires the existing permission,
idempotency, readback, and receipt contracts.

If deterministic preview creation fails after a valid Pi request, the failure
is an inbound Control failure, not a successful Agent write and not a Session
fact. The Pi transcript may remember that a preview was requested, while the
operation store remains the sole authority on whether a pending operation was
actually created.

### 16.6 S6 channel and Control acceptance

Adapt existing tests to prove:

1. ordinary investment Q&A and project inspection both enter the same
   `om_chat` Pi path without keyword routing;
2. two senders in one group have different Host leases and Pi histories;
3. the same sender/conversation in the same trusted key or canonical-path scope
   continues from committed history, while another key/path is isolated;
4. path aliases resolve to one identity, distinct paths isolate, key/path
   conflict fails closed, and no plaintext config path enters Session storage
   or model-visible context;
5. missing authenticated identity or failure to resolve trusted authority scope
   fails before Node spawn;
6. current pending-Control context is ephemeral, refreshed each run, and
   outranks remembered prose;
7. a valid natural-language mutation request creates preview only, then requires
   a separate explicit confirmation before apply/readback;
8. mixed/multiple control batches execute no call; direct write-tool requests
   are unavailable and rejected;
9. explicit commands, confirmation, cancellation, duplicate message replay,
   inbound audit, response rendering, and reply outbox preserve current behavior;
10. no path imports or calls legacy session message APIs after cutover;
11. local diagnostic runs never receive `request_control_preview`;
12. no TUI, Web, remote Pi server, or second product command is introduced.

S6 exits when the channel and inbound Control suites pass with the real Pi
process boundary and temporary databases. No production message is sent as
part of automated acceptance.

## 17. S7 Packaging, Atomic Cutover, And Cleanup Detailed Design

### 17.1 Objective and operational files

S7 makes the mixed Python/Node release installable and rollback-safe, runs the
full acceptance gate, and removes the now-unreachable generic OM runtime.

Modify only the existing operational surfaces that own these checks:

```text
scripts/install.sh
scripts/release_preflight.sh
src/application/service_upgrade.py
src/application/setup/check.py
.github/workflows/guardrails.yml
.github/workflows/_release-reusable.yml
tests/test_install_script.py
tests/test_setup_check.py
tests/test_service_deploy.py
```

The already-added `agent-runtime/package.json`, lockfile, and `main.ts` are
included by the existing source archive. No container, bundled executable,
Node version manager, or package cache service is added.

### 17.2 Runtime prerequisite and install behavior

Production requires `node >=22.19.0` and `npm`. OM reports a missing/old
runtime but never installs or upgrades Node implicitly.

`scripts/install.sh` performs, before moving the temporary release into place:

```text
resolve node and npm from PATH
-> validate node semantic version >=22.19.0
-> npm ci --omit=dev --ignore-scripts --prefix agent-runtime
-> import pi-agent-core, pi-ai, and sqlite-node packages with Node
-> run the deterministic Pi process smoke
-> only then move target and switch current symlink
```

An install failure deletes only the temporary target through the existing trap;
the current release remains unchanged. Re-running against an already active
release verifies imports rather than mutating its `node_modules`; `--force`
recreates it through the normal temporary path.

The installer help and completion message state the Node prerequisite. It does
not write secrets, create the Session database, start services, or run a live
model call.

### 17.3 Controlled service upgrade

`service_upgrade._ensure_release_runtime()` keeps its Python dependency-cache
logic and, after the Python runtime is valid, calls one private
`_ensure_pi_runtime(target_dir, run_cmd, operations)` helper. The helper runs
the same version, locked install, package-import, and deterministic smoke checks
inside the target release before runtime-config commit or `current` symlink
switch.

Node dependencies are installed per release. They are small enough that V1
does not add a shared `node_modules` cache, cross-release symlink, or new lock.
The existing upgrade lock already serializes preparation. `package-lock.json`
hash and observed Node version are recorded in `runtime_prepare.pi_runtime` for
audit. A partial target may remain for diagnosis, but it is never activated;
the next confirmed upgrade reruns
`npm ci --omit=dev --ignore-scripts --prefix agent-runtime` from the lockfile.

`_dependency_hash()` continues to describe only the cached Python virtualenv.
Mixing the Node lock into that hash would rebuild an unrelated Python cache.

### 17.4 Setup and release gates

`om setup check` adds read-only checks:

| Check | Error condition |
|---|---|
| `install.node` | Node missing, unparseable, or `<22.19.0` |
| `install.npm` | npm missing |
| `install.pi_packages` | locked production package import fails |
| `copilot.model_context` | active `context_window_tokens` is missing/invalid or is not greater than `max_output_tokens + 2000` |
| `copilot.pi_session_path` | parent of the derived Host-sibling Session database is missing or not writable |

The check does not create the database or install packages. It gives the exact
remediation (`npm ci --omit=dev --ignore-scripts --prefix agent-runtime`) and
observed executable/version without exposing environment secrets.
The model-context check reads only the resolved active assistant profile and
does not query a provider or infer a value from the model name.

`release_preflight.sh`, Guardrails, and reusable Release CI set up Node
`22.19.0`, run locked production install, execute `tests/test_pi_agent_process.py`
and the focused Copilot/Control suites, then continue through current Python
checks. `tests/test_pi_agent_process.py` reads the three package versions and
the lockfile root package directly and requires exact `0.84.2`; the existing
release check remains unchanged. No check queries npm or the network.

The release archive still comes from `git archive`; `node_modules` is never
published. Release CI proves a clean archive can restore packages solely from
the lockfile before publishing.

### 17.5 Legacy deletion and guards

After all Pi call sites and tests pass, delete:

```text
src/application/copilot/engine.py
src/application/copilot/model_client.py
src/application/copilot/conversation_memory.py
src/application/copilot/agent.py
```

Remove the now-unused transcript surface at the same time:

- `host.py`: `session_messages()`, `record_session_turn()`, their Scene import,
  and public exports;
- `host_store.py`: the two legacy transcript methods, while leaving database
  columns and historical rows untouched;
- `channel_facade.py`: `record_channel_turn()` and its export;
- `scene.py` and `om_chat.scene.json`: `conversation_max_messages()` and the
  obsolete `conversation` block.

Delete or rewrite tests that assert their private implementation behavior.
Keep tests for public result, tool, Control, run, cancellation, recovery, and
answer-quality contracts against Pi. Leave legacy database columns and rows
untouched.

Add one architecture assertion to the existing focused test file: production
Copilot modules may not import the four retired modules, and no environment
switch/fallback string can select a legacy engine. Do not create a general
dependency-lint framework for this one boundary.

Engine-only behaviors intentionally removed are:

- character-count transcript pruning, replaced by Pi Session compaction;
- `__read_observation__` raw-result paging;
- keyword-based fresh-evidence recheck;
- Python model response/tool-call parsing;
- legacy transcript and memory writes.

Canonical tools, Scene instructions, Host evidence events, result admission,
and the existing answer-quality suite determine whether any removal causes a
product regression. If they do, fix the owning OM guard/tool contract; do not
restore a second Agent loop.

### 17.6 Pre-release commands and acceptance order

The development release gate is:

```bash
npm ci --omit=dev --ignore-scripts --prefix agent-runtime
./.venv/bin/python -m ruff check .
./.venv/bin/python -m pytest -q tests/test_pi_agent_process.py
./.venv/bin/python -m pytest -q \
  tests/test_copilot_phase1.py \
  tests/test_copilot_conversation_memory.py \
  tests/test_inbound_control.py \
  tests/test_setup_check.py \
  tests/test_cli_operator_commands.py \
  tests/test_install_script.py \
  tests/test_service_deploy.py \
  tests/test_release_check.py
./scripts/release_preflight.sh --full --allow-dirty
git diff --check
```

Then, under separate explicit authorization:

1. install the candidate in a non-production release directory;
2. run `om setup check` and deterministic local eval;
3. run one read-only local canary per configured provider;
4. rehearse selecting the previous release and confirm it ignores, but does not
   delete, the Pi Session database;
5. release the complete source unit;
6. use the controlled upgrade workflow to prepare dependencies and switch the
   symlink;
7. run one read-only product request, one same-sender/same-config continuity
   check, one same-sender/cross-config isolation check, one cross-sender
   isolation check, and one preview-without-confirm check;
8. verify Host run/events/outbox and inbound audit, without sending synthetic
   writes.

Release, upgrade, live messages, and production Control remain separate
authorization boundaries.

### 17.7 Rollback and S7 exit

There is no runtime fallback. Rollback selects the prior complete release via
the existing controlled upgrade/rollback path and restarts only the affected
service according to its preserved activation state. It does not downgrade or
delete `pi_sessions.sqlite3`; the old release simply does not read it.

S7 is complete only when:

- the full Python suite and all Pi tests pass from a clean locked install;
- setup, install, release, service-upgrade, and rollback tests pass;
- `scripts/copilot_p1_eval.py` and the existing answer-quality suite meet their
  current safety/quality gates with Pi;
- public Agent/Control/cancellation/recovery/outbox behavior passes the final
  acceptance matrix;
- source search proves no legacy runtime caller/fallback remains;
- a clean release archive can be installed without repository-local state;
- the previous release remains selectable and its rollback rehearsal passes.

## 18. Packaging, Cutover, And Rollback

Pi introduces a production Node prerequisite. The installer must not install or
upgrade Node implicitly. Before S7 cutover:

- setup/update preflight requires `node >=22.19.0` and `npm`;
- install/update runs
  `npm ci --omit=dev --ignore-scripts --prefix agent-runtime` inside the target
  release before switching the `current` symlink;
- release CI installs the locked Node dependencies and runs the focused Pi
  tests;
- runtime verification imports the three pinned packages and confirms the Pi
  Session path is writable by the service user and the resolved active model
  has a valid declared context window;
- failure leaves the previous `current` release and services unchanged.

Cutover is atomic at the release level:

1. deploy a release containing the complete Pi runtime and dependencies;
2. verify config, Node, package imports, storage, and deterministic tests;
3. switch the production Copilot call site from legacy Engine to Pi;
4. verify one controlled read-only local run, then channel behavior;
5. keep the previous release intact for rollback.

There is no runtime fallback. Rollback selects the previous complete release,
restarts only the affected service through the controlled upgrade workflow, and
verifies read-only behavior. Pi's new Session database is preserved; the old
release simply does not read it.

## 19. Final Acceptance Matrix

| Area | Required evidence |
|---|---|
| Entrypoint | free text through `./om assistant handle` reaches the single Pi-backed `om_chat` path |
| Investment Q&A | current facts come only from canonical tools and missing data stays explicit |
| Inspection | runtime/config/job questions use the same Agent and read tools |
| Control | model can request preview only; confirm/apply/readback remain deterministic |
| Memory | same sender/conversation/canonical key-or-path scope continues; different scopes or senders cannot share memory, and plaintext paths are absent |
| Context | effective budget is `min(model capability, Scene cap)`; pre-run compaction is independently durable and current-turn admission preserves complete message/tool groups |
| Tools | model-visible list equals the Host projection; no Pi builtin write/shell/file tool exists; one lock preserves every concurrent lifecycle/tool event and metric; abandoned reads cannot exceed one worker or write late Host events |
| Providers | OpenAI, DeepSeek, Kimi, Kimi Code, and Ollama contract tests pass |
| Cancellation | two independent Host connections racing cancel against commit/discard accept exactly one durable winner, reflected consistently in CLI, Session, Host result, and outbox |
| Recovery | only bounded successful read observations are reused; no Control action is replayed; forced-kill Pi lease is busy only until fenced TTL takeover |
| Audit | prompt/tool fingerprints, normalized events, metrics, and final state remain available without secrets or reasoning text |
| Delivery | a validated terminal survives child cleanup and final channel reply remains idempotent through the existing outbox |
| Operations | install, upgrade, verification, and release-level rollback are proven |
| Cleanup | legacy Engine/model/memory modules and their callers are removed; no production fallback remains |

## 20. Decisions That Are Closed

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
  name or Scene cap.
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

No product or architecture decision remains open for S1-S7. Implementation may
correct a verified upstream signature or repository fact, but changing an
ownership boundary, product scope, protocol guarantee, or cutover strategy
requires updating this document before code.
