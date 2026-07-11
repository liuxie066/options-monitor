#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.application.agent_tool_registry import pure_read_tool_names
from src.application.copilot.channel_facade import run_channel_request
from src.application.research.redaction import redact_value


CASES = (
    ("july_income", "7月收益", {"monthly_income_report"}, "income"),
    ("risk_concentration", "当前期权风险主要集中在哪里", {"option_positions_read"}, "risk"),
    ("operation_review", "分析最近的期权操作有没有不合理", {"option_positions_read"}, "review"),
    ("candidate_diagnosis", "为什么 NVDA 没进候选", {"candidate_filter_explain"}, "candidate"),
    ("follow_up_conclusion", "结论呢", set(), "review"),
)

HOST_READ_ACTIONS = frozenset({"__read_observation__"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the production-side OM Copilot P1 read-only evaluation.")
    parser.add_argument("--assistant-config", required=True)
    parser.add_argument("--config-key", choices=("us", "hk"), default="us")
    parser.add_argument("--output")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="om-copilot-p1-") as temp_dir:
        payload = run_eval(
            assistant_config=args.assistant_config,
            config_key=args.config_key,
            host_db=str(Path(temp_dir) / "host.sqlite3"),
        )
    text = json.dumps(redact_value(payload), ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if payload["structural_pass"] else 1


def run_eval(*, assistant_config: str, config_key: str, host_db: str) -> dict[str, Any]:
    allowed = set(pure_read_tool_names())
    results: list[dict[str, Any]] = []
    for name, question, expected_tools, conversation_id in CASES:
        try:
            result = run_channel_request(
                user_message=question,
                config_key=config_key,
                assistant_config_path=assistant_config,
                channel="copilot-p1-eval",
                sender_id="copilot-p1-eval",
                conversation_id=conversation_id,
                host_db_path=host_db,
            )
        except SystemExit as exc:
            results.append(_failed_case(name, question, expected_tools, f"SystemExit: {exc}"))
            continue
        except Exception as exc:
            results.append(_failed_case(name, question, expected_tools, f"{type(exc).__name__}: {exc}"))
            continue
        events = [_event_payload(event) for event in result.events if event.type in {"model_turn_completed", "tool_call", "tool_result"}]
        tool_names = [
            str(event["payload"].get("tool_name") or "")
            for event in events
            if event["type"] == "tool_call"
        ]
        response = result.user_response.strip()
        checks = {
            "answered": result.status == "answered" and bool(response),
            "expected_tools_called": expected_tools.issubset(set(tool_names)),
            "pure_read_only": all(tool in allowed or tool in HOST_READ_ACTIONS for tool in tool_names),
            "scope_preserved": _scope_preserved(events, config_key),
            "conclusion_first": _conclusion_first(response),
            "no_tool_protocol_leak": not _contains_tool_protocol(response),
        }
        results.append(
            {
                "name": name,
                "question": question,
                "status": result.status,
                "response": result.user_response,
                "error": result.error,
                "tool_names": tool_names,
                "checks": checks,
                "structural_pass": all(checks.values()),
                "events": events,
            }
        )
    return {
        "schema_version": "om.copilot.p1_eval.v1",
        "config_key": config_key,
        "structural_pass": all(item["structural_pass"] for item in results),
        "answer_quality_review": "pending_human_review",
        "cases": results,
    }


def _failed_case(name: str, question: str, expected_tools: set[str], error: str) -> dict[str, Any]:
    checks = {
        "answered": False,
        "expected_tools_called": not expected_tools,
        "pure_read_only": True,
    }
    return {
        "name": name,
        "question": question,
        "status": "failed",
        "response": "",
        "error": error,
        "tool_names": [],
        "checks": checks,
        "structural_pass": False,
        "events": [],
    }


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "type": event.type,
        "payload": dict(event.payload),
        "visible_ref": event.visible_ref,
    }


def _scope_preserved(events: list[dict[str, Any]], config_key: str) -> bool:
    for event in events:
        if event["type"] != "tool_call":
            continue
        tool_input = event["payload"].get("tool_input")
        if not isinstance(tool_input, dict) or "config_key" not in tool_input:
            continue
        if str(tool_input.get("config_key") or "").strip().lower() != config_key:
            return False
    return True


def _conclusion_first(response: str) -> bool:
    first_line = next((line.strip() for line in response.splitlines() if line.strip()), "")
    return "结论" in first_line


def _contains_tool_protocol(response: str) -> bool:
    return any(marker in response for marker in ("<tool_calls>", "<｜｜DSML｜｜tool_calls>", "<｜｜DSML｜｜invoke"))


if __name__ == "__main__":
    raise SystemExit(main())
