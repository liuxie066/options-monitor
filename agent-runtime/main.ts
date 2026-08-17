// S2 Node runtime: one real Pi Agent run with sequential Host tools over
// `om-pi-ipc.v1` JSONL stdio.
//
// Pinned to @earendil-works/pi-agent-core@0.84.2 and @earendil-works/pi-ai@0.84.2.
// The protocol (envelope, start payload, terminal payload) is specified by
// docs/PI_AGENT_CORE_INTEGRATION.md sections 5, 11, and 12 and mirrored by
// src/infrastructure/pi_agent_process.py.

import process from "node:process";
import { Agent } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type {
  Api,
  AssistantMessage,
  AssistantMessageEventStream,
  Context,
  Model,
  StreamFn,
  Usage,
} from "@earendil-works/pi-ai";
import type { AgentEvent, AgentMessage, AgentTool } from "@earendil-works/pi-agent-core";

const PROTOCOL = "om-pi-ipc.v1";
const MAX_LINE_BYTES = 1_048_576;
const MAX_SAFE_MESSAGE_CHARS = 240;
const MAX_FIXTURE_DELAY_MS = 300_000;

const ALLOWED_ERROR_CODES = new Set([
  "PROTOCOL_ERROR",
  "CONFIG_ERROR",
  "MODEL_ERROR",
  "SESSION_ERROR",
  "TOOL_BRIDGE_ERROR",
  "BUDGET_EXHAUSTED",
  "INTERNAL_ERROR",
]);
const ALLOWED_ERROR_STAGES = new Set([
  "protocol",
  "config",
  "model",
  "session",
  "tool",
  "budget",
  "runtime",
]);

type JsonObject = Record<string, unknown>;

interface Identity {
  requestId: string;
  runId: string;
}

interface Envelope {
  type: string;
  payload: JsonObject;
}

interface FixtureToolCall {
  call_id: string;
  tool_name: string;
  arguments: JsonObject;
}

type FixtureTurn = { text: string } | { tool_calls: FixtureToolCall[] };

interface PendingTool {
  callId: string;
  toolName: string;
  resolve: (observation: JsonObject) => void;
  reject: (error: Error) => void;
  signal?: AbortSignal;
  abortListener?: () => void;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, keys: string[]): boolean {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((k) => Object.hasOwn(value, k));
}

// Incremental line reader. Must yield each complete line as it arrives rather
// than draining stdin to EOF: the Python parent keeps stdin open until it sees
// a terminal envelope, so the naive EOF loop deadlocks.
async function* readJsonLines(
  input: NodeJS.ReadableStream
): AsyncGenerator<string, void, unknown> {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = Buffer.alloc(0);
  for await (const chunk of input) {
    const part = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as Uint8Array);
    buffer = Buffer.concat([buffer, part]);
    let idx: number;
    while ((idx = buffer.indexOf(0x0a)) !== -1) {
      let end = idx;
      if (end > 0 && buffer[end - 1] === 0x0d) end -= 1;
      const lineBytes = buffer.subarray(0, end);
      if (lineBytes.length === 0) throw new Error("blank record");
      if (lineBytes.length > MAX_LINE_BYTES) throw new Error("line exceeds ceiling");
      yield decoder.decode(lineBytes);
      buffer = buffer.subarray(idx + 1);
    }
    if (buffer.length > MAX_LINE_BYTES) throw new Error("line exceeds ceiling");
  }
  if (buffer.length > 0) throw new Error("missing final newline");
}

