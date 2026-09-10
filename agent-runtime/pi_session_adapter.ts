import path from "node:path";
import { lstat, readFile, realpath } from "node:fs/promises";
import {
  TODO_CONTEXT,
  branchTip,
  createCompactionSummaryMessage,
  insertEntry,
  setValue,
  value,
} from "@earendil-works/pi-agent-core";
import type {
  AgentMessage,
  CompactResult,
  Entry,
  JsonValue,
  NewEntry,
  Session,
} from "@earendil-works/pi-agent-core";
import {
  SqliteSessionRepo,
  SqliteStorage,
  createNodeSqliteFactory,
} from "@earendil-works/pi-session-backend-sqlite-node";
import type {
  SqliteSessionMetadata,
} from "@earendil-works/pi-session-backend-sqlite-node";
import {
  OM_SESSION_FORMAT,
  OM_TURN_COMMIT_TYPE,
  exportCommittedMain as exportCommittedMainCore,
  isCommitMarker,
  loadCommittedContext as loadCommittedContextCore,
  probeTargetStore as probeTargetStoreCore,
  readCommittedMain as readCommittedMainCore,
  validateToolGroups,
} from "./pi_session_core.mjs";

export { OM_SESSION_FORMAT, OM_TURN_COMMIT_TYPE };
export const OM_SESSION_FORMAT_VALUE = value<string>("om.pi", "session-format");
const MAIN_BRANCH = "main";
const MIGRATION_RECEIPT_VERSION = "om-pi-migration.v1";

export type PiStoreFormat = "missing" | "target" | "legacy" | "unknown" | "corrupt";

export interface PiStoreProbe {
  format: PiStoreFormat;
  sessionIds: string[];
  writerLeaseCount: number;
}

export interface CommittedSessionState {
  entries: Entry[];
  messages: AgentMessage[];
}

export interface OpenedPiSession {
  repository: SqliteSessionRepo;
  session: Session<SqliteSessionMetadata>;
  state: CommittedSessionState;
}

