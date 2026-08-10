from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from src.application.account_config import (
    normalize_account_label,
    resolve_holdings_account,
)
from src.application.ai_decision_advice.config import (
    PORTFOLIO_DISTRIBUTION_PROVIDER_NONE,
    PORTFOLIO_DISTRIBUTION_PROVIDER_PM,
    ai_decision_advice_enabled,
    portfolio_distribution_provider,
)
from src.application.position_advice_source_receipts import sha256_bytes
from src.application.tick_run_workspace import (
    AccountRunConfigAuthority,
    load_retained_account_run_config,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)
from src.infrastructure.portfolio_management_client import (
    DISTRIBUTION_VALUATION_CURRENCY,
    PortfolioManagementClient,
    PortfolioManagementConfigError,
    PortfolioManagementHTTPError,
    PortfolioManagementProtocolError,
    PortfolioManagementTransportError,
    validate_distribution_response,
)


PREPARED_PORTFOLIO_DISTRIBUTION_SCHEMA = (
    "prepared_portfolio_distribution.v1"
)
PREPARED_PORTFOLIO_DISTRIBUTION_NAME = (
    "prepared_portfolio_distribution.v1.json"
)

_PROVIDER_STATUSES = frozenset({"ready", "degraded", "unavailable"})
_FRESHNESS_STATUSES = frozenset(
    {"fresh", "stale", "unknown", "unavailable"}
)
_TRUST_STATUSES = frozenset(
    {"trusted", "partial", "untrusted", "unavailable"}
)
_NORMALIZED_ASSET_TYPES = frozenset(
    {"cash", "fund", "stock", "crypto", "other"}
)
_CURRENCIES = frozenset({"CNY", "USD", "HKD"})
_SHA256_LENGTH = 64
_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "account",
        "mapped_pm_account",
        "provider",
        "status",
        "reason",
        "account_config_sha256",
        "fetched_at_utc",
        "validation",
    }
)
_NOT_APPLICABLE_REASONS = frozenset(
    {"advice_disabled", "provider_none"}
)
_PM_FAILURE_REASONS = frozenset(
    {
        "pm_config_error",
        "pm_transport_error",
        "pm_http_error",
        "pm_protocol_error",
        "pm_read_failed",
    }
)
_PM_RESPONSE_REASONS = frozenset(
    {
        "pm_response_errors",
        "portfolio_freshness_unknown",
        "portfolio_quality_untrusted",
        "portfolio_quality_unavailable",
    }
)


class PreparedPortfolioDistributionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedPortfolioDistribution:
    envelope: dict[str, Any]
    artifact_path: Path | None
    artifact_sha256: str | None

    @property
    def status(self) -> str:
        authority = self.envelope.get("authority")
        if not isinstance(authority, Mapping):
            return "unavailable"
        return str(authority.get("status") or "unavailable")

    @property
    def reason(self) -> str:
        authority = self.envelope.get("authority")
        if not isinstance(authority, Mapping):
            return "artifact_invalid"
        return str(authority.get("reason") or "artifact_invalid")

    @property
    def provider(self) -> str:
        authority = self.envelope.get("authority")
        if not isinstance(authority, Mapping):
            return PORTFOLIO_DISTRIBUTION_PROVIDER_NONE
        return str(
            authority.get("provider")
            or PORTFOLIO_DISTRIBUTION_PROVIDER_NONE
        )


@dataclass(frozen=True)
class PreparedPortfolioDistributionBatch:
    by_account: dict[str, PreparedPortfolioDistribution]
    pm_read_count: int


