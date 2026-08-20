// S3 Node runtime: one real Pi Agent run with durable Pi Session history and
// sequential Host tools over
// `om-pi-ipc.v1` JSONL stdio.
//
// Pinned to @earendil-works/pi-agent-core@0.84.2 and @earendil-works/pi-ai@0.84.2.
// The protocol (envelope, start payload, terminal payload) is specified by
// docs/PI_AGENT_CORE_INTEGRATION.md sections 5, 11, and 12 and mirrored by
// src/infrastructure/pi_agent_process.py.

import process from "node:process";
import path from "node:path";
import {
  Agent,
  SessionError,
  buildSessionContext,
  compact,
  convertToLlm,
  createCompactionSummaryMessage,
  estimateContextTokens,
  estimateTokens,
  getLastAssistantUsage,
  prepareCompaction,
  shouldCompact,
  uuidv7,
} from "@earendil-works/pi-agent-core";
import { NodeExecutionEnv } from "@earendil-works/pi-agent-core/node";
import {
  createAssistantMessageEventStream,
  createModels,
  createProvider,
  envApiKeyAuth,
  isRetryableAssistantError,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { openAIResponsesApi } from "@earendil-works/pi-ai/api/openai-responses.lazy";
import type {
  Api,
  AssistantMessage,
  AssistantMessageEventStream,
  Context,
  FetchFunction,
  Model,
  Models,
  ProviderStreams,
  SimpleStreamOptions,
  StreamFn,
  StreamOptions,
  Usage,
} from "@earendil-works/pi-ai";
import type {
  AgentEvent,
  AgentMessage,
  AgentTool,
  Entry,
  Session,
} from "@earendil-works/pi-agent-core";
import {
  SqliteSessionRepository,
  createNodeSqliteFactory,
} from "@earendil-works/pi-session-backend-sqlite-node";

const PROTOCOL = "om-pi-ipc.v1";
const MAX_LINE_BYTES = 1_048_576;
const MAX_SAFE_MESSAGE_CHARS = 240;
const MAX_FIXTURE_DELAY_MS = 300_000;
const SESSION_ID_PATTERN = /^om_[0-9a-f]{64}$/;
const SESSION_SCHEMA = "om-pi-session.v1";
const TURN_COMMIT_TYPE = "om.turn.commit.v1";
const WRITER_LEASE_TTL_MS = 30_000;
const WRITER_HEARTBEAT_MS = 10_000;
const OLLAMA_LOCAL_API_KEY = "ollama-local";
const CONTROL_PREVIEW_TOOL = "request_control_preview";
const CONTINUATION_PROMPT =
  "Continue exactly where the previous answer stopped. Do not repeat earlier text. Return only the continuation.";
const PROVIDER_API_KINDS: Record<string, Api> = {
  openai: "openai-responses",
  deepseek: "openai-completions",
  kimi: "openai-completions",
  "kimi-code": "openai-completions",
  ollama: "openai-completions",
};
const COMPACTION_INSTRUCTIONS = [
  "Preserve the user's investment goals and stable preferences.",
  "Keep timestamps on historical claims and preserve unresolved questions and Control references.",
  "Never present remembered financial facts as current facts.",
  "Omit coding and file-operation guidance.",
].join(" ");

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
  resolve: (result: ToolBridgeResult) => void;
  reject: (error: Error) => void;
  signal?: AbortSignal;
  abortListener?: () => void;
}

interface ToolBridgeResult {
  observation: JsonObject;
  controlRequest: JsonObject | null;
}

interface SessionState {
  entries: Entry[];
  messages: AgentMessage[];
}

interface RequestCall {
  attempts: number;
  statuses: number[];
}

interface CompletedCall extends RequestCall {
  usage: JsonObject;
  usageTotal: JsonObject;
  modelRetryCount: number;
}

interface FinalizedCompaction {
  retryCount: number;
  usage: JsonObject;
}

class SafeRunFailure extends Error {
  readonly payload: JsonObject;