const CORE_API = {
  context: TODO_CONTEXT,
  branchTip,
  createCompactionSummaryMessage,
  insertEntry,
  setValue,
  value,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
  const keys = Object.keys(value);
  return required.every((key) => keys.includes(key)) &&
    keys.every((key) => required.includes(key) || optional.includes(key));
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

const PI_RUNTIME_PACKAGES = [
  "@earendil-works/pi-agent-core",
  "@earendil-works/pi-ai",
  "@earendil-works/pi-session-backend-sqlite-node",
];

function isRuntimeIdentity(value: unknown, expectedVersion: "0.84.2" | "0.85.1"): boolean {
  if (!isRecord(value) || value.version !== expectedVersion || value.ok !== true ||
      !isSha256(value.lockSha256) || !isRecord(value.packages) ||
      Object.keys(value.packages).sort().join("\0") !== [...PI_RUNTIME_PACKAGES].sort().join("\0")) {
    return false;
  }
  return PI_RUNTIME_PACKAGES.every((name) => {
    const installed = value.packages[name];
    return isRecord(installed) && installed.version === expectedVersion &&
      isSha256(installed.manifestSha256) && isSha256(installed.entrySha256);
  });
}

function isConversionSummary(value: unknown): boolean {
  return isRecord(value) && exactKeys(value, [
    "sessionCount", "entryCount", "excludedTailCount",
    "sourceCanonicalSha256", "targetCanonicalSha256",
  ]) && [value.sessionCount, value.entryCount, value.excludedTailCount].every((item) =>
    Number.isSafeInteger(item) && (item as number) >= 0) &&
    isSha256(value.sourceCanonicalSha256) && isSha256(value.targetCanonicalSha256);
}

async function assertMigrationReceiptAllowsStartup(databasePath: string): Promise<void> {
  const receiptPath = `${databasePath}.om-pi-migration.json`;
  let receiptInfo;
  try {
    receiptInfo = await lstat(receiptPath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw new Error("Pi migration receipt is unavailable");
  }
  if (!receiptInfo.isFile() || receiptInfo.isSymbolicLink() ||
      (receiptInfo.mode & 0o077) !== 0 || receiptInfo.uid !== process.getuid?.()) {
    throw new Error("Pi migration receipt is invalid");
  }
  let receipt: unknown;
  try {
    receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  } catch {
    throw new Error("Pi migration receipt is invalid");
  }
  if (!isRecord(receipt) || !exactKeys(
    receipt,
    ["version", "database", "phase", "rollbackRequired", "source", "target", "backup"],
    ["readback", "published", "prior", "conversion"],
  ) || receipt.version !== MIGRATION_RECEIPT_VERSION || receipt.rollbackRequired !== true ||
      !["prepared", "validated", "published"].includes(receipt.phase as string) ||
      (receipt.conversion !== undefined && !isConversionSummary(receipt.conversion))) {
    throw new Error("Pi migration receipt is invalid");
  }
  let canonicalDatabase: string;
  try {
    canonicalDatabase = await realpath(databasePath);
  } catch {
    throw new Error("Pi migration receipt does not match the session database");
  }
  if (receipt.database !== canonicalDatabase) {
    throw new Error("Pi migration receipt does not match the session database");
  }
  if (receipt.phase !== "published") {
    throw new Error("Pi migration publication is incomplete");
  }
  const source = receipt.source;
  const target = receipt.target;
  const backup = receipt.backup;
  const readback = receipt.readback;
  const published = receipt.published;
  const backupPrefix = `${canonicalDatabase}.om-pi-recovery-0.84.2-to-0.85.1-`;
  const backupSuffix = ".sqlite3";
  const backupPath = isRecord(backup) && typeof backup.path === "string" ? backup.path : "";
  const backupGeneration = backupPath.startsWith(backupPrefix) && backupPath.endsWith(backupSuffix)
    ? backupPath.slice(backupPrefix.length, -backupSuffix.length)
    : "";
  if (!isRecord(source) || !exactKeys(source, ["runtimePath", "runtime", "database"]) ||
      typeof source.runtimePath !== "string" || !path.isAbsolute(source.runtimePath) ||
      !isRuntimeIdentity(source.runtime, "0.84.2") || !isRecord(source.database) ||
      !exactKeys(source.database, ["sha256", "format"]) ||
      !isSha256(source.database.sha256) || source.database.format !== "legacy" ||
      !isRecord(target) || !exactKeys(target, ["runtimePath", "runtime", "database"]) ||
      typeof target.runtimePath !== "string" || !path.isAbsolute(target.runtimePath) ||
      !isRuntimeIdentity(target.runtime, "0.85.1") ||
      !isRecord(target.database) || !exactKeys(target.database, ["sha256", "format"]) ||
      !isSha256(target.database.sha256) || target.database.format !== "target" ||
      !isRecord(backup) || !exactKeys(backup, ["path", "sha256"]) ||
      !/^[0-9a-f]{32}$/.test(backupGeneration) ||
      !isSha256(backup.sha256) ||
      !isRecord(readback) || !exactKeys(readback, ["equivalent", "contextSha256"]) ||
      readback.equivalent !== true || !isSha256(readback.contextSha256) ||
      !isRecord(published) || !exactKeys(published, ["databaseSha256"]) ||
      !isSha256(published.databaseSha256)) {
    throw new Error("Pi migration receipt is invalid");
  }
}

export async function probeTargetStore(databasePath: string): Promise<PiStoreProbe> {
  return await probeTargetStoreCore(databasePath, {
    createNodeSqliteFactory,
  }) as PiStoreProbe;
}

async function probeAfterConcurrentBootstrap(databasePath: string): Promise<PiStoreProbe> {
  let probe = await probeTargetStore(databasePath);
  for (let attempt = 0; attempt < 40 && (probe.format === "unknown" || probe.format === "corrupt"); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 25));
    probe = await probeTargetStore(databasePath);
  }
  return probe;
}

async function inspectTargetSession(databasePath: string, sessionId: string): Promise<{
  exists: boolean;
  marker?: unknown;
  mainTip?: unknown;
  empty: boolean;
}> {
  const db = await createNodeSqliteFactory().openReadOnly(databasePath);
  const storage = new SqliteStorage(db, { sessionId });
  try {
    const exists = db.prepare("SELECT 1 AS found FROM sessions WHERE id = ?").get(sessionId) !== undefined;
    if (!exists) return { exists: false, empty: true };
    const [marker, mainTip] = await Promise.all([
      storage.getValue(OM_SESSION_FORMAT_VALUE, TODO_CONTEXT),
      storage.getValue(branchTip(MAIN_BRANCH), TODO_CONTEXT),
    ]);
    const counts = db.prepare(`
      SELECT
        (SELECT COUNT(*) FROM entries WHERE session_id = ?) AS entries,
        (SELECT COUNT(*) FROM scalar_values WHERE session_id = ?) AS scalars,
        (SELECT COUNT(*) FROM list_values WHERE session_id = ?) AS lists,
        (SELECT COUNT(*) FROM usage_ledger WHERE session_id = ?) AS usage,
        (SELECT COUNT(*) FROM branch_meta WHERE session_id = ?) AS branches
    `).get<Record<string, number>>(sessionId, sessionId, sessionId, sessionId, sessionId);
    return {
      exists: true,
      marker: marker?.value,
      mainTip: mainTip?.value,
      empty: counts !== undefined && Object.values(counts).every((count) => count === 0),
    };
  } finally {
    await storage.close(TODO_CONTEXT);
    db.close();
  }
}

