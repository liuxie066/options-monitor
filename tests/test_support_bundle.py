from __future__ import annotations

import json
import socket
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.application.support_bundle import collect_support_bundle


@pytest.mark.parametrize("case", [
    "ready", "not_ready", "execution_error", "legacy_no_summary", "runtime_summary_false",
])
def test_support_bundle_cli_preserves_health_readiness_and_artifact_success(
    monkeypatch, capsys, tmp_path: Path, example_config_path: Path, case: str,
) -> None:
    import src.application.support_bundle as bundle_mod
    import src.application.tool_execution as tool_mod
    import src.interfaces.cli.main as cli

    ready = case not in {"not_ready", "execution_error"}
    health_result = {
        "tool_name": "healthcheck", "ok": case != "execution_error",
        "data": None if case == "execution_error" else {"marker": "synthetic-health-details"},
        "warnings": [],
        "error": {"code": "SYNTHETIC_EXECUTION_FAILURE", "message": "synthetic tool failure"}
        if case == "execution_error" else None,
    }
    if health_result["data"] is not None and case != "legacy_no_summary":
        health_result["data"]["summary"] = {"ok": case != "not_ready"}
    calls = []
    forbidden_calls = []
    local_section_calls = []

    def execute_tool(name, payload):
        calls.append((name, deepcopy(payload)))
        if name == "healthcheck":
            return deepcopy(health_result)
        if name == "runtime_status":
            return {"tool_name": name, "ok": True, "data": {
                "summary": {"ok": case != "runtime_summary_false"},
            }}
        forbidden_calls.append("unexpected_tool:" + name)
        raise AssertionError("unexpected diagnostic tool")

    def local_section(**kwargs):
        local_section_calls.append(kwargs)
        return {"summary": {"ok": True, "warning_count": 0}}

    def forbidden_network(*args, **kwargs):
        forbidden_calls.append("network.connect")
        raise AssertionError("isolated diagnostics must not connect to a real service")

    monkeypatch.setattr(cli, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(bundle_mod, "run_setup_check", local_section)
    monkeypatch.setattr(bundle_mod, "diagnose_effective_settings", local_section)
    monkeypatch.setattr(bundle_mod, "execute_tool", execute_tool)
    monkeypatch.setattr(tool_mod, "execute_tool", execute_tool)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    env_file = tmp_path / "isolated.env"
    env_file.write_text("", encoding="utf-8")
    profile_path = str(tmp_path / "isolated.profile.json")
    output_dir = tmp_path / "support"
    config_before = example_config_path.read_bytes()
    args = [
        "--config-path", str(example_config_path), "--accounts", "lx",
        "--profile-path", profile_path, "--env-file", str(env_file), "--no-local-env-file",
    ]
    bundle_rc = cli.main([
        "support", "bundle", *args, "--include-healthcheck", "--output-dir", str(output_dir),
    ])
    response = json.loads(capsys.readouterr().out)
    assert bundle_rc == 0 and response["ok"] is True
    assert response["tool_name"] == "support.bundle"
    files = list(output_dir.iterdir())
    assert len(files) == 1 and files[0].name == response["data"]["bundle_name"]
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["sections"]["healthcheck"]["items"][0]["result"] == health_result
    assert stored["summary"] == response["data"]["summary"]
    health_rc = cli.main(["healthcheck", *args])
    health_output = json.loads(capsys.readouterr().out)
    doctor_rc = cli.main(["doctor", *args])
    doctor_output = json.loads(capsys.readouterr().out)
    assert health_rc == doctor_rc == (0 if ready else 2)
    assert health_output == health_result
    assert doctor_output["data"]["execution_ok"] is health_result["ok"]
    assert doctor_output["data"]["readiness_ok"] is ready
    assert doctor_output["ok"] is ready
    expected_scope = {
        "config_path": str(example_config_path), "accounts": ["lx"], "profile_path": profile_path,
    }
    assert calls[:2] == [("runtime_status", expected_scope), ("healthcheck", expected_scope)]
    assert calls[2:] == [("healthcheck", {**expected_scope, "env_file": str(env_file)})] * 2
    assert len(local_section_calls) == 2
    assert all(call["repo_root"] == tmp_path.resolve()
               and call["env_file"] == str(env_file)
               and call["include_local_env_file"] is False for call in local_section_calls)
    assert example_config_path.read_bytes() == config_before
    assert forbidden_calls == []
    summary = stored["summary"]
    assert summary["section_status"]["runtime_status"] == "ok"
    detail = {"summary": summary, "health_rc": health_rc, "doctor_rc": doctor_rc, "bundle_rc": bundle_rc}
    assert summary["diagnostic_ok"] is ready, detail
    assert summary["ok"] is ready
    assert summary["error_count"] == (0 if ready else 1)
    assert summary["section_status"]["healthcheck"] == ("ok" if ready else "error")


def test_support_bundle_writes_redacted_diagnostics(tmp_path: Path, example_config_path: Path) -> None:
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=cli_1",
                "OM_FEISHU_BOT_APP_SECRET=secret_1",
                "OM_FEISHU_BOT_USER_OPEN_ID=ou_123456789012345678",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, dict]] = []

    def _execute_tool(name: str, payload: dict) -> dict:
        calls.append((name, payload))
        return {
            "tool_name": name,
            "ok": True,
            "data": {
                "summary": {"ok": True, "warning_count": 0},
                "stdout_tail": "Bearer live-token https://example.com/webhook/token account 999000000000000001",
            },
            "warnings": [],
        }

    out = collect_support_bundle(
        repo_root=Path(__file__).resolve().parents[1],
        config_path=example_config_path,
        accounts=["lx"],
        env_file=env_file,
        include_local_env_file=False,
        include_healthcheck=True,
        output_dir=tmp_path / "support",
        execute_tool_fn=_execute_tool,
        now_fn=lambda: datetime(2026, 5, 21, 5, 30, tzinfo=timezone.utc),
    )

    bundle_path = tmp_path / "support" / "options-monitor-support-20260521T053000Z.json"
    assert bundle_path.name == "options-monitor-support-20260521T053000Z.json"
    assert bundle_path.exists()
    assert out["bundle_name"] == bundle_path.name
    assert out["bundle_path_public"] == f".../{bundle_path.name}"
    assert "bundle_path" not in out
    assert bundle_path.stat().st_mode & 0o777 == 0o600
    assert bundle_path.parent.stat().st_mode & 0o777 == 0o700
    assert calls == [
        ("runtime_status", {"config_path": str(example_config_path), "accounts": ["lx"]}),
        ("healthcheck", {"config_path": str(example_config_path), "accounts": ["lx"]}),
    ]
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle_text = json.dumps(bundle, ensure_ascii=False)

    assert bundle["schema_version"] == "support_bundle.v1"
    assert bundle["summary"]["section_status"]["config_validate"] == "ok"
    assert bundle["summary"]["section_status"]["healthcheck"] == "ok"
    assert bundle["sections"]["healthcheck"]["items"][0]["result"]["ok"] is True
    assert "secret_1" not in bundle_text
    assert "live-token" not in bundle_text
    assert "webhook/token" not in bundle_text
    assert "999000000000000001" not in bundle_text
    assert "Bearer ***REDACTED***" in bundle_text


