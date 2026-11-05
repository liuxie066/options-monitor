from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _runs(root: Path) -> Path:
    name = (datetime.now(timezone.utc) - timedelta(days=9)).strftime("%Y%m%dT%H%M%SZ")
    old = root / "output_runs" / name
    old.mkdir(parents=True)
    (old / "payload.json").write_text("old evidence", encoding="utf-8")
    latest = root / "output_runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    latest.mkdir()
    return old


def _call(root: Path, capsys, *args: str) -> tuple[int, dict]:
    from src.interfaces.cli.main import main

    code = main(["maintenance", "prune-runs", "--runtime-root", str(root), *args])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("args", [(), ("--dry-run",)])
def test_prune_cli_defaults_to_preview(tmp_path: Path, capsys, args: tuple[str, ...]) -> None:
    old = _runs(tmp_path)
    code, payload = _call(tmp_path, capsys, *args)
    assert code == 0
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["keep_days"] == 7
    assert old.exists()
    assert not (tmp_path / "output_shared").exists()


def test_prune_cli_confirm_audit_is_visible_in_logs(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    old = _runs(tmp_path)
    code, payload = _call(tmp_path, capsys, "--confirm")
    assert code == 0
    assert payload["data"]["dry_run"] is False
    assert not old.exists()
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "output_runs_retention" for event in events)
    rc = main(["logs", "--runs-root", str(tmp_path / "output_runs"), "--kind", "maintenance", "--json"])
    logs = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert str(audit) == logs["data"]["files"][0]["path"]
    assert any(old.name in line for line in logs["data"]["files"][0]["tail"])


def test_prune_cli_uses_config_and_keep_days_override(tmp_path: Path, capsys) -> None:
    old = _runs(tmp_path)
    (tmp_path / "config.us.json").write_text('{"runtime":{"output_runs_keep_days":14}}', encoding="utf-8")
    code, payload = _call(tmp_path, capsys, "--confirm")
    assert code == 0 and payload["data"]["keep_days"] == 14
    assert old.exists()
    code, payload = _call(tmp_path, capsys, "--keep-days", "7", "--confirm")
    assert code == 0 and payload["data"]["keep_days"] == 7
    assert not old.exists()


def test_prune_cli_shared_root_uses_longer_market_retention(tmp_path: Path, capsys) -> None:
    old = _runs(tmp_path)
    (tmp_path / "config.us.json").write_text('{"runtime":{"output_runs_keep_days":7}}', encoding="utf-8")
    (tmp_path / "config.hk.json").write_text('{"runtime":{"output_runs_keep_days":14}}', encoding="utf-8")
    code, payload = _call(tmp_path, capsys, "--confirm")
    assert code == 0 and payload["data"]["keep_days"] == 14
    assert old.exists()


def test_prune_cli_rejects_bad_retention_without_deleting(tmp_path: Path, capsys) -> None:
    old = _runs(tmp_path)
    code, payload = _call(tmp_path, capsys, "--keep-days", "0", "--confirm")
    assert code == 2
    assert payload["ok"] is False
    assert old.exists()


def test_prune_cli_dry_run_cannot_be_combined_with_confirm() -> None:
    from src.interfaces.cli.main import parse_args

    with pytest.raises(SystemExit):
        parse_args(["maintenance", "prune-runs", "--confirm", "--dry-run"])