async function readCommittedMain(session: Session): Promise<{ entries: Entry[]; committedTip: string | null }> {
  return await readCommittedMainCore(session, CORE_API) as {
    entries: Entry[];
    committedTip: string | null;
  };
}

export async function exportCommittedMain(session: Session): Promise<Entry[]> {
  return (await exportCommittedMainCore(session, CORE_API)).entries as Entry[];
}

export async function loadCommittedContext(session: Session): Promise<CommittedSessionState> {
  return await loadCommittedContextCore(session, CORE_API) as CommittedSessionState;
}

async function initializeSession(session: Session): Promise<void> {
  await session.mutate(async (mutator, context) => {
    const marker = await mutator.getValue(OM_SESSION_FORMAT_VALUE, context);
    const tip = await mutator.getValue(branchTip(MAIN_BRANCH), context);
    if (marker !== undefined || tip !== undefined) throw new Error("session initialization is not empty");
    await mutator.commit([
      setValue(OM_SESSION_FORMAT_VALUE, OM_SESSION_FORMAT),
      setValue(branchTip(MAIN_BRANCH), null),
    ], context);
  }, TODO_CONTEXT);
}

function isTransientSqliteBusy(error: unknown): boolean {
  return error instanceof Error && /database is (?:locked|busy)/i.test(error.message);
}

function validateInspectedSession(inspected: {
  exists: boolean;
  marker?: unknown;
  mainTip?: unknown;
  empty: boolean;
}): void {
  if (inspected.exists && inspected.marker !== OM_SESSION_FORMAT) {
    if (inspected.marker !== undefined || !inspected.empty) {
      throw new Error("Pi session format marker is missing or invalid");
    }
  }
  if (inspected.exists && inspected.marker === OM_SESSION_FORMAT && inspected.mainTip === undefined) {
    throw new Error("Pi session main branch is missing");
  }
}

export async function openTargetSession(databasePath: string, sessionId: string): Promise<OpenedPiSession> {
  await assertMigrationReceiptAllowsStartup(databasePath);
  let probe = await probeAfterConcurrentBootstrap(databasePath);
  let bootstrapAdmitted = probe.format === "missing";
  let lastBusy: unknown;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    if (attempt > 0) probe = await probeTargetStore(databasePath);
    if (probe.format === "missing") bootstrapAdmitted = true;
    if (probe.format !== "missing" && probe.format !== "target" &&
        !(bootstrapAdmitted && (probe.format === "unknown" || probe.format === "corrupt"))) {
      throw new Error(`unsupported Pi session store format: ${probe.format}`);
    }
    const inspected = probe.format === "target"
      ? await inspectTargetSession(databasePath, sessionId)
      : { exists: false, empty: true };
    validateInspectedSession(inspected);
    const repository = new SqliteSessionRepo({
      directory: path.dirname(databasePath),
      databasePath,
      databaseFactory: createNodeSqliteFactory(),
    });
    try {
      let session: Session<SqliteSessionMetadata>;
      if (inspected.exists) {
        const metadata = (await repository.list(undefined, TODO_CONTEXT)).find((item) => item.id === sessionId);
        if (!metadata) throw new Error("Pi session metadata is unavailable");
        session = await repository.open(metadata, TODO_CONTEXT);
        if (inspected.marker === undefined) await initializeSession(session);
      } else {
        session = await repository.create({ id: sessionId }, TODO_CONTEXT);
        await initializeSession(session);
      }
      return { repository, session, state: await loadCommittedContext(session) };
    } catch (error) {
      await repository.close(TODO_CONTEXT).catch(() => undefined);
      if (!isTransientSqliteBusy(error)) throw error;
      lastBusy = error;
      const delayMs = 20 * (2 ** attempt) + (process.pid % 23);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
  throw lastBusy ?? new Error("Pi session database remained busy during initialization");
}

function entryWrites(
  session: Session,
  parentId: string | null,
  entries: Array<{ type: "message"; message: AgentMessage } | ({ type: "compaction" } & CompactResult)>,
): { writes: ReturnType<typeof insertEntry>[]; tip: string | null } {
  const writes: ReturnType<typeof insertEntry>[] = [];
  let tip = parentId;
  for (const entry of entries) {
    const id = session.idGenerator.next();
    const next: NewEntry = entry.type === "message"
      ? { id, parentId: tip, type: "message", message: structuredClone(entry.message) }
      : { id, parentId: tip, type: "compaction", ...structuredClone(entry), fromHook: false };
    writes.push(insertEntry(next));
    tip = id;
  }
  return { writes, tip };
}

function comparablePayload(
  payload: Array<{ type: "message"; message: AgentMessage } | ({ type: "compaction" } & CompactResult)>,
): unknown[] {
  return payload.map((item) => item.type === "message"
    ? { type: "message", message: item.message }
    : { type: "compaction", ...item, fromHook: false });
}

function stableJson(value: unknown): string {
  return JSON.stringify(value, (_key, item) => {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return item;
    return Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]]));
  });
}

