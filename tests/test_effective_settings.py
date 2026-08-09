from __future__ import annotations

import json
import os

from src.application.settings import (
    bootstrap_process_env,
    build_effective_env,
    diagnose_effective_settings,
    explain_effective_setting,
    inspect_effective_settings,
)


def test_effective_env_file_overlays_process_env_with_source(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                'OM_FEISHU_BOT_APP_SECRET="from_file"',
                "OM_FEISHU_BOT_USER_OPEN_ID=ou_file # local comment",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    effective = build_effective_env(
        environ={
            "OM_FEISHU_BOT_APP_SECRET": "from_process",
            "OM_FEISHU_BOT_APP_ID": "cli_process",
        },
        env_file=env_file,
    )

    assert effective.get("OM_FEISHU_BOT_APP_SECRET") == "from_file"
    assert effective.get("OM_FEISHU_BOT_APP_ID") == "cli_process"
    assert effective.source_of("OM_FEISHU_BOT_APP_SECRET").public_value() == "env_file"
    assert effective.source_of("OM_FEISHU_BOT_APP_ID").public_value() == "process_env"


def test_settings_inspect_redacts_secret_values(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        'OM_FEISHU_BOT_APP_ID="cli_1"\nOM_FEISHU_BOT_APP_SECRET="secret_1"\n',
        encoding="utf-8",
    )

    out = inspect_effective_settings(environ={}, env_file=env_file)

    assert out["env_file_loaded"] is True
    assert out["entries"]["OM_FEISHU_BOT_APP_ID"]["value"] == "<redacted>"
    assert out["entries"]["OM_FEISHU_BOT_APP_SECRET"]["value"] == "<redacted>"
    assert out["env_file"] == "<configured-env-file>"
    assert str(env_file) not in json.dumps(out, ensure_ascii=False)


def test_settings_explain_accepts_public_alias(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text('OM_FEISHU_BOT_USER_OPEN_ID="ou_1234567890"\n', encoding="utf-8")

    out = explain_effective_setting("feishu.bot.user_open_id", environ={}, env_file=env_file)

    assert out["env_name"] == "OM_FEISHU_BOT_USER_OPEN_ID"
    assert out["configured"] is True
    assert out["source"] == "env_file"
    assert out["value"] == "<redacted>"
    assert out["env_file"] == "<configured-env-file>"
    assert str(env_file) not in json.dumps(out, ensure_ascii=False)


def test_settings_inspect_and_explain_cover_llm_api_key(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text('OM_LLM_API_KEY="sk-secret"\n', encoding="utf-8")

    inspected = inspect_effective_settings(environ={}, env_file=env_file)
    assert inspected["entries"]["OM_LLM_API_KEY"]["configured"] is True
    assert inspected["entries"]["OM_LLM_API_KEY"]["value"] == "<redacted>"
    assert inspected["entries"]["OM_LLM_API_KEY"]["secret"] is True

    explained = explain_effective_setting("assistant.llm.api_key", environ={}, env_file=env_file)
    assert explained["env_name"] == "OM_LLM_API_KEY"
    assert explained["configured"] is True
    assert explained["value"] == "<redacted>"


def test_settings_explain_defaults_unknown_env_identifiers_to_full_masking(tmp_path) -> None:
    env_file = tmp_path / "private-settings.env"
    env_file.write_text('OM_CUSTOM_ACCOUNT_ID="customer-stable-id-12345"\n', encoding="utf-8")

    explained = explain_effective_setting("OM_CUSTOM_ACCOUNT_ID", environ={}, env_file=env_file)
    serialized = json.dumps(explained, ensure_ascii=False)

    assert explained["configured"] is True
    assert explained["value"] == "<redacted>"
    assert explained["source"] == "env_file"
    assert "customer-stable-id-12345" not in serialized
    assert str(env_file) not in serialized


def test_settings_public_diagnostics_do_not_echo_missing_env_file_path(tmp_path) -> None:
    missing = tmp_path / "private-user" / "missing.env"

    inspected = inspect_effective_settings(environ={}, env_file=missing)
    diagnosed = diagnose_effective_settings(environ={}, env_file=missing)
    serialized = json.dumps({"inspect": inspected, "doctor": diagnosed}, ensure_ascii=False)

    assert inspected["warnings"] == ["env file not found"]
    assert str(missing) not in serialized
    assert "private-user" not in serialized


def test_effective_env_loads_repo_local_env_file_when_enabled(tmp_path) -> None:
    repo = tmp_path / "repo"
    env_dir = repo / ".env"
    env_dir.mkdir(parents=True)
    env_file = env_dir / "options-monitor.env"
    env_file.write_text("OM_RUNTIME_ROOT=/tmp/runtime-from-local\n", encoding="utf-8")

    out = build_effective_env(environ={}, repo_root=repo, include_local_env_file=True)

    assert out.env_file == env_file.resolve()
    assert out.env_file_loaded is True
    assert out.get("OM_RUNTIME_ROOT") == "/tmp/runtime-from-local"


def test_bootstrap_process_env_loads_selected_env_file(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("OM_RUNTIME_ROOT=/tmp/runtime-from-bootstrap\n", encoding="utf-8")
    old_runtime_root = os.environ.get("OM_RUNTIME_ROOT")
    old_env_file = os.environ.get("OM_ENV_FILE")
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    try:
        out = bootstrap_process_env(env_file=env_file, include_local_env_file=False)

        assert out.env_file_loaded is True
        assert os.environ["OM_RUNTIME_ROOT"] == "/tmp/runtime-from-bootstrap"
        assert os.environ["OM_ENV_FILE"] == str(env_file.resolve())
    finally:
        if old_runtime_root is None:
            os.environ.pop("OM_RUNTIME_ROOT", None)
        else:
            os.environ["OM_RUNTIME_ROOT"] = old_runtime_root
        if old_env_file is None:
            os.environ.pop("OM_ENV_FILE", None)
        else:
            os.environ["OM_ENV_FILE"] = old_env_file


def test_settings_doctor_reports_deprecated_env_without_secret_values(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=cli_1",
                "OM_FEISHU_BOT_APP_SECRET=secret_1",
                "OM_FEISHU_BOT_ALLOWED_OPEN_IDS=ou_1,ou_2",
                "OM_FEISHU_ACK_REACTION=SMILE",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = diagnose_effective_settings(environ={}, env_file=env_file)
    checks = {item["name"]: item for item in out["checks"]}

    assert out["summary"]["ok"] is True
    assert checks["deprecated_env"]["status"] == "warn"
    assert "secret_1" not in json.dumps(out, ensure_ascii=False)
    assert checks["feishu_bot_credentials"]["status"] == "ok"
    assert checks["feishu_bot_recipients"]["value"]["allowed_open_ids_count"] == 2


def test_settings_doctor_reports_deprecated_ack_duplicate_conflict(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=cli_1",
                "OM_FEISHU_BOT_APP_SECRET=secret_1",
                "OM_FEISHU_BOT_ALLOWED_OPEN_IDS=ou_1",
                "OM_FEISHU_ACK_REACTION=SMILE",
                'OM_FEISHU_ACK_REACTION="thumbsup"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = diagnose_effective_settings(environ={}, env_file=env_file)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["deprecated_env"]["status"] == "warn"
    assert checks["deprecated_env_duplicates"]["status"] == "warn"
    duplicate = checks["deprecated_env_duplicates"]["value"]["duplicates"][0]
    assert duplicate["name"] == "OM_FEISHU_ACK_REACTION"
    assert duplicate["conflict"] is True
    assert duplicate["migration_target"] == "inbound.feishu_ws.ack_reaction"
    assert "overwrite runtime config" in checks["deprecated_env_duplicates"]["value"]["action"]
    assert checks["duplicate_env_keys"]["status"] == "warn"


def test_settings_doctor_reports_inbound_trade_write_gate_readiness(tmp_path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=cli_1",
                "OM_FEISHU_BOT_APP_SECRET=secret_1",
                "OM_FEISHU_BOT_ALLOWED_OPEN_IDS=ou_1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = diagnose_effective_settings(environ={}, env_file=env_file)
    checks = {item["name"]: item for item in out["checks"]}

    assert checks["inbound_trade_write_readiness"]["status"] == "warn"
    assert checks["inbound_trade_write_readiness"]["value"]["missing_enabled_env"] == [
        "OM_INBOUND_OPERATIONS_ENABLED",
        "OM_INBOUND_TRADE_WRITE_ENABLED",
    ]
    assert checks["inbound_upgrade_write_readiness"]["value"]["missing_enabled_env"] == [
        "OM_INBOUND_OPERATIONS_ENABLED",
        "OM_INBOUND_UPGRADE_WRITE_ENABLED",
    ]
    assert checks["inbound_monitor_run_readiness"]["value"]["missing_enabled_env"] == [
        "OM_INBOUND_OPERATIONS_ENABLED",
        "OM_INBOUND_MONITOR_RUN_ENABLED",
    ]


def test_cli_settings_explain_outputs_redacted_json(tmp_path, capsys) -> None:
    from src.interfaces.cli.main import main

    env_file = tmp_path / "options-monitor.env"
    env_file.write_text('OM_FEISHU_BOT_APP_SECRET="secret_1"\n', encoding="utf-8")

    rc = main(["settings", "explain", "--key", "feishu.bot.app_secret", "--env-file", str(env_file)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["env_name"] == "OM_FEISHU_BOT_APP_SECRET"
    assert payload["data"]["value"] == "<redacted>"


def test_cli_settings_doctor_outputs_summary(tmp_path, capsys) -> None:
    from src.interfaces.cli.main import main

    env_file = tmp_path / "options-monitor.env"
    env_file.write_text('OM_FEISHU_BOT_APP_ID="cli_1"\n', encoding="utf-8")

    rc = main(["settings", "doctor", "--env-file", str(env_file)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool_name"] == "settings.doctor"
    assert payload["data"]["summary"]["ok"] is True
    assert payload["data"]["env_file_loaded"] is True


def test_cli_settings_explain_unknown_key_returns_input_error(capsys) -> None:
    from src.interfaces.cli.main import main

    rc = main(["settings", "explain", "--key", "unknown.key", "--no-local-env-file"])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"
