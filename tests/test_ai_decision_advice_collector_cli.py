"""CLI entry tests for `om ai-evidence-collector` (docs 4.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.interfaces.cli import ai_evidence_collector


def _write_config(tmp_path: Path, *, enabled: bool) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    body = {
        "accounts": {"lx": {}},
        "symbols": ["NVDA"],
    }
    if enabled:
        body["ai_decision_advice"] = {"enabled": True}
    for key in ("us", "hk"):
        (config_dir / f"config.{key}.json").write_text(json.dumps(body), encoding="utf-8")
    return config_dir


def test_run_collector_skips_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = _write_config(tmp_path, enabled=False)

    def _load(config_key: str, expected_market: str | None = None):
        path = config_dir / f"config.{config_key}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(ai_evidence_collector, "load_runtime_config", _load)
    result = ai_evidence_collector.run_collector(
        config_keys=["us", "hk"],
        runtime_root=tmp_path / "runtime",
    )
    assert result == {"status": "skipped", "reason": "ai_decision_advice_disabled"}


def test_run_collector_fails_without_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = _write_config(tmp_path, enabled=True)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def _load(config_key: str, expected_market: str | None = None):
        path = config_dir / f"config.{config_key}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(ai_evidence_collector, "load_runtime_config", _load)
    result = ai_evidence_collector.run_collector(
        config_keys=["us"],
        runtime_root=tmp_path / "runtime",
    )
    assert result == {"status": "failed", "reason": "missing_api_key"}


def test_run_collector_dry_run_plans_without_model_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = _write_config(tmp_path, enabled=True)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def _load(config_key: str, expected_market: str | None = None):
        path = config_dir / f"config.{config_key}.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(ai_evidence_collector, "load_runtime_config", _load)
    result = ai_evidence_collector.run_collector(
        config_keys=["us"],
        runtime_root=tmp_path / "runtime",
        dry_run=True,
    )
    assert result["status"] == "dry_run"
    assert result["observed_symbols"] == ["NVDA"]
    assert "NVDA" in result["cutoffs"]


def test_main_returns_nonzero_on_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _boom(config_key: str, expected_market: str | None = None):
        raise FileNotFoundError(f"missing config for {config_key}")

    monkeypatch.setattr(ai_evidence_collector, "load_runtime_config", _boom)
    exit_code = ai_evidence_collector.main(["--config-key", "us", "--dry-run"])
    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert "FileNotFoundError" in out["reason"]
