from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.position_fields import normalize_account, normalize_broker
from domain.domain.performance.models import (
    EvidenceEnvelope,
    FXRateFact,
    ValuationMarkFact,
    normalize_currency,
    select_fx_rate,
    select_valuation_mark,
)
from domain.domain.portfolio_scope import portfolio_scope_id
from domain.services import adapt_option_positions_context
from src.infrastructure.exchange_rates import (
    exchange_rate_observation_status,
    get_exchange_rates_or_fetch_latest,
)
from src.application.ledger.api import (
    CURRENT_DECISION_READ_SCHEMA,
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
from src.application.performance.adapters import (
    ledger_performance_inputs_from_rows,
    load_option_valuation_inputs,
)
from src.application.performance.evidence_collection import (
    collect_current_performance_evidence,
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


PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA = "prepared_option_positions_context.v2"
LEGACY_PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA = (
    "prepared_option_positions_context.v1"
)
PREPARED_OPTION_POSITIONS_PAYLOAD_NAME = "option_positions_context.json"
PREPARED_OPTION_POSITIONS_MANIFEST_NAME = (
    "prepared_option_positions_context.v2.json"
)
LEGACY_PREPARED_OPTION_POSITIONS_MANIFEST_NAME = (
    "prepared_option_positions_context.v1.json"
)
PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V1 = (
    LEGACY_PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA
)
PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2 = (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA
)
PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMAS = (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V1,
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2,
)
PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V1 = (
    LEGACY_PREPARED_OPTION_POSITIONS_MANIFEST_NAME
)
PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2 = (
    PREPARED_OPTION_POSITIONS_MANIFEST_NAME
)
OPTION_MARKET_EVIDENCE_SCHEMA = "option_market_evidence.v1"
OPTION_MARKET_EVIDENCE_SELECTION_POLICY = "performance_evidence.latest_at_or_before.v1"

_OPTION_MARKET_EVIDENCE_FIELDS = {
    "schema_version",
    "status",
    "reason_code",
    "run_id",
    "account",
    "account_config_sha256",
    "evidence_at_utc",
    "selection_policy_version",
    "ledger_generation_sha256_a",
    "ledger_generation_sha256_b",
    "decision_state_fingerprint_a",
    "decision_state_fingerprint_b",
    "open_option_positions",
    "valuation_mark_facts",
    "fx_rate_facts",
    "content_sha256",
}
_OPTION_MARKET_POSITION_FIELDS = {
    "lot_id",
    "account",
    "broker",
    "instrument_key",
    "symbol",
    "option_type",
    "strike",
    "expiration_ymd",
    "currency",
    "multiplier",
    "position_side",
    "contracts_open",
    "market_code",
}
_OPTION_MARKET_MARK_FIELDS = {
    "fact_id",
    "instrument_key",
    "price",
    "mark_kind",
    "effective_at_ms",
    "observed_at_ms",
    "source",
    "source_id",
    "revision",
    "supersedes_fact_id",
    "source_fact_sha256",
}
_OPTION_MARKET_FX_FIELDS = {
    "fact_id",
    "base_currency",
    "quote_currency",
    "rate",
    "rate_kind",
    "effective_at_ms",
    "observed_at_ms",
    "source",
    "source_id",
    "revision",
    "supersedes_fact_id",
    "source_fact_sha256",
}


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


def _reuse_existing_valuation_marks(
    envelope: EvidenceEnvelope,
    existing_marks: tuple[ValuationMarkFact, ...],
) -> EvidenceEnvelope:
    existing_by_source = {fact.source_identity: fact for fact in existing_marks}
    marks: list[ValuationMarkFact] = []
    for fact in envelope.valuation_marks:
        existing = existing_by_source.get(fact.source_identity)
        if existing is None:
            marks.append(fact)
            continue
        incoming_payload = fact.normalized_payload(include_fact_id=False)
        existing_payload = existing.normalized_payload(include_fact_id=False)
        incoming_payload.pop("observed_at_ms")
        existing_payload.pop("observed_at_ms")
        marks.append(existing if incoming_payload == existing_payload else fact)
    return EvidenceEnvelope(valuation_marks=tuple(marks))


def _persist_current_option_marks(
    *,
    configs: Mapping[str, Mapping[str, Any]],
    accounts_by_ledger_path: Mapping[Path, list[str]],
    repos_by_ledger_path: Mapping[Path, Any],
    rows_a_by_ledger_path: Mapping[Path, Mapping[str, Mapping[str, Any]]],
    mark_evidence_accounts: frozenset[str],
    config_path: Path,
    now_ms: int,
    log: Callable[[str], None] | None,
) -> dict[Path, frozenset[str]]:
    captured_fact_ids: dict[Path, frozenset[str]] = {}
    for ledger_path, accounts in accounts_by_ledger_path.items():
        selected_accounts = sorted(mark_evidence_accounts.intersection(accounts))
        if not selected_accounts:
            continue
        captured_fact_ids[ledger_path] = frozenset()
        repo = repos_by_ledger_path.get(ledger_path)
        rows_by_account = rows_a_by_ledger_path.get(ledger_path)
        if repo is None or not isinstance(rows_by_account, Mapping):
            continue
        try:
            positions = []
            for account in selected_accounts:
                portfolio = configs[account].get("portfolio")
                portfolio = portfolio if isinstance(portfolio, Mapping) else {}
                valuation = load_option_valuation_inputs(
                    ledger_performance_inputs_from_rows(rows_by_account[account]),
                    as_of_ms=now_ms,
                    account=account,
                    broker=normalize_broker(portfolio.get("broker") or "富途"),
                )
                if valuation.diagnostics:
                    raise ValueError("option valuation projection is incomplete")
                positions.extend(valuation.positions)
            if not positions:
                continue
            collection = collect_current_performance_evidence(
                period_status="partial_current",
                refresh_quotes=True,
                option_positions=positions,
                now_ms=now_ms,
                cfg=configs[selected_accounts[0]],
                base_dir=config_path.parent,
                fx_payload_fetcher=lambda: None,
            )
            evidence_repo = open_performance_evidence_repository(repo)
            envelope = _reuse_existing_valuation_marks(
                EvidenceEnvelope(valuation_marks=collection.valuation_marks),
                evidence_repo.read_all().valuation_marks,
            )
            try:
                evidence_repo.import_envelope(
                    envelope,
                    apply=True,
                    migrated_at_ms=now_ms,
                )
            except Exception:
                envelope = _reuse_existing_valuation_marks(
                    EvidenceEnvelope(valuation_marks=collection.valuation_marks),
                    evidence_repo.read_all().valuation_marks,
                )
                evidence_repo.import_envelope(
                    envelope,
                    apply=True,
                    migrated_at_ms=now_ms,
                )
            captured_fact_ids[ledger_path] = frozenset(
                str(fact.fact_id) for fact in envelope.valuation_marks
            )
        except Exception as exc:
            if log is not None:
                log(
                    "[WARN] formal option mark evidence capture failed: "
                    f"{type(exc).__name__}"
                )
    return captured_fact_ids


def _ledger_generation_sha256(
    rows_by_account: Mapping[str, Mapping[str, Any]],
    accounts: list[str],
) -> str:
    first_rows = rows_by_account[accounts[0]]
    return canonical_sha256(
        {
            "trade_events": list(first_rows["trade_events"]),
            "stored_position_lots": list(first_rows["stored_position_lots"]),
            "wheel_events_by_account": {
                account: list(
                    rows_by_account[account].get("account_wheel_events") or []
                )
                for account in sorted(accounts)
            },
        }
    )


def _scan_currency(config_path: Path) -> str:
    name = Path(config_path).name.lower()
    if ".hk." in name or name.endswith(".hk.json"):
        return "HKD"
    if ".us." in name or name.endswith(".us.json"):
        return "USD"
    raise ValueError("prepared option scan market is unavailable")


def _minimal_mark_fact(fact: ValuationMarkFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "instrument_key": fact.instrument_key,
        "price": str(fact.price),
        "mark_kind": fact.mark_kind,
        "effective_at_ms": fact.effective_at_ms,
        "observed_at_ms": fact.observed_at_ms,
        "source": fact.source,
        "source_id": fact.source_id,
        "revision": fact.revision,
        "supersedes_fact_id": fact.supersedes_fact_id,
        "source_fact_sha256": canonical_sha256(
            fact.normalized_payload(include_fact_id=True)
        ),
    }


def _minimal_fx_fact(fact: FXRateFact) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "base_currency": fact.base_currency,
        "quote_currency": fact.quote_currency,
        "rate": str(fact.rate),
        "rate_kind": fact.rate_kind,
        "effective_at_ms": fact.effective_at_ms,
        "observed_at_ms": fact.observed_at_ms,
        "source": fact.source,
        "source_id": fact.source_id,
        "revision": fact.revision,
        "supersedes_fact_id": fact.supersedes_fact_id,
        "source_fact_sha256": canonical_sha256(
            fact.normalized_payload(include_fact_id=True)
        ),
    }


def _option_market_evidence_payload(
    *,
    run_id: str,
    account: str,
    account_config_sha256: str,
    evidence_at_utc: str,
    ledger_generation_sha256_a: str,
    ledger_generation_sha256_b: str,
    decision_state_fingerprint_a: str,
    decision_state_fingerprint_b: str,
    status: str,
    reason_code: str | None,
    open_option_positions: list[dict[str, Any]] | None = None,
    valuation_mark_facts: list[dict[str, Any]] | None = None,
    fx_rate_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": OPTION_MARKET_EVIDENCE_SCHEMA,
        "status": status,
        "reason_code": reason_code,
        "run_id": run_id,
        "account": account,
        "account_config_sha256": account_config_sha256,
        "evidence_at_utc": evidence_at_utc,
        "selection_policy_version": OPTION_MARKET_EVIDENCE_SELECTION_POLICY,
        "ledger_generation_sha256_a": ledger_generation_sha256_a,
        "ledger_generation_sha256_b": ledger_generation_sha256_b,
        "decision_state_fingerprint_a": decision_state_fingerprint_a,
        "decision_state_fingerprint_b": decision_state_fingerprint_b,
        "open_option_positions": open_option_positions or [],
        "valuation_mark_facts": valuation_mark_facts or [],
        "fx_rate_facts": fx_rate_facts or [],
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def build_option_market_evidence_payload(
    *,
    run_id: str,
    account: str,
    account_config_sha256: str,
    broker: str,
    scan_currency: str,
    rows_a: Mapping[str, Any],
    evidence_bundle: Any,
    evidence_at_utc: str,
    ledger_generation_sha256_a: str,
    ledger_generation_sha256_b: str,
    decision_state_fingerprint_a: str,
    decision_state_fingerprint_b: str,
    captured_mark_fact_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build the strict evidence slice without reading mutable state."""

    common = {
        "run_id": run_id,
        "account": account,
        "account_config_sha256": account_config_sha256,
        "evidence_at_utc": evidence_at_utc,
        "ledger_generation_sha256_a": ledger_generation_sha256_a,
        "ledger_generation_sha256_b": ledger_generation_sha256_b,
        "decision_state_fingerprint_a": decision_state_fingerprint_a,
        "decision_state_fingerprint_b": decision_state_fingerprint_b,
    }
    if (
        ledger_generation_sha256_a != ledger_generation_sha256_b
        or decision_state_fingerprint_a != decision_state_fingerprint_b
    ):
        return _option_market_evidence_payload(
            **common,
            status="unavailable",
            reason_code="option_market_evidence_position_drift",
        )
    if str(getattr(evidence_bundle, "schema_state", "")) != "initialized_v1":
        return _option_market_evidence_payload(
            **common,
            status="unavailable",
            reason_code="option_market_evidence_repository_unavailable",
        )
    try:
        evidence_at_ms = int(
            datetime.fromisoformat(evidence_at_utc.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .timestamp()
            * 1000
        )
        ledger_inputs = ledger_performance_inputs_from_rows(rows_a)
        valuation_inputs = load_option_valuation_inputs(
            ledger_inputs,
            as_of_ms=evidence_at_ms,
            account=account,
            broker=broker,
        )
        account_norm = normalize_account(account)
        broker_norm = normalize_broker(broker)
        for diagnostic in valuation_inputs.diagnostics:
            diagnostic_account = normalize_account(diagnostic.get("account"))
            diagnostic_broker = normalize_broker(diagnostic.get("broker"))
            if (
                not diagnostic_account or diagnostic_account == account_norm
            ) and (not diagnostic_broker or diagnostic_broker == broker_norm):
                raise ValueError("option valuation projection is incomplete")
        positions = list(valuation_inputs.positions)
        open_positions = [
            {
                "lot_id": position.lot_id,
                "account": position.account,
                "broker": position.broker,
                "instrument_key": position.instrument.instrument_key,
                "symbol": position.instrument.symbol,
                "option_type": position.instrument.option_type,
                "strike": str(position.instrument.strike),
                "expiration_ymd": position.instrument.expiration_ymd,
                "currency": position.instrument.currency,
                "multiplier": str(position.instrument.multiplier),
                "position_side": position.position_side,
                "contracts_open": position.contracts_open,
                "market_code": position.market_code,
            }
            for position in positions
        ]
    except Exception:
        return _option_market_evidence_payload(
            **common,
            status="unavailable",
            reason_code="option_market_evidence_position_invalid",
        )

    marks: list[dict[str, Any]] = []
    for instrument_key in sorted(
        {position.instrument.instrument_key for position in positions}
    ):
        selected = select_valuation_mark(
            evidence_bundle.valuation_marks,
            instrument_key=instrument_key,
            at_ms=evidence_at_ms,
        )
        if (
            not isinstance(selected.fact, ValuationMarkFact)
            or selected.fact.observed_at_ms > evidence_at_ms
            or (
                captured_mark_fact_ids is not None
                and str(selected.fact.fact_id) not in captured_mark_fact_ids
            )
        ):
            return _option_market_evidence_payload(
                **common,
                status="unavailable",
                reason_code="option_market_evidence_mark_missing",
            )
        marks.append(_minimal_mark_fact(selected.fact))

    try:
        currencies = {
            normalize_currency(scan_currency),
            *(position.instrument.currency for position in positions),
        }
    except ValueError:
        return _option_market_evidence_payload(
            **common,
            status="unavailable",
            reason_code="option_market_evidence_fx_missing",
        )
    rates: list[dict[str, Any]] = []
    for currency in sorted(currencies - {"CNY"}):
        selected = select_fx_rate(
            evidence_bundle.fx_rates,
            base_currency=currency,
            quote_currency="CNY",
            at_ms=evidence_at_ms,
        )
        if (
            not isinstance(selected.fact, FXRateFact)
            or selected.fact.observed_at_ms > evidence_at_ms
        ):
            return _option_market_evidence_payload(
                **common,
                status="unavailable",
                reason_code="option_market_evidence_fx_missing",
            )
        rates.append(_minimal_fx_fact(selected.fact))

    return _option_market_evidence_payload(
        **common,
        status="ready",
        reason_code=None,
        open_option_positions=sorted(open_positions, key=lambda item: item["lot_id"]),
        valuation_mark_facts=marks,
        fx_rate_facts=rates,
    )


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
    mark_evidence_accounts: tuple[str, ...] = (),
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
    mark_evidence_account_set = frozenset(
        account
        for account in (normalize_account(value) for value in mark_evidence_accounts)
        if account in configs
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

    observed_at = datetime.now(timezone.utc)
    observed_at_utc = observed_at.isoformat()
    lifecycle_now_ms = int(observed_at.timestamp() * 1000)
    rows_a_by_ledger_path: dict[Path, dict[str, dict[str, Any]]] = {}
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
            rows_a_by_ledger_path[ledger_path] = read_decision_state_rows_many(
                repo,
                accounts=tuple(sorted(accounts)),
            )
            repos_by_ledger_path[ledger_path] = repo
            ledger_read_count += 1
        except Exception as exc:
            reason = f"coherent_position_ledger_unavailable:{type(exc).__name__}"
            for account in accounts:
                unavailable[account] = reason

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
    captured_mark_fact_ids_by_ledger = _persist_current_option_marks(
        configs=configs,
        accounts_by_ledger_path=accounts_by_ledger_path,
        repos_by_ledger_path=repos_by_ledger_path,
        rows_a_by_ledger_path=rows_a_by_ledger_path,
        mark_evidence_accounts=mark_evidence_account_set,
        config_path=Path(config_path),
        now_ms=lifecycle_now_ms,
        log=log,
    )

    evidence_by_ledger_path: dict[Path, Any] = {}
    rows_b_by_ledger_path: dict[Path, dict[str, dict[str, Any]]] = {}
    evidence_at_by_ledger_path: dict[Path, str] = {}
    for ledger_path, accounts in sorted(
        accounts_by_ledger_path.items(),
        key=lambda item: str(item[0]),
    ):
        repo = repos_by_ledger_path.get(ledger_path)
        if repo is None:
            continue
        try:
            evidence_by_ledger_path[ledger_path] = (
                open_performance_evidence_repository(repo).read_all()
            )
        except Exception:
            evidence_by_ledger_path[ledger_path] = None
        try:
            rows_b_by_ledger_path[ledger_path] = read_decision_state_rows_many(
                repo,
                accounts=tuple(sorted(accounts)),
            )
            ledger_read_count += 1
            evidence_at_by_ledger_path[ledger_path] = datetime.now(
                timezone.utc
            ).isoformat()
        except Exception:
            evidence_at_by_ledger_path[ledger_path] = datetime.now(
                timezone.utc
            ).isoformat()

    manifests: dict[str, dict[str, Any]] = {}
    records_by_account: dict[str, list[dict[str, Any]]] = {}
    wheel_models_by_account: dict[str, dict[str, Any]] = {}
    for ledger_path, accounts in sorted(
        accounts_by_ledger_path.items(),
        key=lambda item: str(item[0]),
    ):
        rows_a_by_account = rows_a_by_ledger_path.get(ledger_path)
        rows_b_by_account = rows_b_by_ledger_path.get(ledger_path)
        rows_by_account = rows_b_by_account or rows_a_by_account
        if not isinstance(rows_by_account, dict):
            continue
        try:
            first_rows = rows_by_account[accounts[0]]
            if not isinstance(rows_a_by_account, dict):
                raise ValueError("position fence snapshot A is unavailable")
            ledger_generation_sha256_a = _ledger_generation_sha256(
                rows_a_by_account,
                accounts,
            )
            ledger_generation_sha256_b = (
                _ledger_generation_sha256(rows_b_by_account, accounts)
                if isinstance(rows_b_by_account, dict)
                else ""
            )
            ledger_generation_sha256 = _ledger_generation_sha256(
                rows_by_account,
                accounts,
            )
            records = list(first_rows["stored_position_lots"])
            snapshots = {}
            snapshots_a = {}
            for account in accounts:
                snapshots_a[account] = decision_state_snapshot_from_rows(
                    rows_a_by_account[account],
                    account=account,
                    portfolio_scope_id=portfolio_scope_id(account),
                    source_observed_at=observed_at_utc,
                    current_projection=None,
                    current_decision_now_ms=lifecycle_now_ms,
                )
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
                decision_state_fingerprint_a = str(
                    snapshots_a[account].get("decision_state_fingerprint") or ""
                )
                decision_state_fingerprint_b = (
                    str(
                        snapshots[account].get("decision_state_fingerprint")
                        or ""
                    )
                    if isinstance(rows_b_by_account, dict)
                    else ""
                )
                try:
                    option_market_evidence = build_option_market_evidence_payload(
                        run_id=run_id_norm,
                        account=account,
                        account_config_sha256=authority.account_config_sha256,
                        broker=broker,
                        scan_currency=_scan_currency(Path(config_path)),
                        rows_a=rows_a_by_account[account],
                        evidence_bundle=evidence_by_ledger_path.get(ledger_path),
                        evidence_at_utc=evidence_at_by_ledger_path[ledger_path],
                        ledger_generation_sha256_a=(
                            ledger_generation_sha256_a
                        ),
                        ledger_generation_sha256_b=(
                            ledger_generation_sha256_b
                        ),
                        decision_state_fingerprint_a=(
                            decision_state_fingerprint_a
                        ),
                        decision_state_fingerprint_b=(
                            decision_state_fingerprint_b
                        ),
                        captured_mark_fact_ids=(
                            captured_mark_fact_ids_by_ledger.get(ledger_path)
                            if account in mark_evidence_account_set
                            else None
                        ),
                    )
                except Exception:
                    option_market_evidence = _option_market_evidence_payload(
                        run_id=run_id_norm,
                        account=account,
                        account_config_sha256=authority.account_config_sha256,
                        evidence_at_utc=evidence_at_by_ledger_path.get(
                            ledger_path,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                        ledger_generation_sha256_a=ledger_generation_sha256_a,
                        ledger_generation_sha256_b=ledger_generation_sha256_b,
                        decision_state_fingerprint_a=(
                            decision_state_fingerprint_a
                        ),
                        decision_state_fingerprint_b=(
                            decision_state_fingerprint_b
                        ),
                        status="unavailable",
                        reason_code="option_market_evidence_contract_missing",
                    )
                prepared_authority = {
                    "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2,
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
                decision_snapshot = snapshots[account]
                context["current_decision_read"] = dict(
                    decision_snapshot["current_decision_read"]
                )
                context["decision_snapshot_actionable"] = bool(
                    decision_snapshot.get("actionable") is True
                )
                context["current_decision_shadow"] = dict(
                    decision_snapshot["current_decision_shadow"]
                )
                context["strategy_lab_option_market_evidence"] = (
                    option_market_evidence
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


def find_prepared_option_positions_manifest(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> Path | None:
    run_id_norm = _required_text(run_id, "run_id")
    account_norm = normalize_account(account)
    if (
        run_id_norm in {".", ".."}
        or "/" in run_id_norm
        or "\\" in run_id_norm
        or not account_norm
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option manifest identity is invalid"
        )
    state_dir = (
        Path(base).resolve()
        / "output_runs"
        / run_id_norm
        / "accounts"
        / account_norm
        / "state"
    )
    for name in (
        PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2,
        PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V1,
    ):
        path = (state_dir / name).resolve()
        if path.is_file():
            return path
    return None


def _validate_option_market_evidence(
    evidence: Any,
    *,
    manifest: Mapping[str, Any],
) -> None:
    if not isinstance(evidence, Mapping) or set(evidence) != (
        _OPTION_MARKET_EVIDENCE_FIELDS
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence contract is invalid"
        )
    if evidence.get("schema_version") != OPTION_MARKET_EVIDENCE_SCHEMA:
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence schema mismatch"
        )
    if evidence.get("selection_policy_version") != (
        OPTION_MARKET_EVIDENCE_SELECTION_POLICY
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence selection policy mismatch"
        )
    try:
        evidence_at = datetime.fromisoformat(
            str(evidence.get("evidence_at_utc") or "").replace("Z", "+00:00")
        )
        if evidence_at.utcoffset() != timezone.utc.utcoffset(evidence_at):
            raise ValueError
        evidence_at_ms = int(evidence_at.timestamp() * 1000)
    except (TypeError, ValueError) as exc:
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence timestamp is invalid"
        ) from exc
    if evidence.get("status") != "ready" or evidence.get("reason_code") is not None:
        raise PreparedOptionPositionsContextError(
            str(
                evidence.get("reason_code")
                or "option_market_evidence_missing"
            )
        )
    for key in ("run_id", "account", "account_config_sha256"):
        if str(evidence.get(key) or "") != str(manifest.get(key) or ""):
            raise PreparedOptionPositionsContextError(
                f"prepared option market evidence mismatch: {key}"
            )
    for key in (
        "ledger_generation_sha256_a",
        "ledger_generation_sha256_b",
        "decision_state_fingerprint_a",
        "decision_state_fingerprint_b",
    ):
        _required_sha256(evidence.get(key), key)
    if (
        evidence["ledger_generation_sha256_a"]
        != evidence["ledger_generation_sha256_b"]
        or evidence["ledger_generation_sha256_b"]
        != manifest.get("ledger_generation_sha256")
        or evidence["decision_state_fingerprint_a"]
        != evidence["decision_state_fingerprint_b"]
        or evidence["decision_state_fingerprint_b"]
        != manifest.get("decision_state_fingerprint")
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence position drift"
        )
    supplied_hash = _required_sha256(
        evidence.get("content_sha256"),
        "option market evidence content_sha256",
    )
    content = dict(evidence)
    content.pop("content_sha256")
    if canonical_sha256(content) != supplied_hash:
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence content hash mismatch"
        )

    rows_by_field = {
        "open_option_positions": _OPTION_MARKET_POSITION_FIELDS,
        "valuation_mark_facts": _OPTION_MARKET_MARK_FIELDS,
        "fx_rate_facts": _OPTION_MARKET_FX_FIELDS,
    }
    for field, expected_fields in rows_by_field.items():
        rows = evidence.get(field)
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) or set(row) != expected_fields
            for row in rows
        ):
            raise PreparedOptionPositionsContextError(
                f"prepared option market evidence {field} is invalid"
            )
    positions = evidence["open_option_positions"]
    marks = evidence["valuation_mark_facts"]
    rates = evidence["fx_rate_facts"]
    if any(
        normalize_account(row.get("account")) != manifest.get("account")
        for row in positions
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence account mismatch"
        )
    lot_ids = [str(row.get("lot_id") or "") for row in positions]
    if (
        not all(lot_ids)
        or len(lot_ids) != len(set(lot_ids))
        or any(
            row.get("position_side") not in {"short", "long"}
            or isinstance(row.get("contracts_open"), bool)
            or not isinstance(row.get("contracts_open"), int)
            or row["contracts_open"] <= 0
            for row in positions
        )
    ):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence positions are invalid"
        )
    instrument_keys = {str(row.get("instrument_key") or "") for row in positions}
    mark_keys = {str(row.get("instrument_key") or "") for row in marks}
    if not all(instrument_keys) or instrument_keys != mark_keys:
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence mark coverage mismatch"
        )
    if len(mark_keys) != len(marks):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence marks are duplicated"
        )
    try:
        currencies = {
            normalize_currency(row.get("currency")) for row in positions
        } - {"CNY"}
        fx_currencies = {
            normalize_currency(row.get("base_currency"))
            for row in rates
            if normalize_currency(row.get("quote_currency")) == "CNY"
        }
    except ValueError as exc:
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence currency is invalid"
        ) from exc
    if not currencies.issubset(fx_currencies) or len(fx_currencies) != len(rates):
        raise PreparedOptionPositionsContextError(
            "prepared option market evidence FX coverage mismatch"
        )
    for row in [*marks, *rates]:
        _required_text(row.get("fact_id"), "option market evidence fact_id")
        _required_sha256(row.get("source_fact_sha256"), "source_fact_sha256")
        for field in ("effective_at_ms", "observed_at_ms"):
            value = row.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > evidence_at_ms
            ):
                raise PreparedOptionPositionsContextError(
                    f"prepared option market evidence {field} is invalid"
                )


def validate_strategy_lab_option_market_evidence(
    evidence: Any,
    *,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    _validate_option_market_evidence(evidence, manifest=manifest)
    return dict(evidence)


def _load_prepared_option_positions_context_artifacts(
    *,
    manifest_path: Path,
    expected_base: Path,
    expected_run_id: str,
    expected_account: str,
    expected_account_config_sha256: str,
    expected_manifest_sha256: str | None = None,
    expected_runtime_config: Mapping[str, Any] | None = None,
    require_option_market_evidence: bool = False,
) -> dict[str, Any]:
    run_id = _required_text(expected_run_id, "expected_run_id")
    account = normalize_account(expected_account)
    if not account:
        raise PreparedOptionPositionsContextError(
            "expected prepared option account is invalid"
        )
    supplied_path = Path(
        os.path.abspath(str(Path(manifest_path).expanduser()))
    )
    manifest_name = supplied_path.name
    supported_manifests = {
        PREPARED_OPTION_POSITIONS_MANIFEST_NAME: PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA,
        LEGACY_PREPARED_OPTION_POSITIONS_MANIFEST_NAME: (
            LEGACY_PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA
        ),
    }
    expected_schema = supported_manifests.get(manifest_name)
    if expected_schema is None:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest path mismatch"
        )
    expected_path = (
        Path(expected_base).resolve()
        / "output_runs"
        / run_id
        / "accounts"
        / account
        / "state"
        / manifest_name
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
            name=manifest_name,
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
    if manifest.get("schema_version") != expected_schema:
        raise PreparedOptionPositionsContextError(
            "prepared option manifest schema mismatch"
        )
    if require_option_market_evidence and expected_schema != (
        PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2
    ):
        raise PreparedOptionPositionsContextError(
            "option_market_evidence_contract_missing"
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
        "schema_version",
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
    if expected_schema == LEGACY_PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA:
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
    else:
        current_read = payload.get("current_decision_read")
        if not isinstance(current_read, Mapping):
            raise PreparedOptionPositionsContextError(
                "prepared option current decision read is missing"
            )
        if set(current_read) == {"status", "reason"}:
            if str(current_read.get("status") or "") != "data_unavailable":
                raise PreparedOptionPositionsContextError(
                    "prepared option current decision read is invalid"
                )
        elif (
            current_read.get("schema_version") != CURRENT_DECISION_READ_SCHEMA
            or normalize_account(current_read.get("account")) != account
            or not isinstance(current_read.get("position_lots"), list)
            or (
                current_read.get("payload") is not None
                and not isinstance(current_read.get("payload"), Mapping)
            )
        ):
            raise PreparedOptionPositionsContextError(
                "prepared option current decision read is invalid"
            )
        if not isinstance(payload.get("decision_snapshot_actionable"), bool):
            raise PreparedOptionPositionsContextError(
                "prepared option decision actionability is invalid"
            )
        if not isinstance(payload.get("current_decision_shadow"), Mapping):
            raise PreparedOptionPositionsContextError(
                "prepared option current decision shadow is missing"
            )
        decision_fingerprint = str(
            payload.get("decision_state_fingerprint") or ""
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
    if require_option_market_evidence:
        _validate_option_market_evidence(
            payload.get("strategy_lab_option_market_evidence"),
            manifest=manifest,
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
    require_option_market_evidence: bool = False,
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
        require_option_market_evidence=require_option_market_evidence,
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
    require_option_market_evidence: bool = False,
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
        require_option_market_evidence=require_option_market_evidence,
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
        "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2,
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
        "schema_version": PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2,
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
        name=PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2,
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
    prepared = context.get("prepared_authority")
    if (
        isinstance(prepared, Mapping)
        and prepared.get("schema_version")
        == PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2
    ):
        evidence = context.get("strategy_lab_option_market_evidence")
        if isinstance(evidence, Mapping) and evidence.get("status") == "ready":
            _validate_option_market_evidence(
                evidence,
                manifest={
                    "run_id": prepared.get("run_id"),
                    "account": prepared.get("account"),
                    "account_config_sha256": prepared.get(
                        "account_config_sha256"
                    ),
                    "ledger_generation_sha256": prepared.get(
                        "ledger_generation_sha256"
                    ),
                    "decision_state_fingerprint": context.get(
                        "decision_state_fingerprint"
                    ),
                },
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
            separators=(",", ":"),
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
    "LEGACY_PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA",
    "LEGACY_PREPARED_OPTION_POSITIONS_MANIFEST_NAME",
    "PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA",
    "PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V1",
    "PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2",
    "PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMAS",
    "PREPARED_OPTION_POSITIONS_MANIFEST_NAME",
    "PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V1",
    "PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2",
    "PREPARED_OPTION_POSITIONS_PAYLOAD_NAME",
    "PreparedOptionPositionsBatch",
    "PreparedOptionPositionsContextError",
    "build_option_market_evidence_payload",
    "cny_per_currency_rates_from_option_context",
    "exchange_rate_scalars_from_option_context",
    "find_prepared_option_positions_manifest",
    "load_prepared_option_positions_context",
    "load_prepared_option_positions_context_receipt",
    "prepare_option_positions_contexts",
    "validate_strategy_lab_option_market_evidence",
]
