"""Small authoring boundary for human feature configuration."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.channels.plugins import CHANNEL_FLAGS, CHANNEL_PACKAGES, canonical_channel
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_primitives import configured_markets, deep_merge_config, file_sha256
from src.application.config_yaml import load_yaml_config_file
from src.application.write_contract import attach_write_contract


def write_model_config_update(
    *, config_path: str | Path, before_doc: dict[str, Any], after_doc: dict[str, Any],
    apply: bool, action: str, payload: dict[str, Any], repo_root: Path | None = None,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    """Publish model changes above the pure profile resolver used by generation."""
    path = Path(config_path).expanduser().resolve()
    source_sha = config_source_sha256(path)
    if load_yaml_config_file(path) != before_doc:
        raise AgentToolError(code="STALE_PREVIEW", message="model authoring source changed before publication")
    transaction = publish_feature_document(
        repo_root=repo_root or Path(__file__).resolve().parents[2], config_path=path,
        config_doc=after_doc, apply=apply, expected_source_sha256=expected_source_sha256 or source_sha,
    )
    return attach_write_contract(
        {"ok": True, "action": action, "config_yaml_path": str(path),
         "changed": before_doc != after_doc, **payload, **transaction},
        dry_run=not apply, write_applied=apply, backup_path=transaction["backup_path"],
        audit_id=transaction["audit_id"], generate_audit_id=False,
        rollback_hint=f"restore {transaction['backup_path']} and rebuild the generation" if apply else None,
    )


def publish_feature_document(*, repo_root: Path, config_path: Path, config_doc: dict[str, Any],
                             runtime_root: Path | None = None, apply: bool = False,
                             expected_source_sha256: str | None = None) -> dict[str, Any]:
    """Publish one generation; callers bind the pre-prompt source revision."""
    transaction = publish_yaml_config_generation(
        repo_root=repo_root, config_yaml_path=config_path, config_doc=config_doc,
        runtime_root=runtime_root or config_path.parent, markets=configured_markets(config_doc),
        include_bot=True, apply=apply, backup=True,
        expected_source_sha256=expected_source_sha256,
    )
    verified = []
    if apply:
        targets = [(config_path, transaction["source_revision"]["after_sha256"])]
        targets.extend((Path(item["output_config_path"]), item["sha256"])
                       for item in transaction["markets"].values())
        targets.append((Path(transaction["bot"]["output_config_path"]), transaction["bot"]["sha256"]))
        for path, expected in targets:
            try:
                if sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("published generation differs from prepared content")
            except (OSError, ValueError) as exc:
                raise AgentToolError(code="CONFIG_READBACK_FAILED", message=str(exc), details={
                    "write_applied": True, "target": str(path), "audit_id": transaction["audit_id"],
                    "backup_path": transaction["backup_path"],
                }) from exc
            verified.append(str(path))
    return {**transaction, "verified_targets": verified, "restart_performed": False,
            "process_reload_required": bool(apply), "external_check": "not_performed"}


def feature_document(config_doc: dict[str, Any], *, feature: str, enabled: bool | None,
                     values: dict[str, Any] | None = None,
                     legacy_receive_channels: set[str] | None = None) -> dict[str, Any]:
    out = deepcopy(config_doc)
    values = dict(values or {})
    def section(parent: dict, key: str) -> dict:
        value = parent.setdefault(key, {})
        if not isinstance(value, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"{key} must be an object")
        return value
    if feature == "bot":
        section(out, "bot")["enabled"] = enabled
    elif feature == "channel":
        controls = "channels" in out or any(key in values for key in ("receive_enabled", "notify_enabled"))
        if controls:
            try:
                channel = canonical_channel(values.get("provider", ""))
            except ValueError as exc:
                raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
            if "channels" not in out:
                out["channels"] = _legacy_channel_controls(config_doc, legacy_receive_channels=legacy_receive_channels)
            selected = section(section(out, "channels"), channel)
            updates = {key: values[key] for key in ("receive_enabled", "notify_enabled") if key in values}
            if enabled is not None:
                updates["enabled"] = enabled
            if any(type(value) is not bool for value in updates.values()):
                raise AgentToolError(code="INPUT_ERROR", message="channel controls must be booleans")
            for key in CHANNEL_FLAGS:
                selected.setdefault(key, False)
            selected.update(updates)
            if not values.get("configure_connection"):
                return out
            validate_channel_connection_setup(config_doc, values["provider"])
        notifications = section(out, "notifications")
        if not controls:
            notifications["enabled"] = enabled
        if enabled:
            provider = values["provider"]
            if provider not in {"feishu_app", "wechat_clawbot"}:
                raise AgentToolError(code="INPUT_ERROR", message="unsupported notification provider")
            notifications.update(provider=provider, channel=provider)
            if provider == "wechat_clawbot":
                for key in ("target", "wechat_clawbot_label", "wechat_clawbot_state_dir"):
                    notifications[key] = values[key]
                inbound = section(section(out, "inbound"), "wechat_clawbot")
                inbound.update(label=values["wechat_clawbot_label"], state_dir=values["wechat_clawbot_state_dir"])
                if "allowed_senders" in values:
                    inbound["allowed_senders"] = values["allowed_senders"]
            else:
                for key in ("target", "wechat_clawbot_label", "wechat_clawbot_state_dir"):
                    notifications.pop(key, None)
    elif feature == "close-advice":
        section(out, "close_advice")["enabled"] = enabled
    elif feature == "portfolio-management":
        section(out, "portfolio_management")["enabled"] = enabled
    else:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported feature: {feature}")
    return out


def validate_channel_connection_setup(config_doc: dict[str, Any], provider: str) -> None:
    """The single-target form cannot replace explicit routes or another platform."""
    notifications = config_doc.get("notifications") or {}
    if "routes" in notifications:
        raise AgentToolError(code="INPUT_ERROR", message="channel connection setup cannot edit notifications.routes; configure those routes explicitly")
    selected = notifications.get("provider") or notifications.get("channel")
    if selected and canonical_channel(selected) != canonical_channel(provider):
        raise AgentToolError(code="INPUT_ERROR", message="channel connection setup would replace another platform's route; configure notifications.routes explicitly")


def legacy_channel_receivers(*, config_path: Path, runtime_root: Path) -> tuple[set[str], dict[str, Any] | None]:
    """Read only the bound instance's explicit service choices during migration."""
    from src.application.service_deploy import load_service_profile

    path = runtime_root / "service.profile.json"
    if not path.exists():
        return set(), None
    revision = file_sha256(path)
    profile = load_service_profile(path)
    bound_root = Path(str(profile.get("runtime_root") or "")).expanduser()
    authoring = profile.get("config_authoring") or {}
    bound_source = authoring.get("config_yaml")
    if (not bound_root.is_absolute() or bound_root.resolve() != runtime_root.resolve()
            or (bound_source and Path(bound_source).expanduser().resolve() != config_path.resolve())):
        raise AgentToolError(code="CONFIG_ERROR", message="legacy channel service profile is not bound to this authoring/runtime target")
    if file_sha256(path) != revision:
        raise AgentToolError(code="STALE_PREVIEW", message="legacy channel service profile changed while reading")
    names = {(item.get("name") or item.get("label")) if isinstance(item, dict) else item
             for item in profile.get("services", [])}
    selected = set()
    for channel, key, units in (
        ("feishu", "feishu_ws", {"options-monitor-feishu-ws.service", "com.options-monitor.feishu-ws"}),
        ("wechat_clawbot", "wechat_clawbot", {"options-monitor-wechat-clawbot.service", "com.options-monitor.wechat-clawbot"}),
    ):
        enabled = (profile.get(key) or {}).get("enabled")
        if enabled is True or (enabled is not False and names & units):
            selected.add(channel)
    return selected, {"path": str(path), "sha256": revision, "receive_channels": sorted(selected)}


