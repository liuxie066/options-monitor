from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.config_yaml_init import init_yaml_config
from src.interfaces.cli import symbols


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_symbols_cli_previews_then_publishes_yaml_and_runtime(tmp_path: Path, capsys) -> None:
    source = tmp_path / "config.yaml"
    runtime = tmp_path / "config.us.json"
    init_yaml_config(
        account_label="lx", repo_root=REPO_ROOT,
        output_config_yaml_path=source,
        runtime_output_dir=tmp_path,
        markets=["us"],
        us_symbols=["NVDA"],
        dry_run=False,
    )
    before = source.read_bytes(), runtime.read_bytes()
    with pytest.raises(SystemExit, match="--strategy"):
        symbols.main(["add", "TSLA", "--config-yaml", str(source), "--apply"])
    with pytest.raises(SystemExit, match="--csp-max-strike"):
        symbols.main(["add", "TSLA", "--strategy", "csp", "--config-yaml", str(source), "--apply"])
    with pytest.raises(SystemExit, match="--cc-min-strike"):
        symbols.main(["add", "TSLA", "--strategy", "cc", "--config-yaml", str(source), "--apply"])
    with pytest.raises(SystemExit, match="CSP strike requires"):
        symbols.main([
            "add", "TSLA", "--strategy", "cc", "--cc-min-strike", "100",
            "--csp-max-strike", "90", "--config-yaml", str(source), "--apply",
        ])
    with pytest.raises(SystemExit, match="min strike exceeds max strike"):
        symbols.main([
            "add", "TSLA", "--strategy", "csp", "--csp-min-strike", "120",
            "--csp-max-strike", "100", "--config-yaml", str(source), "--apply",
        ])
    assert (source.read_bytes(), runtime.read_bytes()) == before
    command = [
        "add", "TSLA", "--strategy", "csp", "--csp-min-strike", "50", "--csp-max-strike", "100",
        "--config-yaml", str(source), "--format", "json",
    ]

    assert symbols.main(command) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert (source.read_bytes(), runtime.read_bytes()) == before

    assert symbols.main([*command, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["write_applied"] is True
    assert yaml.safe_load(source.read_text(encoding="utf-8"))["markets"]["us"]["symbols"] == ["NVDA", "TSLA"]
    assert [row["symbol"] for row in json.loads(runtime.read_text(encoding="utf-8"))["symbols"]] == ["NVDA", "TSLA"]
    tsla = json.loads(runtime.read_text(encoding="utf-8"))["symbols"][1]
    assert tsla["sell_put"]["enabled"] is True
    assert tsla["sell_put"]["min_strike"] == 50
    assert tsla["sell_put"]["max_strike"] == 100
    assert tsla["sell_call"]["enabled"] is False

    assert symbols.main(["list", "--config-yaml", str(source), "--format", "json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["symbols"] == ["NVDA", "TSLA"]
    assert [entry["symbol"] for entry in listed["effective"]] == ["NVDA", "TSLA"]


def test_symbols_cli_uses_active_runtime_root_and_refuses_last_removal(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "config.yaml"
    runtime = tmp_path / "config.us.json"
    init_yaml_config(
        account_label="lx", repo_root=REPO_ROOT, output_config_yaml_path=source,
        runtime_output_dir=tmp_path, markets=["us"], us_symbols=["NVDA"], dry_run=False,
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    assert symbols.main(["list", "--market", "us", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["config_yaml_path"] == str(source)

    before = source.read_bytes(), runtime.read_bytes()
    with pytest.raises(SystemExit, match="non-empty"):
        symbols.main(["rm", "NVDA", "--market", "us", "--apply"])
    assert (source.read_bytes(), runtime.read_bytes()) == before

    assert symbols.main([
        "add", "TSLA", "--strategy", "both", "--csp-max-strike", "100", "--cc-min-strike", "200",
        "--market", "us", "--apply",
    ]) == 0
    output = capsys.readouterr().out
    assert "配置备份：" in output
    assert "CSP: on · max_strike=100.0" in output
    assert "CC: on · min_strike=200.0" in output
    assert symbols.main(["edit", "TSLA", "--set", "sell_put.enabled=false", "--apply"]) == 0
    capsys.readouterr()
    assert symbols.main(["edit", "TSLA", "--set", 'accounts=["lx"]', "--apply"]) == 0
    capsys.readouterr()
    assert symbols.main(["rm", "NVDA", "--apply"]) == 0
    assert yaml.safe_load(source.read_text(encoding="utf-8"))["markets"]["us"]["symbols"] == ["TSLA"]
    assert yaml.safe_load(source.read_text(encoding="utf-8"))["markets"]["us"]["overrides"]["TSLA"]["accounts"] == ["lx"]
    assert [row["symbol"] for row in json.loads(runtime.read_text(encoding="utf-8"))["symbols"]] == ["TSLA"]


def test_symbols_cli_infers_market_from_symbol_and_rejects_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    init_yaml_config(
        account_label="lx", repo_root=REPO_ROOT, output_config_yaml_path=source, runtime_output_dir=tmp_path,
        markets=["us", "hk"], us_symbols=["NVDA"], hk_symbols=["0700.HK"], dry_run=False,
    )

    assert symbols.main([
        "add", "TSLA", "--strategy", "csp", "--csp-max-strike", "100",
        "--config-yaml", str(source), "--apply",
    ]) == 0
    assert symbols.main([
        "add", "9992.HK", "--strategy", "cc", "--cc-min-strike", "200", "--cc-max-strike", "300",
        "--config-yaml", str(source), "--apply",
    ]) == 0
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert document["markets"]["us"]["symbols"] == ["NVDA", "TSLA"]
    assert document["markets"]["hk"]["symbols"] == ["0700.HK", "9992.HK"]
    assert document["markets"]["hk"]["overrides"]["9992.HK"] == {
        "sell_put": {"enabled": False},
        "covered_call": {"enabled": True, "min_strike": 200.0, "max_strike": 300.0},
    }

    before = source.read_bytes()
    with pytest.raises(SystemExit, match="belongs to us, not hk"):
        symbols.main([
            "add", "AAPL", "--strategy", "csp", "--csp-max-strike", "100",
            "--market", "hk", "--config-yaml", str(source), "--apply",
        ])
    with pytest.raises(SystemExit, match="requires --market"):
        symbols.main(["list", "--config-yaml", str(source)])
    assert source.read_bytes() == before


def test_symbols_cli_rejects_generated_json_as_authoring_source(tmp_path: Path) -> None:
    source = tmp_path / "config.us.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="config.yaml"):
        symbols.main(["list", "--market", "us", "--config-yaml", str(source)])


def test_explicit_yaml_path_ignores_broken_default_record(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "valid" / "config.yaml"
    source.parent.mkdir()
    init_yaml_config(
        account_label="lx", repo_root=REPO_ROOT, output_config_yaml_path=source,
        runtime_output_dir=source.parent, markets=["us"], us_symbols=["NVDA"], dry_run=False,
    )
    record = tmp_path / "home" / ".config" / "options-monitor" / "runtime-root"
    record.parent.mkdir(parents=True)
    record.write_text("invalid\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)

    assert symbols.main(["list", "--market", "us", "--config-yaml", str(source), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["symbols"] == ["NVDA"]
    assert symbols.main([
        "add", "TSLA", "--strategy", "csp", "--csp-max-strike", "100",
        "--market", "us", "--config-yaml", str(source), "--apply",
    ]) == 0
