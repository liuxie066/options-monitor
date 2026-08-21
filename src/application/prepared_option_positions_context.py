from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.position_fields import normalize_account, normalize_broker
from domain.domain.performance.models import EvidenceEnvelope, FXRateFact
from domain.domain.portfolio_scope import portfolio_scope_id
from domain.services import adapt_option_positions_context
from src.infrastructure.exchange_rates import (
    exchange_rate_observation_status,
    get_exchange_rates_or_fetch_latest,
)
from src.application.ledger.api import (
    decision_state_snapshot_from_rows,
    open_performance_evidence_repository,
    open_position_ledger_from_data_config,
    read_current_decision_projection,
    read_decision_state_rows_many,
    resolve_position_data_config_path,
    resolve_position_ledger_sqlite_path,
    validate_position_fact_snapshot_contract,
)
from src.application.source_receipts import sha256_bytes
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
from src.application.payload_helpers import required_text
from src.application.wheel.read_model import build_wheel_read_model_from_rows
from functools import partial


_required_text = partial(required_text, error=lambda m: PreparedOptionPositionsContextError(m))


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
    wheel_read_models_by_account: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )
    fx_evidence_status: str = "not_attempted"
    fx_evidence_ledger_count: int = 0
    fx_evidence_inserted_count: int = 0
    fx_evidence_idempotent_count: int = 0
    fx_evidence_error_count: int = 0


def _fx_evidence_envelope(
    observation: Mapping[str, Any],
    *,
    observation_status: str,
    captured_at_ms: int,
) -> EvidenceEnvelope:
    provider_source = str(observation.get("source") or "").strip()
    timestamp = str(observation.get("timestamp") or "").strip()
    rates = observation.get("rates")
    if not provider_source or not timestamp or not isinstance(rates, Mapping):
        raise ValueError("FX evidence requires source, timestamp, and rates")
    effective_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    effective_at_ms = int(effective_at.astimezone(timezone.utc).timestamp() * 1000)
    if effective_at_ms > int(captured_at_ms):
        raise ValueError("FX evidence observation timestamp is in the future")
    required_pairs = ("USDCNY", "HKDCNY")
    if any(rates.get(pair) in (None, "") for pair in required_pairs):
        raise ValueError("FX evidence observation is missing a required rate")
    raw = dict(observation)
    evidence_source = (
        "cache_snapshot"
        if observation_status == "unavailable_stale"
        else "realtime_snapshot"
    )
    quality = {
        "capture_path": "scheduled_tick",
        "provider_source": provider_source,
    }
    if evidence_source == "cache_snapshot":
        quality["stale_cache_fallback"] = True
    facts = tuple(
        FXRateFact(
            fact_id=None,
            base_currency=pair[:3],
            quote_currency="CNY",
            rate=rates[pair],
            rate_kind="spot",
            effective_at_ms=effective_at_ms,
            observed_at_ms=int(captured_at_ms),
            source=evidence_source,
            source_id=f"{provider_source}:{pair}:{effective_at_ms}",
            quality=quality,
            raw=raw,
        )
        for pair in required_pairs
    )
    return EvidenceEnvelope(fx_rates=facts)


def _reuse_existing_fx_facts(
    envelope: EvidenceEnvelope,
    existing_rates: tuple[FXRateFact, ...],
) -> EvidenceEnvelope:
    existing_by_source = {item.source_identity: item for item in existing_rates}
    existing_by_observation = {
        (item.source_id, item.revision): item for item in existing_rates
    }
    facts: list[FXRateFact] = []
    for fact in envelope.fx_rates:
        existing = existing_by_source.get(fact.source_identity)
        same_observation = True
        if existing is None:
            existing = existing_by_observation.get((fact.source_id, fact.revision))
            same_observation = existing is not None
        if existing is None:
            facts.append(fact)
            continue
        incoming_payload = fact.normalized_payload(include_fact_id=False)
        existing_payload = existing.normalized_payload(include_fact_id=False)
        incoming_payload.pop("observed_at_ms")
        existing_payload.pop("observed_at_ms")
        if same_observation and existing.source_identity != fact.source_identity:
            for field_name in ("source", "quality"):
                incoming_payload.pop(field_name)
                existing_payload.pop(field_name)
            if incoming_payload != existing_payload:
                raise ValueError("FX provider observation identity conflict")
        facts.append(existing if incoming_payload == existing_payload else fact)
    return EvidenceEnvelope(fx_rates=tuple(facts))


