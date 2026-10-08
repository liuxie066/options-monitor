"""Preview and publish the optional Portfolio Exposure Holdings setting."""

from __future__ import annotations

import json
from hashlib import sha256
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_primitives import configured_markets, resolve_config_path
from src.application.config_yaml import default_yaml_config_path, load_yaml_config_file, resolve_yaml_runtime_config
from src.application.portfolio_assignment_scenario import (
    non_futu_broker_inventory,
    read_portfolio_valuation_evidence,
)
from src.application.write_contract import attach_write_contract
from src.application.portfolio_management import portfolio_management_enabled


def holdings_included(config: dict[str, Any]) -> bool:
    portfolio = config.get("portfolio")
    holdings = portfolio.get("holdings") if isinstance(portfolio, dict) else None
    return holdings.get("enabled") is True if isinstance(holdings, dict) else False


def _probe_holdings(config: dict[str, Any], *, service_url: str | None = None) -> dict[str, Any]:
    configured = config.get("accounts")
    if not isinstance(configured, list) or not configured:
        raise ValueError("OM configured accounts are unavailable")
    candidate_accounts = sorted(
        {account.strip().lower() for account in configured if isinstance(account, str) and account.strip()}
    )
    if not candidate_accounts:
        raise ValueError("OM configured accounts are unavailable")
    client = None
    if service_url is not None:
        from src.application.portfolio_management import portfolio_management_enabled
        from src.infrastructure.portfolio_management_client import PortfolioManagementClient
        if not portfolio_management_enabled(config):
            raise ValueError("portfolio_management.enabled is false")
        client = PortfolioManagementClient(service_url=service_url)
    evidence = read_portfolio_valuation_evidence(
        accounts=candidate_accounts,
        supplemental_codes=[],
        price_timeout=10,
        runtime_config=config,
        holdings_scope="non_futu",
        client=client,
    )
    inventory = non_futu_broker_inventory(evidence, candidate_accounts)
    quality = evidence.get("freshness")
    if (
        evidence.get("status") != "complete"
        or evidence.get("warnings")
        or not isinstance(quality, dict)
        or quality.get("status") != "fresh"
        or quality.get("trust_status") != "trusted"
        or any(item.get("status") != "complete" for item in evidence.get("account_status", []))
        or any(
            any(row["classification"] == "unknown" for row in inventory[account])
            or evidence["scope"]["holding_counts"][account]["unsupported"]
            for account in candidate_accounts
        )
    ):
        raise ValueError("PM Holdings valuation evidence is incomplete or stale")
    rows = evidence["holdings"]
    approved = {
        account: sorted(row["broker"] for row in inventory[account] if row["classification"] == "non_futu")
        for account in candidate_accounts
    }
    return {
        "status": "ready_observed" if rows else "ready_empty",
        "scope": "non_futu",
        "accounts_observed": candidate_accounts,
        "broker_inventory": inventory,
        "holding_counts": evidence["scope"]["holding_counts"],
        "approved_non_futu_brokers": approved,
        "eligible_rows": len(rows),
        "source_observed_at": quality.get("observed_at_utc"),
        "warnings": [],
    }


