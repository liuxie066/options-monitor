from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.assistant import (
    capability_catalog_payload,
    capability_catalog_text,
    command_catalog_payload,
)
from src.application.assistant.config_loader import load_assistant_config
from src.application.assistant.context_eval import format_context_eval_text, run_context_eval_suite
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.diagnostics import check_llm_planner
from src.application.assistant.llm_model_profiles import (
    add_model_profile_to_config,
    configured_model_profiles_payload,
    current_model_payload,
    model_catalog,
    parse_model_profiles,
    switch_active_model_profile,
    write_model_config_update,
)
from src.application.assistant.operation_diagnostics import collect_pending_operations, collect_recent_audit
from src.application.assistant.runtime import handle_assistant_message
from src.application.assistant.settings import AssistantSettings
from src.application.assistant.upgrade_operations import run_confirmed_upgrade_operation
from src.application.config_yaml import default_yaml_config_path, load_yaml_config_file


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _assistant_settings_for_cli(
    *,
    config_key: str | None,
    config_path: str | None,
    assistant_config_path: str | None = None,
    force_enabled: bool | None = None,
) -> AssistantSettings:
    del config_key, config_path
    assistant_explicit = bool(assistant_config_path is not None and str(assistant_config_path).strip())
    _assistant_path, assistant_cfg = load_assistant_config(
        config_path=assistant_config_path,
        missing_ok=not assistant_explicit,
    )
    if assistant_cfg:
        configured = AssistantSettings.from_runtime_config(assistant_cfg)
        return AssistantSettings(
            enabled=configured.enabled if force_enabled is None else bool(force_enabled),
            context_window_messages=configured.context_window_messages,
            default_market_scope=configured.default_market_scope,
            planner=configured.planner,
            llm=configured.llm,
        )
    return AssistantSettings(enabled=True if force_enabled is None else bool(force_enabled))


