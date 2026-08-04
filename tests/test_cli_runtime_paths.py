from __future__ import annotations

import json
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
    assert captured["state_dir"] == str((runtime_root / "output_shared" / "state").resolve())


def test_scheduler_cli_rejects_run_if_due_before_runtime_resolution(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    from src.interfaces.cli import main as cli
    from src.interfaces.cli import scheduler_ops

    calls: list[str] = []

    def _unexpected_call(*args, **kwargs):
        calls.append("unexpected")
        raise AssertionError("run-if-due must fail before runtime or adapter access")

    monkeypatch.setattr(cli, "repo_base", _unexpected_call)
    monkeypatch.setattr(cli, "run_scheduler", _unexpected_call)
    monkeypatch.setattr(scheduler_ops, "resolve_runtime_root", _unexpected_call)

    config_path = tmp_path / "missing.json"
    state_path = tmp_path / "scheduler_state.json"
    rc = cli.main(
        [
            "scheduler",
            "--config",
            str(config_path),
            "--state",
            str(state_path),
            "--account",
            "sy",
            "--run-if-due",
            "--mark-scanned",
            "--force",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNSUPPORTED_OPERATION"
    assert "tick" in payload["error"]["hint"]
    assert calls == []
    assert not config_path.exists()
    assert not state_path.exists()


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
    assert captured["out_dir"] == str((runtime_root / "output_shared" / "state").resolve())
