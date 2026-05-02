from __future__ import annotations

from pathlib import Path


def test_send_if_needed_returns_multi_tick_exit_code(
    argv_scope,
    example_config_path: Path,
    monkeypatch,
    send_if_needed_module,
) -> None:
    monkeypatch.setattr(send_if_needed_module, "run_tick", lambda _argv: 7)

    argv_scope(["send_if_needed.py", "--config", str(example_config_path)])

    assert send_if_needed_module.main() == 7


def test_send_if_needed_explicit_accounts_override_legacy_portfolio_account(
    argv_scope,
    tmp_path: Path,
    monkeypatch,
    send_if_needed_module,
) -> None:
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text('{"portfolio":{"account":"legacy"},"symbols":[]}', encoding="utf-8")
    seen: list[list[str]] = []
    monkeypatch.setattr(send_if_needed_module, "run_tick", lambda argv: seen.append(list(argv)) or 0)

    argv_scope(["send_if_needed.py", "--config", str(cfg_path), "--accounts", "LX"])

    assert send_if_needed_module.main() == 0
    assert seen == [["--config", str(cfg_path), "--market-config", "auto", "--accounts", "lx"]]


def test_send_if_needed_no_longer_contains_single_account_business_chain() -> None:
    src = Path("scripts/send_if_needed.py").read_text(encoding="utf-8")

    assert "src.application.multi_account_tick" in src
    assert "run_pipeline_script" not in src
    assert "run_scan_scheduler_cli" not in src
    assert "execute_single_account_delivery" not in src
    assert "prepare_single_account_delivery" not in src
