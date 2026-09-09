"""Private, source-backed Bot memory in the Host's SQLite database."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.application.bot.contracts import contract_from_payload


@dataclass(frozen=True)
class MemoryScope:
    owner_scope: str
    allowed_accounts: frozenset[str] = frozenset()


def scope_from_contract(contract: Any, allowed_accounts: Any = ()) -> MemoryScope:
    data = contract.input
    if contract.execution_environment != "channel":
        raise ValueError("MEMORY_UNAUTHENTICATED")
    channel = str(data.get("authenticated_channel") or "").strip().lower()
    sender = str(data.get("authenticated_sender_id") or "").strip()
    key, path = data.get("config_key"), data.get("config_path")
    if not channel or not sender or bool(key) == bool(path):
        raise ValueError("MEMORY_UNAUTHENTICATED")
    if path:
        from src.application.agent_tool_config import resolve_runtime_config_path
        authority = "path:" + str(resolve_runtime_config_path(config_path=path).resolve(strict=True))
    else:
        authority = "key:" + str(key).lower().strip()
    owner = hashlib.sha256(json.dumps([channel, sender, authority], ensure_ascii=False).encode()).hexdigest()
    return MemoryScope(owner, frozenset(str(a).lower() for a in allowed_accounts))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bot_memory (
            id TEXT PRIMARY KEY, owner_scope TEXT NOT NULL, account_scope TEXT,
            kind TEXT NOT NULL, content TEXT NOT NULL, source_refs_json TEXT NOT NULL,
            superseded_source_refs_json TEXT NOT NULL DEFAULT '[]', source_time TEXT NOT NULL,
            revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS bot_memory_scope ON bot_memory(owner_scope, state, account_scope, kind, updated_at);
        CREATE TABLE IF NOT EXISTS bot_memory_jobs (
            run_id TEXT PRIMARY KEY, owner_scope TEXT NOT NULL, source_refs_json TEXT NOT NULL,
            memory_epoch INTEGER NOT NULL, lease_id TEXT, lease_until REAL,
            attempt INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
            cost_json TEXT NOT NULL DEFAULT '{}', last_error TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS bot_memory_jobs_pending ON bot_memory_jobs(state, updated_at);
    """)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> dict | None:
    cursor = conn.execute(sql, args)
    row = cursor.fetchone()
    return dict(zip((d[0] for d in cursor.description), row)) if row else None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _check_deadline(deadline: float) -> None:
    if not isinstance(deadline, (float, int)) or not time.monotonic() < deadline:
        raise ValueError("MEMORY_DEADLINE")


def _source_ref(run: dict) -> str:
    return f"user:{run['run_id']}:{run['request_id']}"


def _public(row: dict) -> dict:
    return {"id": row["id"], "kind": row["kind"], "account_scope": row["account_scope"],
            "content": row["content"], "revision": row["revision"],
            "source_refs": json.loads(row["source_refs_json"]), "source_time": row["source_time"]}


