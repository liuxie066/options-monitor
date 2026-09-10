import { createHash } from "node:crypto";
import { stat } from "node:fs/promises";

export const OM_SESSION_FORMAT = "om-pi-session.v1";
export const OM_TURN_COMMIT_TYPE = "om.turn.commit.v1";
export const OM_FORMAT_NAMESPACE = "om.pi";
export const OM_FORMAT_KEY = "session-format";
export const MAIN_BRANCH = "main";
export const TARGET_SCHEMA_SHA256 = "0dd7799bef18ac929627e15ef1a55ef3f293ea5d869c0fe48815b812b464a604";
export const LEGACY_SCHEMA_SHA256 = "6af76c4064917bff53e2f72db67213490ff6e63064f231377ce53d475d48a79d";

function fail(message) {
  throw new Error(message);
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value, keys) {
  return isRecord(value) && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
}

function requireString(value, label) {
  if (typeof value !== "string" || value.length === 0) fail(`${label} is invalid`);
}

function requireTimestamp(value) {
  if (!Number.isSafeInteger(value) || value < 0) fail("entry timestamp is invalid");
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (isRecord(value)) {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
}

function contentSha256(value) {
  return createHash("sha256").update(JSON.stringify(stable(value))).digest("hex");
}

export function sqliteSchemaHash(db) {
  const rows = db.prepare(
    "SELECT type, name, tbl_name, sql FROM sqlite_schema " +
    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name",
  ).all();
  return createHash("sha256").update(JSON.stringify(rows)).digest("hex");
}

export async function probeTargetStore(databasePath, sqlite) {
  try {
    const info = await stat(databasePath);
    if (!info.isFile()) return { format: "unknown", sessionIds: [], writerLeaseCount: 0 };
  } catch (error) {
    if (error?.code === "ENOENT") {
      return { format: "missing", sessionIds: [], writerLeaseCount: 0 };
    }
    throw error;
  }
  let db;
  try {
    db = await sqlite.createNodeSqliteFactory().openReadOnly(databasePath);
    const integrity = db.prepare("PRAGMA integrity_check").get();
    if (!integrity || Object.values(integrity)[0] !== "ok") {
      return { format: "corrupt", sessionIds: [], writerLeaseCount: 0 };
    }
    const fingerprint = sqliteSchemaHash(db);
    if (fingerprint === TARGET_SCHEMA_SHA256) {
      const rows = db.prepare("SELECT id, storage_version FROM sessions ORDER BY id").all();
      if (rows.some((row) => row.storage_version !== 1)) {
        return {
          format: "unknown",
          sessionIds: rows.map((row) => row.id),
          writerLeaseCount: 0,
        };
      }
      return {
        format: "target",
        sessionIds: rows.map((row) => row.id),
        writerLeaseCount: 0,
      };
    }
    if (fingerprint === LEGACY_SCHEMA_SHA256) {
      const rows = db.prepare("SELECT id FROM sessions ORDER BY id").all();
      const lease = db.prepare("SELECT COUNT(*) AS count FROM writer_leases").get();
      return {
        format: "legacy",
        sessionIds: rows.map((row) => row.id),
        writerLeaseCount: lease?.count ?? 0,
      };
    }
    return { format: "unknown", sessionIds: [], writerLeaseCount: 0 };
  } catch {
    return { format: "corrupt", sessionIds: [], writerLeaseCount: 0 };
  } finally {
    db?.close();
  }
}

export function isCommitMarker(entry) {
  return entry?.type === "custom" &&
    entry.customType === OM_TURN_COMMIT_TYPE &&
    hasExactKeys(entry.data, ["run_id", "kind"]) &&
    typeof entry.data.run_id === "string" && entry.data.run_id.length > 0 &&
    (entry.data.kind === "turn" || entry.data.kind === "compaction");
}

export function isContextMessage(message) {
  return message?.role !== "assistant" ||
    !["error", "aborted", "deferred"].includes(message.stopReason);
}

export function validateToolGroups(messages) {
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (!isRecord(message) || !["user", "assistant", "toolResult"].includes(message.role)) {
      fail("unsupported message role in session context");
    }
    if (message.role === "toolResult") fail("orphaned tool result in session context");
    if (message.role !== "assistant") continue;
    const calls = Array.isArray(message.content)
      ? message.content.filter((item) => item?.type === "toolCall")
      : [];
    const expected = new Map();
    for (const call of calls) {
      requireString(call.id, "tool call id");
      requireString(call.name, "tool call name");
      if (expected.has(call.id)) fail("duplicate tool call id in session context");
      expected.set(call.id, call.name);
    }
    for (let resultIndex = 0; resultIndex < calls.length; resultIndex += 1) {
      const result = messages[++index];
      if (!isRecord(result) || result.role !== "toolResult" ||
          expected.get(result.toolCallId) !== result.toolName) {
        fail("incomplete tool group in session context");
      }
      expected.delete(result.toolCallId);
    }
    if (expected.size > 0) fail("incomplete tool group in session context");
  }
}

