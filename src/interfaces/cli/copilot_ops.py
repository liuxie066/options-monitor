from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

from src.application.copilot.contracts import AppResult, CopilotRequest, CopilotScope, new_id, to_payload
from src.application.copilot.local_harness import run_local_request


def add_copilot_commands(subparsers: Any) -> argparse.ArgumentParser:
    copilot = subparsers.add_parser("copilot", help="run OM Copilot v2 local read-only tasks")
    copilot_sub = copilot.add_subparsers(dest="copilot_command", required=True)

    run = copilot_sub.add_parser("run", help="run one local read-only Copilot question")
    run.add_argument("--text", required=True)
    run.add_argument("--config-key", default=None, choices=("us", "hk"))
    run.add_argument("--symbol", default=None)
    run.add_argument("--month", default=None)
    run.add_argument("--include-events", action="store_true")
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
        return to_payload(
            _run_local_request(
                request,
                model_config_json=args.model_config_json,
                assistant_config_path=args.assistant_config,
                model_turn_json=None,
            ),
            include_events=bool(args.include_events),
        )

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
) -> AppResult:
    return run_local_request(
        request,
        reference_year=_reference_year(),
        model_config_json=model_config_json,
        assistant_config_path=assistant_config_path,
        model_turn_json=model_turn_json,
    )


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
