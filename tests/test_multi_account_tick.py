from __future__ import annotations

import sys
from typing import Any


def test_run_tick_prefixes_cli_argv_and_returns_main_exit_code(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    seen: dict[str, Any] = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 7

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick(["--config", "config.us.json", "--accounts", "lx", "sy"])

    assert out == 7
    assert seen["argv"] == [
        "om",
        "run",
        "tick",
        "--config",
        "config.us.json",
        "--accounts",
        "lx",
        "sy",
    ]


def test_run_tick_uses_default_cli_prefix_when_argv_is_none(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    seen: dict[str, Any] = {}

    def fake_main() -> int:
        seen["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick()

    assert out == 0
    assert seen["argv"] == ["om", "run", "tick"]


def test_run_tick_restores_sys_argv_after_success(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    original = ["pytest", "-k", "multi-account"]
    monkeypatch.setattr(sys, "argv", list(original))

    def fake_main() -> int:
        assert sys.argv == ["om", "run", "tick", "--config", "config.hk.json"]
        return 3

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick(["--config", "config.hk.json"])

    assert out == 3
    assert sys.argv == original


def test_run_tick_restores_sys_argv_after_exception(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    original = ["pytest", "tests/test_multi_account_tick.py"]
    monkeypatch.setattr(sys, "argv", list(original))

    def fake_main() -> int:
        assert sys.argv == ["om", "run", "tick", "--no-send"]
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
    from scripts.multi_tick.main import current_run_id as source_current_run_id

    assert mod.current_run_id is source_current_run_id