def prepare_portfolio_distributions(
    *,
    base: Path,
    run_id: str,
    account_configs: Mapping[str, Mapping[str, Any]],
    account_config_authorities: Mapping[str, AccountRunConfigAuthority],
    timeout_sec: float,
    client_factory: Callable[[], Any] = PortfolioManagementClient,
    now_fn: Callable[[], datetime] | None = None,
) -> PreparedPortfolioDistributionBatch:
    """Prepare one immutable, account-bound strategic distribution per account."""

    run_id_norm = _required_text(run_id, "run_id")
    configs, authorities = _validated_scopes(
        account_configs=account_configs,
        account_config_authorities=account_config_authorities,
    )
    try:
        timeout = float(timeout_sec)
    except (TypeError, ValueError) as exc:
        raise PreparedPortfolioDistributionError(
            "portfolio distribution timeout is invalid"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise PreparedPortfolioDistributionError(
            "portfolio distribution timeout must be positive and finite"
        )

    clock = now_fn or (lambda: datetime.now(timezone.utc))
    prepared: dict[str, PreparedPortfolioDistribution] = {}
    pm_read_count = 0

    for account in sorted(configs):
        config = configs[account]
        authority = authorities[account]
        if authority.run_id != run_id_norm:
            raise PreparedPortfolioDistributionError(
                "portfolio distribution config authority run mismatch"
            )
        try:
            retained_config = load_retained_account_run_config(
                authority=authority,
                base=base,
                run_id=run_id_norm,
                account=account,
            )
        except Exception as exc:
            raise PreparedPortfolioDistributionError(
                "portfolio distribution config authority is invalid"
            ) from exc
        if retained_config != config:
            raise PreparedPortfolioDistributionError(
                "portfolio distribution config does not match its authority"
            )
        provider = _effective_provider(config)
        mapped_pm_account = _mapped_pm_account(config, account)

        try:
            prepared[account] = load_prepared_portfolio_distribution(
                base=base,
                run_id=run_id_norm,
                account=account,
                expected_account_config_sha256=(
                    authority.account_config_sha256
                ),
                expected_mapped_pm_account=mapped_pm_account,
                expected_provider=provider,
            )
            continue
        except PreparedPortfolioDistributionError:
            if not _artifact_is_absent(
                base=base,
                run_id=run_id_norm,
                account=account,
            ):
                prepared[account] = unavailable_prepared_portfolio_distribution(
                    run_id=run_id_norm,
                    account=account,
                    mapped_pm_account=mapped_pm_account,
                    provider=provider,
                    account_config_sha256=authority.account_config_sha256,
                    reason="artifact_invalid",
                    fetched_at_utc=_utc_timestamp(clock()),
                )
                continue

        fetched_at_utc = _utc_timestamp(clock())
        if not ai_decision_advice_enabled(config):
            envelope = _unavailable_envelope(
                run_id=run_id_norm,
                account=account,
                mapped_pm_account=mapped_pm_account,
                provider=provider,
                account_config_sha256=authority.account_config_sha256,
                fetched_at_utc=fetched_at_utc,
                reason="advice_disabled",
            )
        elif provider == PORTFOLIO_DISTRIBUTION_PROVIDER_NONE:
            envelope = _unavailable_envelope(
                run_id=run_id_norm,
                account=account,
                mapped_pm_account=mapped_pm_account,
                provider=provider,
                account_config_sha256=authority.account_config_sha256,
                fetched_at_utc=fetched_at_utc,
                reason="provider_none",
            )
        else:
            try:
                client = client_factory()
                pm_read_count += 1
                response = client.read_distribution(
                    account=mapped_pm_account,
                    timeout=timeout,
                )
                response = validate_distribution_response(
                    response,
                    requested_account=mapped_pm_account,
                )
                envelope = _envelope_from_response(
                    response=response,
                    run_id=run_id_norm,
                    account=account,
                    mapped_pm_account=mapped_pm_account,
                    provider=provider,
                    account_config_sha256=authority.account_config_sha256,
                    fetched_at_utc=fetched_at_utc,
                )
            except Exception as exc:
                envelope = _unavailable_envelope(
                    run_id=run_id_norm,
                    account=account,
                    mapped_pm_account=mapped_pm_account,
                    provider=provider,
                    account_config_sha256=authority.account_config_sha256,
                    fetched_at_utc=fetched_at_utc,
                    reason=_read_failure_reason(exc),
                )

        try:
            prepared[account] = _publish_and_load(
                base=base,
                envelope=envelope,
                run_id=run_id_norm,
                account=account,
                mapped_pm_account=mapped_pm_account,
                provider=provider,
                account_config_sha256=authority.account_config_sha256,
            )
        except PreparedPortfolioDistributionError:
            prepared[account] = unavailable_prepared_portfolio_distribution(
                run_id=run_id_norm,
                account=account,
                mapped_pm_account=mapped_pm_account,
                provider=provider,
                account_config_sha256=authority.account_config_sha256,
                reason="artifact_publish_failed",
                fetched_at_utc=fetched_at_utc,
            )

    return PreparedPortfolioDistributionBatch(
        by_account=prepared,
        pm_read_count=pm_read_count,
    )


def load_prepared_portfolio_distribution(
    *,
    base: Path,
    run_id: str,
    account: str,
    expected_account_config_sha256: str,
    expected_mapped_pm_account: str,
    expected_provider: str,
    expected_artifact_sha256: str | None = None,
) -> PreparedPortfolioDistribution:
    run_id_norm = _required_text(run_id, "run_id")
    account_norm = normalize_account_label(account)
    mapped_pm_account = _required_text(
        expected_mapped_pm_account,
        "mapped PM account",
    )
    provider = _provider(expected_provider)
    config_sha256 = _sha256(
        expected_account_config_sha256,
        "account config sha256",
    )
    try:
        raw = read_account_run_state_bytes_safely(
            base=base,
            run_id=run_id_norm,
            account=account_norm,
            name=PREPARED_PORTFOLIO_DISTRIBUTION_NAME,
        )
    except Exception as exc:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution artifact is missing"
        ) from exc

    artifact_sha256 = sha256_bytes(raw)
    if expected_artifact_sha256 is not None:
        expected_hash = _sha256(
            expected_artifact_sha256,
            "prepared portfolio distribution artifact sha256",
        )
        if artifact_sha256 != expected_hash:
            raise PreparedPortfolioDistributionError(
                "prepared portfolio distribution artifact hash mismatch"
            )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution artifact is not valid JSON"
        ) from exc
    if not isinstance(decoded, Mapping):
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution envelope must be an object"
        )
    envelope = dict(decoded)
    try:
        _validate_envelope(
            envelope,
            expected_run_id=run_id_norm,
            expected_account=account_norm,
            expected_mapped_pm_account=mapped_pm_account,
            expected_provider=provider,
            expected_account_config_sha256=config_sha256,
        )
    except PreparedPortfolioDistributionError:
        raise
    except Exception as exc:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution semantics are invalid"
        ) from exc
    artifact_path = (
        Path(base).resolve()
        / "output_runs"
        / run_id_norm
        / "accounts"
        / account_norm
        / "state"
        / PREPARED_PORTFOLIO_DISTRIBUTION_NAME
    )
    return PreparedPortfolioDistribution(
        envelope=envelope,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
    )


