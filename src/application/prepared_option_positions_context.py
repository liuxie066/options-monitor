from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.position_fields import normalize_account, normalize_broker
from domain.domain.position_advice_authority import scope_for
from domain.services import adapt_option_positions_context
from src.application.ledger.api import (
    decision_state_snapshot_from_rows,
    open_position_ledger_from_data_config,
    read_decision_state_rows_many,
    resolve_position_data_config_path,
    resolve_position_ledger_sqlite_path,
    validate_position_fact_snapshot_contract,
)
from src.application.position_advice_source_receipts import sha256_bytes
from src.application.positions.context_builder import (
    build_shared_context,
    slice_shared_context_for_account,
    validate_option_positions_context_account,
)
from src.application.tick_run_workspace import (
    AccountRunConfigAuthority,
    AccountRunConfigError,
    ensure_run_state_directory_safely,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)
from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest


PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA = (
    "prepared_option_positions_context.v1"
)
PREPARED_OPTION_POSITIONS_PAYLOAD_NAME = "option_positions_context.json"
PREPARED_OPTION_POSITIONS_MANIFEST_NAME = (
    "prepared_option_positions_context.v1.json"
)


class PreparedOptionPositionsContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedOptionPositionsBatch:
    manifests: dict[str, dict[str, Any]]
    position_records_by_account: dict[str, list[dict[str, Any]]]
    unavailable_by_account: dict[str, str]
    observed_at_utc: str
    ledger_read_count: int
    fx_observation_count: int


