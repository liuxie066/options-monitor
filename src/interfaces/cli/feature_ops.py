"""Terminal forms for optional features; all durable writes keep their owners."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.control.llm_model_profiles import add_model_profile_to_config, parse_model_profiles, switch_active_model_profile
from src.application.channels.wechat_clawbot.binding import connect_wechat_clawbot_target
from src.application.config_authoring_transaction import config_source_sha256
from src.application.config_env import env_source_sha256, feature_env_path, write_feature_env
from src.application.config_features import feature_document, publish_feature_document
from src.application.config_yaml import load_yaml_config_file, resolve_yaml_config_path
from src.application.config_yaml_holdings import set_yaml_holdings_inclusion
from src.application.llm_provider_registry import provider_catalog_payload
from src.application.notification_delivery_route import notifications_enabled
from src.application.portfolio_management import portfolio_management_enabled
from src.application.settings import build_effective_env
from src.infrastructure.portfolio_management_client import DEFAULT_SERVICE_URL
from src.interfaces.cli.secret_ops import run_store_command


def parse_enabled(value: str) -> bool:
    if value.lower() in {"true", "yes", "y", "1", "on"}:
        return True
    if value.lower() in {"false", "no", "n", "0", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true/false")


def add_feature_configure_parser(subparsers: Any, feature: str) -> None:
    parser = subparsers.add_parser("configure", help=f"configure {feature} with preview and terminal prompts")
    parser.set_defaults(feature=feature)
    parser.add_argument("--config-yaml", default=None)
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--enabled", type=parse_enabled, default=None)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--expected-source-sha256", default=None)
    parser.add_argument("--expected-preview-sha256", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--expected-env-sha256", default=None)
    if feature == "bot":
        parser.add_argument("--profile", default=None)
        parser.add_argument("--provider", default=None)
        parser.add_argument("--model", default=None)
        parser.add_argument("--base-url", default=None)
        parser.add_argument("--context-window-tokens", type=int, default=None)
        parser.add_argument("--max-output-tokens", type=int, default=None)
        parser.add_argument("--replace", action="store_true")
    elif feature == "channel":
        parser.add_argument("--provider", choices=("feishu_app", "wechat_clawbot"), default=None)
        parser.add_argument("--app-id", default=None)
        parser.add_argument("--recipient", default=None, help="Feishu open_id notification recipient")
        parser.add_argument("--allowed-senders", default=None)
        parser.add_argument("--target", default=None, help="existing WeChat binding target")
        parser.add_argument("--label", default="default")
        parser.add_argument("--binding-name", default="ops")
    elif feature == "holdings":
        parser.add_argument("--service-url", default=None)
        parser.add_argument("--enable-pm", action="store_true", help="include global PM integration in the same Holdings config publication")


def add_holdings_commands(subparsers: Any) -> None:
    parser = subparsers.add_parser("holdings", help="configure optional Portfolio Exposure Holdings source")
    add_feature_configure_parser(parser.add_subparsers(dest="holdings_command", required=True), "holdings")


def run_feature_configure(args: argparse.Namespace, *, repo_base_fn: Callable[[], Path] = repo_base,
                          input_fn: Callable[[str], str] = input, output_fn: Callable[[str], Any] = print,
                          input_is_tty: Callable[[], bool] = lambda: sys.stdin.isatty(),
                          secret_runner: Callable[..., dict] = run_store_command,
                          wechat_connector: Callable[..., dict] = connect_wechat_clawbot_target,
                          holdings_setter: Callable[..., dict] = set_yaml_holdings_inclusion) -> dict[str, Any]:
    """Complete a scoped form, reporting completed steps even after cancellation."""
    feature = args.feature
    source = resolve_yaml_config_path(getattr(args, "config_yaml", None), repo_root=repo_base_fn())
    root = Path(args.runtime_root).expanduser().resolve() if getattr(args, "runtime_root", None) else source.parent
    revision = config_source_sha256(source)
    doc = load_yaml_config_file(source)
    interactive = bool(getattr(args, "interactive", False) or getattr(args, "enabled", None) is None)
    if interactive and not input_is_tty():
        raise AgentToolError(code="INPUT_ERROR", message="configure requires a terminal or explicit --enabled and feature flags")
    if getattr(args, "apply", False) and not interactive and (
        not getattr(args, "confirm", False) or not getattr(args, "expected_source_sha256", None)
    ):
        raise AgentToolError(code="CONFIRMATION_REQUIRED", message="apply requires --confirm and --expected-source-sha256 from preview")
    if getattr(args, "expected_source_sha256", None) and args.expected_source_sha256 != revision:
        raise AgentToolError(code="STALE_PREVIEW", message="config.yaml changed after preview")
    completed: list[dict] = []
    explicit_env = getattr(args, "env_file", None)
    inherited_env = str(os.environ.get("OM_ENV_FILE") or "").strip()
    env_path = feature_env_path(root, explicit_env)
    env_revision = env_source_sha256(env_path)
    ordinary = build_effective_env(environ={}, env_file=env_path).values

    def ask(prompt: str, default: str = "") -> str:
        value = input_fn(prompt + (f" [{default}]" if default else "") + ": ").strip()
        return value or default

    def yes(prompt: str) -> bool:
        return ask(prompt + " [y/N]").lower() in {"y", "yes"}

    def preview_confirm(preview: dict, question: str) -> bool:
        if not interactive:
            return bool(getattr(args, "apply", False))
        output_fn(json.dumps(preview, ensure_ascii=False, indent=2))
        return yes(question)

    def channel_resume_preview(after: dict) -> dict:
        if feature != "channel" or notifications_enabled(doc) or not notifications_enabled(after):
            return {}
        effect = {
            "pending_receipts": "resume_on_next_scheduled_run",
            "includes_accumulated_while_disabled": True,
            "confirmed_receipts": "not_replayed",
            "notice": "重新开启后，后续调度会恢复 pending / explicit_failed 待处理回执，包括关闭期间积累的记录；已确认送达的回执不会重复发送。",
        }
        identity = {"config_yaml": str(source), "runtime_root": str(root), "source_sha256": revision,
                    "config_doc": after, "notification_resume": effect}
        digest = sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if not interactive and getattr(args, "apply", False):
            expected = getattr(args, "expected_preview_sha256", None)
            if not expected:
                raise AgentToolError(code="CONFIRMATION_REQUIRED", message="enabling notifications resumes pending receipts; apply requires --expected-preview-sha256 from its preview")
            if expected != digest:
                raise AgentToolError(code="STALE_PREVIEW", message="notification enablement differs from the confirmed preview")
        return {"notification_resume": effect, "preview_sha256": digest}

    def publish(after: dict, *, title: str) -> dict:
        nonlocal revision, doc
        preview = publish_feature_document(repo_root=repo_base_fn(), config_path=source, config_doc=after,
                                           runtime_root=root, expected_source_sha256=revision)
        resume_preview = channel_resume_preview(after)
        preview.update(resume_preview)
        preview["requested_setting"] = {"feature": feature, "enabled": enabled}
        if feature == "bot":
            bot_config = after.get("bot") or {}
            profile_name = bot_config.get("active_model")
            profiles = parse_model_profiles(bot_config.get("models"))
            preview["requested_setting"]["profile"] = profiles[profile_name].public_payload(active=True) if profile_name in profiles else None
        elif feature == "channel":
            preview["requested_setting"]["route"] = {key: (after.get("notifications") or {}).get(key)
                                                        for key in ("provider", "channel", "target")}
        if (after.get("portfolio_management") or {}) != (doc.get("portfolio_management") or {}):
            preview["requested_setting"] = {"feature": "portfolio-management", "enabled": True,
                                             "scope": "global PM integration"}
        if resume_preview:
            title = resume_preview["notification_resume"]["notice"] + " 确认开启？"
        if not preview_confirm(preview, title):
            return preview
        try:
            result = publish_feature_document(repo_root=repo_base_fn(), config_path=source, config_doc=after,
                                              runtime_root=root, apply=True, expected_source_sha256=revision)
        except KeyboardInterrupt:
            completed.append({"stage": "config_publication", "status": "interrupted", "write_applied": None,
                              "next_step": "Inspect authoring transaction recovery before retrying"})
            raise
        result.update(resume_preview)
        completed.append(result)
        revision = result["source_revision"]["after_sha256"]
        doc = after
        return result

    def save_env(updates: dict[str, str]) -> dict:
        nonlocal env_revision, ordinary
        if not explicit_env and inherited_env and Path(inherited_env).expanduser().resolve() != env_path.resolve():
            raise AgentToolError(code="CONFIG_ERROR", message="another OM_ENV_FILE shadows this instance; pass --env-file explicitly",
                                 details={"env_file": inherited_env, "instance_env_file": str(env_path)})
        preview = write_feature_env(path=env_path, updates=updates,
                                    expected_source_sha256=getattr(args, "expected_env_sha256", None) or env_revision)
        if not preview_confirm(preview, "保存这些普通连接设置？"):
            return preview
        if not interactive and not getattr(args, "expected_env_sha256", None):
            raise AgentToolError(code="CONFIRMATION_REQUIRED", message="env apply also requires --expected-env-sha256")
        try:
            result = write_feature_env(path=env_path, updates=updates, apply=True, expected_source_sha256=env_revision)
        except KeyboardInterrupt:
            completed.append({"stage": "env_publication", "status": "interrupted", "write_applied": None,
                              "env_file": str(env_path), "next_step": "Read back this file and its backup before retrying"})
            raise
        completed.append(result)
        env_revision = result["source_revision"]["after_sha256"]
        ordinary = build_effective_env(environ={}, env_file=env_path).values
        return result

    def provision(logical_name: str) -> dict:
        if not interactive or not yes(f"现在在终端隐藏输入 {logical_name}？"):
            return {"status": "not_checked", "logical_name": logical_name,
                    "next_step": f"om secrets set {logical_name}"}
        action = ask("凭证操作（set 新增 / rotate 替换）", "set")
        if action not in {"set", "rotate"}:
            raise AgentToolError(code="INPUT_ERROR", message="credential action must be set or rotate")
        from src.infrastructure.secret_store.systemd_credentials import DEFAULT_ENCRYPTED_STORE
        namespace = argparse.Namespace(store_action=action, logical_name=logical_name, backend=None,
                                       store_root=str(DEFAULT_ENCRYPTED_STORE), confirm=False)
        try:
            result = secret_runner(namespace)
        except KeyboardInterrupt:
            completed.append({"stage": "credential", "logical_name": logical_name, "status": "interrupted",
                              "write_applied": None, "next_step": f"om secrets status {logical_name}"})
            raise
        except (AgentToolError, PermissionError) as exc:
            # No privilege escalation and no clear-text fallback from the form.
            return {"status": "pending", "logical_name": logical_name, "reason": str(exc),
                    "next_step": (f'sudo "$(command -v om)" secrets {action} {logical_name} --backend systemd'
                                  if sys.platform.startswith("linux") else f"om secrets {action} {logical_name} --backend keychain")}
        completed.append(result)
        return {"status": "stored", "logical_name": logical_name, "restart_required": True}

    try:
        enabled = getattr(args, "enabled", None)
        if enabled is None:
            from src.application.config_yaml_holdings import holdings_included
            bot_config = doc.get("bot") if isinstance(doc.get("bot"), dict) else {}
            bot = bot_config
            close = doc.get("close_advice") if isinstance(doc.get("close_advice"), dict) else {}
            current_enabled = {"bot": bot.get("enabled") is True,
                               "channel": notifications_enabled(doc), "holdings": holdings_included(doc),
                               "close-advice": close.get("enabled") is not False}[feature]
            enabled = parse_enabled(ask(f"启用 {feature}（true/false）", str(current_enabled).lower()))
        credential = None
        extras: dict[str, Any] = {}
        after = feature_document(doc, feature=feature, enabled=enabled) if feature != "holdings" and (feature != "channel" or not enabled) else doc
        if feature == "bot" and enabled:
            profiles = parse_model_profiles((doc.get("bot") or {}).get("models"))
            if interactive:
                output_fn("已有模型: " + (", ".join(profiles) or "无"))
            name = getattr(args, "profile", None) or (ask("模型配置名（已有或新建）") if interactive else "")
            if not name:
                raise AgentToolError(code="INPUT_ERROR", message="--profile is required when enabling Bot")
            if name in profiles and not getattr(args, "provider", None):
                after, profile = switch_active_model_profile(after, name=name)
            else:
                if interactive:
                    output_fn("Provider: " + ", ".join(item["provider"] for item in provider_catalog_payload()["providers"]))
                provider = getattr(args, "provider", None) or (ask("Provider") if interactive else "")
                model = getattr(args, "model", None) or (ask("模型名称") if interactive else "")
                context = getattr(args, "context_window_tokens", None)
                limit = getattr(args, "max_output_tokens", None)
                if interactive and context is None:
                    context = int(ask("模型上下文 token 上限（按提供商说明）"))
                if interactive and limit is None:
                    raw = ask("最大输出 token（已知支持自动值的模型可留空）")
                    limit = int(raw) if raw else None
                after, profile = add_model_profile_to_config(
                    after, name=name, provider=provider, model=model, base_url=getattr(args, "base_url", None),
                    context_window_tokens=context, max_output_tokens=limit,
                    replace=bool(getattr(args, "replace", False)), activate=True)
            extras["model"] = profile.public_payload(active=True)
            result = publish(after, title="保存 Bot 模型并启用？")
            if result["write_applied"] and profile.credential_name:
                credential = provision(profile.credential_name)
        elif feature == "channel" and enabled:
            provider = getattr(args, "provider", None) or (ask("通知通道（feishu_app / wechat_clawbot）") if interactive else "")
            values = {"provider": provider}
            if provider == "feishu_app":
                updates = {}
                for attr, key, prompt in (("app_id", "OM_FEISHU_BOT_APP_ID", "飞书 App ID"),
                                           ("recipient", "OM_FEISHU_BOT_USER_OPEN_ID", "通知收件人 open_id"),
                                           ("allowed_senders", "OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "允许与 Bot 对话的 open_id（逗号分隔）")):
                    value = getattr(args, attr, None)
                    if value is None and interactive:
                        value = ask(prompt, ordinary.get(key, ""))
                    if not value:
                        value = ordinary.get(key, "")
                    if not value:
                        raise AgentToolError(code="INPUT_ERROR", message=f"{key} is required")
                    updates[key] = value
                after = feature_document(doc, feature=feature, enabled=True, values=values)
                # Validate the enablement confirmation before the separately authorized env write.
                channel_resume_preview(after)
                extras["env"] = save_env(updates)
                if interactive and not extras["env"]["write_applied"]:
                    return {"ok": True, "status": "cancelled", "completed_steps": completed, **extras}
                result = publish(after, title="启用此通知通道？")
                if result["write_applied"]:
                    credential = provision("feishu.bot.app_secret")
            elif provider == "wechat_clawbot":
                label = getattr(args, "label", "default")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", label):
                    raise AgentToolError(code="INPUT_ERROR", message="invalid WeChat state label")
                state = root / "output_shared" / "state" / "channels" / "wechat_clawbot" / label
                target = getattr(args, "target", None)
                if not target and interactive and yes("开始微信扫码登录并绑定？此步会联系微信并保存绑定"):
                    try:
                        connected = wechat_connector(base=root, label=label, state_dir=str(state),
                                                     name=getattr(args, "binding_name", "ops"),
                                                     progress_fn=lambda event: output_fn(json.dumps(event, ensure_ascii=False)))
                    except KeyboardInterrupt:
                        completed.append({"stage": "wechat_binding", "status": "interrupted", "write_applied": None,
                                          "state_dir": str(state), "next_step": "Inspect channel bindings before retrying"})
                        raise
                    completed.append({"wechat_binding": connected})
                    if not connected.get("ok"):
                        return {"ok": False, "status": "binding_incomplete", "completed_steps": completed}
                    target = (connected.get("data") or {}).get("target")
                if not target:
                    raise AgentToolError(code="INPUT_ERROR", message="--target is required; bind with om channel wechat-clawbot connect")
                from src.application.channels.wechat_clawbot.state import load_wechat_clawbot_binding, resolve_wechat_clawbot_target
                if resolve_wechat_clawbot_target(target, notifications={"wechat_clawbot_label": label}).label != label:
                    raise AgentToolError(code="INPUT_ERROR", message="WeChat target label differs from the selected state label")
                values.update(target=target, wechat_clawbot_label=label, wechat_clawbot_state_dir=str(state))
                binding = load_wechat_clawbot_binding(base=root, target=target, notifications=values)
                allowed = getattr(args, "allowed_senders", None)
                if interactive and allowed is None:
                    existing_allowed = ((doc.get("inbound") or {}).get("wechat_clawbot") or {}).get("allowed_senders", "")
                    allowed = ask("允许与 Bot 对话的微信用户（逗号分隔）", existing_allowed or f"wechat:{binding.to_user_id}")
                if allowed is not None:
                    values["allowed_senders"] = allowed
                after = feature_document(doc, feature=feature, enabled=True, values=values)
                result = publish(after, title="启用此微信通知绑定？")
            else:
                raise AgentToolError(code="INPUT_ERROR", message="--provider must be feishu_app or wechat_clawbot")
        elif feature == "holdings":
            service_url = getattr(args, "service_url", None)
            if enabled and interactive:
                service_url = service_url or ask("同机 PM 服务地址", ordinary.get("PORTFOLIO_SERVICE_URL", DEFAULT_SERVICE_URL))
            enable_pm = bool(enabled and not portfolio_management_enabled(doc))
            if enable_pm and not interactive and not getattr(args, "enable_pm", False):
                raise AgentToolError(code="CONFIG_ERROR", message="PM integration is disabled; preview with --enable-pm or use interactive configure")
            kwargs = dict(repo_root=repo_base_fn(), config_path=source, runtime_root=root, enabled=enabled, enable_pm=enable_pm)
            if enabled:
                kwargs["service_url"] = service_url or ordinary.get("PORTFOLIO_SERVICE_URL", DEFAULT_SERVICE_URL)
            preview = holdings_setter(**kwargs)
            extras["preflight"] = preview.get("preflight")
            if enabled and service_url:
                extras["env"] = write_feature_env(path=env_path, updates={"PORTFOLIO_SERVICE_URL": service_url},
                                                  expected_source_sha256=getattr(args, "expected_env_sha256", None) or env_revision)
                preview["connection"] = extras["env"]
            if (preview.get("preflight") or {}).get("status") == "failed":
                return {"ok": False, "status": "preflight_failed", "result": preview, "completed_steps": completed, **extras}
            if not interactive and getattr(args, "apply", False) and getattr(args, "expected_preview_sha256", None) != preview["preview_sha256"]:
                raise AgentToolError(code="STALE_PREVIEW", message="Holdings apply differs from confirmed preview")
            title = ("同时启用全局 PM 集成，并将预览中的非富途券商纳入指派后分布？" if enable_pm
                     else "将预览中的非富途券商纳入指派后分布？" if enabled else "关闭 Holdings 补充？")
            if preview_confirm(preview, title):
                if enabled and service_url:
                    extras["env"] = save_env({"PORTFOLIO_SERVICE_URL": service_url})
                    if not extras["env"]["write_applied"]:
                        return {"ok": True, "status": "cancelled", "completed_steps": completed, **extras}
                try:
                    result = holdings_setter(**kwargs, apply=True, confirm=True, expected_source_sha256=revision,
                                             expected_preview_sha256=(preview["preview_sha256"] if interactive else getattr(args, "expected_preview_sha256", None)))
                except KeyboardInterrupt:
                    completed.append({"stage": "holdings_publication", "status": "interrupted", "write_applied": None,
                                      "next_step": "Inspect authoring transaction recovery before retrying"})
                    raise
                completed.append(result)
            else:
                result = preview
        else:
            result = publish(after, title=f"保存 {feature} {'启用' if enabled else '关闭'}设置？")
        return {"ok": True, "feature": feature, "status": "configured" if result.get("write_applied") else ("cancelled" if interactive else "preview"),
                "enabled": enabled, "result": result, "credential": credential, "completed_steps": completed,
                "external_check": "not_performed", "service_restarted": False, **extras}
    except (EOFError, KeyboardInterrupt):
        return {"ok": True, "feature": feature, "status": "cancelled", "completed_steps": completed}
    except AgentToolError as exc:
        if completed:
            raise AgentToolError(code=exc.code, message=exc.message, hint=exc.hint,
                                 details={**(exc.details or {}), "completed_steps": completed}) from exc
        raise
    except argparse.ArgumentTypeError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc), details={"completed_steps": completed}) from exc
    except (OSError, ValueError) as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc),
                             details={"completed_steps": completed}) from exc
