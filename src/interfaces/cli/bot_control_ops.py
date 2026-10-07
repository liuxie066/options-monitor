from __future__ import annotations

import time

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.bot.control.capability_catalog import (
    capability_catalog_payload,
    capability_catalog_text,
    command_catalog_payload,
)
from src.application.bot.control.config_loader import load_bot_config
from src.application.bot.control.contracts import BotInboundRequest, BotTurnResult
from src.application.bot.control.diagnostics import check_bot_llm
from src.application.bot.control.llm_model_profiles import (
    add_model_profile_to_config,
    configured_model_profiles_payload,
    current_model_payload,
    model_catalog,
    parse_model_profiles,
    switch_active_model_profile,
)
from src.application.bot.control.operation_diagnostics import collect_pending_operations, collect_recent_audit
from src.application.bot.control.runtime import handle_bot_turn
from src.application.bot.control.settings import BotSettings
from src.application.bot.control.upgrade_operations import run_confirmed_upgrade_operation
from src.application.config_yaml import default_yaml_config_path, load_yaml_config_file
from src.application.config_features import write_model_config_update


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _bot_settings_for_cli(
    *,
    config_key: str | None,
    config_path: str | None,
    bot_config_path: str | None = None,
    force_enabled: bool | None = None,
) -> BotSettings:
    del config_key, config_path
    bot_explicit = bool(bot_config_path is not None and str(bot_config_path).strip())
    _bot_path, bot_cfg = load_bot_config(
        config_path=bot_config_path,
        missing_ok=not bot_explicit,
    )
    if bot_cfg:
        configured = BotSettings.from_runtime_config(bot_cfg)
        return BotSettings(
            enabled=configured.enabled if force_enabled is None else bool(force_enabled),
            context_window_messages=configured.context_window_messages,
            default_market_scope=configured.default_market_scope,
            llm=configured.llm,
        )
    return BotSettings(enabled=False if force_enabled is None else bool(force_enabled))


def add_bot_control_commands(parser: argparse.ArgumentParser) -> None:
    bot_sub = parser.add_subparsers(dest="bot_control_command", required=True)
    register_bot_control_subcommands(bot_sub)