def add_assistant_commands(parser: argparse.ArgumentParser) -> None:
    assistant_sub = parser.add_subparsers(dest="assistant_command", required=True)
    assistant_handle = assistant_sub.add_parser("handle", help="handle one local or remote assistant message")
    assistant_handle.add_argument("--text", required=True)
    assistant_handle.add_argument("--sender", dest="sender_id", default="local")
    assistant_handle.add_argument("--channel", default="local")
    assistant_handle.add_argument("--message-id", default=None)
    assistant_handle.add_argument("--conversation-id", default=None)
    assistant_handle.add_argument("--config-key", default=None, choices=("us", "hk"))
    assistant_handle.add_argument("--config-path", default=None)
    assistant_handle.add_argument("--assistant-config", default=None)
    assistant_handle.add_argument("--audit-db", default=None)
    assistant_handle.add_argument("--env-file", default=None)
    assistant_handle.add_argument("--no-local-env-file", action="store_true")
    assistant_handle.add_argument("--format", choices=("json", "text"), default="json")
    assistant_commands = assistant_sub.add_parser("commands", help="list supported assistant commands and intents")
    assistant_commands.add_argument("--format", choices=("json", "text"), default="json")
    assistant_capabilities = assistant_sub.add_parser(
        "capabilities",
        help="list supported assistant capabilities and LLM routing surface",
    )
    assistant_capabilities.add_argument("--format", choices=("json", "text"), default="json")
    assistant_context_eval = assistant_sub.add_parser(
        "eval-context",
        help="run planner-context eval fixtures and print context decision report",
    )
    assistant_context_eval.add_argument("--fixture", default=None)
    assistant_context_eval.add_argument("--case-id", action="append", default=None)
    assistant_context_eval.add_argument("--format", choices=("json", "text"), default="text")
    assistant_llm_check = assistant_sub.add_parser(
        "llm-check",
        help="check optional LLM planner configuration",
    )
    assistant_llm_check.add_argument("--assistant-config", default=None)
    assistant_llm_check.add_argument("--env-file", default=None)
    assistant_llm_check.add_argument("--no-local-env-file", action="store_true")
    assistant_llm_check.add_argument("--live", action="store_true", help="run one read-only planner probe")
    assistant_llm_check.add_argument("--text", default=None, help="probe text used with --live")
    assistant_model = assistant_sub.add_parser("model", help="manage optional assistant LLM model profiles")
    assistant_model_sub = assistant_model.add_subparsers(dest="assistant_model_command", required=True)
    assistant_model_catalog = assistant_model_sub.add_parser("catalog", help="list built-in supported LLM providers")
    assistant_model_catalog.add_argument("--format", choices=("json", "text"), default="json")
    assistant_model_list = assistant_model_sub.add_parser("list", help="list configured assistant model profiles")
    assistant_model_list.add_argument("--config-yaml", default=None)
    assistant_model_list.add_argument("--env-file", default=None)
    assistant_model_list.add_argument("--no-local-env-file", action="store_true")
    assistant_model_list.add_argument("--format", choices=("json", "text"), default="json")
    assistant_model_current = assistant_model_sub.add_parser("current", help="show authoring and runtime active model")
    assistant_model_current.add_argument("--config-yaml", default=None)
    assistant_model_current.add_argument("--assistant-config", default=None)
    assistant_model_current.add_argument("--format", choices=("json", "text"), default="json")
    assistant_model_add = assistant_model_sub.add_parser("add", help="add or update one assistant model profile")
    assistant_model_add.add_argument("name")
    assistant_model_add.add_argument("--config-yaml", default=None)
    assistant_model_add.add_argument("--provider", required=True)
    assistant_model_add.add_argument("--model", required=True)
    assistant_model_add.add_argument("--base-url", default=None)
    assistant_model_add.add_argument("--api-key-env", default=None)
    assistant_model_add.add_argument("--confidence-min", type=float, default=None)
    assistant_model_add.add_argument("--timeout-seconds", type=int, default=None)
    assistant_model_add.add_argument("--max-output-tokens", type=int, default=None)
    assistant_model_add.add_argument("--replace", action="store_true")
    assistant_model_add.add_argument("--activate", action="store_true")
    assistant_model_add.add_argument("--apply", action="store_true")
    assistant_model_use = assistant_model_sub.add_parser("use", help="switch assistant.active_model")
    assistant_model_use.add_argument("name")
    assistant_model_use.add_argument("--config-yaml", default=None)
    assistant_model_use.add_argument("--apply", action="store_true")
    assistant_model_check = assistant_model_sub.add_parser("check", help="check one configured model profile")
    assistant_model_check.add_argument("name", nargs="?")
    assistant_model_check.add_argument("--active", action="store_true")
    assistant_model_check.add_argument("--config-yaml", default=None)
    assistant_model_check.add_argument("--env-file", default=None)
    assistant_model_check.add_argument("--no-local-env-file", action="store_true")
    assistant_model_check.add_argument("--live", action="store_true")
    assistant_model_check.add_argument("--text", default=None, help="probe text used with --live")
    assistant_model_check.add_argument("--format", choices=("json", "text"), default="json")
    assistant_pending = assistant_sub.add_parser("pending", help="inspect pending assistant operations")
    assistant_pending_sub = assistant_pending.add_subparsers(dest="assistant_pending_command", required=True)
    assistant_pending_list = assistant_pending_sub.add_parser(
        "list",
        help="list previewed operations awaiting confirmation",
    )
    assistant_pending_list.add_argument("--sender", dest="sender_id", default=None)
    assistant_pending_list.add_argument("--channel", default=None)
    assistant_pending_list.add_argument("--conversation-id", default=None)
    assistant_pending_list.add_argument("--operation-type", action="append", dest="operation_types", default=None)
    assistant_pending_list.add_argument("--include-expired", action="store_true")
    assistant_pending_list.add_argument("--limit", type=int, default=20)
    assistant_pending_list.add_argument("--audit-db", default=None)
    assistant_pending_list.add_argument("--format", choices=("json", "text"), default="json")
    assistant_audit = assistant_sub.add_parser("audit", help="inspect assistant audit records")
    assistant_audit_sub = assistant_audit.add_subparsers(dest="assistant_audit_command", required=True)
    assistant_audit_recent = assistant_audit_sub.add_parser("recent", help="show recent assistant audit records")
    assistant_audit_recent.add_argument("--sender", dest="sender_id", default=None)
    assistant_audit_recent.add_argument("--channel", default=None)
    assistant_audit_recent.add_argument("--conversation-id", default=None)
    assistant_audit_recent.add_argument("--limit", type=int, default=20)
    assistant_audit_recent.add_argument("--audit-db", default=None)
    assistant_audit_recent.add_argument("--format", choices=("json", "text"), default="json")
    assistant_upgrade_worker = assistant_sub.add_parser(
        "upgrade-worker",
        help="run one confirmed assistant upgrade operation",
    )
    assistant_upgrade_worker.add_argument("--operation-id", required=True)
    assistant_upgrade_worker.add_argument("--audit-db", default=None)
    assistant_upgrade_worker.add_argument("--env-file", default=None)
    assistant_upgrade_worker.add_argument("--no-local-env-file", action="store_true")
    assistant_upgrade_worker.add_argument("--no-final-receipt", action="store_true")
    assistant_upgrade_worker.add_argument("--format", choices=("json", "text"), default="json")


