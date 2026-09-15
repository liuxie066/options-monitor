"""Append-only current-policy bindings; activation history stays immutable."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from .repository_schema import now_ms


TABLE = "wheel_activation_policy_bindings"
WINDOW_FIELDS = (
    "market", "account", "generation", "activated_at_ms", "deactivated_at_ms",
    "policy_hash", "activation_request_id", "activation_request_hash",
    "deactivation_request_id", "deactivation_request_hash",
)


def ensure_wheel_policy_bindings(conn: sqlite3.Connection) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
          market TEXT NOT NULL CHECK(market IN ('us', 'hk')),
          account TEXT NOT NULL CHECK(account != '' AND account = lower(account)),
          generation INTEGER NOT NULL CHECK(generation > 0),
          revision INTEGER NOT NULL CHECK(revision > 0),
          previous_policy_hash TEXT NOT NULL CHECK(length(previous_policy_hash) = 64 AND previous_policy_hash NOT GLOB '*[^0-9a-f]*'),
          policy_hash TEXT NOT NULL CHECK(length(policy_hash) = 64 AND policy_hash NOT GLOB '*[^0-9a-f]*' AND policy_hash != previous_policy_hash),
          request_id TEXT NOT NULL CHECK(request_id != ''),
          request_hash TEXT NOT NULL CHECK(length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
          actor TEXT NOT NULL CHECK(actor != ''),
          request_json TEXT NOT NULL CHECK(json_valid(request_json)),
          created_at_ms INTEGER NOT NULL CHECK(created_at_ms > 0),
          PRIMARY KEY(market, account, generation, revision),
          UNIQUE(market, account, request_id),
          FOREIGN KEY(market, account, generation) REFERENCES wheel_activation_windows(market, account, generation)
        )
    """)
    for action in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_wheel_policy_bindings_{action.lower()}
            BEFORE {action} ON {TABLE} BEGIN
              SELECT RAISE(ABORT, 'wheel policy bindings are append-only');
            END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_wheel_policy_bindings_insert
        BEFORE INSERT ON {TABLE} BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM wheel_activation_windows w
            WHERE w.market = NEW.market AND w.account = NEW.account
              AND w.generation = NEW.generation AND w.deactivated_at_ms IS NULL
              AND w.generation = (SELECT MAX(generation) FROM wheel_activation_windows
                WHERE market = NEW.market AND account = NEW.account)
              AND NEW.created_at_ms >= w.activated_at_ms
          ) THEN RAISE(ABORT, 'wheel policy binding requires latest open window') END;
          SELECT CASE WHEN NEW.revision != COALESCE((
            SELECT MAX(revision) + 1 FROM {TABLE}
            WHERE market = NEW.market AND account = NEW.account AND generation = NEW.generation
          ), 1) THEN RAISE(ABORT, 'wheel policy binding revision conflict') END;
          SELECT CASE WHEN NEW.previous_policy_hash != COALESCE((
            SELECT policy_hash FROM {TABLE}
            WHERE market = NEW.market AND account = NEW.account AND generation = NEW.generation
            ORDER BY revision DESC LIMIT 1
          ), (SELECT policy_hash FROM wheel_activation_windows WHERE market = NEW.market
            AND account = NEW.account AND generation = NEW.generation))
          THEN RAISE(ABORT, 'wheel policy binding hash conflict') END;
        END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_wheel_policy_bindings_window_close
        BEFORE UPDATE OF deactivated_at_ms ON wheel_activation_windows
        WHEN NEW.deactivated_at_ms IS NOT NULL AND EXISTS (
          SELECT 1 FROM {TABLE} WHERE market = OLD.market AND account = OLD.account
            AND generation = OLD.generation AND created_at_ms > NEW.deactivated_at_ms
        ) BEGIN
          SELECT RAISE(ABORT, 'wheel activation close precedes policy binding');
        END""")


def _hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_request(request: dict[str, Any], request_hash: str) -> None:
    if not isinstance(request, dict) or request.get("schema_version") != "wheel_policy_rebind_request.v1":
        raise ValueError("invalid wheel policy binding request")
    if not _hash(request_hash) or canonical_sha256(request) != request_hash:
        raise ValueError("wheel policy binding request hash mismatch")
    for field in ("request_id", "actor", "account"):
        value = request.get(field)
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"invalid wheel policy binding {field}")
    if request["account"] != request["account"].lower() or request.get("market") not in {"us", "hk"}:
        raise ValueError("invalid wheel policy binding scope")
    if type(request.get("expected_revision")) is not int or request["expected_revision"] < 0:
        raise ValueError("invalid wheel policy binding revision")
    if not all(_hash(request.get(key)) for key in ("effective_policy_hash", "target_policy_hash")):
        raise ValueError("invalid wheel policy binding policy hash")
    if request["effective_policy_hash"] == request["target_policy_hash"]:
        raise ValueError("wheel policy binding requires policy drift")
    window = request.get("window")
    if not isinstance(window, dict) or set(window) != set(WINDOW_FIELDS):
        raise ValueError("wheel policy binding requires complete original window identity")
    if any(window.get(key) is not None for key in ("deactivated_at_ms", "deactivation_request_id", "deactivation_request_hash")):
        raise ValueError("wheel policy binding requires open window")
    for key in ("generation", "activated_at_ms"):
        if type(window.get(key)) is not int or window[key] <= 0:
            raise ValueError("invalid wheel policy binding window identity")
    if any(window[key] != request[key] for key in ("market", "account")):
        raise ValueError("wheel policy binding window scope mismatch")
    if not _hash(window.get("policy_hash")) or not _hash(window.get("activation_request_hash")) or not window.get("activation_request_id"):
        raise ValueError("invalid wheel policy binding original request")
    for key in ("source", "runtime", "ledger"):
        identity = request.get(key)
        if not isinstance(identity, dict) or not isinstance(identity.get("path"), str) or not Path(identity["path"]).is_absolute():
            raise ValueError("invalid wheel policy binding file identity")
        if any(type(identity.get(field)) is not int or identity[field] < 0 for field in ("device", "inode")):
            raise ValueError("invalid wheel policy binding file identity")
        if key != "ledger" and not _hash(identity.get("sha256")):
            raise ValueError("invalid wheel policy binding source hash")


def read_wheel_policy_bindings(
    conn: sqlite3.Connection, *, market: str, account: str, windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate every scoped chain in the caller's window read transaction."""
    if not conn.in_transaction:
        raise ValueError("wheel policy binding read requires a transaction")
    table = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    if table is None:
        return []
    columns = conn.execute(f"PRAGMA table_info({TABLE})").fetchall()
    expected_columns = {"market", "account", "generation", "revision", "previous_policy_hash", "policy_hash",
                        "request_id", "request_hash", "actor", "request_json", "created_at_ms"}
    primary_key = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
    invalid_column = any(
        row["type"] != ("INTEGER" if row["name"] in {"generation", "revision", "created_at_ms"} else "TEXT")
        or not row["notnull"] for row in columns
    )
    if (table["type"] != "table" or {row["name"] for row in columns} != expected_columns
            or primary_key != ["market", "account", "generation", "revision"] or invalid_column):
        raise ValueError("wheel policy binding schema is unreadable")
    rows = conn.execute(f"SELECT * FROM {TABLE} WHERE market=? AND account=? ORDER BY generation, revision", (market, account)).fetchall()
    by_generation = {w["generation"]: w for w in windows}
    previous: dict[int, dict[str, Any]] = {}
    requests: set[str] = set()
    result = []
    for raw in rows:
        row = dict(raw)
        request = json.loads(row.pop("request_json"))
        _validate_request(request, row["request_hash"])
        generation = row["generation"]
        window = by_generation.get(generation)
        if window is None:
            raise ValueError("wheel policy binding window missing")
        original = {key: window[key] for key in WINDOW_FIELDS}
        original.update(deactivated_at_ms=None, deactivation_request_id=None, deactivation_request_hash=None)
        last = previous.get(generation)
        revision = last["revision"] + 1 if last else 1
        policy_hash = last["policy_hash"] if last else window["policy_hash"]
        expected = {
            "market": request["market"], "account": request["account"],
            "generation": request["window"]["generation"], "revision": request["expected_revision"] + 1,
            "previous_policy_hash": request["effective_policy_hash"], "policy_hash": request["target_policy_hash"],
            "request_id": request["request_id"], "actor": request["actor"],
        }
        if (request["window"] != original or any(row.get(k) != v for k, v in expected.items())
                or row["revision"] != revision or row["previous_policy_hash"] != policy_hash
                or row["request_id"] in requests
                or type(row["created_at_ms"]) is not int
                or row["created_at_ms"] < window["activated_at_ms"]
                or (window["deactivated_at_ms"] is not None and row["created_at_ms"] > window["deactivated_at_ms"])
                or (last is not None and row["created_at_ms"] < last["created_at_ms"])):
            raise ValueError("wheel policy binding chain is corrupt")
        row["request"] = request
        requests.add(row["request_id"])
        previous[generation] = row
        result.append(row)
    return result


