#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, realpath, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join, relative, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { pathToFileURL } from "node:url";
import {
  OM_FORMAT_KEY as FORMAT_KEY,
  OM_FORMAT_NAMESPACE as FORMAT_NAMESPACE,
  OM_SESSION_FORMAT as SESSION_FORMAT,
  OM_TURN_COMMIT_TYPE as TURN_COMMIT_TYPE,
  exportCommittedMain,
  importCommittedMain,
  isCommitMarker,
  probeTargetStore,
  validateCommittedEntries,
} from "./pi_session_core.mjs";

const PACKAGES = [
  "@earendil-works/pi-agent-core",
  "@earendil-works/pi-ai",
  "@earendil-works/pi-session-backend-sqlite-node",
];
const SUPPORTED = new Set(["0.84.2", "0.85.1"]);
const EXPORT_FORMAT = "om-pi-export.v1";

function fail(message) {
  throw new Error(message);
}

function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
  }
  return value;
}

function encoded(value) {
  return JSON.stringify(stable(value));
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function parseArgs(argv) {
  const command = argv[2];
  const args = {};
  for (let index = 3; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) fail("invalid arguments");
    args[key.slice(2)] = value;
  }
  if (!command || !args.runtime) fail("command and --runtime are required");
  return { command, args };
}

async function runtimeModules(runtime, expectedVersion) {
  const root = await realpath(resolve(runtime));
  const manifestPath = join(root, "package.json");
  const lockPath = join(root, "package-lock.json");
  const [manifestRaw, lockRaw] = await Promise.all([
    readFile(manifestPath, "utf8"),
    readFile(lockPath, "utf8"),
  ]);
  const manifest = JSON.parse(manifestRaw);
  const lock = JSON.parse(lockRaw);
  const versions = PACKAGES.map((name) => manifest.dependencies?.[name]);
  if (new Set(versions).size !== 1 || !SUPPORTED.has(versions[0])) fail("runtime pins are unsupported");
  const version = versions[0];
  if (expectedVersion && version !== expectedVersion) fail("runtime version does not match request");
  const locked = lock.packages?.[""]?.dependencies ?? {};
  const requireFromRuntime = createRequire(pathToFileURL(manifestPath));
  const packages = {};
  const modules = {};
  for (const name of PACKAGES) {
    if (locked[name] !== version) fail("lockfile root dependency does not match manifest");
    const packagePath = await realpath(join(root, "node_modules", name, "package.json"));
    const entryPath = await realpath(new URL(import.meta.resolve(name, pathToFileURL(manifestPath).href)));
    const packageRelative = relative(join(root, "node_modules"), packagePath);
    const entryRelative = relative(join(root, "node_modules"), entryPath);
    if (packageRelative.startsWith("..") || entryRelative.startsWith("..")) fail("package resolved outside selected runtime");
    const packageRaw = await readFile(packagePath, "utf8");
    const packageJson = JSON.parse(packageRaw);
    if (packageJson.version !== version || lock.packages?.[`node_modules/${name}`]?.version !== version) {
      fail("installed package identity does not match lockfile");
    }
    modules[name] = await import(pathToFileURL(entryPath).href);
    packages[name] = {
      version,
      manifestSha256: sha256(packageRaw),
      entrySha256: sha256(await readFile(entryPath)),
    };
  }
  if (version === "0.84.2") {
    const nodePath = await realpath(new URL(import.meta.resolve(
      "@earendil-works/pi-agent-core/node", pathToFileURL(manifestPath).href,
    )));
    if (relative(join(root, "node_modules"), nodePath).startsWith("..")) fail("node export resolved outside selected runtime");
    modules.node = await import(pathToFileURL(nodePath).href);
  }
  return {
    version,
    identity: { version, lockSha256: sha256(lockRaw), packages },
    core: modules[PACKAGES[0]],
    sqlite: modules[PACKAGES[2]],
    node: modules.node,
  };
}

function canonicalEntry(entry, version) {
  const common = { id: entry.id, parentId: entry.parentId, timestamp: entry.timestamp, type: entry.type };
  if (entry.type === "message") return { ...common, message: entry.message };
  if (entry.type === "compaction") {
    if (version === "0.85.1" && entry.fromHook !== false) fail("hook-origin compaction cannot be converted");
    return {
      ...common,
      summary: entry.summary,
      retainedTail: entry.retainedTail,
      tokensBefore: entry.tokensBefore,
      ...(entry.details === undefined ? {} : { details: entry.details }),
      ...(entry.usage === undefined ? {} : { usage: entry.usage }),
      fromHook: false,
    };
  }
  if (isCommitMarker(entry)) return { ...common, customType: entry.customType, data: entry.data };
  fail("unsupported session entry");
}

