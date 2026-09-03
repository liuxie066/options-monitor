from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_scan_pipeline_uses_runtime_root_and_loaded_config_for_refresh(monkeypatch, tmp_path: Path) -> None:
    from src.application import multiplier_cache
    from src.application import pipeline_runtime
    from src.application import pipeline_watchlist

    runtime_root = tmp_path / "runtime"
    config_path = tmp_path / "config.us.json"
    config_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    events: list[str] = []
    cfg = {
        "symbols": [
            {
                "symbol": "MSFT",
                "fetch": {"source": "opend"},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ],
        "portfolio": {"broker": "富途", "data_config": "portfolio.runtime.json"},
        "notifications": {"enabled": False},
    }

    def _fake_load_config(**kwargs):
        events.append("load")
        captured["load_config_base"] = kwargs["base"]
        captured["load_config_path"] = kwargs["config_path"]
        return cfg

    def _opend_kwargs(loaded: dict) -> dict:
        assert loaded is cfg
        events.append("opend_kwargs")
        return {}

    def _fake_run_watchlist_pipeline_default(**kwargs):
        captured["pipeline_base"] = kwargs["base"]
        captured["report_dir"] = kwargs["report_dir"]
        captured["state_dir"] = kwargs["state_dir"]
        captured["required_data_dir"] = kwargs["required_data_dir"]
        return []

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(pipeline_runtime, "load_runtime_pipeline_config", _fake_load_config)
    monkeypatch.setattr(pipeline_runtime, "opend_fetch_kwargs", _opend_kwargs)
    monkeypatch.setattr(multiplier_cache, "load_cache", lambda _path: {})
    monkeypatch.setattr(multiplier_cache, "save_cache", lambda *_args: None)
    monkeypatch.setattr(
        multiplier_cache,
        "refresh_via_opend",
        lambda **_kwargs: events.append("refresh") or SimpleNamespace(ok=False, multiplier=None),
    )
    monkeypatch.setattr(pipeline_watchlist, "run_watchlist_pipeline_default", _fake_run_watchlist_pipeline_default)

    rc = pipeline_runtime.main([
        "--config",
        str(config_path),
        "--stage",
        "fetch",
        "--no-context",
        "--refresh-multiplier-cache",
    ])

    repo_root = Path(__file__).resolve().parents[1]
    assert rc == 0
    assert events == ["load", "opend_kwargs", "refresh"]
    assert captured["load_config_base"] == repo_root
    assert captured["load_config_path"] == config_path.resolve()
    assert captured["pipeline_base"] == runtime_root.resolve()
    assert captured["report_dir"] == (runtime_root / "output_shared" / "reports").resolve()
    assert captured["state_dir"] == (runtime_root / "output_shared" / "state").resolve()
    assert captured["required_data_dir"] == (runtime_root / "output_shared" / "required_data").resolve()


def test_stage_only_notification_always_builds_compact_compatibility_bundle(monkeypatch, tmp_path: Path) -> None:
    from src.application import pipeline_alert_steps as mod

    (tmp_path / "symbols_alerts.txt").write_text("alerts", encoding="utf-8")
    calls: list[dict] = []
    logs: list[str] = []
    monkeypatch.setattr(mod, "run_pipeline_notification_stage", lambda **kwargs: calls.append(dict(kwargs)))

    mod.run_stage_only_alert_notify(
        report_dir=tmp_path,
        stage_only="notify",
        want=lambda step: step == "notify",
        log=logs.append,
    )

    assert calls[0]["render_style"] == "compact"
    assert calls[0]["output"] == (tmp_path / "symbols_notification.txt").resolve()
    assert any("not delivery evidence" in message for message in logs)


def test_pipeline_runtime_has_no_config_driven_legacy_compatibility_bundle() -> None:
    from src.application import pipeline_runtime as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert 'notifications_cfg.get("render_style")' not in source
    assert 'render_style="compact"' in source
    assert "not delivery evidence" in source


def _authority_args(authority, *, base: Path) -> list[str]:
    return [
        "--config",
        str(authority.state_path),
        "--account-config-base",
        str(base),
        "--account-config-run-id",
        authority.run_id,
        "--account-config-account",
        authority.account,
        "--account-config-compatibility-path",
        str(authority.compatibility_path),
        "--account-config-sha256",
        authority.account_config_sha256,
    ]


def _set_authority_env(monkeypatch, authority) -> None:
    monkeypatch.setenv(
        "OM_ACCOUNT_CONFIG_CANONICAL_B64",
        base64.b64encode(authority.canonical_bytes).decode("ascii"),
    )


def test_pipeline_runtime_passes_validated_account_config_payload_to_loader(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_runtime as mod
    from src.application.tick_run_workspace import publish_account_run_config

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-pipeline",
        account="lx",
        config={
            "portfolio": {"account": "lx"},
            "runtime": {"marker": "validated-authority"},
            "symbols": [],
        },
    )
    report_dir = tmp_path / "reports"
    state_dir = tmp_path / "state"
    observed: dict[str, object] = {}
    pipeline_observed: dict[str, object] = {}
    prepared_options = tmp_path / "prepared-options.json"
    prepared_options.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        mod.report_repo,
        "prepare_dirs",
        lambda **_kwargs: (report_dir, state_dir),
    )
    _set_authority_env(monkeypatch, authority)

    def _load(**kwargs):
        observed.update(kwargs)
        return {"symbols": []}

    monkeypatch.setattr(mod, "load_runtime_pipeline_config", _load)
    from src.application import pipeline_watchlist

    monkeypatch.setattr(
        pipeline_watchlist,
        "run_watchlist_pipeline_default",
        lambda **kwargs: pipeline_observed.update(kwargs) or [],
    )

    assert mod.main(
        _authority_args(authority, base=tmp_path)
        + [
            "--stage",
            "fetch",
            "--source-account-run-id",
            "run-pipeline",
            "--prepared-option-positions-context-manifest",
            str(prepared_options),
            "--prepared-option-positions-context-manifest-sha256",
            "b" * 64,
        ]
    ) == 0
    assert observed["config_payload"]["runtime"]["marker"] == (
        "validated-authority"
    )
    assert observed["config_path"] == authority.state_path
    assert pipeline_observed[
        "prepared_option_positions_context_manifest"
    ] == prepared_options.resolve()
    assert pipeline_observed[
        "prepared_option_positions_context_manifest_sha256"
    ] == "b" * 64


