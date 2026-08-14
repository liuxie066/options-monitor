from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def test_candidate_rank_runtime_root_is_operator_only_input() -> None:
    from src.application.copilot.tools import tool_descriptions
    from src.application.tool_execution import build_tool_manifest

    manifest = build_tool_manifest()
    operator_tool = next(
        item for item in manifest["tools"] if item["name"] == "candidate_rank_explain"
    )
    copilot_tool = tool_descriptions(("candidate_rank_explain",))[0]

    assert "runtime_root" in operator_tool["input_json_schema"]["properties"]
    assert "runtime_root" not in copilot_tool["input_schema"]["properties"]


def test_runtime_artifact_tools_default_to_om_runtime_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.tool_execution import execute_tool

    runtime_root = tmp_path / "runtime"
    run_id = "run-env-root"
    seal_opening_candidate_fixture(
        runtime_root,
        run_id=run_id,
        accepted_rows=[
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA-PUT-ACCEPTED",
                "mode": "put",
            }
        ],
        rejected_rows=[
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA-PUT-REJECTED",
                "mode": "put",
                "rule": "risk_spread",
            }
        ],
    )

    now = datetime.now(timezone.utc)
    shared_audit = runtime_root / "output_shared" / "state" / "audit_events.jsonl"
    shared_audit.parent.mkdir(parents=True, exist_ok=True)
    shared_audit.write_text(
        json.dumps(
            {
                "schema_kind": "om-audit-event",
                "schema_version": "v1",
                "event_type": "assistant_perception",
                "action": "notification_delivery_completed",
                "status": "ok",
                "event_at_utc": now.isoformat(),
                "run_id": run_id,
                "extra": {
                    "event_kind": "notification_delivery_completed",
                    "run_id": run_id,
                    "accounts": ["lx"],
                    "no_send": False,
                    "send_summary": {
                        "sent_accounts": ["lx"],
                        "failure_count": 0,
                        "send_attempted_count": 1,
                        "send_confirmed_count": 1,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_audit = runtime_root / "output_runs" / run_id / "state" / "audit_events.jsonl"
    run_audit.parent.mkdir(parents=True, exist_ok=True)
    run_audit.write_text('{"status":"ok"}\n', encoding="utf-8")

    monkeypatch.delenv("OM_ENV_FILE", raising=False)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))

    filtered = execute_tool(
        "candidate_filter_explain",
        {
            "account": "lx",
            "symbol": "NVDA",
            "run_selector": "latest_notification",
            "notification_date": now.astimezone().date().isoformat(),
        },
    )
    ranked = execute_tool(
        "candidate_rank_explain",
        {"account": "lx", "run_id": run_id, "mode": "put", "top_n": 1},
    )
    runs = execute_tool("runtime_runs", {"run_id": run_id})
    logs = execute_tool(
        "runtime_logs",
        {"run_id": run_id, "kind": "audit", "lines": 1},
    )

    assert filtered["ok"] is True
    assert filtered["meta"]["source_files"][0]["run_resolution"]["resolved_run_id"] == run_id
    assert ranked["ok"] is True
    assert ranked["data"]["ranked"][0]["contract_symbol"] == "NVDA-PUT-ACCEPTED"
    assert runs["ok"] is True
    assert runs["data"]["summary"]["requested_found"] is True
    assert logs["ok"] is True
    assert logs["data"]["summary"]["requested_run_found"] is True
    assert logs["data"]["summary"]["existing_file_count"] == 1