def _legacy_channel_controls(config_doc: dict[str, Any], *, legacy_receive_channels: set[str] | None = None) -> dict[str, dict[str, bool]]:
    """Freeze explicit legacy intent, never infer activation from credentials."""
    from domain.domain.multi_tick import resolve_notification_route_from_config

    inbound = config_doc.get("inbound") or {}
    controls = {}
    for channel in CHANNEL_PACKAGES:
        settings = inbound.get("feishu_ws" if channel == "feishu" else channel) or {}
        receive = (bool(settings) or channel in (legacy_receive_channels or set())) and settings.get("enabled") is not False
        controls[channel] = {"enabled": receive, "receive_enabled": receive, "notify_enabled": False}
    notifications_root = config_doc.get("notifications") or {}
    scopes = [notifications_root, *(deep_merge_config(notifications_root, scope.get("notifications") or {})
                                   for scope in (config_doc.get("markets") or {}).values())]
    for notifications in scopes:
        if notifications.get("enabled") is not True:
            continue
        routes = notifications.get("routes") if "routes" in notifications else [
            resolve_notification_route_from_config(config={"notifications": notifications})]
        for route in routes:
            try:
                channel = canonical_channel(route.get("provider", ""))
            except ValueError:
                continue
            controls[channel].update(enabled=True, notify_enabled=True)
    return controls
