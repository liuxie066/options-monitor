from __future__ import annotations

import sys
import threading
from typing import Any


def test_run_tick_forwards_cli_argv_and_returns_main_exit_code(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    seen: dict[str, Any] = {}

    def fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = list(argv or [])
        return 7

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick(["--config", "config.us.json", "--accounts", "lx", "sy"])

    assert out == 7
    assert seen["argv"] == [
        "--config",
        "config.us.json",
        "--accounts",
        "lx",
        "sy",
    ]


def test_run_tick_uses_empty_argv_when_argv_is_none(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    seen: dict[str, Any] = {}

    def fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick()

    assert out == 0
    assert seen["argv"] == []


def test_run_tick_restores_sys_argv_after_success(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    original = ["pytest", "-k", "multi-account"]
    monkeypatch.setattr(sys, "argv", list(original))

    def fake_main(argv: list[str] | None = None) -> int:
        assert argv == ["--config", "config.hk.json"]
        return 3

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick(["--config", "config.hk.json"])

    assert out == 3
    assert sys.argv == original


def test_run_tick_restores_sys_argv_after_exception(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    original = ["pytest", "tests/test_multi_account_tick.py"]
    monkeypatch.setattr(sys, "argv", list(original))

    def fake_main(argv: list[str] | None = None) -> int:
        assert argv == ["--no-send"]
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    try:
        mod.run_tick(["--no-send"])
        raise AssertionError("expected runtime error")
    except RuntimeError as exc:
        assert str(exc) == "boom"

    assert sys.argv == original


def test_current_run_id_is_reexported_from_multi_tick_main() -> None:
    from src.application import multi_account_tick as mod

    assert callable(mod.current_run_id)


def test_run_account_outcomes_runs_parallel_and_preserves_account_order() -> None:
    from src.application import multi_account_tick as mod

    started: list[str] = []
    lock = threading.Lock()
    both_started = threading.Event()

    def run_account(acct: str) -> str:
        with lock:
            started.append(acct)
            if len(started) == 2:
                both_started.set()
        assert both_started.wait(1.0), "account runs did not overlap"
        return f"done-{acct}"

    out = mod._run_account_outcomes(
        account_ids=["lx", "sy"],
        max_workers=2,
        run_account_fn=run_account,
    )

    assert out == ["done-lx", "done-sy"]
    assert sorted(started) == ["lx", "sy"]


def test_account_worker_count_is_bounded_by_runtime_config() -> None:
    from src.application import multi_account_tick as mod

    assert mod._resolve_account_run_max_workers({"runtime": {}}, 3) == 1
    assert mod._resolve_account_run_max_workers({"runtime": {"multi_account_max_workers": 2}}, 5) == 2
    assert mod._resolve_account_run_max_workers({"runtime": {"multi_account_max_workers": 0}}, 5) == 1
    assert mod._should_update_account_legacy_output(1) is True
    assert mod._should_update_account_legacy_output(2) is False


def test_default_account_must_be_active_account() -> None:
    from src.application import multi_account_tick as mod

    assert mod._resolve_default_account(None, ["lx", "sy"]) == "lx"
    assert mod._resolve_default_account("SY", ["lx", "sy"]) == "sy"

    try:
        mod._resolve_default_account("other", ["lx", "sy"])
        raise AssertionError("expected config error")
    except SystemExit as exc:
        assert "--default-account must be one of active accounts" in str(exc)


def test_mark_scanned_accounts_updates_each_ran_account(tmp_path) -> None:
    import json
    from pathlib import Path
    from src.application import multi_account_tick as mod

    base = tmp_path
    config = tmp_path / "config.us.json"
    config.write_text(json.dumps({"schedule": {"enabled": True}}), encoding="utf-8")
    state = tmp_path / "scheduler_state.json"

    mod._mark_scanned_accounts(
        base=base,
        config=config,
        state=state,
        state_dir=Path("output_runs/run-1/state"),
        schedule_key="schedule",
        accounts=["lx", "sy"],
    )

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["last_scan_utc"]
    assert set(data["last_scan_utc_by_account"]) == {"lx", "sy"}