def test_support_bundle_skips_healthcheck_by_default(tmp_path: Path, example_config_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    def _execute_tool(name: str, payload: dict) -> dict:
        calls.append((name, payload))
        return {"tool_name": name, "ok": True, "data": {"summary": {"ok": True}}, "warnings": []}

    collect_support_bundle(
        repo_root=Path(__file__).resolve().parents[1],
        config_path=example_config_path,
        include_local_env_file=False,
        output_dir=tmp_path,
        execute_tool_fn=_execute_tool,
        now_fn=lambda: datetime(2026, 5, 21, 5, 31, tzinfo=timezone.utc),
    )
    bundle_path = tmp_path / "options-monitor-support-20260521T053100Z.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    assert [name for name, _payload in calls] == ["runtime_status"]
    assert bundle["sections"]["healthcheck"] == {"status": "skipped", "reason": "include_healthcheck=false"}


def test_support_bundle_redacts_realistic_identity_and_host_path_markers(
    tmp_path: Path,
    example_config_path: Path,
) -> None:
    sender_id = "ou_A9x7PrivateSender"
    conversation_id = "oc_B8y6PrivateChat"
    message_id = "om_C7z5PrivateMessage"
    host_path = "/home/" + "private-user/apps/options-monitor/private.json"

    def _execute_tool(name: str, _payload: dict) -> dict:
        return {
            "tool_name": name,
            "ok": True,
            "data": {
                "sender_id": sender_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "details": f"failed while reading {host_path}",
            },
            "warnings": [],
        }

    out = collect_support_bundle(
        repo_root=Path(__file__).resolve().parents[1],
        config_path=example_config_path,
        include_local_env_file=False,
        output_dir=tmp_path,
        execute_tool_fn=_execute_tool,
        now_fn=lambda: datetime(2026, 5, 21, 5, 32, tzinfo=timezone.utc),
    )

    bundle_path = tmp_path / out["bundle_name"]
    serialized = bundle_path.read_text(encoding="utf-8")
    for marker in (sender_id, conversation_id, message_id, host_path, str(tmp_path)):
        assert marker not in serialized
    assert "***REDACTED_ID***" in serialized