def effective_wheel_window(window: dict[str, Any], bindings: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [row for row in bindings if row["generation"] == window["generation"]]
    last = matches[-1] if matches else None
    return {**window, "effective_policy_hash": last["policy_hash"] if last else window["policy_hash"],
            "policy_binding_revision": last["revision"] if last else 0}


class WheelPolicyRepositoryMixin:
    def list_wheel_policy_bindings(self, *, market: str, account: str, conn=None) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active:
            if not active.in_transaction:
                active.execute("BEGIN")
            windows = self.list_wheel_activation_windows(market=market, account=account, conn=active)
            return read_wheel_policy_bindings(active, market=market.strip().lower(), account=account.strip(), windows=windows)

    def append_wheel_policy_binding(self, *, request: dict[str, Any], request_hash: str, conn) -> dict[str, Any]:
        if conn is None or not conn.in_transaction:
            raise ValueError("wheel policy binding requires an active transaction")
        _validate_request(request, request_hash)
        market, account = request["market"], request["account"]
        windows = self.list_wheel_activation_windows(market=market, account=account, conn=conn)
        bindings = read_wheel_policy_bindings(conn, market=market, account=account, windows=windows)
        latest = windows[-1] if windows else None
        current = effective_wheel_window(latest, bindings) if latest else None
        replay = next((row for row in bindings if row["request_id"] == request["request_id"]), None)
        if replay:
            if replay["request_hash"] != request_hash or replay["request"] != request:
                raise ValueError("wheel policy binding request identity conflict")
            if (not current or current["deactivated_at_ms"] is not None
                    or current["generation"] != replay["generation"]
                    or current["policy_binding_revision"] != replay["revision"]):
                raise ValueError("wheel policy binding request superseded")
            return replay
        if (not current or latest != request["window"] or current["deactivated_at_ms"] is not None
                or current["policy_binding_revision"] != request["expected_revision"]
                or current["effective_policy_hash"] != request["effective_policy_hash"]):
            raise ValueError("wheel policy binding CAS conflict")
        path = self.db_path.resolve()
        database = next(row for row in conn.execute("PRAGMA database_list") if row["name"] == "main")
        if Path(database["file"]).resolve() != path:
            raise ValueError("wheel policy binding transaction ledger mismatch")
        stat = path.stat()
        if request["ledger"] != {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino}:
            raise ValueError("wheel policy binding ledger identity conflict")
        created = max(now_ms(), current["activated_at_ms"], bindings[-1]["created_at_ms"] if bindings else 0)
        conn.execute(f"""INSERT INTO {TABLE} (market, account, generation, revision,
            previous_policy_hash, policy_hash, request_id, request_hash, actor, request_json, created_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            market, account, current["generation"], request["expected_revision"] + 1,
            request["effective_policy_hash"], request["target_policy_hash"], request["request_id"], request_hash,
            request["actor"], json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")), created,
        ))
        return read_wheel_policy_bindings(conn, market=market, account=account, windows=windows)[-1]
