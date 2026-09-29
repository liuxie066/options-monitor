"""Preview and publish the optional Portfolio Exposure Holdings setting."""

from __future__ import annotations

import json
from hashlib import sha256
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from domain.domain.symbol_identity import symbol_market
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_primitives import configured_markets, resolve_config_path
from src.application.config_yaml import default_yaml_config_path, load_yaml_config_file, resolve_yaml_runtime_config
from src.application.portfolio_management import (
    PORTFOLIO_MANAGEMENT_DISABLED,
    resolve_portfolio_management_client,
)
from src.application.portfolio_assignment_scenario import read_portfolio_valuation_evidence
from src.application.write_contract import attach_write_contract


def holdings_included(config: dict[str, Any]) -> bool:
    portfolio = config.get("portfolio")
    holdings = portfolio.get("holdings") if isinstance(portfolio, dict) else None
    return holdings.get("enabled") is True if isinstance(holdings, dict) else False


def _probe_holdings(config: dict[str, Any]) -> dict[str, Any]:
    client = resolve_portfolio_management_client(config, urlopen_fn=urllib.request.urlopen)
    if client == PORTFOLIO_MANAGEMENT_DISABLED:
        raise ValueError("portfolio_management.enabled is false")
    response = client.read_view("accounts", query={"include_default": "false"}, timeout=10.0)
    freshness = response.get("freshness")
    accounts = response.get("accounts")
    if response.get("success") is not True or not isinstance(accounts, list) or not accounts:
        raise ValueError("PM account discovery has no usable accounts")
    if (
        not isinstance(freshness, dict)
        or freshness.get("status") != "fresh"
        or freshness.get("trust_status") != "trusted"
    ):
        raise ValueError("PM account discovery is stale or untrusted")
    warnings = response.get("warnings")
    if isinstance(warnings, list) and any(
        isinstance(item, dict) and item.get("source") == "holdings" for item in warnings
    ):
        raise ValueError("PM Holdings account discovery reported a holdings warning")
    candidate_accounts = sorted({str(account).strip().lower() for account in accounts if str(account).strip()})
    if not candidate_accounts:
        raise ValueError("PM account discovery has no usable accounts")
    evidence = read_portfolio_valuation_evidence(
        accounts=candidate_accounts,
        supplemental_codes=[],
        price_timeout=10,
        client=client,
        runtime_config=config,
    )
    quality = evidence.get("freshness")
    if (
        evidence.get("status") != "complete"
        or evidence.get("warnings")
        or not isinstance(quality, dict)
        or quality.get("status") != "fresh"
        or quality.get("trust_status") != "trusted"
        or any(item.get("status") != "complete" for item in evidence.get("account_status", []))
    ):
        raise ValueError("PM Holdings valuation evidence is incomplete or stale")
    rows = [row for row in evidence.get("holdings", []) if isinstance(row, dict)]
    observed_accounts = sorted(
        {str(row.get("account") or "").strip().lower() for row in rows if str(row.get("account") or "").strip()}
    )
    if not observed_accounts:
        raise ValueError("PM Holdings valuation evidence has no observed holdings")
    return {
        "status": "ready_observed",
        "scope": "observed_only",
        "accounts_observed": observed_accounts,
        "brokers_observed": sorted(
            {str(row.get("broker") or "").strip() for row in rows if str(row.get("broker") or "").strip()}
        ),
        "markets_observed": sorted(
            {str(market).upper() for row in rows if (market := symbol_market(str(row.get("code") or "")))}
        ),
        "source_observed_at": quality.get("observed_at_utc"),
        "warnings": [],
    }


def _preview_sha256(transaction: dict[str, Any], *, enabled: bool) -> str:
    identity = {
        "config_yaml_path": transaction["config_yaml_path"],
        "runtime_root": transaction["runtime_root"],
        "source_revision": transaction["source_revision"],
        "enabled": enabled,
        "markets": {market: item["output_config_path"] for market, item in transaction["markets"].items()},
        "assistant": transaction["assistant"]["output_config_path"],
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
        if holdings_included(load_yaml_config_file(target)) != enabled:
            raise ValueError("Holdings differs in config.yaml after apply")
        for item in transaction["markets"].values():
            target = Path(item["output_config_path"])
            runtime = json.loads(require_sha(item["sha256"]).decode("utf-8"))
            if holdings_included(runtime) != enabled:
                raise ValueError("Holdings differs in market runtime config after apply")
        assistant = transaction["assistant"]
        target = Path(assistant["output_config_path"])
        json.loads(require_sha(assistant["sha256"]).decode("utf-8"))
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
) -> dict[str, Any]:
    source = resolve_config_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    before_sha = config_source_sha256(source)
    if apply and (not confirm or expected_source_sha256 != before_sha):
        raise AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message="apply requires --confirm and the before_sha256 from a current preview",
        )
    doc = deepcopy(load_yaml_config_file(source))
    current_enabled = holdings_included(doc)
    portfolio = doc.setdefault("portfolio", {})
    if not isinstance(portfolio, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="portfolio must be an object")
    holdings = portfolio.setdefault("holdings", {})
    if not isinstance(holdings, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="portfolio.holdings must be an object")
    holdings["enabled"] = enabled
    markets = configured_markets(doc)
    target_root = Path(runtime_root).expanduser().resolve() if runtime_root else source.parent

    preview = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=source,
        config_doc=doc,
        runtime_root=target_root,
        markets=markets,
        include_assistant=True,
        apply=False,
        backup=True,
        expected_source_sha256=before_sha,
    )
    preview_sha = _preview_sha256(preview, enabled=enabled)
    if apply and expected_preview_sha256 != preview_sha:
        raise AgentToolError(
            code="STALE_PREVIEW",
            message="apply target differs from the confirmed Holdings preview",
            details={"expected_preview_sha256": expected_preview_sha256, "actual_preview_sha256": preview_sha},
        )

    preflight = None
    if enabled:
        current, _meta = resolve_yaml_runtime_config(
            repo_root=repo_root,
            market=markets[0],
            config_path=source,
        )
        try:
            preflight = _probe_holdings(current)
        except Exception as exc:
            preflight = {"status": "failed", "reason": str(exc)}
            if apply:
                raise AgentToolError(
                    code="HOLDINGS_PREFLIGHT_FAILED",
                    message="Holdings source is not ready; inclusion remains unchanged",
                    details={"reason": str(exc), "write_applied": False},
                ) from exc

    transaction = preview
    if apply:
        transaction = publish_yaml_config_generation(
            repo_root=repo_root,
            config_yaml_path=source,
            config_doc=doc,
            runtime_root=target_root,
            markets=markets,
            include_assistant=True,
            apply=True,
            backup=True,
            expected_source_sha256=before_sha,
        )
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
            "effect_scope": "configuration_only",
            "effect_note": "The current assignment-scenario query does not consume this setting.",
            "preview_sha256": preview_sha,
            "preflight": preflight,
            "config_yaml_path": str(source),
            "runtime_root": str(target_root),
            "source_revision": transaction["source_revision"],
            "validation": transaction["markets"],
            "assistant": transaction["assistant"],
            "verified_targets": verified_targets,
        },
        dry_run=not apply,
        write_applied=apply,
        backup_path=transaction["backup_path"],
        audit_id=transaction["audit_id"],
        generate_audit_id=False,
        rollback_hint=(
            f"先将 {transaction['backup_path']} 恢复到 {source}，再用 om config build 和 "
            "om config build-assistant 重建 validation/assistant 列出的全部目标并读回。"
            if apply
            else None
        ),
    )