def prepare_option_positions_contexts(
    *,
    base: Path,
    run_id: str,
    config_path: Path,
    account_configs: Mapping[str, Mapping[str, Any]],
    account_config_authorities: Mapping[str, AccountRunConfigAuthority],
    run_state_dir: Path,
    log: Callable[[str], None] | None = None,
) -> PreparedOptionPositionsBatch:
    """Publish exact account option contexts from coherent ledger/FX facts."""

    base_path = Path(base).resolve()
    run_id_norm = _required_text(run_id, "run_id")
    expected_run_state_dir = ensure_run_state_directory_safely(
        base=base_path,
        run_id=run_id_norm,
    )
    supplied_run_state_dir = Path(
        os.path.abspath(str(Path(run_state_dir).expanduser()))
    )
    if supplied_run_state_dir != expected_run_state_dir:
        raise PreparedOptionPositionsContextError(
            "prepared option shared state path is outside the current run"
        )

    configs = {
        normalize_account(account): dict(config)
        for account, config in account_configs.items()
        if normalize_account(account) and isinstance(config, Mapping)
    }
    authorities = {
        normalize_account(account): authority
        for account, authority in account_config_authorities.items()
        if normalize_account(account)
    }
    if not configs or set(configs) != set(authorities):
        raise PreparedOptionPositionsContextError(
            "prepared option config/authority scopes do not match"
        )

    accounts_by_ledger_path: dict[Path, list[str]] = {}
    data_config_by_ledger_path: dict[Path, Path] = {}
    unavailable: dict[str, str] = {}
    for account in sorted(configs):
        try:
            data_path = resolve_position_data_config_path(
                base=base_path,
                cfg=configs[account],
                config_path=Path(config_path),
            ).resolve()
            ledger_path = resolve_position_ledger_sqlite_path(
                base=base_path,
                data_config=data_path,
            )
        except Exception as exc:
            unavailable[account] = (
                f"position_ledger_path_unavailable:{type(exc).__name__}"
            )
            continue
        accounts_by_ledger_path.setdefault(ledger_path, []).append(account)
        data_config_by_ledger_path.setdefault(ledger_path, data_path)

    rows_by_ledger_path: dict[Path, dict[str, dict[str, Any]]] = {}
    ledger_read_count = 0
    for ledger_path, accounts in sorted(
        accounts_by_ledger_path.items(),
        key=lambda item: str(item[0]),
    ):
        try:
            _resolved_path, repo = open_position_ledger_from_data_config(
                base=base_path,
                data_config=data_config_by_ledger_path[ledger_path],
            )
            rows_by_ledger_path[ledger_path] = read_decision_state_rows_many(
                repo,
                accounts=tuple(sorted(accounts)),
            )
            ledger_read_count += 1
        except Exception as exc:
            reason = f"coherent_position_ledger_unavailable:{type(exc).__name__}"
            for account in accounts:
                unavailable[account] = reason

    observed_at = datetime.now(timezone.utc)
    observed_at_utc = observed_at.isoformat()
    lifecycle_now_ms = int(observed_at.timestamp() * 1000)
    rates: dict[str, Any] | None
    fx_status = "ready"
    fx_error_type: str | None = None
    try:
        candidate = get_exchange_rates_or_fetch_latest(
            cache_path=(expected_run_state_dir / "rate_cache.json").resolve(),
            max_age_hours=24,
            log=log,
        )
        rates = dict(candidate) if isinstance(candidate, Mapping) else None
        if rates is None:
            fx_status = "unavailable"
    except Exception as exc:
        rates = None
        fx_status = "unavailable"
        fx_error_type = type(exc).__name__
        if log is not None:
            log(f"[WARN] prepared option FX observation unavailable: {exc}")
    fx_observation_sha256 = canonical_sha256(
        {
            "status": fx_status,
            "rates": rates,
            "error_type": fx_error_type,
        }
    )

    manifests: dict[str, dict[str, Any]] = {}
    records_by_account: dict[str, list[dict[str, Any]]] = {}
    for ledger_path, accounts in sorted(
        accounts_by_ledger_path.items(),
        key=lambda item: str(item[0]),
    ):
        rows_by_account = rows_by_ledger_path.get(ledger_path)
        if not isinstance(rows_by_account, dict):
            continue
        try:
            generation_payloads = {
                account: {
                    "trade_events": list(rows_by_account[account]["trade_events"]),
                    "stored_position_lots": list(
                        rows_by_account[account]["stored_position_lots"]
                    ),
                }
                for account in accounts
            }
            generation_hashes = {
                canonical_sha256(payload)
                for payload in generation_payloads.values()
            }
            if len(generation_hashes) != 1:
                raise PreparedOptionPositionsContextError(
                    "multi-account ledger generation is inconsistent"
                )
            ledger_generation_sha256 = next(iter(generation_hashes))
            records = list(
                generation_payloads[accounts[0]]["stored_position_lots"]
            )
            snapshots = {
                account: decision_state_snapshot_from_rows(
                    rows_by_account[account],
                    account=account,
                    portfolio_scope_id=scope_for(account),
                    source_observed_at=observed_at_utc,
                )
                for account in accounts
            }
        except Exception as exc:
            reason = f"coherent_position_projection_unavailable:{type(exc).__name__}"
            for account in accounts:
                unavailable[account] = reason
            continue

        accounts_by_broker: dict[str, list[str]] = {}
        for account in accounts:
            snapshot = snapshots[account]
            contract_reasons = validate_position_fact_snapshot_contract(
                snapshot
            )
            if (
                snapshot.get("snapshot_status") != "trusted"
                or snapshot.get("actionable") is not True
                or contract_reasons
            ):
                unavailable[account] = "coherent_position_projection_untrusted"
                continue
            portfolio = configs[account].get("portfolio")
            portfolio = portfolio if isinstance(portfolio, Mapping) else {}
            broker = normalize_broker(portfolio.get("broker") or "富途")
            accounts_by_broker.setdefault(broker, []).append(account)
            records_by_account[account] = records

        for broker, broker_accounts in sorted(accounts_by_broker.items()):
            shared_context = build_shared_context(
                records,
                broker=broker,
                rates=rates,
                decision_snapshots_by_account=snapshots,
                lifecycle_now_ms=lifecycle_now_ms,
                accounts=broker_accounts,
                observed_at=observed_at,
            )
            for account in sorted(broker_accounts):
                context = slice_shared_context_for_account(
                    shared_context,
                    account,
                )
                if not isinstance(context, dict):
                    unavailable[account] = "prepared_option_account_slice_missing"
                    records_by_account.pop(account, None)
                    continue
                authority = authorities[account]
                prepared_authority = {
                    "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
                    "run_id": run_id_norm,
                    "account": account,
                    "account_config_sha256": authority.account_config_sha256,
                    "ledger_generation_sha256": ledger_generation_sha256,
                    "fx_observation_sha256": fx_observation_sha256,
                    "source_observed_at": observed_at_utc,
                }
                context = dict(context)
                context["decision_state_snapshot"] = dict(
                    snapshots[account]
                )
                context["context_source"] = "prepared"
                context["prepared_authority"] = prepared_authority
                try:
                    _validate_option_context_account(
                        context,
                        expected_account=account,
                        expected_broker=broker,
                    )
                    manifest = _publish_ready_context(
                        base=base_path,
                        run_id=run_id_norm,
                        account=account,
                        account_config_sha256=authority.account_config_sha256,
                        context=context,
                        ledger_generation_sha256=ledger_generation_sha256,
                        decision_state_fingerprint=str(
                            snapshots[account].get(
                                "decision_state_fingerprint"
                            )
                            or ""
                        ),
                        source_observed_at=observed_at_utc,
                        fx_status=fx_status,
                        fx_observation_sha256=fx_observation_sha256,
                        fx_error_type=fx_error_type,
                    )
                except Exception as exc:
                    unavailable[account] = (
                        f"prepared_option_publication_failed:{type(exc).__name__}"
                    )
                    records_by_account.pop(account, None)
                    continue
                manifests[account] = manifest

    for account, reason in sorted(unavailable.items()):
        if account in manifests:
            continue
        try:
            manifests[account] = _publish_unavailable_manifest(
                base=base_path,
                run_id=run_id_norm,
                account=account,
                account_config_sha256=authorities[account].account_config_sha256,
                reason=reason,
                source_observed_at=observed_at_utc,
                fx_status=fx_status,
                fx_observation_sha256=fx_observation_sha256,
                fx_error_type=fx_error_type,
            )
        except Exception:
            pass

    return PreparedOptionPositionsBatch(
        manifests=manifests,
        position_records_by_account=records_by_account,
        unavailable_by_account=unavailable,
        observed_at_utc=observed_at_utc,
        ledger_read_count=ledger_read_count,
        fx_observation_count=1,
    )


