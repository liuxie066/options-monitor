from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from src.infrastructure.pi_agent_process import _runtime_command, derive_pi_session_id
from test_pi_agent_process import (
    _run_session,
    _seed_compactable_history,
    _session_entries,
    _start_payload,
    _tool_payload,
    _tool_turn,
)


REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "agent-runtime"


def _node_eval(source: str, *arguments: str) -> dict:
    command, _ = _runtime_command(None, None)
    completed = subprocess.run(
        [command[0], "--no-warnings", "--input-type=module", "--eval", source, *arguments],
        cwd=RUNTIME,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _create_empty_target_session(database: Path, session_id: str) -> None:
    result = _node_eval(
        """
        import path from "node:path";
        import { TODO_CONTEXT } from "@earendil-works/pi-agent-core";
        import {
          SqliteSessionRepo, createNodeSqliteFactory,
        } from "@earendil-works/pi-session-backend-sqlite-node";
        const [database, sessionId] = process.argv.slice(1);
        const repo = new SqliteSessionRepo({
          directory: path.dirname(database), databasePath: database,
          databaseFactory: createNodeSqliteFactory(),
        });
        await repo.create({ id: sessionId }, TODO_CONTEXT);
        await repo.close(TODO_CONTEXT);
        process.stdout.write(JSON.stringify({ ok: true }));
        """,
        str(database),
        session_id,
    )
    assert result == {"ok": True}


def _receipt(database: Path, phase: str = "published", target_version: str = "0.85.1") -> dict:
    def runtime_identity(version: str, fill: str) -> dict:
        return {
            "ok": True,
            "version": version,
            "lockSha256": fill * 64,
            "packages": {
                name: {
                    "version": version,
                    "manifestSha256": fill * 64,
                    "entrySha256": fill * 64,
                }
                for name in (
                    "@earendil-works/pi-agent-core",
                    "@earendil-works/pi-ai",
                    "@earendil-works/pi-session-backend-sqlite-node",
                )
            },
        }

    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    receipt = {
        "version": "om-pi-migration.v1",
        "database": str(database.resolve()),
        "phase": phase,
        "rollbackRequired": True,
        "source": {
            "runtimePath": str(RUNTIME.resolve()),
            "runtime": runtime_identity("0.84.2", "4"),
            "database": {"sha256": "1" * 64, "format": "legacy"},
        },
        "target": {
            "runtimePath": str(RUNTIME.resolve()),
            "runtime": runtime_identity(target_version, "5"),
            "database": {"sha256": digest, "format": "target"},
        },
        "backup": {
            "path": str(database.resolve()) + ".om-pi-recovery-0.84.2-to-0.85.1-" + "a" * 32 + ".sqlite3",
            "sha256": "2" * 64,
        },
    }
    if phase == "published":
        receipt["readback"] = {"equivalent": True, "contextSha256": "3" * 64}
        receipt["published"] = {"databaseSha256": digest}
    return receipt


def _write_receipt(database: Path, receipt: dict) -> Path:
    target = Path(str(database) + ".om-pi-migration.json")
    target.write_text(json.dumps(receipt), encoding="utf-8")
    target.chmod(0o600)
    return target


def test_empty_public_session_initialization_recovers_without_disturbing_peer(tmp_path):
    database = tmp_path / "sessions.sqlite3"
    peer = derive_pi_session_id("feishu", "peer", "group", "key:us")
    recovering = derive_pi_session_id("feishu", "recover", "group", "key:us")
    peer_result = _run_session(
        database,
        peer,
        _start_payload(
            session_id=peer,
            user_message="peer-history",
            debug={"fixture_response": "peer-answer", "delay_ms": 0},
        ),
        run_id="peer_seed",
    )
    assert peer_result["ok"] is True
    peer_entries = _session_entries(database, peer)
    _create_empty_target_session(database, recovering)

    recovered = _run_session(
        database,
        recovering,
        _start_payload(
            session_id=recovering,
            user_message="recovered-history",
            debug={"fixture_response": "recovered-answer", "delay_ms": 0},
        ),
        run_id="empty_recovery",
    )

    assert recovered["ok"] is True, recovered
    assert _session_entries(database, peer) == peer_entries
    with sqlite3.connect(database) as connection:
        values = dict(
            connection.execute(
                "SELECT namespace || ':' || key, value FROM scalar_values "
                "WHERE session_id = ?",
                (recovering,),
            )
        )
    assert json.loads(values["om.pi:session-format"]) == "om-pi-session.v1"
    assert json.loads(values["pi.branch.tip:main"]) == _session_entries(database, recovering)[-1]["id"]


def test_exact_schema_probe_rejects_altered_constraint_without_writing_database(tmp_path):
    database = tmp_path / "hostile.sqlite3"
    session_id = derive_pi_session_id("feishu", "schema", "group", "key:us")
    seeded = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "seed", "delay_ms": 0}),
        run_id="schema_seed",
    )
    assert seeded["ok"] is True
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER trg_entries_validate")
        connection.execute(
            "CREATE TRIGGER trg_entries_validate BEFORE INSERT ON entries BEGIN SELECT 1; END"
        )
    before = database.read_bytes()
    entries = _session_entries(database, session_id)

    rejected = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "must-not-run", "delay_ms": 0}),
        run_id="schema_rejected",
    )

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "SESSION_ERROR"
    assert database.read_bytes() == before
    assert _session_entries(database, session_id) == entries