function parseStartEnvelope(line: string): { identity: Identity; payload: JsonObject } {
  let obj: unknown;
  try {
    obj = JSON.parse(line);
  } catch {
    throw new Error("malformed JSON");
  }
  if (!isRecord(obj)) throw new Error("record is not an object");
  if (!exactKeys(obj, ["protocol", "type", "request_id", "run_id", "seq", "payload"])) {
    throw new Error("envelope fields are not closed");
  }
  if (obj.protocol !== PROTOCOL) throw new Error("unknown protocol");
  if (obj.type !== "run.start") throw new Error("expected run.start");
  if (!isNonEmptyString(obj.request_id)) throw new Error("request_id must be non-empty");
  if (!isNonEmptyString(obj.run_id)) throw new Error("run_id must be non-empty");
  if (obj.seq !== 1) throw new Error("start sequence must be 1");
  if (!isRecord(obj.payload)) throw new Error("payload is not an object");
  validateStart(obj.payload);
  return {
    identity: { requestId: obj.request_id, runId: obj.run_id },
    payload: obj.payload,
  };
}

function parseEnvelope(
  line: string,
  expectedSeq: number,
  identity: Identity,
  allowedTypes: Set<string>
): Envelope {
  let obj: unknown;
  try {
    obj = JSON.parse(line);
  } catch {
    throw new Error("malformed JSON");
  }
  if (!isRecord(obj)) throw new Error("record is not an object");
  if (!exactKeys(obj, ["protocol", "type", "request_id", "run_id", "seq", "payload"])) {
    throw new Error("envelope fields are not closed");
  }
  if (obj.protocol !== PROTOCOL) throw new Error("unknown protocol");
  if (!isNonEmptyString(obj.type) || !allowedTypes.has(obj.type)) {
    throw new Error("unknown or empty type");
  }
  if (obj.request_id !== identity.requestId || obj.run_id !== identity.runId) {
    throw new Error("mismatched identity");
  }
  if (obj.seq !== expectedSeq) throw new Error("sequence is not contiguous");
  if (!isRecord(obj.payload)) throw new Error("payload is not an object");
  return { type: obj.type, payload: obj.payload };
}

