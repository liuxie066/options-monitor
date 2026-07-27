from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import (
    normalize_account_label,
    normalize_portfolio_source,
    portfolio_account_identity_hash,
)
from src.application.account_config import (
    accounts_from_config,
    build_account_config_view,
)
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.position_advice_authority_service import (
    build_identity_binding_evidence,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    sha256_bytes,
    validate_source_receipt,
)


class PositionAdviceIdentityBindingError(RuntimeError):
    """Raised when first-use authority identity cannot be proven end to end."""

    def __init__(
        self,
        message: str,
        *,
        intent_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.intent_evidence = dict(intent_evidence or {})


def build_first_use_identity_binding_from_runtime(
    *,
    repo_root: Path,
    runtime_root: Path,
    normalized_account: str,
    config_yaml_path: Path | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Bind authoring, generated runtime, and latest fresh source identities."""

    try:
        return _build_first_use_identity_binding_from_runtime(
            repo_root=repo_root,
            runtime_root=runtime_root,
            normalized_account=normalized_account,
            config_yaml_path=config_yaml_path,
            now=now,
        )
    except PositionAdviceIdentityBindingError as exc:
        intent = _collect_failed_binding_intent(
            repo_root=repo_root,
            runtime_root=runtime_root,
            normalized_account=normalized_account,
            config_yaml_path=config_yaml_path,
            now=now,
            failure_detail=str(exc),
        )
        raise PositionAdviceIdentityBindingError(
            str(exc),
            intent_evidence=intent,
        ) from exc


def _build_first_use_identity_binding_from_runtime(
    *,
    repo_root: Path,
    runtime_root: Path,
    normalized_account: str,
    config_yaml_path: Path | None,
    now: datetime | str | None,
) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    runtime = Path(runtime_root).resolve()
    account = normalize_account_label(normalized_account)
    config_yaml = (
        Path(config_yaml_path).expanduser().resolve()
        if config_yaml_path is not None
        else (runtime / "config.yaml").resolve()
    )
    if not config_yaml.is_file() or config_yaml.is_symlink():
        raise PositionAdviceIdentityBindingError(
            f"canonical config.yaml is unavailable: {config_yaml}"
        )
    checked_at = _datetime(now or datetime.now(timezone.utc))
    market_bindings: list[dict[str, Any]] = []
    common_source: str | None = None
    common_identity: str | None = None
    for market in ("us", "hk"):
        authored, _meta = resolve_yaml_runtime_config(
            repo_root=repo,
            market=market,
            config_path=config_yaml,
        )
        if account not in accounts_from_config(authored, fallback=()):
            continue
        runtime_path = runtime / f"config.{market}.json"
        generated = _read_json_object(runtime_path, "generated runtime config")
        authored_view = _identity_view(authored, account=account)
        generated_view = _identity_view(generated, account=account)
        if authored_view != generated_view:
            raise PositionAdviceIdentityBindingError(
                f"{market.upper()} generated account identity differs from config.yaml"
            )
        source = normalize_portfolio_source(authored_view["portfolio_source"])
        identity = portfolio_account_identity_hash(
            normalized_portfolio_source=source,
            broker_account_identifiers=authored_view["account_identifiers"],
        )
        summary, portfolio_receipt_hash = _latest_fresh_source_summary(
            runtime_root=runtime,
            account=account,
            market=market.upper(),
            now=checked_at,
        )
        if (
            normalize_portfolio_source(
                summary.get("normalized_portfolio_source")
            )
            != source
            or summary.get("portfolio_account_identity_hash") != identity
        ):
            raise PositionAdviceIdentityBindingError(
                f"{market.upper()} source receipt identity differs from config.yaml"
            )
        if common_source is not None and (
            source != common_source or identity != common_identity
        ):
            raise PositionAdviceIdentityBindingError(
                "enabled markets disagree on portfolio identity"
            )
        common_source = source
        common_identity = identity
        market_bindings.append(
            {
                "market": market.upper(),
                "generated_config_hash": sha256_bytes(
                    runtime_path.read_bytes()
                ),
                "source_receipt_hash": portfolio_receipt_hash,
                "normalized_account": account,
                "normalized_portfolio_source": source,
                "portfolio_account_identity_hash": identity,
                "source_receipt_fresh": True,
            }
        )
    if not market_bindings or common_source is None or common_identity is None:
        raise PositionAdviceIdentityBindingError(
            "account has no enabled market with a complete identity binding"
        )
    evidence = build_identity_binding_evidence(
        normalized_account=account,
        normalized_portfolio_source=common_source,
        portfolio_account_identity_hash=common_identity,
        authoring_config_hash=sha256_bytes(config_yaml.read_bytes()),
        market_bindings=market_bindings,
    )
    return {
        "normalized_portfolio_source": common_source,
        "portfolio_account_identity_hash": common_identity,
        "identity_binding_evidence": evidence,
    }


def _collect_failed_binding_intent(
    *,
    repo_root: Path,
    runtime_root: Path,
    normalized_account: str,
    config_yaml_path: Path | None,
    now: datetime | str | None,
    failure_detail: str,
) -> dict[str, Any]:
    """Return read-only audit evidence for a rejected first-use attempt."""

    repo = Path(repo_root).resolve()
    runtime = Path(runtime_root).resolve()
    account = str(normalized_account or "").strip().lower()
    config_yaml = (
        Path(config_yaml_path).expanduser().resolve()
        if config_yaml_path is not None
        else (runtime / "config.yaml").resolve()
    )
    checked_at = _datetime(now or datetime.now(timezone.utc)).isoformat()
    market_bindings: list[dict[str, Any]] = []
    enabled_markets: list[str] = []
    for market in ("us", "hk"):
        enabled = False
        try:
            authored, _meta = resolve_yaml_runtime_config(
                repo_root=repo,
                market=market,
                config_path=config_yaml,
            )
            enabled = account in accounts_from_config(authored, fallback=())
        except (OSError, TypeError, ValueError):
            enabled = False
        if not enabled:
            continue
        market_name = market.upper()
        enabled_markets.append(market_name)
        market_bindings.append(
            {
                "market": market_name,
                "generated_config_hash": _regular_file_hash(
                    runtime / f"config.{market}.json"
                ),
                "source_receipt_hashes": _source_receipt_hashes_for_market(
                    runtime_root=runtime,
                    account=account,
                    market=market_name,
                ),
                "binding_result": "failed",
            }
        )
    payload = {
        "schema_version": (
            "position_advice_first_use_identity_binding_attempt.v1"
        ),
        "normalized_account": account,
        "authoring_config_hash": _regular_file_hash(config_yaml),
        "enabled_markets": sorted(enabled_markets),
        "market_bindings": sorted(
            market_bindings,
            key=lambda item: item["market"],
        ),
        "binding_result": "failed",
        "reason_codes": ["first_use_identity_binding_failed"],
        "failure_detail": str(failure_detail),
        "checked_at": checked_at,
    }
    return {**payload, "intent_evidence_hash": canonical_sha256(payload)}


def _regular_file_hash(path: Path) -> str | None:
    target = Path(path)
    try:
        if not target.is_file() or target.is_symlink():
            return None
        return sha256_bytes(target.read_bytes())
    except OSError:
        return None


def _source_receipt_hashes_for_market(
    *,
    runtime_root: Path,
    account: str,
    market: str,
) -> list[str]:
    hashes: set[str] = set()
    pattern = (
        f"output_runs/*/accounts/{account}/state/"
        "position_advice_sources.v2.json"
    )
    for path in runtime_root.glob(pattern):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(summary, dict)
                or market
                not in {
                    str(item).upper()
                    for item in summary.get("included_markets") or []
                }
            ):
                continue
            for item in summary.get("source_receipts") or []:
                if (
                    isinstance(item, Mapping)
                    and item.get("source_kind") == "portfolio"
                ):
                    receipt_hash = str(item.get("receipt_hash") or "").strip()
                    if len(receipt_hash) == 64:
                        hashes.add(receipt_hash)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(hashes)


def _identity_view(config: Mapping[str, Any], *, account: str) -> dict[str, Any]:
    if account not in accounts_from_config(dict(config), fallback=()):
        raise PositionAdviceIdentityBindingError(
            f"account is absent from generated config: {account}"
        )
    view = build_account_config_view(dict(config), account=account)
    source = normalize_portfolio_source(
        view.portfolio_source_plan.primary_source
    )
    if source == "futu":
        identifiers = sorted(
            {str(item).strip() for item in view.futu_acc_ids if str(item).strip()}
        )
    else:
        identifiers = [
            str(view.holdings_account or "").strip()
        ]
        identifiers = [item for item in identifiers if item]
    if not identifiers:
        raise PositionAdviceIdentityBindingError(
            f"account identity identifiers are unavailable: {account}"
        )
    return {
        "account": account,
        "account_type": view.account_type,
        "portfolio_source": source,
        "account_identifiers": identifiers,
    }


def _latest_fresh_source_summary(
    *,
    runtime_root: Path,
    account: str,
    market: str,
    now: datetime,
) -> tuple[dict[str, Any], str]:
    candidates: list[tuple[str, dict[str, Any], str]] = []
    pattern = (
        f"output_runs/*/accounts/{account}/state/"
        "position_advice_sources.v2.json"
    )
    for path in runtime_root.glob(pattern):
        try:
            summary = _read_json_object(path, "position advice source summary")
            account_run_id = str(summary.get("account_run_id") or "").strip()
            if (
                summary.get("schema_version")
                != "position_advice_account_sources.v2"
                or not account_run_id
                or path.parents[3].name != account_run_id
                or summary.get("account") != account
                or market not in {
                    str(item).upper()
                    for item in summary.get("included_markets") or []
                }
            ):
                continue
            portfolio = next(
                dict(item)
                for item in summary.get("source_receipts") or []
                if isinstance(item, Mapping)
                and item.get("source_kind") == "portfolio"
            )
            receipt_path = Path(str(portfolio.get("receipt_path") or "")).resolve()
            producer_root = Path(
                str(portfolio.get("producer_root") or "")
            ).resolve()
            expected_producer_root = path.parent.resolve()
            if (
                producer_root != expected_producer_root
                or receipt_path.parent != producer_root
                or receipt_path.is_symlink()
            ):
                raise PositionAdviceIdentityBindingError(
                    "portfolio source receipt escapes its account run"
                )
            receipt = _read_json_object(receipt_path, "portfolio source receipt")
            validated = validate_source_receipt(
                receipt,
                producer_root=producer_root,
                now=now,
                require_fresh=True,
                expected_source_kind="portfolio",
                expected_account=account,
                expected_identity_hash=str(
                    summary.get("portfolio_account_identity_hash") or ""
                ),
                expected_producer_account_run_id=account_run_id,
            )
            if market not in {
                str(item).upper()
                for item in (
                    dict(validated.get("receipt") or {}).get(
                        "included_markets"
                    )
                    or []
                )
            }:
                raise PositionAdviceIdentityBindingError(
                    "portfolio source receipt market binding is invalid"
                )
            receipt_hash = sha256_bytes(receipt_path.read_bytes())
            if (
                portfolio.get("receipt_hash") != receipt_hash
                or portfolio.get("snapshot_id") != validated["snapshot_id"]
            ):
                raise PositionAdviceIdentityBindingError(
                    "source summary receipt binding is invalid"
                )
            candidates.append(
                (
                    str(validated["source_observed_at"]),
                    summary,
                    receipt_hash,
                )
            )
        except (
            KeyError,
            StopIteration,
            OSError,
            ValueError,
            PositionAdviceIdentityBindingError,
            PositionAdviceSourceError,
        ):
            continue
    if not candidates:
        raise PositionAdviceIdentityBindingError(
            f"{market} has no fresh completed portfolio source receipt"
        )
    _observed_at, summary, receipt_hash = max(
        candidates,
        key=lambda item: item[0],
    )
    return summary, receipt_hash


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise PositionAdviceIdentityBindingError(
            f"{label} is unavailable: {target}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdviceIdentityBindingError(
            f"{label} is unreadable: {target}"
        ) from exc
    if not isinstance(payload, dict):
        raise PositionAdviceIdentityBindingError(
            f"{label} must be an object"
        )
    return payload


def _datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "PositionAdviceIdentityBindingError",
    "build_first_use_identity_binding_from_runtime",
]