def load_prepared_option_positions_context(
    *,
    manifest_path: Path,
    expected_base: Path,
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
    expected_manifest_sha256: str | None = None,
    expected_runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = _required_text(expected_run_id, "expected_run_id")
    account = normalize_account(expected_account)
    if not account:
        raise PreparedOptionPositionsContextError(
            "expected prepared option account is invalid"
        )
    expected_path = (
        Path(expected_base).resolve()
        / "output_runs"
        / run_id
        / "accounts"
        / account
        / "state"
        / PREPARED_OPTION_POSITIONS_MANIFEST_NAME
    )
    supplied_path = Path(
        os.path.abspath(str(Path(manifest_path).expanduser()))
    )
    if supplied_path != expected_path:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest path mismatch"
        )
    try:
        manifest_bytes = read_account_run_state_bytes_safely(
            base=expected_base,
            run_id=run_id,
            account=account,
            name=PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
        )
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (
        AccountRunConfigError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest is unreadable"
        ) from exc
    if not isinstance(manifest, dict):
        raise PreparedOptionPositionsContextError(
            "prepared option manifest must be an object"
        )
    if expected_manifest_sha256 is not None and sha256_bytes(
        manifest_bytes
    ) != _required_sha256(
        expected_manifest_sha256,
        "expected_manifest_sha256",
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option manifest generation mismatch"
        )
    if manifest.get("schema_version") != PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest schema mismatch"
        )
    if _required_text(manifest.get("run_id"), "manifest run_id") != run_id:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest run mismatch"
        )
    if normalize_account(manifest.get("account")) != account:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest account mismatch"
        )
    expected_config_hash = _required_sha256(
        expected_account_config_sha256,
        "expected_account_config_sha256",
    )
    if _required_sha256(
        manifest.get("account_config_sha256"),
        "manifest account_config_sha256",
    ) != expected_config_hash:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest account config hash mismatch"
        )
    if str(manifest.get("status") or "").strip().lower() != "ready":
        raise PreparedOptionPositionsContextError(
            str(manifest.get("reason") or "prepared option context unavailable")
        )
    if manifest.get("payload_relpath") != PREPARED_OPTION_POSITIONS_PAYLOAD_NAME:
        raise PreparedOptionPositionsContextError(
            "prepared option payload path mismatch"
        )
    try:
        payload_bytes = read_account_run_state_bytes_safely(
            base=expected_base,
            run_id=run_id,
            account=account,
            name=PREPARED_OPTION_POSITIONS_PAYLOAD_NAME,
        )
    except AccountRunConfigError as exc:
        raise PreparedOptionPositionsContextError(
            "prepared option payload is unavailable"
        ) from exc
    if sha256_bytes(payload_bytes) != _required_sha256(
        manifest.get("payload_sha256"),
        "payload_sha256",
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option payload hash mismatch"
        )
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedOptionPositionsContextError(
            "prepared option payload is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise PreparedOptionPositionsContextError(
            "prepared option payload must be an object"
        )
    portfolio = (
        expected_runtime_config.get("portfolio")
        if isinstance(expected_runtime_config, Mapping)
        and isinstance(expected_runtime_config.get("portfolio"), Mapping)
        else {}
    )
    expected_broker = normalize_broker(portfolio.get("broker") or "富途")
    configured_account = normalize_account(portfolio.get("account"))
    if configured_account and configured_account != account:
        raise PreparedOptionPositionsContextError(
            "prepared option runtime account mismatch"
        )
    _validate_option_context_account(
        payload,
        expected_account=account,
        expected_broker=expected_broker,
    )
    prepared = payload.get("prepared_authority")
    if not isinstance(prepared, Mapping):
        raise PreparedOptionPositionsContextError(
            "prepared option payload authority is missing"
        )
    for key in (
        "run_id",
        "account",
        "account_config_sha256",
        "ledger_generation_sha256",
        "fx_observation_sha256",
        "source_observed_at",
    ):
        if str(prepared.get(key) or "") != str(manifest.get(key) or ""):
            raise PreparedOptionPositionsContextError(
                f"prepared option payload authority mismatch: {key}"
            )
    if str(prepared.get("account_config_sha256") or "") != expected_config_hash:
        raise PreparedOptionPositionsContextError(
            "prepared option payload account config hash mismatch"
        )
    decision_snapshot = payload.get("decision_state_snapshot")
    if not isinstance(decision_snapshot, Mapping):
        raise PreparedOptionPositionsContextError(
            "prepared option decision snapshot is missing"
        )
    if validate_position_fact_snapshot_contract(decision_snapshot):
        raise PreparedOptionPositionsContextError(
            "prepared option decision snapshot contract is invalid"
        )
    decision_fingerprint = str(
        decision_snapshot.get("decision_state_fingerprint") or ""
    )
    if (
        decision_fingerprint
        != str(manifest.get("decision_state_fingerprint") or "")
        or decision_fingerprint
        != str(payload.get("decision_state_fingerprint") or "")
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option decision snapshot fingerprint mismatch"
        )
    return payload


