from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from src.application.copilot.contracts import AppResult, CopilotRequest, CopilotScope, new_id, to_payload
from src.application.copilot.host import session_run_slot
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.local_harness import run_local_request, run_prepared_contract


def add_copilot_commands(subparsers: Any) -> argparse.ArgumentParser:
    copilot = subparsers.add_parser("copilot", help="run OM Copilot v2 local read-only tasks")
    copilot_sub = copilot.add_subparsers(dest="copilot_command", required=True)

    run = copilot_sub.add_parser("run", help="run one local read-only Copilot question")
    run.add_argument("--text", required=True)
    run.add_argument("--config-key", default=None, choices=("us", "hk"))
    run.add_argument("--symbol", default=None)
    run.add_argument("--month", default=None)
    run.add_argument("--include-events", action="store_true")
    run.add_argument("--host-db", default=None, help="optional Copilot Host SQLite path for durable runs")
    run.add_argument("--session-key", default=None, help="optional durable conversation session key")
    run_model = run.add_mutually_exclusive_group()
    run_model.add_argument(
        "--model-config-json",
        default=None,
        help=(
            "explicit local opt-in model config JSON; sends model-visible "
            "read-only observations to the configured provider"
        ),
    )
    run_model.add_argument(
        "--assistant-config",
        default=None,
        help="optional assistant runtime config path used to load the local Copilot model",
    )

    eval_cmd = copilot_sub.add_parser("eval", help="run one deterministic Copilot eval fixture")
    eval_cmd.add_argument("--fixture", required=True)
    eval_cmd.add_argument("--text", default="请根据 eval fixture 回答这个只读问题")
    eval_cmd.add_argument("--config-key", default="us", choices=("us", "hk"))
    eval_cmd.add_argument("--symbol", default=None)
    eval_cmd.add_argument("--month", default=None)
    eval_cmd.add_argument("--include-events", action="store_true")
    eval_model = eval_cmd.add_mutually_exclusive_group()
    eval_model.add_argument(
        "--model-config-json",
        default=None,
        help="explicit model config JSON for eval-only fixture synthesis; sends fixture facts to provider",
    )
    eval_model.add_argument(
        "--assistant-config",
        default=None,
        help="optional assistant runtime config path for eval-only fixture synthesis",
    )
    eval_model.add_argument(
        "--model-turn-json",
        default=None,
        help="explicit eval-only model turn JSON or JSON array; does not call a provider",
    )
    eval_model.add_argument(
        "--model-turn-json-file",
        default=None,
        help="path to explicit eval-only model turn JSON or JSON array; does not call a provider",
    )

    runs = copilot_sub.add_parser("runs", help="list durable Copilot runs")
    runs.add_argument("--host-db", required=True)
    runs.add_argument("--limit", type=int, default=20)

    cancel = copilot_sub.add_parser("cancel", help="request cancellation of a running Copilot run")
    cancel.add_argument("--host-db", required=True)
    cancel.add_argument("--run-id", required=True)

    resume = copilot_sub.add_parser("resume", help="resume a failed or interrupted read-only Copilot run")
    resume.add_argument("--host-db", required=True)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--include-events", action="store_true")
    resume_model = resume.add_mutually_exclusive_group()
    resume_model.add_argument("--model-config-json", default=None)
    resume_model.add_argument("--assistant-config", default=None)

    events = copilot_sub.add_parser("events", help="poll durable Copilot run events")
    events.add_argument("--host-db", required=True)
    events.add_argument("--run-id", required=True)
    events.add_argument("--after-event-id", default=None)

    replies = copilot_sub.add_parser("replies", help="list durable Copilot reply outbox entries")
    replies.add_argument("--host-db", required=True)
    replies.add_argument("--limit", type=int, default=50)
    return copilot