function validateStart(payload: JsonObject): void {
  const keys = [
    "execution_environment",
    "session_id",
    "system_prompt",
    "runtime_context",
    "user_message",
    "model",
    "tools",
    "limits",
    "recovered_observations",
    "debug",
  ];
  if (!exactKeys(payload, keys)) throw new Error("start payload keys are not closed");
  if (payload.execution_environment !== "eval") throw new Error("execution_environment must be eval");
  if (payload.session_id !== null) throw new Error("session_id must be null");
  if (!isNonEmptyString(payload.system_prompt)) throw new Error("system_prompt must be non-empty");
  if (!isNonEmptyString(payload.user_message)) throw new Error("user_message must be non-empty");

  if (!Array.isArray(payload.runtime_context)) throw new Error("runtime_context must be an array");
  for (const item of payload.runtime_context) {
    if (
      !isRecord(item) ||
      !exactKeys(item, ["role", "content"]) ||
      item.role !== "system" ||
      !isNonEmptyString(item.content)
    ) {
      throw new Error("runtime_context item is not a closed system message");
    }
  }

  if (!Array.isArray(payload.tools)) throw new Error("tools must be an array");
  const toolNames = new Set<string>();
  for (const tool of payload.tools) {
    if (
      !isRecord(tool) ||
      !exactKeys(tool, ["name", "description", "input_schema"]) ||
      !isNonEmptyString(tool.name) ||
      !isNonEmptyString(tool.description) ||
      !isRecord(tool.input_schema) ||
      toolNames.has(tool.name)
    ) {
      throw new Error("tool definition is invalid");
    }
    toolNames.add(tool.name);
  }
  if (!Array.isArray(payload.recovered_observations) || payload.recovered_observations.length !== 0) {
    throw new Error("S1/S2 require an empty recovered_observations array");
  }

  if (!isRecord(payload.model)) throw new Error("model must be an object");
  const model = payload.model;
  if (
    !exactKeys(model, [
      "provider",
      "api_kind",
      "model",
      "base_url",
      "timeout_seconds",
      "context_window_tokens",
      "max_output_tokens",
      "max_attempts",
    ])
  ) {
    throw new Error("model fields are not closed");
  }
  for (const key of ["provider", "model", "base_url"] as const) {
    if (!isNonEmptyString(model[key])) throw new Error(`model.${key} must be non-empty`);
  }
  if (model.api_kind !== "openai-responses" && model.api_kind !== "openai-completions") {
    throw new Error("model.api_kind is not allowed");
  }
  for (const key of ["timeout_seconds", "context_window_tokens", "max_output_tokens", "max_attempts"] as const) {
    if (!isPositiveInteger(model[key])) throw new Error(`model.${key} must be a positive integer`);
  }
  let baseUrl: URL;
  try {
    baseUrl = new URL(model.base_url as string);
  } catch {
    throw new Error("model.base_url is not a valid URL");
  }
  if (baseUrl.protocol !== "http:" && baseUrl.protocol !== "https:") {
    throw new Error("model.base_url must be http or https");
  }

  if (!isRecord(payload.limits)) throw new Error("limits must be an object");
  const limits = payload.limits;
  const limitKeys = [
    "timeout_seconds",
    "max_iterations",
    "max_tool_calls",
    "max_context_tokens",
    "max_consecutive_failed_tool_batches",
    "final_answer_reserve_seconds",
  ];
  if (!exactKeys(limits, limitKeys)) throw new Error("limits fields are not closed");
  for (const key of limitKeys) {
    if (!isPositiveInteger(limits[key])) throw new Error(`limits.${key} must be a positive integer`);
  }

  if (!isRecord(payload.debug)) throw new Error("debug must be an object");
  const debug = payload.debug;
  if (Object.hasOwn(debug, "fixture_response")) {
    if (!exactKeys(debug, ["fixture_response", "delay_ms"])) {
      throw new Error("debug fields are not closed");
    }
    if (typeof debug.fixture_response !== "string") {
      throw new Error("debug.fixture_response must be a string");
    }
  } else if (Object.hasOwn(debug, "fixture_turns")) {
    if (!exactKeys(debug, ["fixture_turns", "delay_ms"]) || !Array.isArray(debug.fixture_turns)) {
      throw new Error("debug.fixture_turns must be a closed array fixture");
    }
    for (const turn of debug.fixture_turns) {
      if (!isRecord(turn) || Object.keys(turn).length !== 1) {
        throw new Error("fixture turn must hold exactly one field");
      }
      if (Object.hasOwn(turn, "text")) {
        if (typeof turn.text !== "string") throw new Error("fixture turn text must be a string");
        continue;
      }
      if (!Array.isArray(turn.tool_calls) || turn.tool_calls.length === 0) {
        throw new Error("fixture turn tool_calls must be a non-empty array");
      }
      for (const call of turn.tool_calls) {
        if (
          !isRecord(call) ||
          !exactKeys(call, ["call_id", "tool_name", "arguments"]) ||
          !isNonEmptyString(call.call_id) ||
          !isNonEmptyString(call.tool_name) ||
          !isRecord(call.arguments)
        ) {
          throw new Error("fixture tool call is invalid");
        }
      }
    }
  } else {
    throw new Error("debug fixture is missing");
  }
  const delay = debug.delay_ms;
  if (
    typeof delay !== "number" ||
    !Number.isInteger(delay) ||
    delay < 0 ||
    delay > MAX_FIXTURE_DELAY_MS
  ) {
    throw new Error("debug.delay_ms must be within [0, 300000]");
  }
}

function emit(type: string, payload: JsonObject, identity: Identity, seq: number): void {
  const record = {
    protocol: PROTOCOL,
    type,
    request_id: identity.requestId,
    run_id: identity.runId,
    seq,
    payload,
  };
  const line = JSON.stringify(record) + "\n";
  if (Buffer.byteLength(line, "utf-8") > MAX_LINE_BYTES) {
    throw new Error("outbound envelope exceeds line ceiling");
  }
  process.stdout.write(line);
}