  constructor(payload: JsonObject) {
    super("safe run failure");
    this.payload = payload;
  }
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

function isControlRequest(value: unknown): value is JsonObject {
  return isRecord(value) &&
    exactKeys(value, ["intent_name", "arguments", "source", "confidence"]) &&
    isNonEmptyString(value.intent_name) &&
    isRecord(value.arguments) &&
    value.source === "copilot_control_preview" &&
    value.confidence === 1;
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
  if (
    payload.execution_environment !== "local" &&
    payload.execution_environment !== "eval" &&
    payload.execution_environment !== "channel"
  ) {
    throw new Error("execution_environment is not allowed");
  }
  if (payload.session_id !== null && (
    !isNonEmptyString(payload.session_id) || !SESSION_ID_PATTERN.test(payload.session_id)
  )) {
    throw new Error("session_id must be null or OM-derived");
  }
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
  if (!Array.isArray(payload.recovered_observations)) {
    throw new Error("recovered_observations must be an array");
  }
  for (const observation of payload.recovered_observations) {
    if (!isRecord(observation)) throw new Error("recovered observation must be an object");
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
  if (PROVIDER_API_KINDS[model.provider as string] !== model.api_kind) {
    throw new Error("model provider/API pair is not allowed");
  }
  const modelBounds: Record<string, [number, number]> = {
    timeout_seconds: [1, 120],
    context_window_tokens: [4_096, 2_000_000],
    max_output_tokens: [64, 4_096],
    max_attempts: [1, 3],
  };
  for (const [key, [minimum, maximum]] of Object.entries(modelBounds)) {
    const value = model[key];
    if (!isPositiveInteger(value) || value < minimum || value > maximum) {
      throw new Error(`model.${key} is outside the allowed range`);
    }
  }
  if (
    (model.context_window_tokens as number) <=
    (model.max_output_tokens as number) + 2_000
  ) {
    throw new Error("model.context_window_tokens must exceed max_output_tokens by more than 2000");
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

  if (payload.execution_environment !== "eval") {
    if (payload.debug !== null) throw new Error("debug must be null outside eval");
    return;
  }
  if (!isRecord(payload.debug)) throw new Error("eval debug must be an object");
  const debug = payload.debug;
  const commonDebugKeys = new Set([
    "delay_ms",
    "persist_delay_ms",
    "expected_history",
    "forbidden_history",
    "compaction_response",
  ]);
  const fixtureKey = Object.hasOwn(debug, "fixture_response")
    ? "fixture_response"
    : Object.hasOwn(debug, "fixture_turns")
      ? "fixture_turns"
      : null;
  if (
    fixtureKey === null ||
    Object.keys(debug).some((key) => key !== fixtureKey && !commonDebugKeys.has(key))
  ) {
    throw new Error("debug fields are not closed");
  }
  if (Object.hasOwn(debug, "fixture_response")) {
    if (typeof debug.fixture_response !== "string") {
      throw new Error("debug.fixture_response must be a string");
    }
  } else if (Object.hasOwn(debug, "fixture_turns")) {
    if (!Array.isArray(debug.fixture_turns)) {
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
  for (const key of ["expected_history", "forbidden_history"] as const) {
    if (
      Object.hasOwn(debug, key) &&
      (!Array.isArray(debug[key]) || !debug[key].every((item) => typeof item === "string"))
    ) {
      throw new Error(`debug.${key} must be a string array`);
    }
  }
  if (Object.hasOwn(debug, "compaction_response") && typeof debug.compaction_response !== "string") {
    throw new Error("debug.compaction_response must be a string");
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
  if (
    Object.hasOwn(debug, "persist_delay_ms") &&
    (!Number.isInteger(debug.persist_delay_ms) ||
      (debug.persist_delay_ms as number) < 0 ||
      (debug.persist_delay_ms as number) > MAX_FIXTURE_DELAY_MS)
  ) {
    throw new Error("debug.persist_delay_ms must be within [0, 300000]");
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
  const normalized: Model<Api> = {
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
  if (model.provider !== "openai") {
    normalized.compat = {
      supportsStore: false,
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
    };
  }
  return normalized;
}

class RunMetrics {
  private readonly pending: RequestCall[] = [];
  private retryCount = 0;
  private total: JsonObject = zeroPublicUsage();
  lastCompleted: CompletedCall | null = null;

  startCall(): RequestCall {
    const call = { attempts: 0, statuses: [] };
    this.pending.push(call);
    return call;
  }

  beginCompaction(): number {
    return this.pending.length;
  }

  finishTurn(message: AssistantMessage): CompletedCall {
    const call = this.pending.shift() ?? { attempts: 0, statuses: [] };
    this.retryCount += Math.max(call.attempts - 1, 0);
    const usage = normalizeUsage(message.usage ?? {});
    this.total = addPublicUsage(this.total, usage);
    const completed = {
      ...call,
      usage,
      usageTotal: { ...this.total },
      modelRetryCount: this.retryCount,
    };
    this.lastCompleted = completed;
    return completed;
  }

  finishCompaction(
    pendingStart: number,
    usage: Record<string, unknown>
  ): FinalizedCompaction {
    const calls = this.pending.splice(pendingStart);
    return {
      retryCount: calls.reduce(
        (total, call) => total + Math.max(call.attempts - 1, 0),
        0
      ),
      usage: normalizeUsage(usage),
    };
  }

  commitCompaction(compaction: FinalizedCompaction): void {
    this.retryCount += compaction.retryCount;
    this.total = addPublicUsage(this.total, compaction.usage);
  }

  usageTotal(): JsonObject {
    return { ...this.total };
  }
}

function zeroPublicUsage(): JsonObject {
  return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 };
}

function addPublicUsage(left: JsonObject, right: JsonObject): JsonObject {
  const out = zeroPublicUsage();
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]) {
    out[key] = ((left[key] as number | undefined) ?? 0) +
      ((right[key] as number | undefined) ?? 0);
  }
  return out;
}

function countingFetch(call: RequestCall, delegate: FetchFunction): FetchFunction {
  return async (input, init) => {
    call.attempts += 1;
    const response = await delegate(input, init);
    call.statuses.push(response.status);
    return response;
  };
}

function withOmRequestPolicy(
  api: ProviderStreams,
  start: JsonObject,
  model: Model<Api>,
  metrics: RunMetrics,
  remainingSceneMs: () => number
): ProviderStreams {
  const raw = start.model as JsonObject;
  const modelTimeoutMs = (raw.timeout_seconds as number) * 1_000;
  const maxAttempts = raw.max_attempts as number;

  const requestOptions = <T extends StreamOptions>(options: T | undefined): T => {
    const remaining = Math.max(1, Math.floor(remainingSceneMs()));
    const callerTimeout = options?.timeoutMs ?? modelTimeoutMs;
    const callerMaxTokens = options?.maxTokens ?? model.maxTokens;
    const call = metrics.startCall();
    const delegate = options?.fetch ?? globalThis.fetch;
    const fixed: StreamOptions = {
      ...options,
      timeoutMs: Math.max(1, Math.min(modelTimeoutMs, callerTimeout, remaining)),
      maxTokens: Math.min(callerMaxTokens, model.maxTokens),
      maxRetries: maxAttempts - 1,
      maxRetryDelayMs: Math.max(
        1,
        Math.min(options?.maxRetryDelayMs || remaining, remaining)
      ),
      fetch: countingFetch(call, delegate),
    };
    if (model.provider === "openai" || model.provider === "deepseek" || model.provider === "ollama") {
      fixed.temperature = 0;
    }
    if (model.provider === "deepseek") {
      fixed.samplingParams = {
        ...(options?.samplingParams ?? {}),
        thinking: { type: "disabled" },
      };
    }
    return fixed as T;
  };

  return {
    stream: (selected, context, options) =>
      api.stream(selected, context, requestOptions(options)),
    streamSimple: (selected, context, options) =>
      api.streamSimple(
        selected,
        context,
        requestOptions(options as SimpleStreamOptions) as SimpleStreamOptions
      ),
  };
}

function createProviderModels(
  start: JsonObject,
  model: Model<Api>,
  metrics: RunMetrics,
  remainingSceneMs: () => number
): Models {
  const baseApi = model.api === "openai-responses"
    ? openAIResponsesApi()
    : openAICompletionsApi();
  const api = withOmRequestPolicy(baseApi, start, model, metrics, remainingSceneMs);
  const apiKey = model.provider === "ollama"
    ? {
        name: "Ollama local",
        resolve: async ({ signal }: { signal: AbortSignal }) => {
          signal.throwIfAborted();
          return {
            auth: { apiKey: OLLAMA_LOCAL_API_KEY },
            source: "process-local sentinel",
          };
        },
      }
    : envApiKeyAuth("OM model API key", ["OM_PI_MODEL_API_KEY"]);
  const provider = createProvider({
    id: model.provider,
    name: model.provider,
    baseUrl: model.baseUrl,
    auth: { apiKey },
    models: [model],
    api,
  });
  const models = createModels();
  models.setProvider(provider);
  return models;
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
      const persistedHistory = stableJson(context.messages);
      let expectedOffset = 0;
      for (const expected of (debug.expected_history ?? []) as string[]) {
        const found = persistedHistory.indexOf(expected, expectedOffset);
        if (found < 0) throw new Error("fixture history mismatch");
        expectedOffset = found + expected.length;
      }
      for (const forbidden of (debug.forbidden_history ?? []) as string[]) {
        if (persistedHistory.includes(forbidden)) throw new Error("fixture history leak");
      }
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

function createFixtureModels(start: JsonObject): Models {
  const debug = start.debug as JsonObject;
  const delayMs = debug.delay_ms as number;
  return {
    completeSimple: async (model, _context, options) => {
      await waitAbortOrDelay(options?.signal, delayMs);
      if (options?.signal?.aborted) {
        return makeAssistantMessage(model, [], "aborted", "aborted");
      }
      if (
        typeof debug.compaction_response !== "string" ||
        debug.compaction_response.trim().length === 0
      ) {
        return makeAssistantMessage(model, [], "error", "fixture compaction failed");
      }
      return makeAssistantMessage(
        model,
        [{ type: "text", text: debug.compaction_response }],
        "stop"
      );
    },
  } as Models;
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
  controlRequest: () => JsonObject | null;
} {
  let pending: PendingTool | null = null;
  let controlRequest: JsonObject | null = null;

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
      const result = await new Promise<ToolBridgeResult>((resolve, reject) => {
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
        content: [{ type: "text", text: stableJson(result.observation) }],
        details: { observation: result.observation },
        terminate: result.controlRequest !== null,
      };
    },
  }));

  return {
    tools,
    acceptResult(result) {
      const hasControlRequest = Object.hasOwn(result, "control_request");
      if (
        !exactKeys(
          result,
          hasControlRequest
            ? ["call_id", "tool_name", "observation", "control_request"]
            : ["call_id", "tool_name", "observation"]
        ) ||
        !isNonEmptyString(result.call_id) ||
        !isNonEmptyString(result.tool_name) ||
        !isRecord(result.observation) ||
        !pending ||
        result.call_id !== pending.callId ||
        result.tool_name !== pending.toolName
      ) {
        throw new Error("tool result mismatch");
      }
      if (
        hasControlRequest &&
        (pending.toolName !== CONTROL_PREVIEW_TOOL ||
          controlRequest !== null ||
          !isControlRequest(result.control_request) ||
          result.observation.ok !== true ||
          result.observation.status !== "preview_requested")
      ) {
        throw new Error("control tool result mismatch");
      }
      if (
        pending.toolName === CONTROL_PREVIEW_TOOL &&
        !hasControlRequest &&
        result.observation.ok !== false
      ) {
        throw new Error("control tool result is incomplete");
      }
      const current = pending;
      pending = null;
      if (current.signal && current.abortListener) {
        current.signal.removeEventListener("abort", current.abortListener);
      }
      if (hasControlRequest) controlRequest = result.control_request as JsonObject;
      current.resolve({
        observation: result.observation,
        controlRequest: hasControlRequest ? controlRequest : null,
      });
    },
    cancelPending() {
      settlePending(new Error("tool call aborted"));
    },
    rejectPending() {
      settlePending(new Error("tool bridge failed"));
    },
    hasPending: () => pending !== null,
    controlRequest: () => controlRequest,
  };
}

function effectiveSystemPrompt(
  systemPrompt: string,
  runtimeContext: JsonObject[],
  recoveredObservations: JsonObject[]
): string {
  const parts = [systemPrompt];
  if (runtimeContext.length > 0) {
    parts.push(
      `<om-runtime-context>\n${runtimeContext.map((item) => item.content as string).join("\n\n")}\n</om-runtime-context>`
    );
  }
  if (recoveredObservations.length > 0) {
    parts.push(
      `<om-recovered-observations>\n${stableJson(recoveredObservations)}\n</om-recovered-observations>`
    );
  }
  return parts.join("\n\n");
}

function isCommitMarker(entry: Entry): boolean {
  return entry.type === "custom" &&
    entry.customType === TURN_COMMIT_TYPE &&
    isRecord(entry.data) &&
    exactKeys(entry.data, ["run_id", "kind"]) &&
    isNonEmptyString(entry.data.run_id) &&
    (entry.data.kind === "turn" || entry.data.kind === "compaction");
}

async function loadCommittedState(session: Session): Promise<SessionState> {
  const reachable = await session.findEntriesOnBranch({ order: "oldestFirst" });
  let committedLeaf: string | null = null;
  for (const entry of reachable) {
    if (isCommitMarker(entry)) committedLeaf = entry.id;
  }
  await session.moveLane("main", committedLeaf);
  const entries = await session.findEntriesOnBranch({ order: "oldestFirst" });
  return { entries, messages: buildSessionContext(entries).messages };
}

async function openPiSession(sessionId: string): Promise<{
  repository: SqliteSessionRepository;
  session: Session;
  state: SessionState;
}> {
  const databasePath = process.env.OM_PI_SESSION_DB;
  if (
    !isNonEmptyString(databasePath) ||
    databasePath.includes("\0") ||
    !path.isAbsolute(databasePath)
  ) {
    throw new SafeRunFailure(
      safeError("SESSION_ERROR", "session", "session storage is unavailable", false)
    );
  }

  const repositoryRoot = process.cwd();
  const repository = new SqliteSessionRepository({
    env: new NodeExecutionEnv({ cwd: repositoryRoot }),
    sqlite: createNodeSqliteFactory(),
    databasePath,
    writerLease: {
      ttlMs: WRITER_LEASE_TTL_MS,
      heartbeatIntervalMs: WRITER_HEARTBEAT_MS,
    },
  });
  try {
    const metadata = (await repository.list()).find((candidate) => candidate.id === sessionId);
    let session: Session;
    if (metadata) {
      if (
        !isRecord(metadata.metadata) ||
        !exactKeys(metadata.metadata, ["schema"]) ||
        metadata.metadata.schema !== SESSION_SCHEMA
      ) {
        throw new SafeRunFailure(
          safeError("SESSION_ERROR", "session", "session storage is unavailable", false)
        );
      }
      session = await repository.open(metadata);
    } else {
      session = await repository.create({
        id: sessionId,
        cwd: repositoryRoot,
        metadata: { schema: SESSION_SCHEMA },
      });
    }
    return { repository, session, state: await loadCommittedState(session) };
  } catch (error) {
    await repository.close().catch(() => {});
    throw error;
  }
}

function mapSessionFailure(error: unknown): JsonObject {
  if (error instanceof SafeRunFailure) return error.payload;
  const retryable = error instanceof SessionError &&
    error.code === "storage" &&
    error.message.includes("active writer");
  return safeError(
    "SESSION_ERROR",
    "session",
    retryable ? "session is temporarily busy" : "session storage is unavailable",
    retryable
  );
}

function textTokenEstimate(text: string): number {
  return estimateTokens({
    role: "user",
    content: [{ type: "text", text }],
    timestamp: 0,
  });
}

function estimateStructuralContextTokens(messages: AgentMessage[]): number {
  return messages.reduce((tokens, message) => tokens + estimateTokens(message), 0);
}

function estimateSessionContextTokens(state: SessionState): number {
  const compactionIndex = state.entries.findLastIndex((entry) => entry.type === "compaction");
  if (
    compactionIndex < 0 ||
    getLastAssistantUsage(state.entries.slice(compactionIndex + 1)) !== undefined
  ) {
    return estimateContextTokens(state.messages).tokens;
  }
  return estimateStructuralContextTokens(state.messages);
}

async function prepareSessionState(
  session: Session | null,
  state: SessionState,
  start: JsonObject,
  model: Model<Api>,
  models: Models,
  metrics: RunMetrics,
  systemPrompt: string,
  signal: AbortSignal,
  runId: string
): Promise<SessionState> {
  const limits = start.limits as JsonObject;
  const contextWindow = Math.min(
    model.contextWindow,
    limits.max_context_tokens as number
  );
  const reserveTokens = model.maxTokens;
  const fixedInputTokens = textTokenEstimate(systemPrompt) +
    textTokenEstimate(start.user_message as string);
  const usableHistoryTokens = contextWindow - reserveTokens - fixedInputTokens;
  if (usableHistoryTokens <= 2_000) {
    throw new SafeRunFailure(
      safeError("CONFIG_ERROR", "config", "configured context budget is too small", false)
    );
  }

  const settings = {
    enabled: true,
    reserveTokens,
    keepRecentTokens: Math.max(2_000, Math.floor(usableHistoryTokens / 2)),
  };
  const historyTokens = estimateSessionContextTokens(state);
  const contextTokens = fixedInputTokens + historyTokens;
  if (!shouldCompact(contextTokens, contextWindow, settings)) return state;
  if (!session) {
    throw new SafeRunFailure(
      safeError("CONFIG_ERROR", "config", "input exceeds configured context budget", false)
    );
  }

  const preparation = prepareCompaction(state.entries, settings);
  if (!preparation.ok || preparation.value === undefined) {
    throw new SafeRunFailure(
      safeError("SESSION_ERROR", "session", "session context compaction failed", false)
    );
  }
  const metricsStart = metrics.beginCompaction();
  let result;
  try {
    result = await compact(
      preparation.value,
      models,
      model,
      COMPACTION_INSTRUCTIONS,
      signal,
      "off",
      { enabled: false, maxRetries: 0, baseDelayMs: 0 }
    );
  } catch (error) {
    metrics.finishCompaction(metricsStart, {});
    throw error;
  }
  const compactionMetrics = metrics.finishCompaction(
    metricsStart,
    result.ok && result.value ? result.value.usage : {}
  );
  if (!result.ok || signal.aborted) {
    throw new SafeRunFailure(
      safeError("SESSION_ERROR", "session", "session context compaction failed", false)
    );
  }

  const compactedMessages = [
    createCompactionSummaryMessage(
      result.value.summary,
      result.value.tokensBefore,
      Date.now()
    ),
    ...result.value.retainedTail,
  ];
  const compactedTokens = fixedInputTokens + estimateStructuralContextTokens(compactedMessages);
  if (compactedTokens > contextWindow - reserveTokens) {
    throw new SafeRunFailure(
      safeError("SESSION_ERROR", "session", "compacted context exceeds configured budget", false)
    );
  }

  await session.appendEntry(
    { type: "compaction", id: uuidv7(), ...result.value },
    "main"
  );
  await session.appendCustomEntry(TURN_COMMIT_TYPE, { run_id: runId, kind: "compaction" });
  metrics.commitCompaction(compactionMetrics);
  return loadCommittedState(session);
}

function validatedTurnSuffix(messages: AgentMessage[], startIndex: number): AgentMessage[] {
  const suffix = messages.slice(startIndex);
  if (suffix.length < 2 || suffix[0].role !== "user") {
    throw new Error("turn suffix does not start with a user message");
  }
  let index = 1;
  while (index < suffix.length) {
    const assistant = suffix[index];
    if (assistant.role !== "assistant") throw new Error("turn suffix group is incomplete");
    index += 1;
    const calls = assistant.content.filter((item) => item.type === "toolCall");
    for (const call of calls) {
      const result = suffix[index];
      if (
        !result ||
        result.role !== "toolResult" ||
        result.toolCallId !== call.id ||
        result.toolName !== call.name
      ) {
        throw new Error("turn suffix tool result is incomplete");
      }
      index += 1;
    }
  }
  const last = suffix[suffix.length - 1];
  if (
    last.role !== "assistant" ||
    (last.stopReason !== "stop" && last.stopReason !== "length") ||
    last.content.some((item) => item.type === "toolCall")
  ) {
    throw new Error("turn suffix has no final answer");
  }
  return suffix;
}

function validatedControlTurnSuffix(
  messages: AgentMessage[],
  startIndex: number
): AgentMessage[] {
  const suffix = messages.slice(startIndex);
  if (
    suffix.length !== 3 ||
    suffix[0].role !== "user" ||
    suffix[1].role !== "assistant" ||
    suffix[2].role !== "toolResult"
  ) {
    throw new Error("control turn suffix is incomplete");
  }
  const calls = suffix[1].content.filter((item) => item.type === "toolCall");
  if (
    calls.length !== 1 ||
    calls[0].name !== CONTROL_PREVIEW_TOOL ||
    suffix[2].toolCallId !== calls[0].id ||
    suffix[2].toolName !== CONTROL_PREVIEW_TOOL
  ) {
    throw new Error("control turn suffix is invalid");
  }
  return suffix;
}

function normalizeContinuationSuffix(
  messages: AgentMessage[],
  startIndex: number
): AgentMessage[] {
  const suffix = messages.slice(startIndex);
  if (suffix.length !== 4) return suffix;
  const [user, first, synthetic, final] = suffix;
  if (
    user.role !== "user" ||
    first.role !== "assistant" ||
    first.stopReason !== "length" ||
    first.content.some((item) => item.type === "toolCall") ||
    synthetic.role !== "user" ||
    synthetic.content.length !== 1 ||
    synthetic.content[0].type !== "text" ||
    synthetic.content[0].text !== CONTINUATION_PROMPT ||
    final.role !== "assistant" ||
    final.content.some((item) => item.type === "toolCall") ||
    (final.stopReason !== "stop" && final.stopReason !== "length")
  ) {
    return suffix;
  }
  const merged: AssistantMessage = {
    ...final,
    content: [{ type: "text", text: extractText(first) + extractText(final) }],
    usage: addAssistantUsage(first.usage, final.usage),
  };
  return [user, merged];
}

async function persistTurn(
  session: Session,
  messages: AgentMessage[],
  runId: string,
  persistDelayMs: number
): Promise<void> {
  for (const message of messages) {
    await session.appendMessage(JSON.parse(JSON.stringify(message)) as AgentMessage);
    if (persistDelayMs > 0) await waitAbortOrDelay(undefined, persistDelayMs);
  }
  await session.appendCustomEntry(TURN_COMMIT_TYPE, { run_id: runId, kind: "turn" });
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

function addAssistantUsage(left: Usage, right: Usage): Usage {
  const usage = emptyUsage();
  for (const key of ["input", "output", "cacheRead", "cacheWrite", "totalTokens"] as const) {
    usage[key] = Math.max(0, Number(left[key]) || 0) + Math.max(0, Number(right[key]) || 0);
  }
  usage.cost = {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    total: 0,
  };
  return usage;
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
function normalizeAgentEvent(
  event: AgentEvent,
  metrics: RunMetrics
): { event_type: string; data: JsonObject } | null {
  switch (event.type) {
    case "agent_start":
      return { event_type: "agent_start", data: {} };
    case "turn_start":
      return { event_type: "turn_start", data: {} };
    case "agent_end":
      return { event_type: "agent_end", data: {} };
    case "message_end": {
      if ((event.message as { role?: unknown }).role !== "assistant") return null;
      const completed = metrics.finishTurn(event.message as AssistantMessage);
      return {
        event_type: "model_turn_completed",
        data: {
          stop_reason: (event.message as AssistantMessage).stopReason,
          attempt_count: completed.attempts,
          model_retry_count: completed.modelRetryCount,
          usage: completed.usage,
          usage_total: completed.usageTotal,
        },
      };
    }
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

function safeModelFailure(
  message: AssistantMessage,
  call: CompletedCall | null
): JsonObject {
  const statuses = call?.statuses ?? [];
  if ((call?.attempts ?? 0) === 0 || statuses.some((status) => status === 401 || status === 403)) {
    return safeError("MODEL_ERROR", "model", "model authentication failed", false);
  }
  const invalidResponse = statuses.some((status) => status >= 200 && status < 300);
  return safeError(
    "MODEL_ERROR",
    "model",
    invalidResponse ? "model response was invalid" : "model request failed",
    isRetryableAssistantError(message)
  );
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
  const sessionId = payload.session_id as string | null;
  let repository: SqliteSessionRepository | null = null;
  let session: Session | null = null;
  let sessionState: SessionState = { entries: [], messages: [] };
  try {
    if (sessionId !== null) {
      const opened = await openPiSession(sessionId);
      repository = opened.repository;
      session = opened.session;
      sessionState = opened.state;
    }
  } catch (error) {
    emitRun("run.error", mapSessionFailure(error));
    process.exitCode = 1;
    return;
  }

  try {
    emitRun("run.accepted", {
      runtime: "pi-agent-core",
      runtime_version: "0.84.2",
      session_id: sessionId,
    });

    const model = modelFromStart(payload);
    const systemPrompt = effectiveSystemPrompt(
      payload.system_prompt as string,
      payload.runtime_context as JsonObject[],
      payload.recovered_observations as JsonObject[]
    );
    const limits = payload.limits as JsonObject;
    const startedAt = performance.now();
    const remainingSceneMs = (): number =>
      (limits.timeout_seconds as number) * 1_000 - (performance.now() - startedAt);
    const runAbort = new AbortController();
    const metrics = new RunMetrics();
    const providerModels = payload.execution_environment === "eval"
      ? createFixtureModels(payload)
      : createProviderModels(payload, model, metrics, remainingSceneMs);
    const streamFn = payload.execution_environment === "eval"
      ? createFixtureStream(payload)
      : providerModels.streamSimple.bind(providerModels);
    let assistantTurns = 0;
    let finalizedToolCalls = 0;
    let consecutiveFailedToolBatches = 0;
    let forcedFinalAtTurn: number | null = null;
    let continuationUsed = false;
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

    const finishError = (error: JsonObject): void => {
      inbound.terminal = true;
      emitRun("run.error", error);
      process.exitCode = 1;
    };
    const finishCancelled = (usage: JsonObject): void => {
      inbound.terminal = true;
      emitRun("run.final", {
        status: "cancelled",
        text: "",
        control_request: null,
        termination_reason: "aborted",
        usage,
        committed: false,
      });
      process.exitCode = 0;
    };

    const bridge = createToolBridge(payload, emitRun, (message) => {
      if (!bridgeFailure) {
        bridgeFailure = safeError("TOOL_BRIDGE_ERROR", "tool", message, false);
        runAbort.abort();
        agent?.abort();
      }
    });

    const failInbound = (): void => {
      if (inbound.terminal || inbound.error) return;
      inbound.error = safeError("PROTOCOL_ERROR", "protocol", "invalid host record", false);
      bridge.rejectPending();
      resolveAction(null);
      runAbort.abort();
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
            runAbort.abort();
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
    void pumpInbound();

    const compactionCountBefore = sessionState.entries.filter(
      (entry) => entry.type === "compaction"
    ).length;
    try {
      sessionState = await prepareSessionState(
        session,
        sessionState,
        payload,
        model,
        providerModels,
        metrics,
        systemPrompt,
        runAbort.signal,
        identity.runId
      );
    } catch (error) {
      if (inbound.error) finishError(inbound.error);
      else if (inbound.cancelled) finishCancelled(metrics.usageTotal());
      else finishError(mapSessionFailure(error));
      return;
    }
    const committedCompactions = sessionState.entries.filter(
      (entry) => entry.type === "compaction"
    ).length - compactionCountBefore;
    if (committedCompactions > 0) {
      emitRun("agent.event", {
        event_type: "context_compaction_committed",
        data: {
          compaction_count: committedCompactions,
          usage_total: metrics.usageTotal(),
        },
      });
    }
    if (inbound.error) {
      finishError(inbound.error);
      return;
    }
    if (inbound.cancelled) {
      finishCancelled(metrics.usageTotal());
      return;
    }

    agent = new Agent({
      initialState: {
        systemPrompt,
        model,
        thinkingLevel: "off",
        tools: bridge.tools,
        messages: sessionState.messages,
      },
      streamFn,
      convertToLlm,
      toolExecution: "sequential",
      beforeToolCall: async ({ assistantMessage, toolCall }) => {
        const calls = assistantMessage.content.filter((item) => item.type === "toolCall");
        const controlCalls = calls.filter((item) => item.name === CONTROL_PREVIEW_TOOL);
        if (controlCalls.length > 0 && (calls.length !== 1 || controlCalls.length !== 1)) {
          return {
            block: true,
            reason: stableJson({
              tool_name: toolCall.name,
              ok: false,
              status: "failed",
              error: "INVALID_ACTION",
              code: "INVALID_ACTION",
              message: "control preview must be the only call in its tool batch",
              retryable: false,
            }),
          };
        }
        return undefined;
      },
      afterToolCall: async ({ result }) => {
        const details = result.details;
        return isRecord(details) &&
          isRecord(details.observation) &&
          details.observation.ok === false
          ? { isError: true }
          : undefined;
      },
      prepareNextTurnWithContext: ({ context, message, newMessages, toolResults }) => {
        const remainingMs = remainingSceneMs();
        const eligibleContinuation =
          !continuationUsed &&
          forcedFinalAtTurn === null &&
          message.stopReason === "length" &&
          extractText(message).trim().length > 0 &&
          message.content.every((item) => item.type !== "toolCall") &&
          newMessages.length === 2 &&
          newMessages[0].role === "user" &&
          newMessages[1] === message &&
          assistantTurns < (limits.max_iterations as number) &&
          remainingMs > (limits.final_answer_reserve_seconds as number) * 1_000;
        if (eligibleContinuation) {
          continuationUsed = true;
          agent?.followUp({
            role: "user",
            content: [{ type: "text", text: CONTINUATION_PROMPT }],
            timestamp: Date.now(),
          });
          return { context: { ...context, tools: [] } };
        }
        if (forcedFinalAtTurn !== null || toolResults.length === 0) return undefined;
        const exhausted =
          assistantTurns >= (limits.max_iterations as number) ||
          finalizedToolCalls >= (limits.max_tool_calls as number) ||
          consecutiveFailedToolBatches >=
            (limits.max_consecutive_failed_tool_batches as number) ||
          remainingMs <= (limits.final_answer_reserve_seconds as number) * 1000;
        if (!exhausted) return undefined;
        forcedFinalAtTurn = assistantTurns;
        emitRun("agent.event", {
          event_type: "forced_final_activated",
          data: {
            reason: assistantTurns >= (limits.max_iterations as number)
              ? "model_turn_limit"
              : finalizedToolCalls >= (limits.max_tool_calls as number)
                ? "tool_call_limit"
                : consecutiveFailedToolBatches >=
                    (limits.max_consecutive_failed_tool_batches as number)
                  ? "tool_failure_limit"
                  : "time_reserve",
          },
        });
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
      const normalized = normalizeAgentEvent(event, metrics);
      if (normalized) emitRun("agent.event", normalized);
    });

    const suffixStart = agent.state.messages.length;
    let promptFailed = false;
    try {
      await agent.prompt(payload.user_message as string);
    } catch {
      promptFailed = true;
    }

    if (inbound.error) {
      finishError(inbound.error);
      return;
    }
    if (bridgeFailure) {
      finishError(bridgeFailure);
      return;
    }
    if (inbound.cancelled) {
      finishCancelled(metrics.usageTotal());
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

    let status: "answered" | "control_requested";
    let text: string;
    let controlRequest: JsonObject | null;
    let terminationReason: string;
    let turnSuffix: AgentMessage[];
    controlRequest = bridge.controlRequest();
    if (controlRequest !== null) {
      status = "control_requested";
      text = "";
      terminationReason = "control_preview_requested";
      try {
        turnSuffix = validatedControlTurnSuffix(agent.state.messages, suffixStart);
      } catch {
        finishError(safeError("INTERNAL_ERROR", "runtime", "invalid control turn", false));
        return;
      }
    } else {
      const stopReason = finalMessage.stopReason;
      const forcedFinalCompleted =
        forcedFinalAtTurn !== null && assistantTurns > forcedFinalAtTurn;
      if (forcedFinalCompleted && extractText(finalMessage).trim() === "") {
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
      if (stopReason !== "stop" && stopReason !== "length") {
        finishError(safeModelFailure(finalMessage, metrics.lastCompleted));
        return;
      }
      status = "answered";
      try {
        turnSuffix = normalizeContinuationSuffix(agent.state.messages, suffixStart);
        turnSuffix = validatedTurnSuffix(turnSuffix, 0);
      } catch {
        finishError(safeError("INTERNAL_ERROR", "runtime", "invalid completed turn", false));
        return;
      }
      const canonicalFinal = turnSuffix[turnSuffix.length - 1] as AssistantMessage;
      text = extractText(canonicalFinal);
      terminationReason = canonicalFinal.stopReason;
      if (text === "") {
        finishError(safeError("MODEL_ERROR", "model", "model response was invalid", false));
        return;
      }
    }

    const usage = metrics.usageTotal();
    inbound.awaitingAdmission = true;
    emitRun("run.proposed", {
      status,
      text,
      control_request: controlRequest,
      termination_reason: terminationReason,
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
    if (action === "run.cancel") {
      finishCancelled(usage);
      return;
    }
    if (action === "run.commit" && session) {
      try {
        await persistTurn(
          session,
          turnSuffix,
          identity.runId,
          (isRecord(payload.debug)
            ? (payload.debug.persist_delay_ms as number | undefined)
            : undefined) ?? 0
        );
      } catch (error) {
        finishError(mapSessionFailure(error));
        return;
      }
    }
    inbound.terminal = true;
    emitRun("run.final", {
      status,
      text,
      control_request: controlRequest,
      termination_reason: terminationReason,
      usage,
      committed: action === "run.commit",
    });
    process.exitCode = 0;
    return;
  } finally {
    if (repository) await repository.close().catch(() => {});
  }
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