def register_bot_control_subcommands(bot_sub: Any) -> None:
    bot_handle = bot_sub.add_parser("handle", help="handle one local or remote Bot message")
    bot_handle.add_argument("--text", required=True)
    bot_handle.add_argument("--sender", dest="sender_id", default="local")
    bot_handle.add_argument("--channel", default="local")
    bot_handle.add_argument("--message-id", default=None)
    bot_handle.add_argument("--conversation-id", default=None)
    bot_handle.add_argument("--config-key", default=None, choices=("us", "hk"))
    bot_handle.add_argument("--config-path", default=None)
    bot_handle.add_argument("--bot-config", default=None)
    bot_handle.add_argument("--audit-db", default=None)
    bot_handle.add_argument("--env-file", default=None)
    bot_handle.add_argument("--no-local-env-file", action="store_true")
    bot_handle.add_argument("--format", choices=("json", "text"), default="json")
    bot_control_commands = bot_sub.add_parser("commands", help="list supported Bot commands and intents")
    bot_control_commands.add_argument("--format", choices=("json", "text"), default="json")
    bot_capabilities = bot_sub.add_parser(
        "capabilities",
        help="list deterministic Control capabilities",
    )
    bot_capabilities.add_argument("--format", choices=("json", "text"), default="json")
    bot_llm_check = bot_sub.add_parser(
        "llm-check",
        help="check optional Bot LLM configuration",
    )
    bot_llm_check.add_argument("--bot-config", default=None)
    bot_llm_check.add_argument("--env-file", default=None)
    bot_llm_check.add_argument("--no-local-env-file", action="store_true")
    bot_llm_check.add_argument("--live", action="store_true", help="report live provider probe removal")
    bot_model = bot_sub.add_parser("model", help="manage optional Bot LLM model profiles")
    bot_model_sub = bot_model.add_subparsers(dest="bot_model_command", required=True)
    bot_model_catalog = bot_model_sub.add_parser("catalog", help="list built-in supported LLM providers")
    bot_model_catalog.add_argument("--format", choices=("json", "text"), default="json")
    bot_model_list = bot_model_sub.add_parser("list", help="list configured Bot model profiles")
    bot_model_list.add_argument("--config-yaml", default=None)
    bot_model_list.add_argument("--env-file", default=None)
    bot_model_list.add_argument("--no-local-env-file", action="store_true")
    bot_model_list.add_argument("--format", choices=("json", "text"), default="json")
    bot_model_current = bot_model_sub.add_parser("current", help="show authoring and runtime active model")
    bot_model_current.add_argument("--config-yaml", default=None)
    bot_model_current.add_argument("--bot-config", default=None)
    bot_model_current.add_argument("--format", choices=("json", "text"), default="json")
    bot_model_add = bot_model_sub.add_parser("add", help="add or update one Bot model profile")
    bot_model_add.add_argument("name")
    bot_model_add.add_argument("--config-yaml", default=None)
    bot_model_add.add_argument("--provider", required=True)
    bot_model_add.add_argument("--model", required=True)
    bot_model_add.add_argument("--base-url", default=None)
    bot_model_add.add_argument("--api-key-env", default=None)
    bot_model_add.add_argument("--confidence-min", type=float, default=None)
    bot_model_add.add_argument("--timeout-seconds", type=int, default=None)
    bot_model_add.add_argument("--context-window-tokens", type=int, required=True)
    bot_model_add.add_argument("--max-output-tokens", type=int, default=None)
    bot_model_add.add_argument("--replace", action="store_true")
    bot_model_add.add_argument("--activate", action="store_true")
    bot_model_add.add_argument("--apply", action="store_true")
    bot_model_add.add_argument("--expected-source-sha256", default=None)
    bot_model_use = bot_model_sub.add_parser("use", help="switch bot.active_model")
    bot_model_use.add_argument("name")
    bot_model_use.add_argument("--config-yaml", default=None)
    bot_model_use.add_argument("--apply", action="store_true")
    bot_model_use.add_argument("--expected-source-sha256", default=None)
    bot_model_check = bot_model_sub.add_parser("check", help="check one configured model profile")
    bot_model_check.add_argument("name", nargs="?")
    bot_model_check.add_argument("--active", action="store_true")
    bot_model_check.add_argument("--config-yaml", default=None)
    bot_model_check.add_argument("--env-file", default=None)
    bot_model_check.add_argument("--no-local-env-file", action="store_true")
    bot_model_check.add_argument("--live", action="store_true", help="report live provider probe removal")
    bot_model_check.add_argument("--format", choices=("json", "text"), default="json")
    bot_pending = bot_sub.add_parser("pending", help="inspect pending Bot operations")
    bot_pending_sub = bot_pending.add_subparsers(dest="bot_pending_command", required=True)
    bot_pending_list = bot_pending_sub.add_parser(
        "list",
        help="list previewed operations awaiting confirmation",
    )
    bot_pending_list.add_argument("--sender", dest="sender_id", default=None)
    bot_pending_list.add_argument("--channel", default=None)
    bot_pending_list.add_argument("--conversation-id", default=None)
    bot_pending_list.add_argument("--operation-type", action="append", dest="operation_types", default=None)
    bot_pending_list.add_argument("--include-expired", action="store_true")
    bot_pending_list.add_argument("--limit", type=int, default=20)
    bot_pending_list.add_argument("--audit-db", default=None)
    bot_pending_list.add_argument("--format", choices=("json", "text"), default="json")
    bot_audit = bot_sub.add_parser("audit", help="inspect Bot audit records")
    bot_audit_sub = bot_audit.add_subparsers(dest="bot_audit_command", required=True)
    bot_audit_recent = bot_audit_sub.add_parser("recent", help="show recent Bot audit records")
    bot_audit_recent.add_argument("--sender", dest="sender_id", default=None)
    bot_audit_recent.add_argument("--channel", default=None)
    bot_audit_recent.add_argument("--conversation-id", default=None)
    bot_audit_recent.add_argument("--limit", type=int, default=20)
    bot_audit_recent.add_argument("--audit-db", default=None)
    bot_audit_recent.add_argument("--format", choices=("json", "text"), default="json")
    bot_upgrade_worker = bot_sub.add_parser(
        "upgrade-worker",
        help="run one confirmed Bot upgrade operation",
    )
    bot_upgrade_worker.add_argument("--operation-id", required=True)
    bot_upgrade_worker.add_argument("--audit-db", default=None)
    bot_upgrade_worker.add_argument("--env-file", default=None)
    bot_upgrade_worker.add_argument("--no-local-env-file", action="store_true")
    bot_upgrade_worker.add_argument("--no-final-receipt", action="store_true")
    bot_upgrade_worker.add_argument("--format", choices=("json", "text"), default="json")
    bot_handle.set_defaults(bot_control_command="handle")
    bot_control_commands.set_defaults(bot_control_command="commands")
    bot_capabilities.set_defaults(bot_control_command="capabilities")
    bot_llm_check.set_defaults(bot_control_command="llm-check")
    bot_model.set_defaults(bot_control_command="model")
    bot_pending.set_defaults(bot_control_command="pending")
    bot_audit.set_defaults(bot_control_command="audit")
    bot_upgrade_worker.set_defaults(bot_control_command="upgrade-worker")


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


