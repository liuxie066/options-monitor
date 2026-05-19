from __future__ import annotations

import json
from pathlib import Path


def _read_json_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_top_level_doctor_wraps_healthcheck(monkeypatch, capsys) -> None:
    import src.interfaces.cli.main as cli

    calls: list[dict] = []

    def _healthcheck(**kwargs):
        calls.append(kwargs)
        return {"tool_name": "healthcheck", "ok": True, "data": {"status": "pass"}}

    monkeypatch.setattr(cli, "run_healthcheck", _healthcheck)

    rc = cli.main(["doctor", "--config-key", "us", "--accounts", "lx", "sy"])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "doctor"
    assert payload["ok"] is True
    assert payload["data"]["healthcheck"]["tool_name"] == "healthcheck"
    assert calls == [{
        "config_key": "us",
        "config_path": None,
        "accounts": ["lx", "sy"],
        "opend_telnet_host": None,
        "opend_telnet_port": None,
    }]


def test_top_level_setup_delegates_to_runtime_init(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    config_path = tmp_path / "config.us.json"
    data_config_path = tmp_path / "portfolio.runtime.json"

    def _init_runtime(**kwargs):
        return kwargs

    monkeypatch.setattr(cli, "init_runtime", _init_runtime)

    rc = cli.main([
        "setup",
        "--market",
        "us",
        "--futu-acc-id",
        "123456",
        "--account-label",
        "lx",
        "--config-path",
        str(config_path),
        "--data-config-path",
        str(data_config_path),
        "--symbol",
        "NVDA",
    ])
    payload = _read_json_output(capsys)

    assert rc == 0
    assert payload["tool_name"] == "setup"
    assert payload["data"]["market"] == "us"
    assert payload["data"]["futu_acc_id"] == "123456"
    assert payload["data"]["account_label"] == "lx"
    assert payload["data"]["config_path"] == str(config_path)
    assert payload["data"]["symbols"] == ["NVDA"]


def test_top_level_update_commands_delegate_to_service_upgrade(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    calls: list[tuple[str, dict]] = []

    def _check(**kwargs):
        calls.append(("check", kwargs))
        return {"ok": True, "status": "current"}

    def _upgrade(**kwargs):
        calls.append(("apply", kwargs))
        return {"ok": True, "status": "dry_run"}

    def _rollback(**kwargs):
        calls.append(("rollback", kwargs))
        return {"ok": True, "status": "dry_run"}

    monkeypatch.setattr(cli, "service_upgrade_check", _check)
    monkeypatch.setattr(cli, "service_upgrade", _upgrade)
    monkeypatch.setattr(cli, "service_rollback", _rollback)

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"

    assert cli.main(["update", "check", "--repo-root", str(repo), "--runtime-root", str(runtime)]) == 0
    assert _read_json_output(capsys)["tool_name"] == "update.check"

    assert cli.main([
        "update",
        "apply",
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--target-version",
        "1.2.70",
    ]) == 0
    assert _read_json_output(capsys)["tool_name"] == "update.apply"

    assert cli.main([
        "update",
        "rollback",
        "--repo-root",
        str(repo),
        "--runtime-root",
        str(runtime),
        "--to-version",
        "1.2.69",
    ]) == 0
    assert _read_json_output(capsys)["tool_name"] == "update.rollback"

    assert calls[0] == ("check", {"repo_root": str(repo), "runtime_root": str(runtime), "remote_name": "origin"})
    assert calls[1][0] == "apply"
    assert calls[1][1]["repo_root"] == str(repo)
    assert calls[1][1]["runtime_root"] == str(runtime)
    assert calls[1][1]["target_version"] == "1.2.70"
    assert calls[1][1]["confirm"] is False
    assert calls[2][0] == "rollback"
    assert calls[2][1]["to_version"] == "1.2.69"
    assert calls[2][1]["confirm"] is False


def test_config_get_and_set_preview_then_apply(capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli

    cfg = {
        "symbols": [
            {
                "symbol": "NVDA",
                "sell_put": {
                    "enabled": True,
                    "min_dte": 7,
                    "max_dte": 45,
                    "max_strike": 100,
                },
            }
        ],
        "runtime": {"prefetch": {"max_workers": 2}},
    }
    path = tmp_path / "config.us.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert cli.main([
        "config",
        "get",
        "--config-path",
        str(path),
        "--key",
        "runtime.prefetch.max_workers",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["tool_name"] == "config.get"
    assert payload["data"]["value"] == 2

    assert cli.main([
        "config",
        "set",
        "--config-path",
        str(path),
        "--key",
        "runtime.prefetch.max_workers",
        "--json-value",
        "4",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["tool_name"] == "config.set"
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["applied"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["runtime"]["prefetch"]["max_workers"] == 2

    assert cli.main([
        "config",
        "set",
        "--config-path",
        str(path),
        "--key",
        "runtime.prefetch.max_workers",
        "--json-value",
        "4",
        "--apply",
        "--confirm",
        "--no-backup",
    ]) == 0
    payload = _read_json_output(capsys)
    assert payload["data"]["applied"] is True
    assert payload["data"]["dry_run"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["runtime"]["prefetch"]["max_workers"] == 4