def test_pipeline_runtime_classifies_prepared_option_context_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_runtime as mod
    from src.application.prepared_option_positions_context import (
        PreparedOptionPositionsContextError,
    )
    from src.application.tick_run_workspace import publish_account_run_config

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-prepared-option-failure",
        account="lx",
        config={
            "portfolio": {"account": "lx"},
            "symbols": [],
        },
    )
    prepared_options = tmp_path / "prepared-options.json"
    prepared_options.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        mod.report_repo,
        "prepare_dirs",
        lambda **_kwargs: (tmp_path / "reports", tmp_path / "state"),
    )
    monkeypatch.setattr(
        mod,
        "load_runtime_pipeline_config",
        lambda **_kwargs: {"symbols": []},
    )
    from src.application import pipeline_watchlist

    monkeypatch.setattr(
        pipeline_watchlist,
        "run_watchlist_pipeline_default",
        lambda **_kwargs: (_ for _ in ()).throw(
            PreparedOptionPositionsContextError("payload hash mismatch")
        ),
    )
    _set_authority_env(monkeypatch, authority)

    with pytest.raises(
        SystemExit,
        match=(
            r"\[CONFIG_ERROR\] "
            r"ACCOUNT_CONFIG_PREPARED_OPTION_CONTEXT_INVALID: "
            r"payload hash mismatch"
        ),
    ):
        mod.main(
            _authority_args(authority, base=tmp_path)
            + [
                "--stage",
                "fetch",
                "--source-account-run-id",
                authority.run_id,
                "--prepared-option-positions-context-manifest",
                str(prepared_options),
                "--prepared-option-positions-context-manifest-sha256",
                "b" * 64,
            ]
        )


