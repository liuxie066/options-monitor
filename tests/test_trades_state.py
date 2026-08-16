from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from src.application.ledger.api import build_lifecycle_attempt_run_seal
from src.application.trades import state as state_module
from src.application.trades.state import (
    append_lifecycle_attempt_checkpoint_seal,
    append_trade_intake_audit,
    is_failed_deal,
    is_retryable_unresolved_deal,
    load_trade_intake_state,
    lookup_deal_state_entry,
    lookup_deal_state,
    read_latest_lifecycle_attempt_run_seal,
    upsert_deal_state,
    write_trade_intake_state,
)


def _append_audit_rows(path: str, *, durable: bool, count: int) -> None:
    for index in range(count):
        payload = (
            build_lifecycle_attempt_run_seal(
                account="lx",
                source_id="source-a",
                completed_at_ms=index + 1,
                heads=[],
                seal_scope="all_heads_checkpoint",
                reason="process_startup",
            )
            if durable
            else {"phase": "ordinary", "index": index}
        )
        append_trade_intake_audit(path, payload, durable=durable)


def test_trade_intake_state_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = upsert_deal_state(
        {},
        bucket="processed_deal_ids",
        deal_id="deal-1",
        payload={"status": "applied", "action": "open", "account": "lx"},
    )
    write_trade_intake_state(state_path, state)

    loaded = load_trade_intake_state(state_path)

    assert lookup_deal_state(loaded, "deal-1")["status"] == "applied"
    assert lookup_deal_state_entry(loaded, "deal-1")[0] == "processed_deal_ids"


def test_trade_intake_audit_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    append_trade_intake_audit(path, {"phase": "received", "deal_id": "deal-1"})
    append_trade_intake_audit(path, {"phase": "resolved", "deal_id": "deal-1"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == 2
    assert '"phase": "received"' in lines[0]


def test_durable_trade_intake_audit_repairs_only_torn_tail_and_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "audit.jsonl"
    complete = b'{"phase":"complete"}\n'
    path.write_bytes(complete + b"x" * 1_000)
    fsync_calls: list[int] = []
    monkeypatch.setattr(state_module.os, "fsync", lambda descriptor: fsync_calls.append(descriptor))

    append_trade_intake_audit(path, {"phase": "sealed"}, durable=True)

    assert path.read_bytes().startswith(complete)
    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"phase":"complete"}',
        '{"phase": "sealed"}',
    ]
    assert len(fsync_calls) == 1


def test_trade_intake_audit_repairs_large_torn_tail_and_refuses_non_durable_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    original = b'{"phase":"complete"}\n' + b"x" * 70_000
    path.write_bytes(original)

    append_trade_intake_audit(path, {"phase": "sealed"}, durable=True)
    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"phase":"complete"}',
        '{"phase": "sealed"}',
    ]

    path.write_bytes(b"x" * 70_000)
    append_trade_intake_audit(path, {"phase": "sealed"}, durable=True)
    assert path.read_text(encoding="utf-8") == '{"phase": "sealed"}\n'

    path.write_bytes(b'{"phase":"torn"}')
    with pytest.raises(OSError, match="unterminated tail"):
        append_trade_intake_audit(path, {"phase": "ordinary"})
    assert path.read_bytes() == b'{"phase":"torn"}'


def test_ordinary_trade_intake_audit_does_not_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    monkeypatch.setattr(state_module.os, "fsync", lambda descriptor: fsync_calls.append(descriptor))

    append_trade_intake_audit(tmp_path / "audit.jsonl", {"phase": "ordinary"})

    assert fsync_calls == []