async function committedRunMatches(
  session: Session,
  runId: string,
  kind: "turn" | "compaction",
  payload: Array<{ type: "message"; message: AgentMessage } | ({ type: "compaction" } & CompactResult)>,
  expectedParentId?: string | null,
): Promise<boolean> {
  const { entries } = await readCommittedMain(session);
  let groupStart = 0;
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    if (!entry || !isCommitMarker(entry)) continue;
    const data = entry.data as { run_id: string; kind: "turn" | "compaction" };
    if (data.run_id === runId && data.kind === kind &&
        (kind === "turn" || entries[groupStart]?.parentId === expectedParentId)) {
      const actual = entries.slice(groupStart, index).map((item) => {
        if (item.type === "message") return { type: "message", message: item.message };
        if (item.type === "compaction") {
          const { id: _id, parentId: _parentId, seq: _seq, timestamp: _timestamp, ...rest } = item;
          return rest;
        }
        throw new Error("invalid committed run payload");
      });
      if (stableJson(actual) !== stableJson(comparablePayload(payload))) {
        throw new Error("committed run identity has different content");
      }
      return true;
    }
    groupStart = index + 1;
  }
  return false;
}

async function appendCommitted(
  session: Session,
  runId: string,
  kind: "turn" | "compaction",
  payload: Array<{ type: "message"; message: AgentMessage } | ({ type: "compaction" } & CompactResult)>,
  expectedParentId?: string | null,
): Promise<void> {
  if (await committedRunMatches(session, runId, kind, payload, expectedParentId)) return;
  try {
    await session.mutate(async (mutator, context) => {
      const storedTip = await mutator.getValue(branchTip(MAIN_BRANCH), context);
      if (!storedTip) throw new Error("missing main branch");
      if (kind === "compaction" && storedTip.value !== expectedParentId) {
        throw new Error("compaction parent is no longer current");
      }
      const { writes, tip } = entryWrites(session, storedTip.value, payload);
      const markerId = session.idGenerator.next();
      writes.push(insertEntry({
        id: markerId,
        parentId: tip,
        type: "custom",
        customType: OM_TURN_COMMIT_TYPE,
        data: { run_id: runId, kind } as JsonValue,
      }));
      await mutator.commit([...writes, setValue(branchTip(MAIN_BRANCH), markerId)], context);
    }, TODO_CONTEXT);
  } catch (error) {
    if (await committedRunMatches(session, runId, kind, payload, expectedParentId).catch(() => false)) return;
    throw error;
  }
}

export async function appendCommittedTurn(
  session: Session,
  messages: AgentMessage[],
  runId: string,
): Promise<void> {
  if (!runId || messages.length === 0 || messages.some((message) =>
    message.role === "compactionSummary" ||
    (message.role === "assistant" && ["error", "aborted", "deferred", "pending"].includes(message.stopReason)))) {
    throw new Error("turn is not eligible for committed session history");
  }
  validateToolGroups(messages);
  await appendCommitted(session, runId, "turn", messages.map((message) => ({ type: "message", message })));
}

export async function appendCommittedCompaction(
  session: Session,
  result: CompactResult,
  runId: string,
  expectedParentId: string | null,
): Promise<void> {
  if (!runId || !result.summary.trim() || !Number.isSafeInteger(result.tokensBefore) ||
      result.tokensBefore < 0) {
    throw new Error("compaction is not eligible for committed session history");
  }
  validateToolGroups(result.retainedTail.filter((message) => message.role !== "compactionSummary"));
  await appendCommitted(
    session, runId, "compaction", [{ type: "compaction", ...result }], expectedParentId,
  );
}