def test_pipeline_runtime_uses_retained_generation_after_path_drift(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import pipeline_runtime as mod
    from src.application.tick_run_workspace import publish_account_run_config

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-pipeline-tamper",
        account="lx",
        config={"portfolio": {"account": "lx"}, "symbols": []},
    )
    authority.state_path.write_text("{}\n", encoding="utf-8")
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        mod.report_repo,
        "prepare_dirs",
        lambda **_kwargs: (tmp_path / "reports", tmp_path / "state"),
    )
    monkeypatch.setattr(
        mod,
        "load_runtime_pipeline_config",
        lambda **kwargs: observed.setdefault("config", kwargs["config_payload"])
        or {"symbols": []},
    )
    from src.application import pipeline_watchlist

    monkeypatch.setattr(
        pipeline_watchlist,
        "run_watchlist_pipeline_default",
        lambda **_kwargs: [],
    )
    _set_authority_env(monkeypatch, authority)

    assert mod.main(_authority_args(authority, base=tmp_path) + ["--stage", "fetch"]) == 0
    assert observed["config"]["portfolio"]["account"] == "lx"


def test_pipeline_subprocess_command_forwards_complete_config_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.infrastructure import external_services as mod

    observed: dict[str, object] = {}

    def _run_command(command, **kwargs):
        observed["command"] = list(command)
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(mod, "run_command", _run_command)
    compatibility = tmp_path / "run" / "config.override.json"
    compatibility.parent.mkdir(parents=True)
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}\n", encoding="utf-8")
    compatibility.symlink_to(replacement)
    prepared_options = tmp_path / "prepared-options.json"
    prepared_options.write_text("{}\n", encoding="utf-8")
    mod.run_pipeline_script(
        vpy=tmp_path / "python",
        base=tmp_path,
        config=tmp_path / "run" / "state" / "config.override.json",
        report_dir=tmp_path / "reports",
        state_dir=tmp_path / "state",
        account_config_base=tmp_path,
        account_config_run_id="run-1",
        account_config_account="lx",
        account_config_compatibility_path=compatibility,
        account_config_sha256="a" * 64,
        account_config_canonical_bytes=b"{}\n",
        prepared_option_positions_context_manifest=prepared_options,
        prepared_option_positions_context_manifest_sha256="b" * 64,
    )

    command = observed["command"]
    assert command[command.index("--account-config-base") + 1] == str(
        tmp_path.resolve()
    )
    assert command[command.index("--account-config-run-id") + 1] == "run-1"
    assert command[command.index("--account-config-account") + 1] == "lx"
    assert command[
        command.index("--account-config-compatibility-path") + 1
    ] == str(compatibility)
    assert command[command.index("--account-config-sha256") + 1] == "a" * 64
    assert command[
        command.index(
            "--prepared-option-positions-context-manifest"
        )
        + 1
    ] == str(prepared_options.resolve())
    assert command[
        command.index(
            "--prepared-option-positions-context-manifest-sha256"
        )
        + 1
    ] == "b" * 64
    assert "OM_ACCOUNT_CONFIG_CANONICAL_B64" in observed["env"]


def test_pipeline_subprocess_command_forwards_experience_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.infrastructure import external_services as mod

    observed: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda command, **kwargs: observed.update(
            {"command": list(command), **kwargs}
        )
        or object(),
    )

    mod.run_pipeline_script(
        vpy=tmp_path / "python",
        base=tmp_path,
        config=tmp_path / "config.override.json",
        report_dir=tmp_path / "reports",
        state_dir=tmp_path / "state",
        experience=True,
        account_display_name="美股模拟期权账户",
    )

    command = observed["command"]
    assert command[command.index("--mode") + 1] == "scheduled"
    assert command[command.index("--account-display-name") + 1] == (
        "美股模拟期权账户"
    )
    assert "--experience" in command


@pytest.mark.parametrize(
    "extra",
    [
        ["--mode", "dev", "--stage", "fetch"],
        ["--mode", "dev", "--stage-only", "notify"],
    ],
)
def test_experience_pipeline_rejects_incomplete_stage(
    tmp_path: Path,
    extra: list[str],
) -> None:
    from src.application import pipeline_runtime

    with pytest.raises(SystemExit, match="manual full scan"):
        pipeline_runtime.main(
            [
                "--config",
                str(tmp_path / "unused.json"),
                "--report-dir",
                str(tmp_path / "report"),
                "--state-dir",
                str(tmp_path / "state"),
                "--experience",
                "--account-display-name",
                "美股模拟期权账户",
                *extra,
            ]
        )