class BotMemoryStore:
    """Composition only: schema and connection lifetime remain owned by BotHostStore."""

    def __init__(self, host_store: Any) -> None:
        self.host_store = host_store

    def _connect(self) -> sqlite3.Connection:
        # Host calls ensure_schema once during its own schema initialization.
        conn = self.host_store._connect()
        conn.execute("PRAGMA busy_timeout=100")
        return conn

    @staticmethod
    def _epoch(conn: sqlite3.Connection, owner: str, *, advance: bool = False) -> int:
        row = _one(conn, "SELECT revision FROM bot_memory WHERE id=?", ("epoch:" + owner,))
        epoch = int(row["revision"]) if row else 0
        if advance:
            epoch += 1
            conn.execute("""INSERT INTO bot_memory
                (id,owner_scope,kind,content,source_refs_json,source_time,revision,state,updated_at)
                VALUES (?,?,'metadata','','[]',?,?,'internal',?)
                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at""",
                ("epoch:" + owner, owner, _now(), epoch, _now()))
        return epoch

    def recall(self, scope: MemoryScope, query: str = "", *, kind: str | None = None,
               include_stable_preferences: bool = True) -> dict:
        if not scope.owner_scope:
            raise ValueError("MEMORY_UNAUTHENTICATED")
        if kind not in {None, "preference", "experience"}:
            raise ValueError("MEMORY_KIND_INVALID")
        accounts = sorted(scope.allowed_accounts)
        placeholders = ",".join("?" for _ in accounts) or "NULL"
        # Filter before the bounded candidate window, including Chinese substrings.
        terms = list(dict.fromkeys(re.findall(r"[\w]+", query.lower())))[:12]
        if query and any("\u4e00" <= c <= "\u9fff" for c in query):
            terms += [query[i:i + 2] for i in range(min(len(query) - 1, 24))]
        conditions = "" if not kind else " AND kind=?"
        params: list[Any] = [scope.owner_scope, *accounts]
        if kind:
            params.append(kind)
        if terms:
            stable = "kind='preference' OR " if include_stable_preferences else ""
            conditions += " AND (" + stable + " OR ".join("instr(lower(content),?)>0" for _ in terms) + ")"
            params.extend(terms)
        with self._connect() as conn:
            rows = _rows(conn, f"""SELECT * FROM bot_memory WHERE owner_scope=? AND state='active'
                AND kind IN ('preference','experience') AND (account_scope IS NULL OR account_scope IN ({placeholders}))
                {conditions} ORDER BY updated_at DESC,id LIMIT 256""", tuple(params))
            epoch = self._epoch(conn, scope.owner_scope)
        rows.sort(key=lambda r: sum(t in r["content"].lower() for t in terms), reverse=True)
        selected: list[dict] = []
        used = 0
        for row in rows:
            item = _public(row)
            # UTF-8 bytes conservatively upper-bound tokens, including metadata.
            cost = len(_json(item).encode("utf-8"))
            if used + cost > 2000:
                # Keep a manageable identity even when the full legal content or
                # source metadata cannot fit. The prefix is explicitly incomplete.
                item = {'id': row['id'], 'revision': row['revision'], 'content': '',
                        'truncated': True, 'content_chars': len(row['content'])}
                available = 2000 - used
                if len(_json(item).encode('utf-8')) >= available:
                    break
                low, high = 0, len(row['content'])
                while low < high:
                    middle = (low + high + 1) // 2
                    item['content'] = row['content'][:middle]
                    if len(_json(item).encode('utf-8')) <= available:
                        low = middle
                    else:
                        high = middle - 1
                item['content'] = row['content'][:low]
                cost = len(_json(item).encode('utf-8'))
            selected.append(item)
            used += cost
            if len(selected) == 8:
                break
        return {"items": selected, "epoch": epoch, "token_upper_bound": used,
                "partial": len(selected) < len(rows) or len(rows) == 256 or any(i.get("truncated") for i in selected)}

    def act(self, *, run_id: str, scope: MemoryScope, action: str, arguments: dict,
            lease_id: str, deadline_monotonic: float, verified_sources: dict | None = None) -> dict:
        if action in {"list", "search"}:
            with self._connect() as conn:
                self._run(conn, run_id, scope, lease_id, deadline_monotonic)
            return self.recall(scope, str(arguments.get("query") or "") if action == "search" else "",
                               kind=arguments.get("kind"), include_stable_preferences=False)
        if action not in {"remember", "correct", "forget"}:
            raise ValueError("MEMORY_ACTION_INVALID")
        key = arguments.get("idempotency_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("MEMORY_IDEMPOTENCY_REQUIRED")
        signature = hashlib.sha256(_json([action, arguments]).encode()).hexdigest()
        _check_deadline(deadline_monotonic)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = _one(conn, "SELECT * FROM bot_runs WHERE run_id=?", (run_id,))
            if not run or scope_from_contract(contract_from_payload(json.loads(run["contract_json"]))).owner_scope != scope.owner_scope:
                raise ValueError("MEMORY_OWNER_MISMATCH")
            receipt = self._receipt(run, key)
            if receipt:
                if receipt["signature"] != signature:
                    raise ValueError("MEMORY_IDEMPOTENCY_CONFLICT")
            else:
                self._run(conn, run_id, scope, lease_id, deadline_monotonic)
                epoch = self._epoch(conn, scope.owner_scope)
                if type(arguments.get("expected_epoch")) is not int or arguments["expected_epoch"] != epoch:
                    raise ValueError("MEMORY_EPOCH_CONFLICT")
                original = str(json.loads(run["contract_json"])["input"].get("user_message") or "")
                quote = arguments.get("source_quote")
                if not isinstance(quote, str) or not quote.strip() or quote not in original:
                    raise ValueError("MEMORY_USER_SOURCE_REQUIRED")
                target = None
                if action != "remember":
                    target = _one(conn, "SELECT * FROM bot_memory WHERE id=? AND owner_scope=? AND state='active'",
                                  (arguments.get("id"), scope.owner_scope))
                    if target is None or target["kind"] == "metadata":
                        raise ValueError("MEMORY_TARGET_NOT_FOUND")
                    if target["account_scope"] and target["account_scope"] not in scope.allowed_accounts:
                        raise ValueError("MEMORY_ACCOUNT_DENIED")
                expected = target["revision"] if target else 0
                if type(arguments.get("expected_revision")) is not int or arguments["expected_revision"] != expected:
                    raise ValueError("MEMORY_REVISION_CONFLICT")
                candidate = dict(arguments)
                if action != "forget":
                    candidate.setdefault("source_refs", [_source_ref(run)])
                    if action == "correct":
                        candidate["source_refs"] = [_source_ref(run)]
                        candidate.setdefault("kind", target["kind"])
                        candidate.setdefault("account_scope", target["account_scope"])
                        if candidate["kind"] != target["kind"] or candidate["account_scope"] != target["account_scope"]:
                            raise ValueError("MEMORY_CORRECTION_SCOPE_CHANGED")
                    sources = {**(verified_sources or {}),
                               _source_ref(run): {"kind": "user", "text": original, "source_time": run["started_at"]}}
                    self._validate_candidate(conn, scope, candidate, sources, explicit_quote=quote,
                                             correction=action == "correct")
                memory_id = target["id"] if target else "memory_" + uuid.uuid4().hex
                suppression = set(json.loads(target["superseded_source_refs_json"])) if target else set()
                if target:
                    suppression.update(json.loads(target["source_refs_json"]))
                refs = candidate.get("source_refs", []) if action != "forget" else json.loads(target["source_refs_json"])
                conn.execute("""INSERT INTO bot_memory
                    (id,owner_scope,account_scope,kind,content,source_refs_json,superseded_source_refs_json,source_time,revision,state,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    account_scope=excluded.account_scope,kind=excluded.kind,content=excluded.content,
                    source_refs_json=excluded.source_refs_json,superseded_source_refs_json=excluded.superseded_source_refs_json,
                    source_time=excluded.source_time,revision=excluded.revision,state=excluded.state,updated_at=excluded.updated_at""",
                    (memory_id, scope.owner_scope, candidate.get("account_scope") if action != "forget" else target["account_scope"],
                     candidate.get("kind", "preference") if action != "forget" else target["kind"],
                     candidate["content"] if action != "forget" else "", _json(refs), _json(sorted(suppression)),
                     run["started_at"], expected + 1, "tombstone" if action == "forget" else "active", _now()))
                epoch = self._epoch(conn, scope.owner_scope, advance=True)
                receipt = {"action": action, "id": memory_id, "revision": expected + 1, "epoch": epoch,
                           "result": "deleted" if action == "forget" else "saved", "idempotency_key": key,
                           "signature": signature, "owner_scope": scope.owner_scope}
                events = json.loads(run["events_json"])
                events.append({"event_id": "memory_" + uuid.uuid4().hex, "run_id": run_id,
                               "type": "memory_operation_receipt", "timestamp": _now(), "payload": receipt})
                conn.execute("UPDATE bot_runs SET events_json=? WHERE run_id=?", (_json(events), run_id))
                _check_deadline(deadline_monotonic)
        return self.readback(run_id=run_id, scope=scope, idempotency_key=key)

    @staticmethod
    def _receipt(run: dict, key: str) -> dict | None:
        return next((e["payload"] for e in json.loads(run["events_json"]) if e.get("type") == "memory_operation_receipt"
                     and e.get("payload", {}).get("idempotency_key") == key), None)

    def readback(self, *, run_id: str, scope: MemoryScope, idempotency_key: str) -> dict:
        with self._connect() as conn:
            run = _one(conn, "SELECT * FROM bot_runs WHERE run_id=?", (run_id,))
            receipt = self._receipt(run, idempotency_key) if run else None
            if not receipt or receipt["owner_scope"] != scope.owner_scope:
                raise ValueError("MEMORY_RECEIPT_NOT_FOUND")
            row = _one(conn, "SELECT revision,state FROM bot_memory WHERE id=? AND owner_scope=?", (receipt["id"], scope.owner_scope))
            if not row or row["revision"] < receipt["revision"]:
                raise ValueError("MEMORY_READBACK_UNAVAILABLE")
        return {k: v for k, v in receipt.items() if k != "signature"} | {"readback": True, "current_revision": row["revision"]}

    @staticmethod
    def _run(conn: sqlite3.Connection, run_id: str, scope: MemoryScope, lease_id: str, deadline: float) -> dict:
        _check_deadline(deadline)
        row = _one(conn, "SELECT * FROM bot_runs WHERE run_id=?", (run_id,))
        if not row or row["cancel_requested"] or row["status"] not in {"running", "waiting_model", "waiting_tool"} or row["admission_state"] != "open":
            raise ValueError("MEMORY_RUN_CLOSED")
        if not lease_id or row.get("lease_id") != lease_id:
            raise ValueError("MEMORY_LEASE_LOST")
        if row.get("deadline_at") and datetime.fromisoformat(row["deadline_at"]) <= datetime.now(timezone.utc):
            raise ValueError("MEMORY_DEADLINE")
        if scope_from_contract(contract_from_payload(json.loads(row["contract_json"]))).owner_scope != scope.owner_scope:
            raise ValueError("MEMORY_OWNER_MISMATCH")
        return row

    @staticmethod
    def _validate_candidate(conn: sqlite3.Connection, scope: MemoryScope, candidate: dict,
                            sources: dict, *, explicit_quote: str | None = None, correction: bool = False) -> None:
        kind, content, refs = candidate.get("kind", "preference"), candidate.get("content"), candidate.get("source_refs")
        if kind not in {"preference", "experience"} or not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
            raise ValueError("MEMORY_CONTENT_INVALID")
        if re.search(r"(?i)(api[_ -]?key|password|secret|bearer\s|密码|密钥|授权.{0,12}(交易|下单|转账)|无需确认.{0,12}(执行|发送|交易))", content):
            raise ValueError("MEMORY_SENSITIVE_CONTENT")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 16 or any(not isinstance(ref, str) or ref not in sources for ref in refs):
            raise ValueError("MEMORY_SOURCE_UNVERIFIED")
        account = candidate.get("account_scope")
        if account is not None and account not in scope.allowed_accounts:
            raise ValueError("MEMORY_ACCOUNT_DENIED")
        if kind == "preference":
            if account is not None or not any(sources[r].get("kind") == "user" and content in sources[r].get("text", "") for r in refs):
                raise ValueError("MEMORY_PREFERENCE_SOURCE_REQUIRED")
            if explicit_quote is not None and content not in explicit_quote:
                raise ValueError("MEMORY_PREFERENCE_SOURCE_REQUIRED")
        elif correction and explicit_quote is not None and account and content in explicit_quote:
            if not all(sources[r].get("kind") == "user" for r in refs):
                raise ValueError("MEMORY_USER_SOURCE_REQUIRED")
        elif not account or not all(sources[r].get("kind") in {"user", "evidence"} for r in refs) or not any(
            sources[r].get("kind") == "evidence" and sources[r].get("account_scope") == account
            and content in sources[r].get("text", "") for r in refs
        ):
            raise ValueError("MEMORY_EXPERIENCE_SOURCE_REQUIRED")
        # Source identity, not candidate IDs/epochs/summary text, prevents resurrection.
        for ref in refs:
            blocked = conn.execute("""SELECT 1 FROM bot_memory m WHERE m.owner_scope=? AND
                (EXISTS (SELECT 1 FROM json_each(m.superseded_source_refs_json) s WHERE s.value=?) OR
                 (m.state='tombstone' AND EXISTS (SELECT 1 FROM json_each(m.source_refs_json) s WHERE s.value=?))) LIMIT 1""",
                (scope.owner_scope, ref, ref)).fetchone()
            if blocked:
                raise ValueError("MEMORY_SOURCE_SUPPRESSED")


def memory_tool_description() -> dict:
    """Private Host protocol only; never registered as a general OM business tool."""
    return {"name": "bot_memory", "description":
        "Manage only this authenticated user's memory. list/search first to obtain epoch and exact id/revision. "
        "A partial result contains bounded previews, not the complete saved text; truncated items retain id/revision for management. "
        "Writes require the current user turn's verbatim source_quote, expected_epoch, expected_revision (0 for remember), "
        "and a unique idempotency_key. preference content must be an exact quote excerpt; experience needs original "
        "verified source_refs and its authorized account_scope. correct/forget need one exact id; clarify ambiguity. "
        "No secrets, action authority, source edits or business writes. Confirm maintenance only from a successful readback receipt. "
        "If a write fails, retry the same idempotency key or submit exactly: 记忆操作未确认，请重试同一幂等键或重新查询。",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "search", "remember", "correct", "forget"]},
            "query": {"type": "string"}, "kind": {"type": "string", "enum": ["preference", "experience"]},
            "id": {"type": "string"}, "content": {"type": "string", "maxLength": 2000},
            "account_scope": {"type": ["string", "null"]},
            "source_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "source_quote": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 0},
            "expected_epoch": {"type": "integer", "minimum": 0},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["action"], "additionalProperties": False}}


def bounded_memory_context(memory: dict, progress: list[dict], bound: dict | None = None,
                           *, limit: int = 3500) -> dict:
    """Bound the combined injection; complete bound goal takes precedence over optional recall."""
    from src.application.bot.tools import conservative_json_tokens

    result = {'memory': {**memory, 'items': list(memory.get('items', []))},
              'unfinished_progress': [], 'bound_progress': bound, 'additional_progress': False}
    while result['memory']['items'] and conservative_json_tokens(result) > limit:
        result['memory']['items'].pop()
        result['memory']['partial'] = True
    if conservative_json_tokens(result) > limit:
        raise ValueError('MEMORY_BOUND_PROGRESS_TOO_LARGE')
    for item in progress:
        if bound and item.get('progress_ref') == bound.get('progress_ref'):
            continue
        candidate = {'progress_ref': item['progress_ref'], 'revision': item['revision'],
                     'goal': str(item.get('goal', ''))[:160], 'accounts': item.get('accounts', []),
                     'detail_required': True}
        result['unfinished_progress'].append(candidate)
        if conservative_json_tokens(result) > limit:
            result['unfinished_progress'].pop()
            result['additional_progress'] = True
            break
    return result