def _preview_sha256(transaction: dict[str, Any], *, enabled: bool) -> str:
    identity = {
        "config_yaml_path": transaction["config_yaml_path"],
        "runtime_root": transaction["runtime_root"],
        "source_revision": transaction["source_revision"],
        "enabled": enabled,
        "approved_non_futu_brokers": transaction.get("approved_non_futu_brokers"),
        "service_url": transaction.get("service_url"),
        "markets": {market: item["output_config_path"] for market, item in transaction["markets"].items()},
        "bot": transaction["bot"]["output_config_path"],
    }
    return sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def _readback_generation(transaction: dict[str, Any], *, enabled: bool) -> list[str]:
    target = Path(transaction["config_yaml_path"])
    verified: list[str] = []

    def require_sha(expected: str) -> bytes:
        payload = target.read_bytes()
        if sha256(payload).hexdigest() != expected:
            raise ValueError("published content differs from prepared generation")
        verified.append(str(target))
        return payload

    try:
        require_sha(transaction["source_revision"]["after_sha256"])
        authored = load_yaml_config_file(target)
        if holdings_included(authored) != enabled or (enabled and authored["portfolio"]["holdings"].get("approved_non_futu_brokers") != transaction.get("approved_non_futu_brokers")):
            raise ValueError("Holdings differs in config.yaml after apply")
        for item in transaction["markets"].values():
            target = Path(item["output_config_path"])
            runtime = json.loads(require_sha(item["sha256"]).decode("utf-8"))
            if holdings_included(runtime) != enabled or (enabled and runtime["portfolio"]["holdings"].get("approved_non_futu_brokers") != transaction.get("approved_non_futu_brokers")):
                raise ValueError("Holdings differs in market runtime config after apply")
        bot_config = transaction["bot"]
        target = Path(bot_config["output_config_path"])
        json.loads(require_sha(bot_config["sha256"]).decode("utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_READBACK_FAILED",
            message="config generation was published but readback failed",
            details={
                "write_applied": True,
                "audit_id": transaction["audit_id"],
                "backup_path": transaction["backup_path"],
                "target": str(target),
                "reason": str(exc),
            },
            hint="Inspect the published targets and backup before retrying the authoring command.",
        ) from exc
    return verified


def set_yaml_holdings_inclusion(
    *,
    repo_root: Path,
    enabled: bool,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    apply: bool = False,
    confirm: bool = False,
    expected_source_sha256: str | None = None,
    expected_preview_sha256: str | None = None,
    service_url: str | None = None,
    enable_pm: bool = False,
) -> dict[str, Any]:
    if service_url is not None:
        from src.infrastructure.portfolio_management_client import PortfolioManagementConfigError, resolve_portfolio_service_origin
        try:
            service_url = resolve_portfolio_service_origin(service_url)
        except PortfolioManagementConfigError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    source = resolve_config_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    before_sha = config_source_sha256(source)
    if apply and (not confirm or expected_source_sha256 != before_sha):
        raise AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message="apply requires --confirm and the before_sha256 from a current preview",
        )
    doc = deepcopy(load_yaml_config_file(source))
    current_enabled = holdings_included(doc)
    pm_before = portfolio_management_enabled(doc)
    if enabled and enable_pm:
        pm = doc.setdefault("portfolio_management", {})
        if not isinstance(pm, dict):
            raise AgentToolError(code="CONFIG_ERROR", message="portfolio_management must be an object")
        pm["enabled"] = True
    pm_after = portfolio_management_enabled(doc)
    portfolio = doc.setdefault("portfolio", {})
    if not isinstance(portfolio, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="portfolio must be an object")
    holdings = portfolio.setdefault("holdings", {})
    if not isinstance(holdings, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="portfolio.holdings must be an object")
    holdings["enabled"] = enabled
    markets = configured_markets(doc)
    target_root = Path(runtime_root).expanduser().resolve() if runtime_root else source.parent

    preflight = None
    if enabled:
        current, _meta = resolve_yaml_runtime_config(repo_root=repo_root, market=markets[0], config_path=source)
        current["accounts"] = list(doc.get("accounts") or {})
        if enabled and enable_pm:
            current["portfolio_management"] = deepcopy(doc["portfolio_management"])
        try:
            preflight = _probe_holdings(current, service_url=service_url) if service_url is not None else _probe_holdings(current)
            approved = preflight["approved_non_futu_brokers"]
            if set(approved) != set(current["accounts"]):
                raise ValueError("PM broker approval does not cover all configured accounts")
            holdings["approved_non_futu_brokers"] = approved
        except Exception as exc:
            preflight = {"status": "failed", "reason": str(exc)}
            if apply:
                raise AgentToolError(
                    code="HOLDINGS_PREFLIGHT_FAILED",
                    message="Holdings source is not ready; inclusion remains unchanged",
                    details={"reason": str(exc), "write_applied": False},
                ) from exc

    preview = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=source,
        config_doc=doc,
        runtime_root=target_root,
        markets=markets,
        include_bot=True,
        apply=False,
        backup=True,
        expected_source_sha256=before_sha,
    )
    preview["approved_non_futu_brokers"] = holdings.get("approved_non_futu_brokers") if enabled else None
    if service_url is not None:
        preview["service_url"] = service_url
    preview_sha = _preview_sha256(preview, enabled=enabled)
    if apply and expected_preview_sha256 != preview_sha:
        raise AgentToolError(
            code="STALE_PREVIEW",
            message="apply target differs from the confirmed Holdings preview",
            details={"expected_preview_sha256": expected_preview_sha256, "actual_preview_sha256": preview_sha},
        )

    transaction = preview
    if apply:
        transaction = publish_yaml_config_generation(
            repo_root=repo_root,
            config_yaml_path=source,
            config_doc=doc,
            runtime_root=target_root,
            markets=markets,
            include_bot=True,
            apply=True,
            backup=True,
            expected_source_sha256=before_sha,
        )
        transaction["approved_non_futu_brokers"] = holdings.get("approved_non_futu_brokers") if enabled else None
        verified_targets = _readback_generation(transaction, enabled=enabled)
    else:
        verified_targets = []
    return attach_write_contract(
        {
            "ok": True,
            "product": "Portfolio Exposure",
            "setting": "portfolio.holdings.enabled",
            "enabled": enabled,
            "current_enabled": current_enabled,
            "target_enabled": enabled,
            "effect_scope": "assignment_scenario",
            "effect_note": "When enabled, assignment distribution includes PM Holdings from non-Futu brokers; Futu stocks, cash and MMF come from OpenD.",
            "preview_sha256": preview_sha,
            "service_url": preview.get("service_url"),
            "preflight": preflight,
            "pm_dependency": {"configured_before": pm_before, "configured_after": pm_after},
            "config_yaml_path": str(source),
            "runtime_root": str(target_root),
            "source_revision": transaction["source_revision"],
            "validation": transaction["markets"],
            "bot": transaction["bot"],
            "verified_targets": verified_targets,
        },
        dry_run=not apply,
        write_applied=apply,
        backup_path=transaction["backup_path"],
        audit_id=transaction["audit_id"],
        generate_audit_id=False,
        rollback_hint=(
            f"先将 {transaction['backup_path']} 恢复到 {source}，再用 om config build 和 "
            "om config build-bot 重建 validation/bot 列出的全部目标并读回。"
            if apply
            else None
        ),
    )
