from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import (
    config_source_sha256,
    publish_yaml_config_generation,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _config_doc() -> dict:
    return {
        "accounts": {
            "lx": {
                "type": "futu",
                "futu_account_id": "12345678",
            }
        },
        "markets": {
            "us": {"accounts": ["lx"], "symbols": ["NVDA"]},
            "hk": {"accounts": ["lx"], "symbols": ["0700.HK"]},
        },
    }


def _write_yaml(path: Path, doc: dict) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_config_authoring_rejects_stale_preview_without_writes(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = _config_doc()
    _write_yaml(config_path, original)
    expected = config_source_sha256(config_path)
    changed = _config_doc()
    changed["markets"]["us"]["symbols"].append("FUTU")
    _write_yaml(config_path, changed)

    with pytest.raises(AgentToolError) as exc_info:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=config_path,
            config_doc=original,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            apply=True,
            expected_source_sha256=expected,
        )

    assert exc_info.value.code == "STALE_PREVIEW"
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "config.hk.json").exists()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == changed


def test_config_authoring_rejects_source_change_during_generation_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    config_path = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(config_path, before_doc)
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    concurrent_doc = _config_doc()
    concurrent_doc["markets"]["us"]["symbols"].append("AMD")
    original_prepare = transaction_module._prepare_generation

    def _prepare_then_change_source(**kwargs):  # type: ignore[no-untyped-def]
        prepared = original_prepare(**kwargs)
        _write_yaml(config_path, concurrent_doc)
        return prepared

    monkeypatch.setattr(transaction_module, "_prepare_generation", _prepare_then_change_source)

    with pytest.raises(AgentToolError) as exc_info:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=config_path,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            apply=True,
        )

    assert exc_info.value.code == "STALE_PREVIEW"
    assert not (tmp_path / "config.us.json").exists()
    assert not (tmp_path / "config.hk.json").exists()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == concurrent_doc


def test_config_authoring_compensates_generation_when_source_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.config_authoring_transaction as transaction_module

    config_path = tmp_path / "config.yaml"
    before_doc = _config_doc()
    _write_yaml(config_path, before_doc)
    us_path = tmp_path / "config.us.json"
    hk_path = tmp_path / "config.hk.json"
    assistant_path = tmp_path / "resolved" / "config.assistant.json"
    assistant_path.parent.mkdir(parents=True)
    us_path.write_text('{"old":"us"}\n', encoding="utf-8")
    hk_path.write_text('{"old":"hk"}\n', encoding="utf-8")
    assistant_path.write_text('{"old":"assistant"}\n', encoding="utf-8")
    before_bytes = {
        config_path: config_path.read_bytes(),
        us_path: us_path.read_bytes(),
        hk_path: hk_path.read_bytes(),
        assistant_path: assistant_path.read_bytes(),
    }
    after_doc = _config_doc()
    after_doc["markets"]["us"]["symbols"].append("FUTU")
    original_atomic_write = transaction_module._atomic_write_bytes
    failed = False

    def _fail_source_once(path: Path, payload: bytes) -> None:
        nonlocal failed
        if path.resolve() == config_path.resolve() and not failed:
            failed = True
            raise OSError("injected source commit failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(transaction_module, "_atomic_write_bytes", _fail_source_once)

    with pytest.raises(AgentToolError) as exc_info:
        publish_yaml_config_generation(
            repo_root=REPO_ROOT,
            config_yaml_path=config_path,
            config_doc=after_doc,
            runtime_root=tmp_path,
            markets=["us", "hk"],
            apply=True,
        )

    assert exc_info.value.code == "CONFIG_WRITE_FAILED"
    assert exc_info.value.details["recovery_error"] is None
    for path, payload in before_bytes.items():
        assert path.read_bytes() == payload
    assert json.loads(us_path.read_text(encoding="utf-8")) == {"old": "us"}