def unavailable_prepared_portfolio_distribution(
    *,
    run_id: str,
    account: str,
    mapped_pm_account: str,
    provider: str,
    account_config_sha256: str,
    reason: str,
    fetched_at_utc: str | None = None,
) -> PreparedPortfolioDistribution:
    envelope = _unavailable_envelope(
        run_id=_required_text(run_id, "run_id"),
        account=normalize_account_label(account),
        mapped_pm_account=_required_text(
            mapped_pm_account,
            "mapped PM account",
        ),
        provider=_provider(provider),
        account_config_sha256=_sha256(
            account_config_sha256,
            "account config sha256",
        ),
        fetched_at_utc=(
            _timestamp(fetched_at_utc, "fetched_at_utc")
            if fetched_at_utc is not None
            else _utc_timestamp(datetime.now(timezone.utc))
        ),
        reason=_required_text(reason, "reason"),
    )
    return PreparedPortfolioDistribution(
        envelope=envelope,
        artifact_path=None,
        artifact_sha256=None,
    )


def portfolio_distribution_metric(
    account: str,
    prepared: PreparedPortfolioDistribution,
) -> dict[str, Any]:
    metric: dict[str, Any] = {
        "account": normalize_account_label(account),
        "provider": prepared.provider,
        "status": prepared.status,
        "reason": prepared.reason,
    }
    if prepared.artifact_sha256 is not None:
        metric["artifact_sha256"] = prepared.artifact_sha256
    return metric


