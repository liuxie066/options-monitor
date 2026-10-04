"""Small authoring boundary for human feature configuration."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import publish_yaml_config_generation
from src.application.config_primitives import configured_markets


def publish_feature_document(*, repo_root: Path, config_path: Path, config_doc: dict[str, Any],
                             runtime_root: Path | None = None, apply: bool = False,
                             expected_source_sha256: str | None = None) -> dict[str, Any]:
    """Publish one generation; callers bind the pre-prompt source revision."""
    transaction = publish_yaml_config_generation(
        repo_root=repo_root, config_yaml_path=config_path, config_doc=config_doc,
        runtime_root=runtime_root or config_path.parent, markets=configured_markets(config_doc),
        include_assistant=True, apply=apply, backup=True,
        expected_source_sha256=expected_source_sha256,
    )
    verified = []
    if apply:
        targets = [(config_path, transaction["source_revision"]["after_sha256"])]
        targets.extend((Path(item["output_config_path"]), item["sha256"])
                       for item in transaction["markets"].values())
        targets.append((Path(transaction["assistant"]["output_config_path"]), transaction["assistant"]["sha256"]))
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


def feature_document(config_doc: dict[str, Any], *, feature: str, enabled: bool,
                     values: dict[str, Any] | None = None) -> dict[str, Any]:
    out = deepcopy(config_doc)
    values = dict(values or {})
    def section(parent: dict, key: str) -> dict:
        value = parent.setdefault(key, {})
        if not isinstance(value, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"{key} must be an object")
        return value
    if feature == "bot":
        assistant = section(out, "assistant")
        assistant["enabled"] = enabled
        section(assistant, "bot")["enabled"] = enabled
    elif feature == "channel":
        notifications = section(out, "notifications")
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