def handle_copilot_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.copilot_command == "run":
        request = CopilotRequest(
            request_id=new_id("req"),
            source_entry="cli",
            user_message=args.text,
            explicit_scope=CopilotScope(
                config_key=args.config_key,
                symbol=args.symbol,
                month=args.month,
            ),
            execution_environment="local",
        )
        host_store = CopilotHostStore(args.host_db) if args.host_db else None
        session_key = args.session_key or (f"cli:{request.request_id}" if host_store is not None else None)
        return to_payload(
            _run_local_request(
                request,
                model_config_json=args.model_config_json,
                assistant_config_path=args.assistant_config,
                model_turn_json=None,
                host_store=host_store,
                session_key=session_key,
            ),
            include_events=bool(args.include_events),
        )

    if args.copilot_command == "runs":
        store = CopilotHostStore(args.host_db)
        store.mark_stale_runs_interrupted()
        return {
            "ok": True,
            "status": "answered",
            "runs": [_run_summary(item) for item in store.list_runs(limit=args.limit)],
        }

    if args.copilot_command == "cancel":
        cancelled = CopilotHostStore(args.host_db).request_cancel(args.run_id)
        return {
            "ok": cancelled,
            "status": "cancel_requested" if cancelled else "not_ready",
            "run_id": args.run_id,
            "user_response": "已请求取消 Copilot 运行。" if cancelled else "该运行不存在或已进入终态。",
        }

    if args.copilot_command == "events":
        store = CopilotHostStore(args.host_db)
        events = store.run_events(args.run_id, after_event_id=args.after_event_id)
        return {
            "ok": True,
            "status": "answered",
            "run_id": args.run_id,
            "events": list(events),
            "progress": list(store.run_progress(args.run_id, after_event_id=args.after_event_id)),
        }

    if args.copilot_command == "replies":
        return {
            "ok": True,
            "status": "answered",
            "replies": [_reply_summary(item) for item in CopilotHostStore(args.host_db).list_replies(limit=args.limit)],
        }

    if args.copilot_command == "resume":
        store = CopilotHostStore(args.host_db)
        store.mark_stale_runs_interrupted()
        source = store.resume_source(args.run_id)
        if source is None:
            return {
                "ok": False,
                "status": "not_ready",
                "run_id": args.run_id,
                "user_response": "该运行不可恢复、恢复次数已耗尽，或不是只读 Copilot 合同。",
            }
        contract, events, session_key = source
        recovered = _successful_observations(events)
        slot_key = session_key or f"resume:{args.run_id}"
        with session_run_slot(slot_key, host_store=store, ttl_seconds=300) as entered:
            if not entered:
                return {
                    "ok": False,
                    "status": "not_ready",
                    "run_id": args.run_id,
                    "user_response": "该会话已有 Copilot 运行正在执行。",
                }
            result = run_prepared_contract(
                contract,
                model_config_json=args.model_config_json,
                assistant_config_path=args.assistant_config,
                host_store=store,
                session_key=slot_key,
                resumed_from=args.run_id,
                recovered_observations=recovered,
            )
        return to_payload(result, include_events=bool(args.include_events))

    if args.copilot_command == "eval":
        request = CopilotRequest(
            request_id=new_id("req"),
            source_entry="cli",
            user_message=args.text,
            explicit_scope=CopilotScope(
                config_key=args.config_key,
                symbol=args.symbol,
                month=args.month,
            ),
            execution_environment="eval",
            debug_overrides={"fixture_id": args.fixture},
        )
        model_turn_json, file_error = _model_turn_json_from_file(args.model_turn_json_file)
        if file_error:
            return to_payload(
                _failed_model_turn_file_result(request, file_error),
                include_events=bool(args.include_events),
            )
        if model_turn_json is None:
            model_turn_json = args.model_turn_json
        return to_payload(
            _run_local_request(
                request,
                model_config_json=args.model_config_json,
                assistant_config_path=args.assistant_config,
                model_turn_json=model_turn_json,
            ),
            include_events=bool(args.include_events),
        )

    return {
        "ok": False,
        "status": "failed",
        "user_response": f"unsupported copilot command: {getattr(args, 'copilot_command', None)}",
    }


def _run_local_request(
    request: CopilotRequest,
    *,
    model_config_json: str | None = None,
    assistant_config_path: str | None = None,
    model_turn_json: str | None = None,
    host_store: CopilotHostStore | None = None,
    session_key: str | None = None,
) -> AppResult:
    return run_local_request(
        request,
        reference_year=_reference_year(),
        model_config_json=model_config_json,
        assistant_config_path=assistant_config_path,
        model_turn_json=model_turn_json,
        host_store=host_store,
        session_key=session_key,
    )


def _successful_observations(events: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(item.get("payload") or {})
        for item in events
        if item.get("type") == "tool_result"
        and isinstance(item.get("payload"), dict)
        and bool(item["payload"].get("ok"))
    )


def _run_summary(record: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {}
    try:
        parsed = json.loads(str(record.get("response_json") or "{}"))
        response = dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        pass
    return {
        "run_id": record.get("run_id"),
        "session_key": record.get("session_key"),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "resumed_from": record.get("resumed_from"),
        "resume_attempts": int(record.get("resume_attempts") or 0),
        "termination_reason": record.get("termination_reason"),
        "metrics": _json_dict(record.get("metrics_json")),
        "response_status": response.get("status"),
    }


def _reply_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "delivery_key": record.get("delivery_key"),
        "channel": record.get("channel"),
        "session_key": record.get("session_key"),
        "run_id": record.get("run_id"),
        "status": record.get("status"),
        "attempt_count": int(record.get("attempt_count") or 0),
        "next_attempt_at": record.get("next_attempt_at"),
        "last_error": record.get("last_error"),
    }


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _reference_year() -> int:
    return date.today().year


def _model_turn_json_from_file(path: str | None) -> tuple[str | None, str | None]:
    if not path:
        return None, None
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except OSError:
        return None, "model_turn_file_read_failed"


def _failed_model_turn_file_result(request: CopilotRequest, reason: str) -> AppResult:
    return AppResult(
        status="failed",
        user_response="Copilot eval 模型轮次文件不可读取。",
        error={"code": "MODEL_TURN_FILE_READ_FAILED", "reason": reason},
        request_id=request.request_id,
        decision_trace={"model_turn_error": reason},
        ok=False,
    )