def _publish_and_load(
    *,
    base: Path,
    envelope: Mapping[str, Any],
    run_id: str,
    account: str,
    mapped_pm_account: str,
    provider: str,
    account_config_sha256: str,
) -> PreparedPortfolioDistribution:
    payload = _artifact_bytes(envelope)
    try:
        write_account_run_state_bytes_once_safely(
            base=base,
            run_id=run_id,
            account=account,
            name=PREPARED_PORTFOLIO_DISTRIBUTION_NAME,
            payload=payload,
        )
    except Exception as exc:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution artifact could not be published"
        ) from exc
    return load_prepared_portfolio_distribution(
        base=base,
        run_id=run_id,
        account=account,
        expected_account_config_sha256=account_config_sha256,
        expected_mapped_pm_account=mapped_pm_account,
        expected_provider=provider,
        expected_artifact_sha256=sha256_bytes(payload),
    )


def _envelope_from_response(
    *,
    response: Mapping[str, Any],
    run_id: str,
    account: str,
    mapped_pm_account: str,
    provider: str,
    account_config_sha256: str,
    fetched_at_utc: str,
) -> dict[str, Any]:
    freshness = response.get("freshness")
    if not isinstance(freshness, Mapping):
        raise PortfolioManagementProtocolError(
            "portfolio distribution freshness evidence is missing"
        )
    response_errors = response.get("errors", [])
    if not isinstance(response_errors, list):
        raise PortfolioManagementProtocolError(
            "portfolio distribution errors must be an array"
        )
    if response_errors:
        return _unavailable_envelope(
            run_id=run_id,
            account=account,
            mapped_pm_account=mapped_pm_account,
            provider=provider,
            account_config_sha256=account_config_sha256,
            fetched_at_utc=fetched_at_utc,
            reason="pm_response_errors",
            source_response=response,
        )

    assets = [
        _normalize_asset_row(item, mapped_pm_account=mapped_pm_account)
        for item in response.get("by_asset", [])
    ]
    assets.sort(
        key=lambda item: (
            item["code"],
            item["normalized_type"],
            item["currency"],
            item["quantity"],
            item["value"],
        )
    )
    freshness_status = str(freshness.get("status"))
    trust_status = str(freshness.get("trust_status"))
    status, reason = _quality_status(
        freshness_status=freshness_status,
        trust_status=trust_status,
    )
    if status == "unavailable":
        return _unavailable_envelope(
            run_id=run_id,
            account=account,
            mapped_pm_account=mapped_pm_account,
            provider=provider,
            account_config_sha256=account_config_sha256,
            fetched_at_utc=fetched_at_utc,
            reason=reason,
            source_response=response,
        )

    derived = _derive_totals(assets)
    payload = _source_payload(response)
    payload.update(
        {
            "valuation_currency": DISTRIBUTION_VALUATION_CURRENCY,
            "assets": assets,
            "derived": derived,
        }
    )
    return _envelope(
        run_id=run_id,
        account=account,
        mapped_pm_account=mapped_pm_account,
        provider=provider,
        status=status,
        reason=reason,
        account_config_sha256=account_config_sha256,
        fetched_at_utc=fetched_at_utc,
        validation_status="passed",
        payload=payload,
    )


