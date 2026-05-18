from __future__ import annotations

from pathlib import Path
import sys


BASE = Path(__file__).resolve().parents[1]


def test_cli_version_returns_structured_json_on_failure(monkeypatch, capsys) -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

    import src.interfaces.cli.main as cli

    monkeypatch.setattr(
        cli,
        "check_version_update",
        lambda: {
            "current_version": "0.1.0",
            "latest_version": None,
            "update_available": False,
            "remote_name": "origin",
            "checked_at": "2026-04-27T12:00:00Z",
            "release_tag": None,
            "message": "版本检查失败",
            "ok": False,
            "error": "network down",
        },
    )

    rc = cli.main(["version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"ok": false' in out
    assert '"error": "network down"' in out


def test_cli_version_help_and_output_contract() -> None:
    src = Path("src/interfaces/cli/main.py").read_text(encoding="utf-8")
    assert 'sub.add_parser("version"' in src
    assert 'if args.command == "version":' in src
    assert "check_version_update" in src
