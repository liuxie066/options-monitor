from __future__ import annotations

from pathlib import Path


def test_scheduler_cli_defaults_state_dir_to_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.interfaces.cli import main as cli

    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    def _fake_run_scheduler(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(cli, "run_scheduler", _fake_run_scheduler)

    rc = cli.main(["scheduler", "--config", str(config_path)])

    assert rc == 0
    assert captured["state_dir"] == str((runtime_root / "output" / "state").resolve())


def test_sell_put_cash_cli_defaults_out_dir_to_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.interfaces.cli import main as cli

    runtime_root = tmp_path / "runtime"
    captured: dict[str, object] = {}

    def _fake_query_sell_put_cash(**kwargs) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(cli, "query_sell_put_cash", _fake_query_sell_put_cash)

    rc = cli.main(["sell-put-cash", "--format", "json"])

    assert rc == 0
    assert captured["out_dir"] == str((runtime_root / "output" / "state").resolve())