def _unavailable_envelope(
    *,
    run_id: str,
    account: str,
    mapped_pm_account: str,
    provider: str,
    account_config_sha256: str,
    fetched_at_utc: str,
    reason: str,
    source_response: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _source_payload(source_response or {})
    payload.update(
        {
            "valuation_currency": DISTRIBUTION_VALUATION_CURRENCY,
            "assets": [],
            "derived": {
                "total_value": None,
                "asset_weights": {},
                "currency_weights": {},
                "cash_and_mmf_weight": None,
            },
        }
    )
    return _envelope(
        run_id=run_id,
        account=account,
        mapped_pm_account=mapped_pm_account,
        provider=provider,
        status="unavailable",
        reason=reason,
        account_config_sha256=account_config_sha256,
        fetched_at_utc=fetched_at_utc,
        validation_status=(
            "not_applicable"
            if reason in {"advice_disabled", "provider_none"}
            else "failed"
        ),
        payload=payload,
    )


def _envelope(
    *,
    run_id: str,
    account: str,
    mapped_pm_account: str,
    provider: str,
    status: str,
    reason: str,
    account_config_sha256: str,
    fetched_at_utc: str,
    validation_status: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    payload_dict = dict(payload)
    return {
        "authority": {
            "schema_version": PREPARED_PORTFOLIO_DISTRIBUTION_SCHEMA,
            "run_id": run_id,
            "account": account,
            "mapped_pm_account": mapped_pm_account,
            "provider": provider,
            "status": status,
            "reason": reason,
            "account_config_sha256": account_config_sha256,
            "fetched_at_utc": fetched_at_utc,
            "validation": {"status": validation_status},
        },
        "payload": payload_dict,
        "integrity": {
            "payload_sha256": sha256_bytes(_canonical_bytes(payload_dict))
        },
    }


def _source_payload(response: Mapping[str, Any]) -> dict[str, Any]:
    freshness = response.get("freshness")
    item = freshness if isinstance(freshness, Mapping) else {}
    dataset_ids = item.get("dataset_ids")
    reason_codes = item.get("reason_codes")
    return {
        "observed_at_utc": (
            str(item.get("observed_at_utc"))
            if item.get("observed_at_utc") is not None
            else None
        ),
        "retrieved_at_utc": (
            str(response.get("retrieved_at_utc"))
            if response.get("retrieved_at_utc") is not None
            else None
        ),
        "freshness_status": str(item.get("status") or "unavailable"),
        "trust_status": str(item.get("trust_status") or "unavailable"),
        "dataset_ids": sorted({
            str(value)
            for value in (dataset_ids if isinstance(dataset_ids, list) else [])
        }),
        "reason_codes": sorted({
            str(value)
            for value in (reason_codes if isinstance(reason_codes, list) else [])
        }),
    }


def _normalize_asset_row(
    value: Any,
    *,
    mapped_pm_account: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PortfolioManagementProtocolError(
            "portfolio distribution asset row must be an object"
        )
    code = value.get("code")
    normalized_type = value.get("normalized_type")
    currency = value.get("currency")
    if not isinstance(code, str) or not code.strip():
        raise PortfolioManagementProtocolError(
            "portfolio distribution asset code is invalid"
        )
    if (
        not isinstance(normalized_type, str)
        or normalized_type not in _NORMALIZED_ASSET_TYPES
    ):
        raise PortfolioManagementProtocolError(
            "portfolio distribution normalized asset type is invalid"
        )
    if not isinstance(currency, str) or currency not in _CURRENCIES:
        raise PortfolioManagementProtocolError(
            "portfolio distribution asset currency is invalid"
        )
    quantity = _finite_number(value.get("quantity"), "asset quantity")
    amount = _finite_number(value.get("value"), "asset value")
    _validate_row_account_scope(value, mapped_pm_account=mapped_pm_account)
    return {
        "code": code.strip(),
        "normalized_type": normalized_type,
        "currency": currency,
        "quantity": quantity,
        "value": amount,
    }


def _validate_row_account_scope(
    row: Mapping[str, Any],
    *,
    mapped_pm_account: str,
) -> None:
    if "accounts" in row:
        accounts = row.get("accounts")
        if not isinstance(accounts, Mapping) or any(
            not isinstance(key, str) or key != mapped_pm_account
            for key in accounts
        ):
            raise PortfolioManagementProtocolError(
                "portfolio distribution row account scope mismatch"
            )
    if "breakdown" in row:
        breakdown = row.get("breakdown")
        if not isinstance(breakdown, list):
            raise PortfolioManagementProtocolError(
                "portfolio distribution row breakdown must be an array"
            )
        for item in breakdown:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("account"), str)
                or item.get("account") != mapped_pm_account
            ):
                raise PortfolioManagementProtocolError(
                    "portfolio distribution row breakdown account mismatch"
                )


def _derive_totals(assets: list[dict[str, Any]]) -> dict[str, Any]:
    if not assets:
        return {
            "total_value": 0.0,
            "asset_weights": {},
            "currency_weights": {},
            "cash_and_mmf_weight": 0.0,
        }
    total = math.fsum(float(item["value"]) for item in assets)
    if not math.isfinite(total) or total <= 0:
        raise PortfolioManagementProtocolError(
            "portfolio distribution non-empty total must be positive and finite"
        )
    asset_values: dict[str, float] = {}
    currency_values: dict[str, float] = {}
    cash_and_mmf_value = 0.0
    for item in assets:
        code = str(item["code"])
        currency = str(item["currency"])
        amount = float(item["value"])
        asset_values[code] = math.fsum((asset_values.get(code, 0.0), amount))
        currency_values[currency] = math.fsum(
            (currency_values.get(currency, 0.0), amount)
        )
        if item["normalized_type"] == "cash":
            cash_and_mmf_value = math.fsum((cash_and_mmf_value, amount))
    return {
        "total_value": total,
        "asset_weights": {
            key: value / total for key, value in sorted(asset_values.items())
        },
        "currency_weights": {
            key: value / total
            for key, value in sorted(currency_values.items())
        },
        "cash_and_mmf_weight": cash_and_mmf_value / total,
    }


def _validate_envelope(
    envelope: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
    expected_mapped_pm_account: str,
    expected_provider: str,
    expected_account_config_sha256: str,
) -> None:
    if set(envelope) != {"authority", "payload", "integrity"}:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution envelope fields are invalid"
        )
    authority = envelope.get("authority")
    payload = envelope.get("payload")
    integrity = envelope.get("integrity")
    if not all(isinstance(item, Mapping) for item in (authority, payload, integrity)):
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution envelope sections are invalid"
        )
    assert isinstance(authority, Mapping)
    assert isinstance(payload, Mapping)
    assert isinstance(integrity, Mapping)
    if set(authority) != _AUTHORITY_FIELDS:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution authority fields are invalid"
        )
    if set(integrity) != {"payload_sha256"}:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution integrity fields are invalid"
        )
    expected_bindings = {
        "schema_version": PREPARED_PORTFOLIO_DISTRIBUTION_SCHEMA,
        "run_id": expected_run_id,
        "account": expected_account,
        "mapped_pm_account": expected_mapped_pm_account,
        "provider": expected_provider,
        "account_config_sha256": expected_account_config_sha256,
    }
    if any(authority.get(key) != value for key, value in expected_bindings.items()):
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution authority binding mismatch"
        )
    status = authority.get("status")
    if status not in _PROVIDER_STATUSES:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution status is invalid"
        )
    if not isinstance(authority.get("reason"), str) or not authority.get("reason"):
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution reason is invalid"
        )
    _timestamp(authority.get("fetched_at_utc"), "fetched_at_utc")
    validation = authority.get("validation")
    if (
        not isinstance(validation, Mapping)
        or set(validation) != {"status"}
        or validation.get("status")
        not in {"passed", "failed", "not_applicable"}
    ):
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution validation status is invalid"
        )
    if status in {"ready", "degraded"} and validation.get("status") != "passed":
        raise PreparedPortfolioDistributionError(
            "available portfolio distribution validation did not pass"
        )
    expected_payload_sha256 = sha256_bytes(_canonical_bytes(payload))
    if integrity.get("payload_sha256") != expected_payload_sha256:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution payload hash mismatch"
        )
    _validate_payload(
        dict(payload),
        provider=expected_provider,
        status=str(status),
        reason=str(authority.get("reason")),
        validation_status=str(validation.get("status")),
    )