def test_dangling_main_tip_fails_closed_without_rewind(tmp_path):
    database = tmp_path / "dangling.sqlite3"
    session_id = derive_pi_session_id("feishu", "dangling", "group", "key:us")
    seeded = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "seed", "delay_ms": 0}),
        run_id="dangling_seed",
    )
    assert seeded["ok"] is True
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "UPDATE scalar_values SET value = ? "
            "WHERE session_id = ? AND namespace = 'pi.branch.tip' AND key = 'main'",
            (json.dumps("missing-entry-id"), session_id),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = database.read_bytes()

    rejected = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "must-not-run", "delay_ms": 0}),
        run_id="dangling_rejected",
    )

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "SESSION_ERROR"
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT value FROM scalar_values WHERE session_id = ? "
            "AND namespace = 'pi.branch.tip' AND key = 'main'",
            (session_id,),
        ).fetchone()
    assert stored is not None and json.loads(stored[0]) == "missing-entry-id"


def test_uncommitted_public_tail_rewinds_before_next_admitted_turn(tmp_path):
    database = tmp_path / "tail.sqlite3"
    session_id = derive_pi_session_id("feishu", "tail", "group", "key:us")
    assert _run_session(
        database, session_id,
        _start_payload(session_id=session_id, user_message="committed question",
                       debug={"fixture_response": "committed answer", "delay_ms": 0}),
        run_id="tail_baseline",
    )["ok"] is True
    baseline = _session_entries(database, session_id)
    _node_eval(
        """
        import { TODO_CONTEXT, branchTip, insertEntry, setValue } from "@earendil-works/pi-agent-core";
        import { openTargetSession } from "./pi_session_adapter.ts";
        const [database, sessionId] = process.argv.slice(1);
        const opened = await openTargetSession(database, sessionId);
        await opened.session.mutate(async (mutator, context) => {
          const parent = await mutator.getValue(branchTip("main"), context);
          await mutator.commit([
            insertEntry({id: "orphan_tail", parentId: parent.value, type: "message",
              message: {role: "user", content: "uncommitted question", timestamp: 1000}}),
            setValue(branchTip("main"), "orphan_tail"),
          ], context);
        }, TODO_CONTEXT);
        await opened.repository.close(TODO_CONTEXT);
        process.stdout.write(JSON.stringify({ok: true}));
        """,
        str(database), session_id,
    )
    recovered = _run_session(
        database, session_id,
        _start_payload(session_id=session_id, user_message="next question", debug={
            "fixture_response": "next answer", "delay_ms": 0,
            "expected_history": ["committed question", "committed answer"],
            "forbidden_history": ["uncommitted question"],
        }), run_id="tail_recovery",
    )
    assert recovered["ok"] is True, recovered
    entries = _session_entries(database, session_id)
    assert entries[:len(baseline)] == baseline
    assert entries[len(baseline)]["id"] == "orphan_tail"
    assert entries[len(baseline) + 1]["parent_id"] == baseline[-1]["id"]
    assert entries[-1]["payload"]["data"]["run_id"] == "tail_recovery"