def test_trade_intake_seal_reader_is_strict_and_tolerates_only_torn_eof(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    seal = build_lifecycle_attempt_run_seal(
        account="lx",
        source_id="source-a",
        completed_at_ms=1,
        heads=[],
        seal_scope="all_heads_checkpoint",
        reason="process_startup",
    )
    append_trade_intake_audit(path, {"phase": "ordinary"})
    append_trade_intake_audit(path, seal)
    with path.open("ab") as handle:
        handle.write(b'{"torn":')

    result = read_latest_lifecycle_attempt_run_seal(
        path,
        account="lx",
        source_id="source-a",
    )

    assert result == {
        "schema_version": "trade_lifecycle_attempt_run_seal_reader.v1",
        "seal_count": 1,
        "last_seal": seal,
        "torn_tail_ignored": True,
    }

    path.write_bytes(b"not-json\n")
    with pytest.raises(ValueError, match="malformed trade intake audit line 1"):
        read_latest_lifecycle_attempt_run_seal(path)

    path.write_bytes(b'{}')
    with pytest.raises(ValueError, match="unterminated trade intake audit line 1"):
        read_latest_lifecycle_attempt_run_seal(path)

    invalid_seal = {**seal, "seal_sha256": "0" * 64}
    path.write_text(json.dumps(invalid_seal) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="run seal hash mismatch"):
        read_latest_lifecycle_attempt_run_seal(path)


def test_checkpoint_helper_reads_account_heads_and_appends_durably(
    tmp_path: Path,
) -> None:
    class Repo:
        def list_trade_lifecycle_attempt_audit_heads_for_account(
            self,
            *,
            account: str,
        ) -> list[dict]:
            assert account == "lx"
            return []

    path = tmp_path / "audit.jsonl"
    seal = append_lifecycle_attempt_checkpoint_seal(
        path,
        Repo(),
        account="lx",
        source_id="source-a",
        completed_at_ms=1,
        reason="cli_apply",
    )

    result = read_latest_lifecycle_attempt_run_seal(path)
    assert result["last_seal"] == seal
    assert seal["reason"] == "cli_apply"


def test_concurrent_ordinary_and_durable_audit_appends_are_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(
            target=_append_audit_rows,
            args=(str(path),),
            kwargs={"durable": False, "count": 50},
        ),
        context.Process(
            target=_append_audit_rows,
            args=(str(path),),
            kwargs={"durable": True, "count": 10},
        ),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert len(raw.splitlines()) == 60
    assert all(isinstance(json.loads(line), dict) for line in raw.splitlines())
    result = read_latest_lifecycle_attempt_run_seal(path)
    assert result["seal_count"] == 10
    assert result["torn_tail_ignored"] is False


def test_retryable_unresolved_state_is_distinguishable_from_terminal_state() -> None:
    state = upsert_deal_state(
        {},
        bucket="unresolved_deal_ids",
        deal_id="deal-retry-1",
        payload={"status": "unresolved", "retryable": True, "attempt_count": 1},
    )
    terminal = upsert_deal_state(
        state,
        bucket="processed_deal_ids",
        deal_id="deal-done-1",
        payload={"status": "applied", "action": "open", "account": "lx"},
    )

    assert is_retryable_unresolved_deal(terminal, "deal-retry-1") is True
    assert lookup_deal_state_entry(terminal, "deal-retry-1")[0] == "unresolved_deal_ids"
    assert is_retryable_unresolved_deal(terminal, "deal-done-1") is False


def test_failed_deal_state_is_distinguishable_from_processed_state() -> None:
    state = upsert_deal_state(
        {},
        bucket="failed_deal_ids",
        deal_id="deal-failed-1",
        payload={"status": "failed", "action": "close", "account": "lx"},
    )
    state = upsert_deal_state(
        state,
        bucket="processed_deal_ids",
        deal_id="deal-done-1",
        payload={"status": "applied", "action": "open", "account": "lx"},
    )

    assert is_failed_deal(state, "deal-failed-1") is True
    assert is_failed_deal(state, "deal-done-1") is False


def test_upsert_deal_state_moves_deal_between_buckets() -> None:
    state = upsert_deal_state(
        {},
        bucket="unresolved_deal_ids",
        deal_id="deal-retry-1",
        payload={"status": "unresolved", "retryable": True, "attempt_count": 1},
    )
    state = upsert_deal_state(
        state,
        bucket="processed_deal_ids",
        deal_id="deal-retry-1",
        payload={"status": "applied", "action": "open", "account": "lx"},
    )

    assert lookup_deal_state_entry(state, "deal-retry-1")[0] == "processed_deal_ids"
    assert "deal-retry-1" not in state["unresolved_deal_ids"]
    assert is_retryable_unresolved_deal(state, "deal-retry-1") is False