def _validate_payload(
    payload: dict[str, Any],
    *,
    provider: str,
    status: str,
    reason: str,
    validation_status: str,
) -> None:
    required = {
        "observed_at_utc",
        "retrieved_at_utc",
        "freshness_status",
        "trust_status",
        "dataset_ids",
        "reason_codes",
        "valuation_currency",
        "assets",
        "derived",
    }
    if set(payload) != required:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution payload fields are invalid"
        )
    if payload.get("valuation_currency") != DISTRIBUTION_VALUATION_CURRENCY:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution valuation currency mismatch"
        )
    freshness = payload.get("freshness_status")
    trust = payload.get("trust_status")
    if freshness not in _FRESHNESS_STATUSES or trust not in _TRUST_STATUSES:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution quality evidence is invalid"
        )
    for field in ("observed_at_utc", "retrieved_at_utc"):
        if payload.get(field) is not None:
            _timestamp(payload[field], field)
    for field in ("dataset_ids", "reason_codes"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
            or values != sorted(values)
            or len(values) != len(set(values))
        ):
            raise PreparedPortfolioDistributionError(
                f"prepared portfolio distribution {field} is invalid"
            )
    assets = payload.get("assets")
    derived = payload.get("derived")
    if not isinstance(assets, list) or not isinstance(derived, Mapping):
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution normalized data is invalid"
        )
    if status == "unavailable":
        expected = {
            "total_value": None,
            "asset_weights": {},
            "currency_weights": {},
            "cash_and_mmf_weight": None,
        }
        if assets or dict(derived) != expected:
            raise PreparedPortfolioDistributionError(
                "unavailable portfolio distribution must not contain assets"
            )
        _validate_unavailable_state(
            payload=payload,
            provider=provider,
            reason=reason,
            validation_status=validation_status,
        )
        return
    if provider != PORTFOLIO_DISTRIBUTION_PROVIDER_PM:
        raise PreparedPortfolioDistributionError(
            "available portfolio distribution must use the PM provider"
        )
    if payload.get("observed_at_utc") is None or payload.get("retrieved_at_utc") is None:
        raise PreparedPortfolioDistributionError(
            "available portfolio distribution timestamps are missing"
        )
    _timestamp(payload["observed_at_utc"], "observed_at_utc")
    _timestamp(payload["retrieved_at_utc"], "retrieved_at_utc")
    normalized = [
        _normalize_asset_row(item, mapped_pm_account="")
        for item in assets
    ]
    if normalized != assets:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution asset rows are not canonical"
        )
    ordered = sorted(
        normalized,
        key=lambda item: (
            item["code"],
            item["normalized_type"],
            item["currency"],
            item["quantity"],
            item["value"],
        ),
    )
    if normalized != ordered:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution asset order is not canonical"
        )
    try:
        expected_derived = _derive_totals(normalized)
    except PortfolioManagementProtocolError as exc:
        raise PreparedPortfolioDistributionError(str(exc)) from exc
    if dict(derived) != expected_derived:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution derived totals mismatch"
        )
    expected_status, expected_reason = _quality_status(
        freshness_status=str(freshness),
        trust_status=str(trust),
    )
    if expected_status != status:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution quality/status mismatch"
        )
    if expected_reason != reason:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution quality/reason mismatch"
        )