function committedPrefix(pathEntries) {
  let lastCommit = -1;
  for (let index = 0; index < pathEntries.length; index += 1) {
    if (isCommitMarker(pathEntries[index])) lastCommit = index;
  }
  if (pathEntries.length > 0 && lastCommit < 0) fail("session has no committed main history");
  return { entries: pathEntries.slice(0, lastCommit + 1), excludedTailCount: pathEntries.length - lastCommit - 1 };
}

async function export0842(runtime, db, core, sqlite, node) {
  const repo = new sqlite.SqliteSessionRepository({
    env: new node.NodeExecutionEnv({ cwd: runtime }),
    sqlite: sqlite.createNodeSqliteFactory(),
    databasePath: db,
  });
  try {
    const sessions = [];
    for (const metadata of (await repo.list()).sort((a, b) => a.id.localeCompare(b.id))) {
      if (encoded(metadata.metadata) !== encoded({ schema: SESSION_FORMAT })) fail("unsupported legacy session metadata");
      const session = await repo.open(metadata);
      const all = await session.findEntries({ order: "oldestFirst" });
      const pathEntries = await session.findEntriesOnBranch({ order: "oldestFirst" });
      const reachableIds = new Set(pathEntries.map((entry) => entry.id));
      const reachableCanonical = pathEntries.map((entry) => canonicalEntry(entry, "0.84.2"));
      const selected = committedPrefix(reachableCanonical);
      const allowedParents = new Set(selected.entries.map((entry) => entry.id));
      allowedParents.add(null);
      for (const entry of all) {
        if (reachableIds.has(entry.id)) continue;
        const converted = canonicalEntry(entry, "0.84.2");
        if (converted.type === "custom" || !allowedParents.has(converted.parentId)) {
          fail("unsupported legacy session branches");
        }
        allowedParents.add(converted.id);
      }
      const entries = selected.entries;
      validateCommittedEntries(entries);
      const selectedRaw = pathEntries.slice(0, entries.length);
      const messages = core.buildSessionContext(selectedRaw).messages;
      sessions.push({
        id: metadata.id,
        createdAt: metadata.createdAt,
        entries,
        excludedTailCount: selected.excludedTailCount + all.length - pathEntries.length,
        contextSha256: sha256(encoded(messages)),
        contentSha256: sha256(encoded(entries)),
      });
    }
    return sessions;
  } finally {
    await repo.close().catch(() => {});
  }
}

async function export0851(runtime, db, core, sqlite) {
  const context = core.TODO_CONTEXT;
  const repo = new sqlite.SqliteSessionRepo({
    directory: dirname(db), databasePath: db, databaseFactory: sqlite.createNodeSqliteFactory(),
  });
  try {
    const sessions = [];
    for (const metadata of (await repo.list(undefined, context)).sort((a, b) => a.id.localeCompare(b.id))) {
      const session = await repo.open(metadata, context);
      const marker = await session.getValue(core.value(FORMAT_NAMESPACE, FORMAT_KEY), context);
      if (marker?.value !== SESSION_FORMAT) fail("target session format marker is missing");
      const selected = await exportCommittedMain(session, {
        context,
        branchTip: core.branchTip,
        setValue: core.setValue,
        createCompactionSummaryMessage: core.createCompactionSummaryMessage,
      }, { requireAllEntries: true });
      const entries = selected.entries.map((entry) => canonicalEntry(entry, "0.85.1"));
      const messages = selected.messages;
      sessions.push({
        id: metadata.id,
        createdAt: metadata.createdAt,
        entries,
        excludedTailCount: selected.excludedTailCount,
        contextSha256: sha256(encoded(messages)),
        contentSha256: sha256(encoded(entries)),
      });
      await session.close(context);
    }
    return sessions;
  } finally {
    await repo.close(context).catch(() => {});
  }
}

async function import0851(runtime, db, payload, core, sqlite) {
  const context = core.TODO_CONTEXT;
  let now = Date.now();
  const repo = new sqlite.SqliteSessionRepo({
    directory: dirname(db), databasePath: db, databaseFactory: sqlite.createNodeSqliteFactory(), now: () => now,
  });
  try {
    if (payload.sessions.length === 0) {
      // Public creation initializes the shared schema; deletion keeps it empty.
      const bootstrap = await repo.create({}, context);
      await bootstrap.close(context);
      await repo.delete(bootstrap.metadata, context);
    }
    for (const source of payload.sessions) {
      const readback = await importCommittedMain(repo, source, {
        context,
        branchTip: core.branchTip,
        value: core.value,
        setValue: core.setValue,
        insertEntry: core.insertEntry,
        insertUsage: core.insertUsage,
        createCompactionSummaryMessage: core.createCompactionSummaryMessage,
      }, (value) => { now = value; });
      const canonical = readback.entries.map((entry) => canonicalEntry(entry, "0.85.1"));
      if (encoded(canonical) !== encoded(source.entries) || sha256(encoded(readback.messages)) !== source.contextSha256) {
        fail("target import readback differs from canonical session");
      }
    }
  } finally {
    await repo.close(context).catch(() => {});
  }
}