function validateEntryShape(entry) {
  if (!isRecord(entry)) fail("session entry is invalid");
  requireString(entry.id, "entry id");
  if (entry.parentId !== null) requireString(entry.parentId, "entry parent id");
  requireTimestamp(entry.timestamp);
  if ("seq" in entry && (!Number.isSafeInteger(entry.seq) || entry.seq < 1)) {
    fail("entry sequence is invalid");
  }
  if (entry.type === "message") {
    const keys = Object.keys(entry);
    const required = ["id", "parentId", "timestamp", "type", "message"];
    if (required.some((key) => !keys.includes(key)) ||
        keys.some((key) => !required.includes(key) && key !== "seq")) {
      fail("message entry shape is invalid");
    }
    if (!isRecord(entry.message)) fail("message entry payload is invalid");
    return;
  }
  if (entry.type === "compaction") {
    const required = ["id", "parentId", "timestamp", "type", "summary", "retainedTail", "tokensBefore", "fromHook"];
    const optional = ["details", "usage", "seq"];
    const keys = Object.keys(entry);
    if (required.some((key) => !keys.includes(key)) ||
        keys.some((key) => !required.includes(key) && !optional.includes(key))) {
      fail("compaction entry shape is invalid");
    }
    if (typeof entry.summary !== "string" || !entry.summary.trim() ||
        !Array.isArray(entry.retainedTail) || !Number.isSafeInteger(entry.tokensBefore) ||
        entry.tokensBefore < 0 || entry.fromHook !== false) {
      fail("compaction entry payload is invalid");
    }
    validateToolGroups(entry.retainedTail.filter(isContextMessage));
    return;
  }
  if (entry.type === "custom") {
    const keys = Object.keys(entry);
    const required = ["id", "parentId", "timestamp", "type", "customType", "data"];
    if (required.some((key) => !keys.includes(key)) ||
        keys.some((key) => !required.includes(key) && key !== "seq") ||
        !isCommitMarker(entry)) {
      fail("unsupported session entry");
    }
    return;
  }
  fail("unsupported session entry");
}

export function validateCommittedEntries(entries) {
  if (!Array.isArray(entries)) fail("committed entries are invalid");
  let parent = null;
  let group = [];
  const ids = new Set();
  const runs = new Set();
  for (const entry of entries) {
    validateEntryShape(entry);
    if (ids.has(entry.id)) fail("duplicate session entry id");
    ids.add(entry.id);
    if (entry.parentId !== parent) fail("broken main ancestry");
    parent = entry.id;
    group.push(entry);
    if (!isCommitMarker(entry)) continue;
    const payload = group.slice(0, -1);
    if (entry.data.kind === "compaction") {
      if (payload.length !== 1 || payload[0]?.type !== "compaction") {
        fail("invalid compaction commit group");
      }
    } else {
      if (payload.length === 0 || payload.some((item) => item.type !== "message")) {
        fail("invalid turn commit group");
      }
      validateToolGroups(payload.map((item) => item.message).filter(isContextMessage));
    }
    const identity = entry.data.kind === "compaction"
      ? `${entry.data.kind}\0${entry.data.run_id}\0${payload[0].parentId ?? ""}`
      : `${entry.data.kind}\0${entry.data.run_id}`;
    if (runs.has(identity)) fail("duplicate committed run identity");
    runs.add(identity);
    group = [];
  }
  if (group.length > 0) fail("uncommitted session tail");
}

function validateReachableEntries(entries) {
  let parent = null;
  let markerIndex = -1;
  const ids = new Set();
  for (let index = 0; index < entries.length; index += 1) {
    const entry = entries[index];
    validateEntryShape(entry);
    if (ids.has(entry.id)) fail("duplicate session entry id");
    ids.add(entry.id);
    if (entry.parentId !== parent) fail("broken main ancestry");
    parent = entry.id;
    if (isCommitMarker(entry)) markerIndex = index;
  }
  if (entries.length > 0 && markerIndex < 0) fail("main branch has no valid commit marker");
  const committed = markerIndex < 0 ? [] : entries.slice(0, markerIndex + 1);
  validateCommittedEntries(committed);
  return {
    entries: committed,
    committedTip: committed.at(-1)?.id ?? null,
    excludedTailCount: entries.length - committed.length,
  };
}

export async function readCommittedMain(session, api, options = {}) {
  const branch = await session.branch(MAIN_BRANCH, api.context);
  if (!branch) fail("missing main branch");
  const tip = await branch.getTipId(api.context);
  const reachable = await branch.findEntries({ order: "oldestFirst" }, api.context);
  if ((tip === null && reachable.length > 0) ||
      (tip !== null && reachable.at(-1)?.id !== tip)) {
    fail("main branch tip is dangling");
  }
  const selected = validateReachableEntries(reachable);
  if (options.requireAllEntries) {
    const all = await session.findEntries({ order: "asc" }, api.context);
    const reachableIds = new Set(reachable.map((entry) => entry.id));
    const allowedParents = new Set(reachableIds);
    const byId = new Map(reachable.map((entry) => [entry.id, entry]));
    let detachedCount = 0;
    for (const entry of all) {
      if (reachableIds.has(entry.id)) continue;
      validateEntryShape(entry);
      const parent = entry.parentId === null ? undefined : byId.get(entry.parentId);
      if (entry.type === "custom" || entry.parentId === null || !allowedParents.has(entry.parentId) ||
          (parent !== undefined && reachableIds.has(parent.id) && !isCommitMarker(parent))) {
        fail("unsupported session branches");
      }
      allowedParents.add(entry.id);
      byId.set(entry.id, entry);
      detachedCount += 1;
    }
    if (reachableIds.size + detachedCount !== all.length) fail("unsupported session branches");
    selected.excludedTailCount += detachedCount;
  }
  return selected;
}