def _model_config_yaml_path(raw: str | None, *, repo_base_fn: Callable[[], Path] = repo_base) -> Path:
    if raw is not None and str(raw).strip():
        return Path(raw).expanduser().resolve()
    return default_yaml_config_path(repo_root=repo_base_fn())


def _load_model_authoring_config(
    raw: str | None,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
) -> tuple[Path, dict[str, Any]]:
    path = _model_config_yaml_path(raw, repo_base_fn=repo_base_fn)
    return path, load_yaml_config_file(path)


def _assistant_model_text(data: dict[str, Any], *, command: str) -> str:
    if command == "catalog":
        return "\n".join(
            f"{item['provider']}: {', '.join(item.get('recommended_models') or [])}"
            for item in data.get("providers", [])
        )
    if command == "list":
        rows = data.get("models") or []
        if not rows:
            return "No assistant model profiles configured."
        return "\n".join(
            f"{'*' if item.get('active') else ' '} {item.get('name')} "
            f"{item.get('provider')}/{item.get('model')} "
            f"credential_configured={bool(item.get('api_key_configured'))}"
            for item in rows
        )
    if command == "current":
        summary = data.get("summary") or {}
        authoring = data.get("authoring") or {}
        runtime = data.get("runtime") or {}
        return "\n".join(
            [
                f"active_model: {summary.get('active_model') or '-'}",
                f"authoring: {_llm_text(authoring.get('llm') if isinstance(authoring, dict) else {})}",
                f"runtime: {_llm_text(runtime.get('llm') if isinstance(runtime, dict) else {})}",
                f"drift: {bool(summary.get('drift'))}",
            ]
        )
    if command == "check":
        summary = data.get("summary") or {}
        llm = data.get("llm") or {}
        return "\n".join(
            [
                f"status: {summary.get('status')}",
                f"ok: {bool(summary.get('ok'))}",
                f"model: {_llm_text(llm)}",
            ]
        )
    return _dumps(data).strip()


def _llm_text(raw: Any) -> str:
    llm = raw if isinstance(raw, dict) else {}
    provider = str(llm.get("provider") or "").strip() or "-"
    model = str(llm.get("model") or "").strip() or "-"
    base_url = str(llm.get("base_url") or "").strip()
    return f"{provider}/{model}" + (f" base_url={base_url}" if base_url else "")


def _check_assistant_model_profile(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    check_llm_planner_fn: Callable[..., dict[str, Any]] = check_llm_planner,
) -> dict[str, Any]:
    config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
    assistant = config_doc.get("assistant") if isinstance(config_doc.get("assistant"), dict) else {}
    profiles = parse_model_profiles(assistant.get("models") if isinstance(assistant, dict) else None)
    if args.name and args.active:
        raise AgentToolError(code="INPUT_ERROR", message="pass either a model profile name or --active, not both")
    profile_name = str(args.name or "").strip()
    if not profile_name:
        profile_name = str(assistant.get("active_model") or "").strip() if isinstance(assistant, dict) else ""
    if not profile_name:
        raise AgentToolError(code="INPUT_ERROR", message="no assistant active model is configured")
    profile = profiles.get(profile_name)
    if profile is None:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"assistant model profile does not exist: {profile_name}",
            details={"available_models": sorted(profiles)},
        )
    runtime_cfg = {
        "assistant": {
            "enabled": True,
            "context_window_messages": 8,
            "planner": {"enabled": True},
            "llm": profile.llm_config(),
        }
    }
    with tempfile.TemporaryDirectory(prefix="om-assistant-model-check-") as tmp_dir:
        assistant_config_path = Path(tmp_dir) / "config.assistant.json"
        assistant_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False), encoding="utf-8")
        data = check_llm_planner_fn(
            repo_root=repo_base_fn(),
            config_path=assistant_config_path,
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
            live=bool(args.live),
            live_text=args.text or "状态",
        )
    data["profile"] = profile.public_payload(active=profile.name == str(assistant.get("active_model") or "").strip())
    data["config_yaml_path"] = str(config_yaml_path)
    if isinstance(data.get("config"), dict):
        data["config"]["config_path"] = str(config_yaml_path)
        data["config"]["model_profile"] = profile.name
    return data