def exchange_rate_scalars_from_option_context(
    context: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    raw_rates = context.get("exchange_rates")
    rates = raw_rates if isinstance(raw_rates, Mapping) else {}
    nested = rates.get("rates")
    rates_map = nested if isinstance(nested, Mapping) else rates
    usdcny = _positive_float(rates_map.get("USDCNY"))
    hkd_cny = _positive_float(rates_map.get("HKDCNY"))
    return ((1.0 / usdcny) if usdcny else None, hkd_cny)


def _publish_ready_context(
    *,
    base: Path,
    run_id: str,
    account: str,
    account_config_sha256: str,
    context: dict[str, Any],
    ledger_generation_sha256: str,
    decision_state_fingerprint: str,
    source_observed_at: str,
    fx_status: str,
    fx_observation_sha256: str,
    fx_error_type: str | None,
) -> dict[str, Any]:
    payload_bytes = _json_bytes(context)
    payload_path = write_account_run_state_bytes_once_safely(
        base=base,
        run_id=run_id,
        account=account,
        name=PREPARED_OPTION_POSITIONS_PAYLOAD_NAME,
        payload=payload_bytes,
    )
    manifest: dict[str, Any] = {
        "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        "run_id": run_id,
        "account": account,
        "status": "ready",
        "account_config_sha256": account_config_sha256,
        "payload_relpath": payload_path.name,
        "payload_sha256": sha256_bytes(payload_bytes),
        "ledger_generation_sha256": ledger_generation_sha256,
        "decision_state_fingerprint": decision_state_fingerprint,
        "source_observed_at": source_observed_at,
        "fx_status": fx_status,
        "fx_observation_sha256": fx_observation_sha256,
    }
    if fx_error_type:
        manifest["fx_error_type"] = fx_error_type
    return _publish_manifest(
        base=base,
        run_id=run_id,
        account=account,
        manifest=manifest,
    )


def _publish_unavailable_manifest(
    *,
    base: Path,
    run_id: str,
    account: str,
    account_config_sha256: str,
    reason: str,
    source_observed_at: str,
    fx_status: str,
    fx_observation_sha256: str,
    fx_error_type: str | None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        "run_id": run_id,
        "account": account,
        "status": "unavailable",
        "reason": str(reason),
        "account_config_sha256": account_config_sha256,
        "source_observed_at": source_observed_at,
        "fx_status": fx_status,
        "fx_observation_sha256": fx_observation_sha256,
    }
    if fx_error_type:
        manifest["fx_error_type"] = fx_error_type
    return _publish_manifest(
        base=base,
        run_id=run_id,
        account=account,
        manifest=manifest,
    )


def _publish_manifest(
    *,
    base: Path,
    run_id: str,
    account: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    manifest_bytes = _json_bytes(manifest)
    manifest_path = write_account_run_state_bytes_once_safely(
        base=base,
        run_id=run_id,
        account=account,
        name=PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
        payload=manifest_bytes,
    )
    return {
        **manifest,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }


def _validate_option_context_account(
    context: Mapping[str, Any],
    *,
    expected_account: str,
    expected_broker: str,
) -> None:
    try:
        validate_option_positions_context_account(
            context,
            account=expected_account,
            broker=expected_broker,
        )
    except ValueError as exc:
        raise PreparedOptionPositionsContextError(str(exc)) from exc
    try:
        adapt_option_positions_context(dict(context))
    except Exception as exc:
        raise PreparedOptionPositionsContextError(
            "prepared option payload contract is invalid"
        ) from exc
    if str(context.get("context_status") or "") != "available":
        raise PreparedOptionPositionsContextError(
            "prepared option payload is unavailable"
        )
    if str(context.get("decision_snapshot_status") or "") != "trusted":
        raise PreparedOptionPositionsContextError(
            "prepared option decision snapshot is untrusted"
        )
    for field in ("open_positions_min", "assigned_stock_events"):
        rows = context.get(field)
        if not isinstance(rows, list):
            raise PreparedOptionPositionsContextError(
                f"prepared option payload {field} is invalid"
            )
        for item in rows:
            if not isinstance(item, Mapping):
                raise PreparedOptionPositionsContextError(
                    f"prepared option payload {field} row is invalid"
                )
            raw_payload = item.get("raw_payload")
            raw_account = (
                raw_payload.get("account")
                if isinstance(raw_payload, Mapping)
                else None
            )
            row_account = normalize_account(
                item.get("account") or raw_account
            )
            if row_account and row_account != expected_account:
                raise PreparedOptionPositionsContextError(
                    f"prepared option payload {field} account mismatch"
                )


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PreparedOptionPositionsContextError(f"{field} is required")
    return text


def _required_sha256(value: Any, field: str) -> str:
    digest = _required_text(value, field).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise PreparedOptionPositionsContextError(f"{field} is invalid")
    return digest


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


__all__ = [
    "PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA",
    "PREPARED_OPTION_POSITIONS_MANIFEST_NAME",
    "PREPARED_OPTION_POSITIONS_PAYLOAD_NAME",
    "PreparedOptionPositionsBatch",
    "PreparedOptionPositionsContextError",
    "exchange_rate_scalars_from_option_context",
    "load_prepared_option_positions_context",
    "prepare_option_positions_contexts",
]