async function import0842(runtime, db, payload, core, sqlite, node) {
  const originalNow = Date.now;
  let now = originalNow();
  Date.now = () => now;
  const wrapperTimes = payload.sessions.flatMap((session) => [
    session.createdAt,
    ...session.entries.map((entry) => entry.timestamp),
  ]);
  const leaseTtlMs = wrapperTimes.length === 0 ? 30_000
    : Math.max(30_000, Math.max(...wrapperTimes) - Math.min(...wrapperTimes) + 60_000);
  if (!Number.isSafeInteger(leaseTtlMs) || leaseTtlMs <= 10_000) fail("canonical timestamp range is unsupported");
  const repo = new sqlite.SqliteSessionRepository({
    env: new node.NodeExecutionEnv({ cwd: runtime }),
    sqlite: sqlite.createNodeSqliteFactory(),
    databasePath: db,
    writerLease: { ttlMs: leaseTtlMs, heartbeatIntervalMs: 10_000 },
  });
  try {
    if (payload.sessions.length === 0) {
      const bootstrap = await repo.create({ cwd: runtime, metadata: { schema: SESSION_FORMAT } });
      await repo.delete(await bootstrap.getMetadata());
    }
    for (const source of payload.sessions) {
      validateCommittedEntries(source.entries);
      now = source.createdAt;
      const session = await repo.create({
        id: source.id, cwd: runtime, metadata: { schema: SESSION_FORMAT },
      });
      let tip = null;
      for (const entry of source.entries) {
        if (entry.parentId !== tip) fail("canonical ancestry is not linear main");
        now = entry.timestamp;
        const { parentId: _parentId, timestamp: _timestamp, fromHook: _fromHook, ...mapped } = entry;
        await session.appendEntry(mapped, "main");
        tip = entry.id;
      }
    }
  } finally {
    Date.now = originalNow;
    await repo.close().catch(() => {});
  }
}

async function main() {
  const { command, args } = parseArgs(process.argv);
  const loaded = await runtimeModules(args.runtime, args["expected-version"]);
  if (command === "identity") return loaded.identity;
  if (command === "probe") {
    if (!args.database) fail("probe database is required");
    if (loaded.version !== "0.85.1") fail("store probe requires the exact 0.85.1 controller runtime");
    let sqlite = loaded.sqlite;
    if (args.immutable === "true") {
      sqlite = {
        createNodeSqliteFactory: () => ({
          openReadOnly: async (database) => {
            const url = pathToFileURL(database);
            url.searchParams.set("immutable", "1");
            return loaded.sqlite.wrapNodeSqliteDatabase(new DatabaseSync(url, { readOnly: true }));
          },
        }),
      };
    } else if (args.immutable !== undefined) {
      fail("probe immutable flag is invalid");
    }
    return { version: loaded.version, ...await probeTargetStore(resolve(args.database), sqlite) };
  }
  if (command === "export") {
    if (!args.database || !args.output) fail("export paths are required");
    const sessions = loaded.version === "0.84.2"
      ? await export0842(args.runtime, resolve(args.database), loaded.core, loaded.sqlite, loaded.node)
      : await export0851(args.runtime, resolve(args.database), loaded.core, loaded.sqlite);
    const payload = { format: EXPORT_FORMAT, sessions };
    await writeFile(resolve(args.output), encoded(payload), { encoding: "utf8", mode: 0o600 });
    return {
      version: loaded.version,
      sessionCount: sessions.length,
      entryCount: sessions.reduce((sum, item) => sum + item.entries.length, 0),
      excludedTailCount: sessions.reduce((sum, item) => sum + item.excludedTailCount, 0),
      canonicalSha256: sha256(encoded(payload)),
      contextSha256: sha256(encoded(sessions.map((item) => [item.id, item.contextSha256]))),
    };
  }
  if (command === "import") {
    if (!args.database || !args.input) fail("import paths are required");
    const payload = JSON.parse(await readFile(resolve(args.input), "utf8"));
    if (payload?.format !== EXPORT_FORMAT || !Array.isArray(payload.sessions)) fail("invalid canonical export");
    if (loaded.version === "0.84.2") {
      await import0842(args.runtime, resolve(args.database), payload, loaded.core, loaded.sqlite, loaded.node);
    } else {
      await import0851(args.runtime, resolve(args.database), payload, loaded.core, loaded.sqlite);
    }
    return { version: loaded.version, imported: true, canonicalSha256: sha256(encoded(payload)) };
  }
  fail("unsupported migration command");
}

main().then(
  (result) => process.stdout.write(`${JSON.stringify({ ok: true, ...result })}\n`),
  (error) => {
    process.stderr.write(`${error instanceof Error ? error.message : "migration bridge failed"}\n`);
    process.exitCode = 1;
  },
);