function safeError(code: string, stage: string, message: string, retryable: boolean): JsonObject {
  if (!ALLOWED_ERROR_CODES.has(code) || !ALLOWED_ERROR_STAGES.has(stage)) {
    return { code: "INTERNAL_ERROR", stage: "runtime", message: "invalid error", retryable: false };
  }
  return { code, stage, message: message.slice(0, MAX_SAFE_MESSAGE_CHARS), retryable };
}

function modelFromStart(start: JsonObject): Model<Api> {
  const model = start.model as JsonObject;
  return {
    id: model.model as string,
    name: model.model as string,
    api: model.api_kind as Api,
    provider: model.provider as string,
    baseUrl: model.base_url as string,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: model.context_window_tokens as number,
    maxTokens: model.max_output_tokens as number,
  };
}

function emptyUsage(): Usage {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

function makeAssistantMessage(
  model: Model<Api>,
  content: AssistantMessage["content"],
  stopReason: AssistantMessage["stopReason"],
  errorMessage?: string
): AssistantMessage {
  const message: AssistantMessage = {
    role: "assistant",
    content,
    api: model.api,
    provider: model.provider,
    model: model.id,
    usage: emptyUsage(),
    stopReason,
    timestamp: Date.now(),
  };
  if (errorMessage !== undefined) message.errorMessage = errorMessage;
  return message;
}

function stableJson(value: unknown): string {
  const text = JSON.stringify(value, (_key, current) => {
    if (!isRecord(current)) return current;
    return Object.fromEntries(Object.keys(current).sort().map((key) => [key, current[key]]));
  });
  if (text === undefined) throw new Error("observation is not JSON serializable");
  return text;
}

function waitAbortOrDelay(signal: AbortSignal | undefined, ms: number): Promise<void> {
  const sources: AbortSignal[] = [AbortSignal.timeout(ms)];
  if (signal) sources.push(signal);
  const combined = AbortSignal.any(sources);
  if (combined.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    combined.addEventListener("abort", () => resolve(), { once: true });
  });
}

function fixtureContextHasResults(context: Context, calls: FixtureToolCall[]): boolean {
  return calls.every((call) => {
    const message = context.messages.find(
      (item) =>
        item.role === "toolResult" &&
        item.toolCallId === call.call_id &&
        item.toolName === call.tool_name
    );
    if (!message || message.role !== "toolResult") return false;
    const details = message.details;
    if (!isRecord(details) || !isRecord(details.observation)) return true;
    return (
      exactKeys(details, ["observation"]) &&
      message.content.length === 1 &&
      message.content[0].type === "text" &&
      message.content[0].text === stableJson(details.observation) &&
      message.isError === (details.observation.ok === false)
    );
  });
}

