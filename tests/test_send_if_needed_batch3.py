from __future__ import annotations

import json
from pathlib import Path


def test_send_if_needed_delegates_to_multi_tick_and_consumes_legacy_args(
    argv_scope,
    example_config_path: Path,
    monkeypatch,
    send_if_needed_module,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(send_if_needed_module, "run_tick", lambda argv: seen.append(list(argv)) or 0)

    argv_scope(
        [
            "send_if_needed.py",
            "--config",
            str(example_config_path),
            "--state-dir",
            "output/state_legacy",
            "--report-dir",
            "output/reports_legacy",
        ]
    )

    assert send_if_needed_module.main() == 0
    assert len(seen) == 1
    delegated = seen[0]
    assert delegated[:2] == ["--config", str(example_config_path)]
    assert "--market-config" in delegated
    assert "--state-dir" not in delegated
    assert "--report-dir" not in delegated


def test_send_if_needed_rejects_live_affecting_legacy_args(
    argv_scope,
    example_config_path: Path,
    monkeypatch,
    send_if_needed_module,
    capsys,
) -> None:
    monkeypatch.setattr(
        send_if_needed_module,
        "run_tick",
        lambda _argv: (_ for _ in ()).throw(AssertionError("run_tick should not be called")),
    )

    argv_scope(
        [
            "send_if_needed.py",
            "--config",
            str(example_config_path),
            "--target",
            "user:test",
            "--notification",
            "README.md",
        ]
    )

    assert send_if_needed_module.main() == 2
    err = capsys.readouterr().err
    assert "refusing ignored live-affecting options" in err
    assert "--target" in err
    assert "--notification" in err


def test_send_if_needed_derives_legacy_portfolio_account_when_top_level_accounts_missing(
    argv_scope,
    tmp_path: Path,
    monkeypatch,
    send_if_needed_module,
) -> None:
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(
            {
                "portfolio": {"account": "LX"},
                "symbols": [],
            }
        ),
        encoding="utf-8",
    )
    seen: list[list[str]] = []
    monkeypatch.setattr(send_if_needed_module, "run_tick", lambda argv: seen.append(list(argv)) or 0)

    argv_scope(["send_if_needed.py", "--config", str(cfg_path)])

    assert send_if_needed_module.main() == 0
    assert seen == [["--config", str(cfg_path), "--market-config", "auto", "--accounts", "lx"]]


def test_send_if_needed_forwards_multi_tick_flags(
    argv_scope,
    example_config_path: Path,
    monkeypatch,
    send_if_needed_module,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(send_if_needed_module, "run_tick", lambda argv: seen.append(list(argv)) or 0)

    argv_scope(
        [
            "send_if_needed.py",
            "--config",
            str(example_config_path),
            "--accounts",
            "LX",
            "SY",
            "--default-account",
            "SY",
            "--market-config",
            "us",
            "--no-send",
            "--force",
            "--debug",
        ]
    )

    assert send_if_needed_module.main() == 0
    delegated = seen[0]
    assert delegated == [
        "--config",
        str(example_config_path),
        "--market-config",
        "us",
        "--accounts",
        "lx",
        "sy",
        "--default-account",
        "sy",
        "--no-send",
        "--force",
        "--debug",
    ]