def test_unresolved_and_invalid_migration_receipts_block_before_session_write(tmp_path):
    database = tmp_path / "receipted.sqlite3"
    session_id = derive_pi_session_id("feishu", "receipt", "group", "key:us")
    seeded = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "seed", "delay_ms": 0}),
        run_id="receipt_seed",
    )
    assert seeded["ok"] is True
    before = database.read_bytes()
    entries = _session_entries(database, session_id)
    receipt_path = _write_receipt(database, _receipt(database, phase="validated"))

    unresolved = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "blocked", "delay_ms": 0}),
        run_id="receipt_unresolved",
    )
    assert unresolved["ok"] is False
    assert unresolved["error"]["code"] == "SESSION_ERROR"
    assert database.read_bytes() == before
    assert _session_entries(database, session_id) == entries

    invalid_receipts = [
        _receipt(database, target_version="0.84.2"),
        _receipt(database),
        _receipt(database),
        _receipt(database),
    ]
    del invalid_receipts[1]["target"]["runtime"]["lockSha256"]
    invalid_receipts[2]["source"]["runtime"]["version"] = "0.85.1"
    invalid_receipts[3]["backup"]["path"] = str(database.resolve()) + ".om-pi-recovery.sqlite3"
    for index, invalid_receipt in enumerate(invalid_receipts):
        _write_receipt(database, invalid_receipt)
        invalid = _run_session(
            database,
            session_id,
            _start_payload(session_id=session_id, debug={"fixture_response": "blocked", "delay_ms": 0}),
            run_id=f"receipt_invalid_{index}",
        )
        assert invalid["ok"] is False
        assert invalid["error"]["code"] == "SESSION_ERROR"
        assert database.read_bytes() == before
        assert _session_entries(database, session_id) == entries
    assert receipt_path.exists()


def test_valid_published_receipt_allows_target_history_to_advance(tmp_path):
    database = tmp_path / "published.sqlite3"
    session_id = derive_pi_session_id("feishu", "published", "group", "key:us")
    seeded = _run_session(
        database,
        session_id,
        _start_payload(session_id=session_id, debug={"fixture_response": "seed", "delay_ms": 0}),
        run_id="published_seed",
    )
    assert seeded["ok"] is True
    _write_receipt(database, _receipt(database))

    for index in range(2):
        advanced = _run_session(
            database,
            session_id,
            _start_payload(
                session_id=session_id,
                user_message=f"published-question-{index}",
                debug={"fixture_response": f"published-answer-{index}", "delay_ms": 0},
            ),
            run_id=f"published_advance_{index}",
        )
        assert advanced["ok"] is True, advanced
    persisted = json.dumps(_session_entries(database, session_id), ensure_ascii=False)
    assert "published-question-0" in persisted
    assert "published-question-1" in persisted


