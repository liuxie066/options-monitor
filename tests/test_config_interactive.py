from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml_init import init_yaml_config
from src.interfaces.cli.config_interactive import _load_candidate, run_interactive_config_edit


REPO_ROOT = Path(__file__).resolve().parents[1]


def _starter(root: Path, *, markets: list[str] | None = None) -> Path:
    init_yaml_config(
        repo_root=REPO_ROOT,
        output_config_yaml_path=root / "config.yaml",
        runtime_output_dir=root,
        markets=markets or ["us"],
        futu_acc_id="12345678",
    )
    return root / "config.yaml"


def _answers(*values: str):
    source = iter(values)
    return lambda: next(source)


def test_config_edit_advanced_entry_validates_and_publishes(monkeypatch, tmp_path: Path, capsys) -> None:
    root = tmp_path / "runtime"
    source = _starter(root)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    def edit(path: Path) -> int:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["markets"]["us"].setdefault("overrides", {})["NVDA"] = {"sell_put": {"enabled": True, "dte": [20, 45]}}
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return 0

    result = run_interactive_config_edit(
        repo_root=REPO_ROOT,
        runtime_root=str(root),
        env_file=str(root / "options-monitor.env"),
        prompt_fn=_answers("2", "yes"),
        input_is_tty=lambda: True,
        editor_fn=edit,
    )

    assert result["write_applied"] is True
    assert "markets.us.overrides.NVDA" in result["changed_paths"]
    assert yaml.safe_load(source.read_text(encoding="utf-8"))["markets"]["us"]["overrides"]["NVDA"]["sell_put"]["enabled"] is True
    assert "CSP" in capsys.readouterr().err


def test_config_edit_cancel_keeps_source_and_snapshots(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    source = _starter(root)
    before = source.read_bytes()
    snapshot_before = (root / "config.us.json").read_bytes()

    def edit(path: Path) -> int:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["markets"]["us"]["symbols"].append("MSFT")
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return 0

    with pytest.raises(AgentToolError) as exc:
        run_interactive_config_edit(
            repo_root=REPO_ROOT,
            runtime_root=str(root),
            prompt_fn=_answers("1", "no"),
            input_is_tty=lambda: True,
            editor_fn=edit,
        )
    assert exc.value.code == "CANCELLED"
    assert source.read_bytes() == before
    assert (root / "config.us.json").read_bytes() == snapshot_before


def test_config_edit_rejects_duplicate_keys_and_secret_fields(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    source = _starter(root)
    before = source.read_bytes()
    for payload in ("accounts: {}\naccounts: {}\n", "api_secret: plain-text\n"):
        with pytest.raises(AgentToolError):
            run_interactive_config_edit(
                repo_root=REPO_ROOT,
                runtime_root=str(root),
                prompt_fn=_answers("7"),
                input_is_tty=lambda: True,
                editor_fn=lambda path: path.write_text(payload, encoding="utf-8") and 0,
            )
        assert source.read_bytes() == before


def test_config_edit_allows_model_token_limits_and_nonsecret_app_token(tmp_path: Path) -> None:
    candidate = tmp_path / "config.yaml"
    candidate.write_text(
        "assistant:\n  models:\n    example:\n      context_window_tokens: 24000\n"
        "      max_output_tokens: 2048\nexternal_holdings:\n  app_token: public-table-id\n",
        encoding="utf-8",
    )

    assert _load_candidate(candidate)["assistant"]["models"]["example"]["context_window_tokens"] == 24000


def test_config_edit_env_and_secret_menu_are_guidance_only(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    source = _starter(root)
    before = source.read_bytes()
    for choice in ("9", "10"):
        result = run_interactive_config_edit(
            repo_root=REPO_ROOT,
            runtime_root=str(root),
            prompt_fn=_answers(choice),
            input_is_tty=lambda: True,
        )
        assert result["write_applied"] is False
        assert result["commands"]
        assert source.read_bytes() == before


def test_config_edit_reports_removed_market_without_deleting_old_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _starter(root, markets=["us", "hk"])
    old_hk = (root / "config.hk.json").read_bytes()

    def edit(path: Path) -> int:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        del doc["markets"]["hk"]
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        return 0

    result = run_interactive_config_edit(
        repo_root=REPO_ROOT,
        runtime_root=str(root),
        prompt_fn=_answers("1", "yes"),
        input_is_tty=lambda: True,
        editor_fn=edit,
    )
    assert result["retirement_pending"] == [str(root / "config.hk.json")]
    assert (root / "config.hk.json").read_bytes() == old_hk
    assert list(yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))["markets"]) == ["us"]


def test_config_edit_rejects_concurrent_source_change(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    source = _starter(root)

    def edit(path: Path) -> int:
        candidate = yaml.safe_load(path.read_text(encoding="utf-8"))
        candidate["markets"]["us"]["symbols"].append("MSFT")
        path.write_text(yaml.safe_dump(candidate, sort_keys=False), encoding="utf-8")
        external = yaml.safe_load(source.read_text(encoding="utf-8"))
        external["markets"]["us"]["symbols"].append("AMD")
        source.write_text(yaml.safe_dump(external, sort_keys=False), encoding="utf-8")
        return 0

    with pytest.raises(AgentToolError) as exc:
        run_interactive_config_edit(
            repo_root=REPO_ROOT,
            runtime_root=str(root),
            prompt_fn=_answers("1"),
            input_is_tty=lambda: True,
            editor_fn=edit,
        )
    assert exc.value.code == "STALE_PREVIEW"
    assert "AMD" in source.read_text(encoding="utf-8")
    assert "MSFT" not in source.read_text(encoding="utf-8")