def _persist_fx_evidence(
    *,
    repos_by_ledger_path: Mapping[Path, Any],
    observation: Mapping[str, Any] | None,
    observation_status: str,
    migrated_at_ms: int,
    log: Callable[[str], None] | None,
) -> dict[str, Any]:
    if observation_status not in {"ready", "unavailable_stale"} or observation is None:
        return {"status": "source_unavailable"}
    if not repos_by_ledger_path:
        return {"status": "no_ledger"}
    try:
        envelope = _fx_evidence_envelope(
            observation,
            observation_status=observation_status,
            captured_at_ms=migrated_at_ms,
        )
    except Exception as exc:
        if log is not None:
            log(f"[WARN] option performance FX evidence invalid: {type(exc).__name__}")
        return {"status": "error", "error_count": 1}

    inserted = 0
    idempotent = 0
    errors = 0
    for repo in repos_by_ledger_path.values():
        try:
            evidence_repo = open_performance_evidence_repository(repo)
            repo_envelope = _reuse_existing_fx_facts(
                envelope,
                evidence_repo.read_all().fx_rates,
            )
            try:
                result = evidence_repo.import_envelope(
                    repo_envelope,
                    apply=True,
                    migrated_at_ms=int(migrated_at_ms),
                )
            except Exception:
                retry_envelope = _reuse_existing_fx_facts(
                    envelope,
                    evidence_repo.read_all().fx_rates,
                )
                if retry_envelope == repo_envelope:
                    raise
                result = evidence_repo.import_envelope(
                    retry_envelope,
                    apply=True,
                    migrated_at_ms=int(migrated_at_ms),
                )
            inserted += int(result.inserted_count)
            idempotent += int(result.idempotent_count)
        except Exception as exc:
            errors += 1
            if log is not None:
                log(
                    "[WARN] option performance FX evidence persistence failed: "
                    f"{type(exc).__name__}"
                )
    successes = len(repos_by_ledger_path) - errors
    status = (
        "partial"
        if errors and successes
        else "error"
        if errors
        else "persisted"
        if inserted
        else "idempotent"
    )
    return {
        "status": status,
        "ledger_count": successes,
        "inserted_count": inserted,
        "idempotent_count": idempotent,
        "error_count": errors,
    }