def _bot_model_text(data: dict[str, Any], *, command: str) -> str:
    if command == "catalog":
        return "\n".join(
            f"{item['provider']}: {', '.join(item.get('recommended_models') or [])}"
            for item in data.get("providers", [])
        )
    if command == "list":
        rows = data.get("models") or []
        if not rows:
            return "No Bot model profiles configured."
        return "\n".join(
            f"{'*' if item.get('active') else ' '} {item.get('name')} "
            f"{item.get('provider')}/{item.get('model')} "
            f"context_window_tokens={item.get('context_window_tokens')} "
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
    context = llm.get("context_window_tokens")
    suffix = f" context_window_tokens={context}" if context is not None else ""
    return f"{provider}/{model}" + (f" base_url={base_url}" if base_url else "") + suffix


def _check_bot_model_profile(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    check_bot_llm_fn: Callable[..., dict[str, Any]] = check_bot_llm,
) -> dict[str, Any]:
    config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
    bot_config = config_doc.get("bot") if isinstance(config_doc.get("bot"), dict) else {}
    profiles = parse_model_profiles(bot_config.get("models") if isinstance(bot_config, dict) else None)
    if args.name and args.active:
        raise AgentToolError(code="INPUT_ERROR", message="pass either a model profile name or --active, not both")
    profile_name = str(args.name or "").strip()
    if not profile_name:
        profile_name = str(bot_config.get("active_model") or "").strip() if isinstance(bot_config, dict) else ""
    if not profile_name:
        raise AgentToolError(code="INPUT_ERROR", message="no Bot active model is configured")
    profile = profiles.get(profile_name)
    if profile is None:
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"Bot model profile does not exist: {profile_name}",
            details={"available_models": sorted(profiles)},
        )
    runtime_cfg = {
        "bot": {
            "enabled": True,
            "context_window_messages": 8,

            "llm": profile.llm_config(),
        }
    }
    with tempfile.TemporaryDirectory(prefix="om-bot-model-check-") as tmp_dir:
        bot_config_path = Path(tmp_dir) / "config.bot.json"
        bot_config_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False), encoding="utf-8")
        data = check_bot_llm_fn(
            repo_root=repo_base_fn(),
            config_path=bot_config_path,
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
            live=bool(args.live),
        )
    data["profile"] = profile.public_payload(active=profile.name == str(bot_config.get("active_model") or "").strip())
    data["config_yaml_path"] = str(config_yaml_path)
    if isinstance(data.get("config"), dict):
        data["config"]["config_path"] = str(config_yaml_path)
        data["config"]["model_profile"] = profile.name
    return data