def test_distinct_sessions_concurrently_bootstrap_shared_database(tmp_path):
    database = tmp_path / "concurrent.sqlite3"
    session_ids = [
        derive_pi_session_id("feishu", f"concurrent-{index}", "group", "key:us")
        for index in range(2)
    ]

    def run(index: int) -> dict:
        return _run_session(
            database,
            session_ids[index],
            _start_payload(
                session_id=session_ids[index],
                user_message=f"concurrent-question-{index}",
                debug={"fixture_response": f"concurrent-answer-{index}", "delay_ms": 100},
            ),
            run_id=f"concurrent_{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, range(2)))

    assert all(result["ok"] is True for result in results), results
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone() == (2,)
    for index, session_id in enumerate(session_ids):
        persisted = json.dumps(_session_entries(database, session_id), ensure_ascii=False)
        assert f"concurrent-question-{index}" in persisted
        assert f"concurrent-answer-{index}" in persisted


def test_one_run_can_commit_initial_and_mid_turn_compactions(tmp_path):
    database = tmp_path / "same_run_compactions.sqlite3"
    session_id = derive_pi_session_id("feishu", "same-run", "group", "key:us")
    _seed_compactable_history(database, session_id, label="same_run")
    payload = _tool_payload(
        [_tool_turn(arguments={"index": 1}), {"text": "finished"}],
        session_id=session_id,
        user_message="current question",
        model={**_start_payload()["model"], "context_window_tokens": 8_000},
    )
    payload["debug"]["compaction_response"] = "same-run-summary"

    result = _run_session(
        database,
        session_id,
        payload,
        run_id="same_run_compactions",
        on_tool_call=lambda _call: {"ok": True, "value": "x" * 4_500},
    )

    assert result["ok"] is True, result
    markers = [
        entry["payload"]["data"]
        for entry in _session_entries(database, session_id)
        if entry["type"] == "custom"
        and entry["payload"]["data"]["run_id"] == "same_run_compactions"
    ]
    assert [marker["kind"] for marker in markers] == ["compaction", "compaction", "turn"]


def test_ambiguous_atomic_commit_readback_deduplicates_matching_run(tmp_path):
    database = tmp_path / "ambiguous.sqlite3"
    session_id = derive_pi_session_id("feishu", "ambiguous", "group", "key:us")
    result = _node_eval(
        """
        import {
          appendCommittedCompaction, appendCommittedTurn, exportCommittedMain, openTargetSession,
        } from "./pi_session_adapter.ts";
        const [database, sessionId] = process.argv.slice(1);
        const opened = await openTargetSession(database, sessionId);
        const originalMutate = opened.session.mutate.bind(opened.session);
        let injectAmbiguousReturn = true;
        opened.session.mutate = async (...args) => {
          const result = await originalMutate(...args);
          if (injectAmbiguousReturn) {
            injectAmbiguousReturn = false;
            throw new Error("injected ambiguous return after durable commit");
          }
          return result;
        };
        const messages = [
          { role: "user", content: [{ type: "text", text: "ambiguous-question" }], timestamp: 1000 },
          {
            role: "assistant", content: [{ type: "text", text: "ambiguous-answer" }],
            api: "openai-completions", provider: "fixture", model: "fixture",
            usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
              cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
            stopReason: "stop", timestamp: 1001,
          },
        ];
        await appendCommittedTurn(opened.session, messages, "ambiguous-run");
        await appendCommittedTurn(opened.session, messages, "ambiguous-run");
        const compactionParent = (await exportCommittedMain(opened.session)).at(-1)?.id ?? null;
        const compaction = {
          summary: "ambiguous-summary", retainedTail: messages, tokensBefore: 17,
          details: { z: 2, a: 1 }, usage: messages[1].usage,
        };
        injectAmbiguousReturn = true;
        await appendCommittedCompaction(
          opened.session, compaction, "ambiguous-compaction", compactionParent,
        );
        await appendCommittedCompaction(opened.session, {
          usage: messages[1].usage, details: { a: 1, z: 2 }, tokensBefore: 17,
          retainedTail: messages, summary: "ambiguous-summary",
        }, "ambiguous-compaction", compactionParent);
        let mismatchRejected = false;
        try {
          await appendCommittedCompaction(opened.session, {
            ...compaction, summary: "different-summary",
          }, "ambiguous-compaction", compactionParent);
        } catch {
          mismatchRejected = true;
        }
        const entries = await exportCommittedMain(opened.session);
        await opened.repository.close((await import("@earendil-works/pi-agent-core")).TODO_CONTEXT);
        process.stdout.write(JSON.stringify({
          entryTypes: entries.map((entry) => entry.type),
          runs: entries.filter((entry) => entry.type === "custom").map((entry) => entry.data.run_id),
          mismatchRejected,
        }));
        """,
        str(database),
        session_id,
    )

    assert result == {
        "entryTypes": ["message", "message", "custom", "compaction", "custom"],
        "runs": ["ambiguous-run", "ambiguous-compaction"],
        "mismatchRejected": True,
    }
    assert len(_session_entries(database, session_id)) == 5
