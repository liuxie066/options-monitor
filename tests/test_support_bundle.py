from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.application.support_bundle import collect_support_bundle


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

    out = collect_support_bundle(
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
