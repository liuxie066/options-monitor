from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml import build_yaml_runtime_config_file
from src.application.runtime_config_freshness import GENERATED_KEY


REPO_ROOT = Path(__file__).resolve().parents[1]


def _inline_runtime_config(*, market: str = "us", source_format: str = "yaml") -> dict:
    return {
        GENERATED_KEY: {
            "schema_version": "1.0",
            "generator": "options-monitor",
            "source_format": source_format,
            "market": market,
            "sources": [
                {"role": "system", "loaded": True, "inline": True, "sha256": "system"},
                {"role": "common_user", "loaded": False, "optional": True, "enabled": False},
                {"role": "market_user", "loaded": True, "inline": True, "sha256": "market"},
            ],
        },
        "_resolved": {
            "source_format": source_format,
            "market": market,
            "runtime_schema": "config-json-v1",
        },
        "portfolio": {},
        "symbols": [],
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_load_runtime_config_accepts_yaml_generated_runtime(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "config.us.json", _inline_runtime_config(market="us"))

    loaded_path, cfg = load_runtime_config(config_path=path)

    assert loaded_path == path
    assert cfg[GENERATED_KEY]["source_format"] == "yaml"
    assert cfg["config_source_path"] == str(path)


def test_load_runtime_config_rejects_key_path_market_mismatch(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "config.us.json", _inline_runtime_config(market="us"))

    with pytest.raises(AgentToolError) as exc:
        load_runtime_config(config_key="hk", config_path=path)

    assert exc.value.code == "CONFIG_ERROR"
    assert "runtime config market does not match requested market" in exc.value.message
    assert exc.value.details["errors"][0]["code"] == "path_market_mismatch"


def test_load_runtime_config_rejects_generated_market_mismatch(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "config.hk.json", _inline_runtime_config(market="us"))

    with pytest.raises(AgentToolError) as exc:
        load_runtime_config(config_path=path)

    assert exc.value.code == "CONFIG_ERROR"
    assert exc.value.details["errors"][0]["code"] == "market_mismatch"


def test_load_runtime_config_rejects_missing_generated_metadata(tmp_path: Path) -> None:
    path = _write_json(tmp_path / "config.us.json", {"portfolio": {}, "symbols": []})

    with pytest.raises(AgentToolError) as exc:
        load_runtime_config(config_path=path)

    assert exc.value.code == "CONFIG_ERROR"
    assert "missing generation metadata" in exc.value.message


def test_config_validate_infers_market_from_yaml_runtime_path(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
        encoding="utf-8",
    )
    runtime_path = tmp_path / "config.us.json"
    build_yaml_runtime_config_file(
        repo_root=REPO_ROOT,
        market="us",
        config_path=config_yaml,
        output_config_path=runtime_path,
    )

    rc = main(["config", "validate", "--config-path", str(runtime_path)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["market"] == "us"
    assert payload["source_format"] == "yaml"
    assert payload["schedule_contract"]["validated"] is True
    assert payload["freshness"]["ok"] is True
