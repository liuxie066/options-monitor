// S1 Node runtime: one real Pi Agent run over `om-pi-ipc.v1` JSONL stdio.
//
// Pinned to @earendil-works/pi-agent-core@0.84.2 and @earendil-works/pi-ai@0.84.2.
// The protocol (envelope, start payload, terminal payload) is specified by
// docs/PI_AGENT_CORE_INTEGRATION.md sections 5 and 11 and mirrored by
// src/infrastructure/pi_agent_process.py.

import process from "node:process";
import { Agent } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type {
  Api,
  AssistantMessage,
  AssistantMessageEventStream,
  Model,
  StreamFn,
  TextContent,
  Usage,
} from "@earendil-works/pi-ai";
import type { AgentEvent, AgentMessage } from "@earendil-works/pi-agent-core";

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

  if (!Array.isArray(payload.tools) || payload.tools.length !== 0) {
    throw new Error("S1 requires an empty tools array");
  }
  if (!Array.isArray(payload.recovered_observations) || payload.recovered_observations.length !== 0) {
    throw new Error("S1 requires an empty recovered_observations array");
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
  if (!exactKeys(debug, ["fixture_response", "delay_ms"])) {
    throw new Error("debug fields are not closed");
  }
  if (typeof debug.fixture_response !== "string") throw new Error("debug.fixture_response must be a string");
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
  content: TextContent[],
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

function waitAbortOrDelay(signal: AbortSignal | undefined, ms: number): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onAbort = () => {
      if (timer) clearTimeout(timer);
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      if (!settled) {
        settled = true;
        resolve();
      }
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

// StreamFn contract (pi-agent-core/types.ts): must not throw or reject; failures
// are encoded as an event stream ending in done(stop/length/toolUse/deferred) or
// error(aborted/error). We drive the fixture entirely through pushed events.
function createFixtureStream(start: JsonObject): StreamFn {
  const debug = start.debug as JsonObject;
  const text = debug.fixture_response as string;
  const delayMs = debug.delay_ms as number;
  return (model, _context, options) => {
    const stream: AssistantMessageEventStream = createAssistantMessageEventStream();
    const empty = makeAssistantMessage(model, [], "pending");
    const final = makeAssistantMessage(model, [{ type: "text", text }], "stop");
    void (async () => {
      stream.push({ type: "start", partial: empty });
      stream.push({ type: "text_start", contentIndex: 0, partial: empty });
      await waitAbortOrDelay(options?.signal, delayMs);
      if (options?.signal?.aborted) {
        const aborted = makeAssistantMessage(model, [], "aborted", "aborted");
        stream.push({ type: "error", reason: "aborted", error: aborted });
        return;
      }
      stream.push({ type: "text_delta", contentIndex: 0, delta: text, partial: final });
      stream.push({ type: "text_end", contentIndex: 0, content: text, partial: final });
      stream.push({ type: "done", reason: "stop", message: final });
    })().catch((err) => {
      const failed = makeAssistantMessage(model, [], "error", String(err));
      stream.push({ type: "error", reason: "error", error: failed });
    });
    return stream;
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

// AgentEvent union (pi-agent-core/types.ts). message_start/message_update and
// tool_execution_* are intentionally skipped: the S1 protocol exposes only the
// lifecycle and turn-terminal events listed in _EVENT_TYPES on the Python side.
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
  return message.content
    .filter((block) => block.type === "text")
    .map((block) => (block as TextContent).text)
    .join("");
}

async function readActionLine(
  lines: AsyncGenerator<string, void, unknown>,
  expectedSeq: number,
  identity: Identity,
  cancelState: { cancelled: boolean },
  agent: Agent
): Promise<Envelope> {
  const next = await lines.next();
  if (next.done) throw new Error("stdin closed before action");
  const envelope = parseEnvelope(
    next.value,
    expectedSeq,
    identity,
    new Set(["run.cancel", "run.commit", "run.discard"])
  );
  if (envelope.type === "run.cancel" && !cancelState.cancelled) {
    cancelState.cancelled = true;
    agent.abort();
  }
  return envelope;
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
  const agent = new Agent({
    initialState: {
      systemPrompt,
      model,
      thinkingLevel: "off",
      tools: [],
      messages: [],
    },
    streamFn: createFixtureStream(payload),
    toolExecution: "sequential",
  });

  agent.subscribe((event) => {
    const normalized = normalizeAgentEvent(event);
    if (normalized) emitRun("agent.event", normalized);
  });

  const cancelState = { cancelled: false };
  const promptPromise = agent.prompt(payload.user_message as string);
  const actionPromise = readActionLine(lines, 2, identity, cancelState, agent).then(
    (env) => ({ env, err: null as Error | null }),
    (err) => ({ env: null as Envelope | null, err: err as Error })
  );

  let promptFailed = false;
  try {
    await promptPromise;
  } catch {
    promptFailed = true;
  }

  if (cancelState.cancelled) {
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
    emitRun("run.error", safeError("INTERNAL_ERROR", "runtime", "agent prompt failed", false));
    process.exitCode = 1;
    return;
  }

  const finalMessage = lastAssistant(agent);
  if (!finalMessage) {
    emitRun("run.error", safeError("INTERNAL_ERROR", "runtime", "no assistant message", false));
    process.exitCode = 1;
    return;
  }

  const stopReason = finalMessage.stopReason;
  const usage = normalizeUsage(finalMessage.usage ?? {});
  const text = extractText(finalMessage);

  if (stopReason === "stop" || stopReason === "length") {
    emitRun("run.proposed", {
      status: "answered",
      text,
      control_request: null,
      termination_reason: stopReason,
      usage,
    });
    const { env: action, err: actionErr } = await actionPromise;
    if (!action) {
      emitRun("run.error", safeError("PROTOCOL_ERROR", "protocol", actionErr?.message ?? "missing action", false));
      process.exitCode = 1;
      return;
    }
    if (action.type === "run.cancel") {
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
        committed: action.type === "run.commit",
      });
    }
    process.exitCode = 0;
    return;
  }

  emitRun("run.error", safeError("MODEL_ERROR", "model", "fixture stream error", false));
  process.exitCode = 1;
}

process.stdin.on("error", () => {});
run().catch((err) => {
  process.stderr.write(`diagnostic: ${String(err)}\n`);
  process.exitCode = 1;
});