// StreamFn contract (pi-agent-core/types.ts): must not throw or reject; failures
// are encoded as an event stream ending in done(stop/length/toolUse/deferred) or
// error(aborted/error). We drive the fixture entirely through pushed events.
function createFixtureStream(start: JsonObject): StreamFn {
  const debug = start.debug as JsonObject;
  const turns: FixtureTurn[] = Object.hasOwn(debug, "fixture_response")
    ? [{ text: debug.fixture_response as string }]
    : (debug.fixture_turns as FixtureTurn[]);
  const delayMs = debug.delay_ms as number;
  let turnIndex = 0;
  let expectedResults: FixtureToolCall[] = [];
  return (model, context, options) => {
    const stream: AssistantMessageEventStream = createAssistantMessageEventStream();
    const empty = makeAssistantMessage(model, [], "pending");
    const turn = turns[turnIndex++];
    void (async () => {
      stream.push({ type: "start", partial: empty });
      await waitAbortOrDelay(options?.signal, delayMs);
      if (options?.signal?.aborted) {
        const aborted = makeAssistantMessage(model, [], "aborted", "aborted");
        stream.push({ type: "error", reason: "aborted", error: aborted });
        return;
      }
      if (!turn) throw new Error("fixture turns exhausted");
      if (expectedResults.length > 0 && !fixtureContextHasResults(context, expectedResults)) {
        throw new Error("fixture context mismatch");
      }
      if ("text" in turn) {
        expectedResults = [];
        const final = makeAssistantMessage(model, [{ type: "text", text: turn.text }], "stop");
        stream.push({ type: "text_start", contentIndex: 0, partial: empty });
        stream.push({ type: "text_delta", contentIndex: 0, delta: turn.text, partial: final });
        stream.push({ type: "text_end", contentIndex: 0, content: turn.text, partial: final });
        stream.push({ type: "done", reason: "stop", message: final });
        return;
      }
      expectedResults = turn.tool_calls;
      const content: AssistantMessage["content"] = turn.tool_calls.map((call) => ({
        type: "toolCall",
        id: call.call_id,
        name: call.tool_name,
        arguments: call.arguments,
      }));
      const partial = makeAssistantMessage(model, content, "pending");
      const final = makeAssistantMessage(model, content, "toolUse");
      for (let index = 0; index < turn.tool_calls.length; index += 1) {
        const toolCall = content[index];
        if (toolCall.type !== "toolCall") continue;
        stream.push({ type: "toolcall_start", contentIndex: index, partial });
        stream.push({
          type: "toolcall_delta",
          contentIndex: index,
          delta: JSON.stringify(toolCall.arguments),
          partial,
        });
        stream.push({ type: "toolcall_end", contentIndex: index, toolCall, partial });
      }
      stream.push({ type: "done", reason: "toolUse", message: final });
    })().catch(() => {
      const failed = makeAssistantMessage(model, [], "error", "fixture stream failed");
      stream.push({ type: "error", reason: "error", error: failed });
    });
    return stream;
  };
}

function createToolBridge(
  start: JsonObject,
  emitRun: (type: string, payload: JsonObject) => void,
  onFailure: (message: string) => void
): {
  tools: AgentTool[];
  acceptResult: (payload: JsonObject) => void;
  cancelPending: () => void;
  rejectPending: () => void;
  hasPending: () => boolean;
} {
  let pending: PendingTool | null = null;

  const settlePending = (error?: Error): void => {
    const current = pending;
    if (!current) return;
    pending = null;
    if (current.signal && current.abortListener) {
      current.signal.removeEventListener("abort", current.abortListener);
    }
    if (error) current.reject(error);
  };

  const tools = (start.tools as JsonObject[]).map((spec): AgentTool => ({
    name: spec.name as string,
    label: spec.name as string,
    description: spec.description as string,
    parameters: spec.input_schema as AgentTool["parameters"],
    executionMode: "sequential",
    execute: async (toolCallId, params, signal) => {
      if (pending) {
        onFailure("second outstanding tool call");
        throw new Error("tool bridge failed");
      }
      if (signal?.aborted) throw new Error("tool call aborted");
      const observation = await new Promise<JsonObject>((resolve, reject) => {
        const abortListener = (): void => {
          if (pending?.callId !== toolCallId) return;
          pending = null;
          reject(new Error("tool call aborted"));
        };
        pending = {
          callId: toolCallId,
          toolName: spec.name as string,
          resolve,
          reject,
          signal,
          abortListener,
        };
        signal?.addEventListener("abort", abortListener, { once: true });
        try {
          emitRun("tool.call", {
            call_id: toolCallId,
            tool_name: spec.name as string,
            arguments: params as JsonObject,
          });
        } catch {
          pending = null;
          signal?.removeEventListener("abort", abortListener);
          onFailure("tool call emit failed");
          reject(new Error("tool bridge failed"));
        }
      });
      return {
        content: [{ type: "text", text: stableJson(observation) }],
        details: { observation },
        terminate: false,
      };
    },
  }));

  return {
    tools,
    acceptResult(result) {
      if (
        !exactKeys(result, ["call_id", "tool_name", "observation"]) ||
        !isNonEmptyString(result.call_id) ||
        !isNonEmptyString(result.tool_name) ||
        !isRecord(result.observation) ||
        !pending ||
        result.call_id !== pending.callId ||
        result.tool_name !== pending.toolName
      ) {
        throw new Error("tool result mismatch");
      }
      const current = pending;
      pending = null;
      if (current.signal && current.abortListener) {
        current.signal.removeEventListener("abort", current.abortListener);
      }
      current.resolve(result.observation);
    },
    cancelPending() {
      settlePending(new Error("tool call aborted"));
    },
    rejectPending() {
      settlePending(new Error("tool bridge failed"));
    },
    hasPending: () => pending !== null,
  };
}