def prepare_option_positions_contexts(
    *,
    base: Path,
    run_id: str,
    config_path: Path,
    account_configs: Mapping[str, Mapping[str, Any]],
    account_config_authorities: Mapping[str, AccountRunConfigAuthority],
    run_state_dir: Path,
    log: Callable[[str], None] | None = None,
    persist_fx_evidence: bool = False,
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
    repos_by_ledger_path: dict[Path, Any] = {}
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
            repos_by_ledger_path[ledger_path] = repo
            ledger_read_count += 1
        except Exception as exc:
            reason = f"coherent_position_ledger_unavailable:{type(exc).__name__}"
            for account in accounts:
                unavailable[account] = reason

    observed_at = datetime.now(timezone.utc)
    observed_at_utc = observed_at.isoformat()
    lifecycle_now_ms = int(observed_at.timestamp() * 1000)
    rates: dict[str, Any] | None
    fx_observation: dict[str, Any] | None = None
    fx_status = "unavailable"
    fx_error_type: str | None = None
    try:
        rate_cache_path = (
            base_path / "output_shared" / "state" / "rate_cache.json"
        ).resolve()
        candidate = get_exchange_rates_or_fetch_latest(
            cache_path=rate_cache_path,
            max_age_hours=24,
            log=log,
        )
        fx_observation = (
            dict(candidate) if isinstance(candidate, Mapping) else None
        )
        fx_status = exchange_rate_observation_status(
            fx_observation,
            max_age_hours=24,
        )
        rates = fx_observation if fx_status == "ready" else None
    except Exception as exc:
        rates = None
        fx_status = "unavailable"
        fx_error_type = type(exc).__name__
        if log is not None:
            log(f"[WARN] prepared option FX observation unavailable: {exc}")
    fx_observation_sha256 = canonical_sha256(
        {
            "status": fx_status,
            "observation": fx_observation,
            "error_type": fx_error_type,
        }
    )
    fx_evidence = (
        _persist_fx_evidence(
            repos_by_ledger_path=repos_by_ledger_path,
            observation=fx_observation,
            observation_status=fx_status,
            migrated_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            log=log,
        )
        if persist_fx_evidence
        else {"status": "disabled"}
    )

    manifests: dict[str, dict[str, Any]] = {}
    records_by_account: dict[str, list[dict[str, Any]]] = {}
    wheel_models_by_account: dict[str, dict[str, Any]] = {}
    for ledger_path, accounts in sorted(
        accounts_by_ledger_path.items(),
        key=lambda item: str(item[0]),
    ):
        rows_by_account = rows_by_ledger_path.get(ledger_path)
        if not isinstance(rows_by_account, dict):
            continue
        try:
            first_rows = rows_by_account[accounts[0]]
            generation_payload = {
                "trade_events": list(first_rows["trade_events"]),
                "stored_position_lots": list(first_rows["stored_position_lots"]),
                "wheel_events_by_account": {
                    account: list(
                        rows_by_account[account].get("account_wheel_events")
                        or []
                    )
                    for account in sorted(accounts)
                },
            }
            ledger_generation_sha256 = canonical_sha256(generation_payload)
            records = list(first_rows["stored_position_lots"])
            snapshots = {}
            for account in accounts:
                try:
                    current_projection = read_current_decision_projection(
                        repos_by_ledger_path[ledger_path],
                        account=account,
                        now_ms=lifecycle_now_ms,
                    )
                except Exception as exc:
                    current_projection = {
                        "status": "data_unavailable",
                        "reason": (
                            "current_projection_read_failed:"
                            f"{type(exc).__name__}"
                        ),
                    }
                snapshots[account] = decision_state_snapshot_from_rows(
                    rows_by_account[account],
                    account=account,
                    portfolio_scope_id=portfolio_scope_id(account),
                    source_observed_at=observed_at_utc,
                    current_projection=current_projection,
                    current_decision_now_ms=lifecycle_now_ms,
                )
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
                    "fx_status": fx_status,
                    "source_observed_at": observed_at_utc,
                }
                context = dict(context)
                try:
                    wheel_model = build_wheel_read_model_from_rows(
                        rows_by_account[account],
                        account=account,
                        as_of_ms=lifecycle_now_ms,
                    )
                except Exception as exc:
                    unavailable[account] = (
                        f"wheel_projection_failed:{type(exc).__name__}"
                    )
                    records_by_account.pop(account, None)
                    continue
                context["wheel_read_model"] = wheel_model
                context["decision_state_snapshot"] = dict(
                    snapshots[account]
                )
                context["current_decision_shadow"] = dict(
                    snapshots[account]["current_decision_shadow"]
                )
                context["context_source"] = "prepared"
                context["prepared_authority"] = prepared_authority
                try:
                    _validate_option_context_account(
                        context,
                        expected_account=account,
                        expected_broker=broker,
                    )
                    application_received_at_utc = datetime.now(timezone.utc).isoformat()
                    prepared_authority["application_received_at_utc"] = (
                        application_received_at_utc
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
                        application_received_at_utc=(application_received_at_utc),
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
                wheel_models_by_account[account] = wheel_model

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
        wheel_read_models_by_account=wheel_models_by_account,
        fx_evidence_status=str(fx_evidence.get("status") or "error"),
        fx_evidence_ledger_count=int(fx_evidence.get("ledger_count") or 0),
        fx_evidence_inserted_count=int(fx_evidence.get("inserted_count") or 0),
        fx_evidence_idempotent_count=int(fx_evidence.get("idempotent_count") or 0),
        fx_evidence_error_count=int(fx_evidence.get("error_count") or 0),
    )


