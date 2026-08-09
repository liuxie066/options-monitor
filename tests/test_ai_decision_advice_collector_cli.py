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


def test_evidence_runner_reduces_provider_response_to_safe_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "output": [
            {
                "type": "web_search_call",
                "id": "must-not-persist",
                "query": "must-not-persist",
                "status": "completed",
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"results":[]}'}],
            },
        ],
        "provider_private": "must-not-persist",
    }
    monkeypatch.setattr(
        ai_evidence_collector,
        "create_deepseek_response",
        lambda **kwargs: response,
    )

    result = ai_evidence_collector._evidence_model_runner("not-a-real-key")(
        "instructions", {}, None, 1
    )

    assert result.output_text == '{"results":[]}'
    assert result.web_search_audit == {
        "count": 1,
        "status_counts": {"completed": 1},
    }
    assert not hasattr(result, "raw_response")
    assert "must-not-persist" not in repr(result)


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
    assert result["observation_count"] == 1
    assert result["cutoff_count"] == 1
    assert "NVDA" not in json.dumps(result)


def test_main_returns_nonzero_on_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _boom(config_key: str, expected_market: str | None = None):
        raise FileNotFoundError(f"missing config for {config_key}")

    monkeypatch.setattr(ai_evidence_collector, "load_runtime_config", _boom)
    exit_code = ai_evidence_collector.main(["--config-key", "us", "--dry-run"])
    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert out["reason"] == "collector_error"
    assert out["error_type"] == "FileNotFoundError"
    assert "missing config" not in json.dumps(out)