function effectiveSystemPrompt(systemPrompt: string, runtimeContext: JsonObject[]): string {
  const parts = [systemPrompt];
  for (const item of runtimeContext) {
    if (typeof item.content === "string") parts.push(item.content);
  }
  return parts.join("\n\n");
}

function normalizeUsage(usage: Record<string, unknown>): JsonObject {
  const keys = ["input", "output", "cacheRead", "cacheWrite", "totalTokens"] as const;
  const out: JsonObject = {};
  for (const key of keys) {
    const value = usage[key];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      out[key] = value;
    }
  }
  return out;
}

function turnData(message: AgentMessage): JsonObject {
  const assistant = message as unknown as AssistantMessage;
  return {
    stop_reason: assistant.stopReason,
    usage: normalizeUsage(assistant.usage ?? {}),
  };
}

// AgentEvent union (pi-agent-core/types.ts). Message updates, arguments,
// observations, and tool update events stay inside the Node process.
function normalizeAgentEvent(event: AgentEvent): { event_type: string; data: JsonObject } | null {
  switch (event.type) {
    case "agent_start":
      return { event_type: "agent_start", data: {} };
    case "turn_start":
      return { event_type: "turn_start", data: {} };
    case "agent_end":
      return { event_type: "agent_end", data: {} };
    case "message_end":
      if ((event.message as { role?: unknown }).role !== "assistant") return null;
      return { event_type: "model_turn_completed", data: turnData(event.message) };
    case "turn_end":
      return { event_type: "turn_end", data: turnData(event.message) };
    case "tool_execution_start":
      return {
        event_type: "tool_execution_start",
        data: { call_id: event.toolCallId, tool_name: event.toolName },
      };
    case "tool_execution_end":
      return {
        event_type: "tool_execution_end",
        data: { call_id: event.toolCallId, tool_name: event.toolName, ok: !event.isError },
      };
    default:
      return null;
  }
}

function lastAssistant(agent: Agent): AssistantMessage | undefined {
  const messages = agent.state.messages;
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message.role === "assistant") return message as AssistantMessage;
  }
  return undefined;
}

function extractText(message: AssistantMessage): string {
  return message.content.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("");
}

