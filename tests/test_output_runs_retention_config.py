from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from src.application.config_defaults import DEFAULT_CONFIG, output_runs_keep_days
from src.application.config_validator import validate_config
from src.application.config_yaml import resolve_yaml_runtime_config


def test_retention_defaults_for_missing_legacy_config() -> None:
    assert output_runs_keep_days({}) == 7
    assert output_runs_keep_days({"runtime": {}}) == 7
    assert DEFAULT_CONFIG["defaults"]["runtime"]["output_runs_keep_days"] == 7


@pytest.mark.parametrize("value", [0, -1, True, None, "7", 7.5])
def test_retention_rejects_invalid_config(value) -> None:
    with pytest.raises(ValueError, match="output_runs_keep_days"):
        output_runs_keep_days({"runtime": {"output_runs_keep_days": value}})
    cfg = deepcopy(DEFAULT_CONFIG["defaults"])
    cfg["symbols"] = [{"symbol": "NVDA", "strategy": "sell_put"}]
    cfg["runtime"]["output_runs_keep_days"] = value
    with pytest.raises(SystemExit):
        validate_config(cfg)


@pytest.mark.parametrize("days", [None, 14])
def test_retention_flows_through_yaml_build(tmp_path: Path, days: int | None) -> None:
    source = tmp_path / "config.yaml"
    source.write_text(
        "accounts:\n  test:\n    type: futu\n    futu_account_id: 'TEST_ACCOUNT'\n"
        "markets:\n  us:\n    accounts: [test]\n    symbols: [NVDA]\n"
        + (f"runtime:\n  output_runs_keep_days: {days}\n" if days is not None else ""),
        encoding="utf-8",
    )
    config, _ = resolve_yaml_runtime_config(repo_root=tmp_path, market="us", config_path=source)
    assert config["runtime"]["output_runs_keep_days"] == (days or 7)