def _validate_unavailable_state(
    *,
    payload: Mapping[str, Any],
    provider: str,
    reason: str,
    validation_status: str,
) -> None:
    freshness = str(payload.get("freshness_status"))
    trust = str(payload.get("trust_status"))
    observed_at = payload.get("observed_at_utc")
    retrieved_at = payload.get("retrieved_at_utc")
    dataset_ids = payload.get("dataset_ids")
    reason_codes = payload.get("reason_codes")

    if reason in _NOT_APPLICABLE_REASONS:
        if (
            provider != PORTFOLIO_DISTRIBUTION_PROVIDER_NONE
            or validation_status != "not_applicable"
            or freshness != "unavailable"
            or trust != "unavailable"
            or observed_at is not None
            or retrieved_at is not None
            or dataset_ids != []
            or reason_codes != []
        ):
            raise PreparedPortfolioDistributionError(
                "not-applicable portfolio distribution state is invalid"
            )
        return

    if provider != PORTFOLIO_DISTRIBUTION_PROVIDER_PM or validation_status != "failed":
        raise PreparedPortfolioDistributionError(
            "unavailable portfolio distribution provider/validation mismatch"
        )
    if reason in _PM_FAILURE_REASONS:
        if (
            freshness != "unavailable"
            or trust != "unavailable"
            or observed_at is not None
            or retrieved_at is not None
            or dataset_ids != []
            or reason_codes != []
        ):
            raise PreparedPortfolioDistributionError(
                "PM failure portfolio distribution payload is invalid"
            )
        return
    if reason == "pm_response_errors":
        if observed_at is None or retrieved_at is None:
            raise PreparedPortfolioDistributionError(
                "PM response-error timestamps are missing"
            )
        return
    if reason in _PM_RESPONSE_REASONS:
        expected_status, expected_reason = _quality_status(
            freshness_status=freshness,
            trust_status=trust,
        )
        if (
            expected_status != "unavailable"
            or expected_reason != reason
            or observed_at is None
            or retrieved_at is None
        ):
            raise PreparedPortfolioDistributionError(
                "unavailable portfolio quality state is invalid"
            )
        return
    raise PreparedPortfolioDistributionError(
        "unavailable portfolio distribution reason is invalid"
    )


