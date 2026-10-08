"""Explicit retirement of inert authoring fields; never runs during config loading."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256
from src.application.config_defaults import default_config_sha256
from src.application.config_features import publish_feature_document
from src.application.config_yaml import default_yaml_config_path, load_yaml_config_file


def migrate_switch_document(source: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    doc = deepcopy(source)
    changes: list[dict[str, Any]] = []

    def section(parent: dict, name: str, path: str) -> dict:
        value = parent.get(name, {})
        if not isinstance(value, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
        return value

    def remove(parent: dict, key: str, path: str, valid) -> None:
        if key not in parent:
            return
        value = parent[key]
        if not valid(value):
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message=f"{path} has an unrecognized legacy value")
        del parent[key]
        changes.append({"path": path, "before": value, "action": "remove_inert_field"})

    if "assistant" in doc:
        if "bot" in doc:
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message="both assistant and bot are configured; resolve the duplicate explicitly")
        assistant = section(doc, "assistant", "assistant")
        if "copilot" in assistant:
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message="assistant.copilot requires om bot migrate --dry-run first; switch migration does not migrate Bot storage")
        nested = section(assistant, "bot", "assistant.bot")
        remove(nested, "toolsets", "assistant.bot.toolsets", lambda v: isinstance(v, dict) and set(v) <= {"portfolio"} and all(isinstance(x, bool) for x in v.values()))
        remove(nested, "tool_loading_mode", "assistant.bot.tool_loading_mode", lambda v: v in ("eager", "directory"))
        if set(nested) - {"enabled", "read_markets"}:
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message="assistant.bot contains unsupported legacy fields")
        for parent, path in ((assistant, "assistant"), (nested, "assistant.bot")):
            if "enabled" in parent and not isinstance(parent["enabled"], bool):
                raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message=f"{path}.enabled must be a boolean")
        if "read_markets" in assistant and "read_markets" in nested:
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message="duplicate legacy read_markets configuration")
        enabled = assistant.get("enabled", True) and nested.get("enabled", False)
        unified = {key: value for key, value in assistant.items() if key not in {"enabled", "bot"}}
        unified.update({key: value for key, value in nested.items() if key != "enabled"})
        unified["enabled"] = enabled
        changes.append({"path": "assistant", "action": "unify_bot_config", "after_path": "bot",
                        "enabled_before": {"assistant": assistant.get("enabled", True), "bot": nested.get("enabled", False)},
                        "enabled_after": enabled})
        del doc["assistant"]
        doc["bot"] = unified
    accounts = section(doc, "accounts", "accounts")
    for account, value in accounts.items():
        if isinstance(value, dict) and "holdings_account" in value:
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message=f"accounts.{account}.holdings_account is retired; verify account identity and remove it explicitly")
    trade_intake = section(doc, "trade_intake", "trade_intake")
    combo = section(trade_intake, "combo_reconciliation", "trade_intake.combo_reconciliation")
    remove(combo, "default_mode", "trade_intake.combo_reconciliation.default_mode", lambda v: v == "off" or v is False)
    markets = section(doc, "markets", "markets")
    scopes = [("", doc)] + [(f"markets.{market}.", value) for market, value in markets.items() if isinstance(value, dict)]
    for prefix, scope in scopes:
        features = section(scope, "features", prefix + "features")
        wheel = section(features, "wheel", prefix + "features.wheel")
        remove(wheel, "enabled", prefix + "features.wheel.enabled", lambda v: isinstance(v, bool))
        notifications = section(scope, "notifications", prefix + "notifications")
        if "enabled" in notifications and not isinstance(notifications["enabled"], bool):
            raise AgentToolError(code="CONFIG_MIGRATION_CONFLICT", message=prefix + "notifications.enabled must be a boolean")
        daily = section(notifications, "daily_brief", prefix + "notifications.daily_brief")
        remove(daily, "enabled", prefix + "notifications.daily_brief.enabled", lambda v: isinstance(v, bool))
    # Legacy omission meant true; market overrides inherit this explicit root value.
    notifications = doc.setdefault("notifications", {})
    if "enabled" not in notifications:
        notifications["enabled"] = True
        changes.append({"path": "notifications.enabled", "before": None, "after": True,
                        "action": "preserve_legacy_effective_value"})
    return doc, changes


def migrate_yaml_switches(*, repo_root: Path, config_path: str | Path | None = None,
                         runtime_root: str | Path | None = None, apply: bool = False,
                         confirm: bool = False, expected_source_sha256: str | None = None,
                         expected_preview_sha256: str | None = None) -> dict[str, Any]:
    source = Path(config_path or default_yaml_config_path(repo_root=repo_root)).expanduser().resolve()
    root = Path(runtime_root).expanduser().resolve() if runtime_root else source.parent
    before = config_source_sha256(source)
    candidate, changes = migrate_switch_document(load_yaml_config_file(source))
    if expected_source_sha256 is not None and expected_source_sha256 != before:
        raise AgentToolError(code="STALE_PREVIEW", message="authoring source changed after migration preview")
    transaction = publish_feature_document(repo_root=repo_root, config_path=source, config_doc=candidate,
                                           runtime_root=root, expected_source_sha256=before)
    identity = {"source": str(source), "runtime_root": str(root), "revision": transaction["source_revision"],
                "defaults": default_config_sha256(), "changes": changes}
    preview_sha = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if apply:
        if not confirm or not expected_source_sha256 or not expected_preview_sha256:
            raise AgentToolError(code="CONFIRMATION_REQUIRED", message="migration apply requires --confirm and both hashes from preview")
        if preview_sha != expected_preview_sha256:
            raise AgentToolError(code="STALE_PREVIEW", message="migration differs from confirmed preview")
        transaction = publish_feature_document(repo_root=repo_root, config_path=source, config_doc=candidate,
                                               runtime_root=root, apply=True, expected_source_sha256=before)
    return {"ok": True, "operation": "migrate_switches", "changes": changes, "changed": bool(changes),
            "preview_sha256": preview_sha, **transaction}