export function projectCommittedEntries(entries, api) {
  validateCommittedEntries(entries);
  let compactionIndex = -1;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    if (entries[index]?.type === "compaction") {
      compactionIndex = index;
      break;
    }
  }
  const messages = [];
  if (compactionIndex >= 0) {
    const entry = entries[compactionIndex];
    messages.push(
      api.createCompactionSummaryMessage(entry.summary, entry.tokensBefore, entry.timestamp),
      ...entry.retainedTail.filter(isContextMessage),
    );
  }
  for (const entry of entries.slice(compactionIndex + 1)) {
    if (entry.type === "message" && isContextMessage(entry.message)) messages.push(entry.message);
  }
  validateToolGroups(messages.filter((message) => message.role !== "compactionSummary"));
  return messages;
}

export async function exportCommittedMain(session, api, options = {}) {
  const selected = await readCommittedMain(session, api, options);
  return {
    ...selected,
    messages: projectCommittedEntries(selected.entries, api),
  };
}

export async function loadCommittedContext(session, api) {
  const selected = await exportCommittedMain(session, api);
  const branch = await session.branch(MAIN_BRANCH, api.context);
  if (!branch) fail("missing main branch");
  if (await branch.getTipId(api.context) !== selected.committedTip) {
    await session.mutate(async (mutator, context) => {
      await mutator.commit([api.setValue(api.branchTip(MAIN_BRANCH), selected.committedTip)], context);
    }, api.context);
  }
  return { entries: selected.entries, messages: selected.messages };
}

function migrationUsageId(entryId) {
  return `om_migration_usage_${createHash("sha256").update(entryId).digest("hex").slice(0, 32)}`;
}

function entryUsage(entry) {
  if (entry.type === "message" && entry.message?.role === "assistant") return entry.message.usage;
  if (entry.type === "compaction") return entry.usage;
  return undefined;
}

export async function importCommittedMain(repo, source, api, setNow) {
  if (!isRecord(source) || !hasExactKeys(source, [
    "id", "createdAt", "entries", "excludedTailCount", "contextSha256", "contentSha256",
  ])) {
    fail("canonical session envelope is invalid");
  }
  requireString(source.id, "session id");
  requireTimestamp(source.createdAt);
  if (!Number.isSafeInteger(source.excludedTailCount) || source.excludedTailCount < 0 ||
      !/^[0-9a-f]{64}$/.test(source.contextSha256) || !/^[0-9a-f]{64}$/.test(source.contentSha256)) {
    fail("canonical session metadata is invalid");
  }
  validateCommittedEntries(source.entries);
  if (source.entries.some((entry) => "seq" in entry)) {
    fail("canonical session entries must not contain physical sequence numbers");
  }
  const projected = projectCommittedEntries(source.entries, api);
  if (contentSha256(source.entries) !== source.contentSha256 ||
      contentSha256(projected) !== source.contextSha256) {
    fail("canonical session fingerprints do not match content");
  }
  setNow(source.createdAt);
  const session = await repo.create({ id: source.id }, api.context);
  try {
    await session.mutate(async (mutator, context) => {
      await mutator.commit([
        api.setValue(api.value(OM_FORMAT_NAMESPACE, OM_FORMAT_KEY), OM_SESSION_FORMAT),
        api.setValue(api.branchTip(MAIN_BRANCH), null),
      ], context);
    }, api.context);
    let tip = null;
    for (const entry of source.entries) {
      if (entry.parentId !== tip) fail("canonical ancestry is not linear main");
      setNow(entry.timestamp);
      const { timestamp: _timestamp, ...mapped } = structuredClone(entry);
      const writes = [
        api.insertEntry(mapped),
        api.setValue(api.branchTip(MAIN_BRANCH), entry.id),
      ];
      const usage = entryUsage(entry);
      if (usage !== undefined) {
        writes.push(api.insertUsage({
          id: migrationUsageId(entry.id),
          entryId: entry.id,
          adjustment: false,
          usage: structuredClone(usage),
          details: { source: "om-pi-export.v1" },
        }));
      }
      await session.mutate(async (mutator, context) => {
        await mutator.commit(writes, context);
      }, api.context);
      tip = entry.id;
    }
    return await exportCommittedMain(session, api, { requireAllEntries: true });
  } finally {
    await session.close(api.context);
  }
}