def _quality_status(*, freshness_status: str, trust_status: str) -> tuple[str, str]:
    if freshness_status == "fresh" and trust_status == "trusted":
        return "ready", "ready"
    if freshness_status in {"fresh", "stale"} and trust_status == "partial":
        return "degraded", "portfolio_partial"
    if freshness_status == "stale" and trust_status == "trusted":
        return "degraded", "portfolio_stale"
    if freshness_status == "unknown":
        return "unavailable", "portfolio_freshness_unknown"
    if trust_status == "untrusted":
        return "unavailable", "portfolio_quality_untrusted"
    return "unavailable", "portfolio_quality_unavailable"


def _effective_provider(config: Mapping[str, Any]) -> str:
    if not ai_decision_advice_enabled(config):
        return PORTFOLIO_DISTRIBUTION_PROVIDER_NONE
    return portfolio_distribution_provider(config)


def _mapped_pm_account(config: Mapping[str, Any], account: str) -> str:
    mapped = resolve_holdings_account(dict(config), account=account)
    return _required_text(mapped, "mapped PM account")


def _validated_scopes(
    *,
    account_configs: Mapping[str, Mapping[str, Any]],
    account_config_authorities: Mapping[str, AccountRunConfigAuthority],
) -> tuple[dict[str, dict[str, Any]], dict[str, AccountRunConfigAuthority]]:
    configs: dict[str, dict[str, Any]] = {}
    for raw_account, config in account_configs.items():
        account = normalize_account_label(raw_account)
        if account in configs or not isinstance(config, Mapping):
            raise PreparedPortfolioDistributionError(
                "portfolio distribution account config scope is invalid"
            )
        configs[account] = dict(config)
    authorities: dict[str, AccountRunConfigAuthority] = {}
    for raw_account, authority in account_config_authorities.items():
        account = normalize_account_label(raw_account)
        if (
            account in authorities
            or not isinstance(authority, AccountRunConfigAuthority)
            or authority.account != account
        ):
            raise PreparedPortfolioDistributionError(
                "portfolio distribution config authority scope is invalid"
            )
        _sha256(authority.account_config_sha256, "account config sha256")
        authorities[account] = authority
    if not configs or set(configs) != set(authorities):
        raise PreparedPortfolioDistributionError(
            "portfolio distribution config/authority scopes do not match"
        )
    return configs, authorities


def _read_failure_reason(exc: Exception) -> str:
    if isinstance(exc, PortfolioManagementConfigError):
        return "pm_config_error"
    if isinstance(exc, PortfolioManagementTransportError):
        return "pm_transport_error"
    if isinstance(exc, PortfolioManagementHTTPError):
        return "pm_http_error"
    if isinstance(exc, PortfolioManagementProtocolError):
        return "pm_protocol_error"
    return "pm_read_failed"


def _artifact_is_absent(*, base: Path, run_id: str, account: str) -> bool:
    path = (
        Path(base).resolve()
        / "output_runs"
        / run_id
        / "accounts"
        / account
        / "state"
        / PREPARED_PORTFOLIO_DISTRIBUTION_NAME
    )
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _provider(value: Any) -> str:
    if value not in {
        PORTFOLIO_DISTRIBUTION_PROVIDER_NONE,
        PORTFOLIO_DISTRIBUTION_PROVIDER_PM,
    }:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution provider is invalid"
        )
    return str(value)


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioManagementProtocolError(
            f"portfolio distribution {field} is invalid"
        )
    number = float(value)
    if not math.isfinite(number):
        raise PortfolioManagementProtocolError(
            f"portfolio distribution {field} must be finite"
        )
    return 0.0 if number == 0 else number


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreparedPortfolioDistributionError(f"{field} is missing")
    return value.strip()


def _sha256(value: Any, field: str) -> str:
    text = _required_text(value, field).lower()
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise PreparedPortfolioDistributionError(f"{field} is invalid")
    return text


def _timestamp(value: Any, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as exc:
        raise PreparedPortfolioDistributionError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PreparedPortfolioDistributionError(
            f"{field} must be timezone aware"
        )
    return text


def _utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PreparedPortfolioDistributionError(
            "portfolio distribution clock must return an aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution is not canonical JSON"
        ) from exc


def _artifact_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PreparedPortfolioDistributionError(
            "prepared portfolio distribution artifact is not JSON serializable"
        ) from exc
