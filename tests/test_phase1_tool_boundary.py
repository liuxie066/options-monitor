from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_scheduler_decision_schema_boundary() -> None:
    from domain.domain import normalize_scheduler_decision_payload

    out = normalize_scheduler_decision_payload({
        "should_run_scan": 1,
        "should_notify": False,
        "reason": "ok",
    })
    assert out["schema_kind"] == "scheduler_decision"
    assert out["schema_version"] == "1.0"
    assert out["should_run_scan"] is True
    assert out["is_notify_window_open"] is False
    assert out["reason"] == "ok"


def test_tool_execution_schema_and_idempotency_key() -> None:
    from domain.domain import build_tool_idempotency_key, normalize_tool_execution_payload

    k1 = build_tool_idempotency_key(
        tool_name="required_data_prefetch",
        symbol="AAPL",
        source="yahoo",
        limit_exp=8,
    )
    k2 = build_tool_idempotency_key(
        tool_name="required_data_prefetch",
        symbol="aapl",
        source="YAHOO",
        limit_exp=8,
    )
    assert k1 == k2

    out = normalize_tool_execution_payload(
        tool_name="required_data_prefetch",
        symbol="AAPL",
        source="yahoo",
        limit_exp=8,
        status="bad_status",
        ok=True,
        message="x",
        idempotency_key=k1,
    )
    assert out["schema_kind"] == "tool_execution"
    assert out["schema_version"] == "1.0"
    assert out["status"] == "error"
    assert out["idempotency_key"] == k1


def test_notify_window_alias_normalization_prefers_canonical_field() -> None:
    from domain.domain.tool_boundary import normalize_notify_window_aliases, resolve_notify_window_open

    only_legacy = normalize_notify_window_aliases({"should_notify": 1})
    assert only_legacy["is_notify_window_open"] is True
    assert resolve_notify_window_open(only_legacy) is True

    canonical_first = normalize_notify_window_aliases(
        {"is_notify_window_open": False, "should_notify": True}
    )
    assert canonical_first["is_notify_window_open"] is False
    assert resolve_notify_window_open(canonical_first) is False


def test_repository_audit_and_text_writers() -> None:
    from domain.storage.repositories import run_repo, state_repo

    with TemporaryDirectory() as td:
        base = Path(td)
        run_id = "r1"

        cfg_path = state_repo.write_account_state_json_text(
            base,
            "lx",
            "config.override.json",
            {"portfolio": {"account": "lx"}},
        )
        assert cfg_path.exists()
        assert '"account": "lx"' in cfg_path.read_text(encoding="utf-8")

        audit_path = state_repo.append_run_audit_jsonl(
            base,
            run_id,
            "tool_execution_audit.jsonl",
            {"schema_kind": "tool_execution", "schema_version": "1.0", "symbol": "AAPL"},
        )
        assert audit_path.exists()
        lines = [ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1

        note_path = run_repo.write_run_account_text(base, run_id, "lx", "symbols_notification.txt", "hello\n")
        assert note_path.exists()
        copied = run_repo.copy_to_run_account(base, run_id, "lx", cfg_path, "config.override.json")
        assert copied.exists()


def test_prefetch_required_data_idempotency_audit() -> None:
    from scripts.multi_tick import required_data_prefetch as mod

    calls: list[tuple[str, str, int]] = []
    old_has = mod.has_shared_required_data
    old_exec = mod.ToolExecutionService.execute
    mod.has_shared_required_data = lambda symbol, shared_dir: False

    def _fake_execute(self, intent):
        calls.append((intent.tool_name, intent.symbol, int(intent.limit_exp)))
        return {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "tool_name": intent.tool_name,
            "symbol": intent.symbol,
            "source": intent.source,
            "limit_exp": int(intent.limit_exp),
            "idempotency_key": "k",
            "status": "fetched" if len(calls) == 1 else "skipped",
            "ok": True,
            "message": "fetched" if len(calls) == 1 else "idempotent_duplicate",
            "returncode": 0,
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "finished_at_utc": "2026-01-01T00:00:01+00:00",
        }

    mod.ToolExecutionService.execute = _fake_execute
    try:
        with TemporaryDirectory() as td:
            out = mod.prefetch_required_data(
                vpy=Path("/usr/bin/python3"),
                base=Path(td),
                cfg={
                    "symbols": [
                        {"symbol": "AAPL", "fetch": {"source": "yahoo", "limit_expirations": 8}},
                        {"symbol": "AAPL", "fetch": {"source": "yahoo", "limit_expirations": 8}},
                    ]
                },
                shared_required=Path(td) / "required_data",
            )
        assert out["fetched_ok"] == 1
        assert out["skipped"] == 1
        assert len(out["audit"]) == 2
        assert len(calls) == 2
    finally:
        mod.has_shared_required_data = old_has
        mod.ToolExecutionService.execute = old_exec


def main() -> None:
    test_scheduler_decision_schema_boundary()
    test_tool_execution_schema_and_idempotency_key()
    test_notify_window_alias_normalization_prefers_canonical_field()
    test_repository_audit_and_text_writers()
    test_prefetch_required_data_idempotency_audit()
    print("OK (phase1 tool boundary)")


if __name__ == "__main__":
    main()