def handle_bot_control_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    check_bot_llm_fn: Callable[..., dict[str, Any]] = check_bot_llm,
    handle_bot_turn_fn: Callable[..., BotTurnResult] = handle_bot_turn,
) -> int:
    if args.bot_control_command == "llm-check":
        data = check_bot_llm_fn(
            repo_root=repo_base_fn(),
            config_path=args.bot_config,
            env_file=args.env_file,
            include_local_env_file=not bool(args.no_local_env_file),
            live=bool(args.live),
        )
        return _print(build_response(
            tool_name="bot.llm_check",
            ok=bool(data.get("summary", {}).get("ok", True)),
            data=data,
        ))

    if args.bot_control_command == "model":
        if args.bot_model_command == "catalog":
            data = model_catalog()
            if args.format == "text":
                sys.stdout.write(_bot_model_text(data, command="catalog") + "\n")
                return 0
            return _print(build_response(tool_name="bot.model.catalog", ok=True, data=data))

        if args.bot_model_command == "list":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            data = configured_model_profiles_payload(
                config_doc=config_doc,
                repo_root=repo_base_fn(),
                env_file=args.env_file,
                include_local_env_file=not bool(args.no_local_env_file),
            )
            data["config_yaml_path"] = str(config_yaml_path)
            if args.format == "text":
                sys.stdout.write(_bot_model_text(data, command="list") + "\n")
                return 0
            return _print(build_response(tool_name="bot.model.list", ok=True, data=data))

        if args.bot_model_command == "current":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            explicit_bot_path = bool(args.bot_config is not None and str(args.bot_config).strip())
            bot_config_path, bot_cfg = load_bot_config(
                config_path=args.bot_config,
                repo_root=repo_base_fn(),
                missing_ok=not explicit_bot_path,
            )
            data = current_model_payload(config_doc=config_doc, runtime_bot_config=bot_cfg)
            data["config_yaml_path"] = str(config_yaml_path)
            data["bot_config_path"] = str(bot_config_path)
            if args.format == "text":
                sys.stdout.write(_bot_model_text(data, command="current") + "\n")
                return 0
            return _print(build_response(tool_name="bot.model.current", ok=True, data=data))

        if args.bot_model_command == "add":
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
                context_window_tokens=args.context_window_tokens,
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
                repo_root=repo_base_fn(),
                expected_source_sha256=getattr(args, "expected_source_sha256", None),
                payload={
                    "profile": profile.public_payload(active=bool(args.activate)),
                    "active_model": (
                        str(after_doc.get("bot", {}).get("active_model") or "")
                        if isinstance(after_doc.get("bot"), dict)
                        else None
                    ),
                },
            )
            return _print(build_response(tool_name="bot.model.add", ok=True, data=data))

        if args.bot_model_command == "use":
            config_yaml_path, config_doc = _load_model_authoring_config(args.config_yaml, repo_base_fn=repo_base_fn)
            after_doc, profile = switch_active_model_profile(config_doc, name=args.name)
            data = write_model_config_update(
                config_path=config_yaml_path,
                before_doc=config_doc,
                after_doc=after_doc,
                apply=bool(args.apply),
                action="use",
                repo_root=repo_base_fn(),
                expected_source_sha256=getattr(args, "expected_source_sha256", None),
                payload={
                    "profile": profile.public_payload(active=True),
                    "active_model": profile.name,
                    "rebuild_hint": "apply publishes and verifies runtime snapshots; reload running Bot services separately",
                },
            )
            return _print(build_response(tool_name="bot.model.use", ok=True, data=data))

        if args.bot_model_command == "check":
            data = _check_bot_model_profile(
                args,
                repo_base_fn=repo_base_fn,
                check_bot_llm_fn=check_bot_llm_fn,
            )
            if args.format == "text":
                sys.stdout.write(_bot_model_text(data, command="check") + "\n")
                return 0 if data.get("summary", {}).get("ok", True) else 2
            return _print(build_response(
                tool_name="bot.model.check",
                ok=bool(data.get("summary", {}).get("ok", True)),
                data=data,
            ))

    if args.bot_control_command in {"commands", "capabilities"}:
        data = (
            capability_catalog_payload()
            if args.bot_control_command == "capabilities"
            else command_catalog_payload()
        )
        if args.format == "text":
            text = (
                capability_catalog_text(data)
                if args.bot_control_command == "capabilities"
                else str(data.get("help_text") or "")
            )
            sys.stdout.write(text.strip() + "\n")
            return 0
        tool_name = "assistant.capabilities" if args.bot_control_command == "capabilities" else "assistant.commands"
        return _print(build_response(tool_name=tool_name, ok=True, data=data))

    if args.bot_control_command == "handle":
        received_monotonic = time.monotonic()
        bot_settings = _bot_settings_for_cli(
            config_key=args.config_key,
            config_path=args.config_path,
            bot_config_path=args.bot_config,
            force_enabled=None,
        )
        request = BotInboundRequest(
            text=args.text,
            received_monotonic=received_monotonic,
            sender_id=args.sender_id,
            channel=args.channel,
            message_id=args.message_id,
            conversation_id=args.conversation_id,
            config_key=args.config_key,
            config_path=args.config_path,
            audit_db=args.audit_db,
            bot_config_path=args.bot_config,
        )
        turn = handle_bot_turn_fn(request, settings=bot_settings)
        out = build_response(
            tool_name="bot.handle",
            ok=turn.ok,
            data=turn.public_payload(),
            error=turn.error if not turn.ok else None,
            meta=dict(turn.meta or {}),
        )
        if args.format == "text":
            text = turn.response_text.strip() or _dumps(out)
            sys.stdout.write(text + "\n")
            return 0 if turn.ok else 2
        return _print(out)

    if args.bot_control_command == "pending" and args.bot_pending_command == "list":
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

    if args.bot_control_command == "audit" and args.bot_audit_command == "recent":
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

    if args.bot_control_command == "upgrade-worker":
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

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported Bot command: {args.bot_control_command}")