def _load_prepared_option_positions_context_artifacts(
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
    return {
        "manifest": manifest,
        "payload": payload,
        "manifest_bytes": manifest_bytes,
        "payload_bytes": payload_bytes,
    }


def load_prepared_option_positions_context_receipt(
    *,
    manifest_path: Path,
    expected_base: Path,
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
    expected_manifest_sha256: str | None = None,
    expected_runtime_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load bytes and expose only the owner-validated application receipt."""

    receipt = _load_prepared_option_positions_context_artifacts(
        manifest_path=manifest_path,
        expected_base=expected_base,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_runtime_config=expected_runtime_config,
    )
    manifest = receipt["manifest"]
    prepared = receipt["payload"]["prepared_authority"]
    application_received_at_utc = _utc_application_receipt(
        manifest.get("application_received_at_utc")
    )
    if (
        str(prepared.get("application_received_at_utc") or "")
        != application_received_at_utc
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option payload authority mismatch: application_received_at_utc"
        )
    return receipt


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
    """Load the existing payload-only facade from a validated receipt."""

    return _load_prepared_option_positions_context_artifacts(
        manifest_path=manifest_path,
        expected_base=expected_base,
        expected_run_id=expected_run_id,
        expected_account=expected_account,
        expected_account_config_sha256=expected_account_config_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_runtime_config=expected_runtime_config,
    )["payload"]


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


def cny_per_currency_rates_from_option_context(
    context: Mapping[str, Any],
) -> dict[str, float]:
    """Expose a prepared OpenD observation as CNY-per-currency rates.

    This helper performs no cache or provider read. CNY can always be valued
    directly; USD/HKD are returned only when the run-coherent prepared
    authority marks its FX observation ready and the rate is positive.
    """

    prepared = context.get("prepared_authority")
    authority = prepared if isinstance(prepared, Mapping) else {}
    out = {"CNY": 1.0}
    if str(authority.get("fx_status") or "").strip().lower() != "ready":
        return out

    raw_rates = context.get("exchange_rates")
    rates = raw_rates if isinstance(raw_rates, Mapping) else {}
    nested = rates.get("rates")
    rates_map = nested if isinstance(nested, Mapping) else rates
    usdcny = _positive_float(rates_map.get("USDCNY"))
    hkd_cny = _positive_float(rates_map.get("HKDCNY"))
    if usdcny is not None:
        out["USD"] = usdcny
    if hkd_cny is not None:
        out["HKD"] = hkd_cny
    return out


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
    application_received_at_utc: str,
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
        "application_received_at_utc": application_received_at_utc,
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
    application_received_at_utc = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        "run_id": run_id,
        "account": account,
        "status": "unavailable",
        "reason": str(reason),
        "account_config_sha256": account_config_sha256,
        "source_observed_at": source_observed_at,
        "application_received_at_utc": application_received_at_utc,
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


def _required_sha256(value: Any, field: str) -> str:
    digest = _required_text(value, field).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise PreparedOptionPositionsContextError(f"{field} is invalid")
    return digest


def _utc_application_receipt(value: Any) -> str:
    text = _required_text(value, "application_received_at_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreparedOptionPositionsContextError(
            "application_received_at_utc is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PreparedOptionPositionsContextError(
            "application_received_at_utc must be UTC"
        )
    return text


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
    "cny_per_currency_rates_from_option_context",
    "exchange_rate_scalars_from_option_context",
    "load_prepared_option_positions_context",
    "load_prepared_option_positions_context_receipt",
    "prepare_option_positions_contexts",
]