def handle_assistant_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    check_llm_planner_fn: Callable[..., dict[str, Any]] = check_llm_planner,
    handle_assistant_message_fn: Callable[..., dict[str, Any]] = handle_assistant_message,
) -> int:
    if args.assistant_command == "llm-check":
        data = check_llm_planner_fn(
            repo_root=repo_base_fn(),
            config_path=args.assistant_config,
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
            live=bool(args.live),
            live_text=args.text or "状态",
        )
        return _print(build_response(
            tool_name="assistant.llm_check",
            ok=bool(data.get("summary", {}).get("ok", True)),
            data=data,
        ))

    if args.assistant_command == "model":
        if args.assistant_model_command == "catalog":
            data = model_catalog()
            if args.format == "text":
                sys.stdout.write(_assistant_model_text(data, command="catalog") + "\n")
                return 0
            return _print(build_response(tool_name="assistant.model.catalog", ok=True, data=data))

        if args.assistant_model_command == "list":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            data = configured_model_profiles_payload(
                config_doc=config_doc,
                repo_root=repo_base_fn(),
                env_file=args.env_file,
                include_local_env_file=not bool(args.no_local_env_file),
            )
            data["config_yaml_path"] = str(config_yaml_path)
            if args.format == "text":
                sys.stdout.write(_assistant_model_text(data, command="list") + "\n")
                return 0
            return _print(build_response(tool_name="assistant.model.list", ok=True, data=data))

        if args.assistant_model_command == "current":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            explicit_assistant_path = bool(args.assistant_config is not None and str(args.assistant_config).strip())
            assistant_config_path, assistant_cfg = load_assistant_config(
                config_path=args.assistant_config,
                repo_root=repo_base_fn(),
                missing_ok=not explicit_assistant_path,
            )
            data = current_model_payload(config_doc=config_doc, runtime_assistant_config=assistant_cfg)
            data["config_yaml_path"] = str(config_yaml_path)
            data["assistant_config_path"] = str(assistant_config_path)
            if args.format == "text":
                sys.stdout.write(_assistant_model_text(data, command="current") + "\n")
                return 0
            return _print(build_response(tool_name="assistant.model.current", ok=True, data=data))

        if args.assistant_model_command == "add":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            after_doc, profile = add_model_profile_to_config(
                config_doc,
                name=args.name,
                provider=args.provider,
                model=args.model,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
                confidence_min=args.confidence_min,
                timeout_seconds=args.timeout_seconds,
                max_output_tokens=args.max_output_tokens,
                replace=bool(args.replace),
                activate=bool(args.activate),
            )
            data = write_model_config_update(
                config_path=config_yaml_path,
                before_doc=config_doc,
                after_doc=after_doc,
                apply=bool(args.apply),
                action="add",
                payload={
                    "profile": profile.public_payload(active=bool(args.activate)),
                    "active_model": (
                        str(after_doc.get("assistant", {}).get("active_model") or "")
                        if isinstance(after_doc.get("assistant"), dict)
                        else None
                    ),
                },
            )
            return _print(build_response(tool_name="assistant.model.add", ok=True, data=data))

        if args.assistant_model_command == "use":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            after_doc, profile = switch_active_model_profile(config_doc, name=args.name)
            data = write_model_config_update(
                config_path=config_yaml_path,
                before_doc=config_doc,
                after_doc=after_doc,
                apply=bool(args.apply),
                action="use",
                payload={
                    "profile": profile.public_payload(active=True),
                    "active_model": profile.name,
                    "rebuild_hint": "run `om config build-assistant --source yaml` after applying this change",
                },
            )
            return _print(build_response(tool_name="assistant.model.use", ok=True, data=data))

        if args.assistant_model_command == "check":
            data = _check_assistant_model_profile(
                args,
                repo_base_fn=repo_base_fn,
                check_llm_planner_fn=check_llm_planner_fn,
            )
            if args.format == "text":
                sys.stdout.write(_assistant_model_text(data, command="check") + "\n")
                return 0 if data.get("summary", {}).get("ok", True) else 2
            return _print(build_response(
                tool_name="assistant.model.check",
                ok=bool(data.get("summary", {}).get("ok", True)),
                data=data,
            ))

    if args.assistant_command in {"commands", "capabilities"}:
        data = (
            capability_catalog_payload()
            if args.assistant_command == "capabilities"
            else command_catalog_payload()
        )
        if args.format == "text":
            text = (
                capability_catalog_text(data)
                if args.assistant_command == "capabilities"
                else str(data.get("help_text") or "")
            )
            sys.stdout.write(text.strip() + "\n")
            return 0
        tool_name = "assistant.capabilities" if args.assistant_command == "capabilities" else "assistant.commands"
        return _print(build_response(tool_name=tool_name, ok=True, data=data))

    if args.assistant_command == "eval-context":
        fixture_path = (
            Path(args.fixture).expanduser().resolve()
            if args.fixture
            else repo_base_fn() / "tests" / "fixtures" / "assistant_agent_eval.jsonl"
        )
        data = run_context_eval_suite(fixture_path=fixture_path, case_ids=args.case_id)
        out = build_response(
            tool_name="assistant.eval_context",
            ok=bool(data.get("summary", {}).get("ok")),
            data=data,
        )
        if args.format == "text":
            sys.stdout.write(format_context_eval_text(data).strip() + "\n")
            return 0 if out.get("ok", True) else 2
        return _print(out)

    if args.assistant_command == "handle":
        assistant_settings = _assistant_settings_for_cli(
            config_key=args.config_key,
            config_path=args.config_path,
            assistant_config_path=args.assistant_config,
            force_enabled=None,
        )
        request = AssistantRequest(
            text=args.text,
            sender_id=args.sender_id,
            channel=args.channel,
            message_id=args.message_id,
            conversation_id=args.conversation_id,
            config_key=args.config_key,
            config_path=args.config_path,
            audit_db=args.audit_db,
        )
        out = handle_assistant_message_fn(request, settings=assistant_settings)
        if args.format == "text":
            data_raw = out.get("data")
            data: dict[str, Any] = data_raw if isinstance(data_raw, dict) else {}
            text = str(data.get("response_text") or "").strip() or _dumps(out)
            sys.stdout.write(text + "\n")
            return 0 if out.get("ok", True) else 2
        return _print(out)

    if args.assistant_command == "pending" and args.assistant_pending_command == "list":
        data = collect_pending_operations(
            audit_db=args.audit_db,
            channel=args.channel,
            sender_id=args.sender_id,
            conversation_id=args.conversation_id,
            operation_types=args.operation_types,
            include_expired=bool(args.include_expired),
            limit=int(args.limit),
        )
        out = build_response(tool_name="assistant.pending.list", ok=True, data=data)
        if args.format == "text":
            sys.stdout.write(str(data.get("response_text") or "").strip() + "\n")
            return 0
        return _print(out)

    if args.assistant_command == "audit" and args.assistant_audit_command == "recent":
        data = collect_recent_audit(
            audit_db=args.audit_db,
            channel=args.channel,
            sender_id=args.sender_id,
            conversation_id=args.conversation_id,
            limit=int(args.limit),
        )
        out = build_response(tool_name="assistant.audit.recent", ok=True, data=data)
        if args.format == "text":
            sys.stdout.write(str(data.get("response_text") or "").strip() + "\n")
            return 0
        return _print(out)

    if args.assistant_command == "upgrade-worker":
        out = run_confirmed_upgrade_operation(
            operation_id=args.operation_id,
            audit_db=args.audit_db,
            send_receipt=not bool(args.no_final_receipt),
        )
        if args.format == "text":
            data_raw = out.get("data")
            data = data_raw if isinstance(data_raw, dict) else {}
            text = str(data.get("response_text") or "").strip() or _dumps(out)
            sys.stdout.write(text + "\n")
            return 0 if out.get("ok", True) else 2
        return _print(out)

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported assistant command: {args.assistant_command}")