async function run(): Promise<void> {
  const lines = readJsonLines(process.stdin);
  const first = await lines.next();
  if (first.done) {
    process.stderr.write("diagnostic: missing run.start\n");
    process.exitCode = 2;
    return;
  }

  let identity: Identity;
  let payload: JsonObject;
  try {
    const start = parseStartEnvelope(first.value);
    identity = start.identity;
    payload = start.payload;
  } catch (err) {
    process.stderr.write(`diagnostic: ${String(err)}\n`);
    process.exitCode = 2;
    return;
  }

  let nodeSeq = 0;
  const emitRun = (type: string, p: JsonObject): void => {
    nodeSeq += 1;
    emit(type, p, identity, nodeSeq);
  };

  emitRun("run.accepted", {
    runtime: "pi-agent-core",
    runtime_version: "0.84.2",
    session_id: null,
  });

  const model = modelFromStart(payload);
  const systemPrompt = effectiveSystemPrompt(
    payload.system_prompt as string,
    payload.runtime_context as JsonObject[]
  );
  const limits = payload.limits as JsonObject;
  const startedAt = performance.now();
  let assistantTurns = 0;
  let finalizedToolCalls = 0;
  let consecutiveFailedToolBatches = 0;
  let forcedFinalAtTurn: number | null = null;
  let bridgeFailure: JsonObject | null = null;
  let agent: Agent | undefined;

  const inbound = {
    expectedSeq: 2,
    cancelled: false,
    terminal: false,
    awaitingAdmission: false,
    action: null as string | null,
    error: null as JsonObject | null,
  };
  let resolveAction!: (action: string | null) => void;
  const actionPromise = new Promise<string | null>((resolve) => {
    resolveAction = resolve;
  });

  const bridge = createToolBridge(payload, emitRun, (message) => {
    if (!bridgeFailure) {
      bridgeFailure = safeError("TOOL_BRIDGE_ERROR", "tool", message, false);
      agent?.abort();
    }
  });

  agent = new Agent({
    initialState: {
      systemPrompt,
      model,
      thinkingLevel: "off",
      tools: bridge.tools,
      messages: [],
    },
    streamFn: createFixtureStream(payload),
    toolExecution: "sequential",
    afterToolCall: async ({ result }) => {
      const details = result.details;
      return isRecord(details) &&
        isRecord(details.observation) &&
        details.observation.ok === false
        ? { isError: true }
        : undefined;
    },
    prepareNextTurnWithContext: ({ context, toolResults }) => {
      if (forcedFinalAtTurn !== null || toolResults.length === 0) return undefined;
      const remainingMs =
        (limits.timeout_seconds as number) * 1000 - (performance.now() - startedAt);
      const exhausted =
        assistantTurns >= (limits.max_iterations as number) ||
        finalizedToolCalls >= (limits.max_tool_calls as number) ||
        consecutiveFailedToolBatches >=
          (limits.max_consecutive_failed_tool_batches as number) ||
        remainingMs <= (limits.final_answer_reserve_seconds as number) * 1000;
      if (!exhausted) return undefined;
      forcedFinalAtTurn = assistantTurns;
      return { context: { ...context, tools: [] } };
    },
    shouldStopAfterTurn: () =>
      forcedFinalAtTurn !== null && assistantTurns > forcedFinalAtTurn,
  });

  agent.subscribe((event) => {
    if (event.type === "message_end" && event.message.role === "assistant") {
      assistantTurns += 1;
    } else if (event.type === "tool_execution_end") {
      finalizedToolCalls += 1;
    } else if (event.type === "turn_end" && event.toolResults.length > 0) {
      consecutiveFailedToolBatches = event.toolResults.every((result) => result.isError)
        ? consecutiveFailedToolBatches + 1
        : 0;
    }
    const normalized = normalizeAgentEvent(event);
    if (normalized) emitRun("agent.event", normalized);
  });

  const failInbound = (): void => {
    if (inbound.terminal || inbound.error) return;
    inbound.error = safeError("PROTOCOL_ERROR", "protocol", "invalid host record", false);
    bridge.rejectPending();
    resolveAction(null);
    agent?.abort();
  };

  const pumpInbound = async (): Promise<void> => {
    try {
      for await (const line of lines) {
        if (inbound.terminal) throw new Error("record after terminal");
        const envelope = parseEnvelope(
          line,
          inbound.expectedSeq,
          identity,
          new Set(["tool.result", "run.cancel", "run.commit", "run.discard"])
        );
        inbound.expectedSeq += 1;

        if (envelope.type === "tool.result") {
          if (inbound.cancelled || inbound.awaitingAdmission || inbound.action) {
            throw new Error("tool result outside active tool call");
          }
          bridge.acceptResult(envelope.payload);
          continue;
        }
        if (envelope.type === "run.cancel") {
          if (
            !exactKeys(envelope.payload, ["reason"]) ||
            !isNonEmptyString(envelope.payload.reason) ||
            inbound.cancelled ||
            inbound.action
          ) {
            throw new Error("invalid cancellation");
          }
          inbound.cancelled = true;
          inbound.action = envelope.type;
          bridge.cancelPending();
          resolveAction(envelope.type);
          agent?.abort();
          continue;
        }
        if (
          !exactKeys(envelope.payload, []) ||
          !inbound.awaitingAdmission ||
          inbound.cancelled ||
          inbound.action ||
          bridge.hasPending()
        ) {
          throw new Error("invalid admission action");
        }
        inbound.action = envelope.type;
        resolveAction(envelope.type);
      }
      if (!inbound.terminal) throw new Error("stdin closed before terminal");
    } catch {
      failInbound();
    }
  };

  const promptPromise = agent.prompt(payload.user_message as string);
  void pumpInbound();

  let promptFailed = false;
  try {
    await promptPromise;
  } catch {
    promptFailed = true;
  }

  const finishError = (error: JsonObject): void => {
    inbound.terminal = true;
    emitRun("run.error", error);
    process.exitCode = 1;
  };

  if (inbound.error) {
    finishError(inbound.error);
    return;
  }
  if (bridgeFailure) {
    finishError(bridgeFailure);
    return;
  }
  if (inbound.cancelled) {
    inbound.terminal = true;
    emitRun("run.final", {
      status: "cancelled",
      text: "",
      control_request: null,
      termination_reason: "aborted",
      usage: {},
      committed: false,
    });
    process.exitCode = 0;
    return;
  }

  if (promptFailed) {
    finishError(safeError("INTERNAL_ERROR", "runtime", "agent prompt failed", false));
    return;
  }

  const finalMessage = lastAssistant(agent);
  if (!finalMessage) {
    finishError(safeError("INTERNAL_ERROR", "runtime", "no assistant message", false));
    return;
  }

  const stopReason = finalMessage.stopReason;
  const usage = normalizeUsage(finalMessage.usage ?? {});
  const text = extractText(finalMessage);
  const forcedFinalCompleted =
    forcedFinalAtTurn !== null && assistantTurns > forcedFinalAtTurn;

  if (forcedFinalCompleted && text.trim() === "") {
    finishError(
      safeError(
        "BUDGET_EXHAUSTED",
        "budget",
        "agent budget exhausted without a final answer",
        false
      )
    );
    return;
  }

  if (stopReason === "stop" || stopReason === "length") {
    if (text === "") {
      finishError(safeError("MODEL_ERROR", "model", "empty answer", false));
      return;
    }
    inbound.awaitingAdmission = true;
    emitRun("run.proposed", {
      status: "answered",
      text,
      control_request: null,
      termination_reason: stopReason,
      usage,
    });
    const action = inbound.action ?? (await actionPromise);
    inbound.awaitingAdmission = false;
    if (inbound.error) {
      finishError(inbound.error);
      return;
    }
    if (!action) {
      finishError(safeError("PROTOCOL_ERROR", "protocol", "missing action", false));
      return;
    }
    inbound.terminal = true;
    if (action === "run.cancel") {
      emitRun("run.final", {
        status: "cancelled",
        text: "",
        control_request: null,
        termination_reason: "aborted",
        usage,
        committed: false,
      });
    } else {
      emitRun("run.final", {
        status: "answered",
        text,
        control_request: null,
        termination_reason: stopReason,
        usage,
        committed: action === "run.commit",
      });
    }
    process.exitCode = 0;
    return;
  }

  finishError(safeError("MODEL_ERROR", "model", "fixture stream error", false));
}

process.stdin.on("error", () => {});
run().then(
  () => process.stdin.destroy(),
  (err) => {
    process.stderr.write(`diagnostic: ${String(err)}\n`);
    process.exitCode = 1;
    process.stdin.destroy();
  }
);
