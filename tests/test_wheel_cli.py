from pathlib import Path

import pytest

import src.interfaces.cli.wheel as wheel_cli


def _end_args(*extra: str):
    return wheel_cli.parse_args(
        [
            "end",
            "--account",
            "lx",
            "--stock-lot-id",
            "assigned-stock-1",
            "--expected-batch-generation-hash",
            "generation-1",
            "--request-id",
            "request-1",
            "--actor",
            "tester",
            "--config-key",
            "us",
            *extra,
        ]
    )


def test_wheel_cli_end_previews_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_position_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        wheel_cli,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )

    def _end(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli, "end_wheel_lifecycle", _end)

    assert wheel_cli.execute(_end_args()) == {"dry_run": True, "write_applied": False}
    assert calls[0]["apply_changes"] is False
    assert calls[0]["stock_lot_id"] == "assigned-stock-1"


def test_wheel_cli_requires_apply_with_confirmation() -> None:
    with pytest.raises(SystemExit, match="require --apply"):
        wheel_cli.execute(_end_args("--confirm"))
