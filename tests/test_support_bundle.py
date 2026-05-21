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
                "stdout_tail": "Bearer live-token https://example.com/webhook/token account 281756479859383816",
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

    bundle_path = Path(out["bundle_path"])
    assert bundle_path.name == "options-monitor-support-20260521T053000Z.json"
    assert bundle_path.exists()
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
    assert "281756479859383816" not in bundle_text
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
    bundle = json.loads(Path(out["bundle_path"]).read_text(encoding="utf-8"))

    assert [name for name, _payload in calls] == ["runtime_status"]
    assert bundle["sections"]["healthcheck"] == {"status": "skipped", "reason": "include_healthcheck=false"}
