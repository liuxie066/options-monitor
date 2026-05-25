from __future__ import annotations

import pytest


def test_cli_run_tick_requires_explicit_config() -> None:
    from src.interfaces.cli import main as cli_main

    with pytest.raises(SystemExit) as exc:
        cli_main.main(["run", "tick", "--accounts", "lx"])

    assert exc.value.code == 2


def test_cli_run_trade_intake_requires_explicit_config() -> None:
    from src.interfaces.cli import main as cli_main

    with pytest.raises(SystemExit) as exc:
        cli_main.main(["run", "trade-intake", "--once"])

    assert exc.value.code == 2


def test_multi_account_tick_requires_explicit_config() -> None:
    from src.application import multi_account_tick

    with pytest.raises(SystemExit) as exc:
        multi_account_tick.main(["--accounts", "lx"])

    assert exc.value.code == 2


def test_trade_auto_intake_requires_explicit_config() -> None:
    from src.application.trades import auto_intake

    with pytest.raises(SystemExit) as exc:
        auto_intake.parse_args(["--once"])

    assert exc.value.code == 2


def test_healthcheck_runner_cli_requires_explicit_config() -> None:
    from src.application import healthcheck_runner

    with pytest.raises(SystemExit) as exc:
        healthcheck_runner.main(["--json"])

    assert exc.value.code == 2


def test_symbols_cli_requires_explicit_config() -> None:
    from src.interfaces.cli import symbols

    with pytest.raises(SystemExit) as exc:
        symbols.main(["list"])

    assert exc.value.code == 2
