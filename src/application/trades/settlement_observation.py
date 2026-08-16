from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from domain.domain.lifecycle_allocation import resolve_allocations
from src.application.ledger.api import (
    latest_trade_lifecycle_settlement_evidence,
    lifecycle_case_coherent_facts,
)
from src.application.trades.close_reason_evidence import (
    build_broker_settlement_observation,
    build_settlement_source_receipt,
    canonical_hash,
    settlement_receipt_retcode_succeeded,
)
from src.application.trades.settlement_attempts import (
    SettlementAttemptOutcome,
    SettlementCapabilitySnapshot,
    SettlementCollectorContract,
    SETTLEMENT_OBSERVATION_CONTEXT_KEY,
    classify_exception_outcome,
    classify_observation_outcome,
    inspect_settlement_capabilities,
)


class SettlementObservationDataError(ValueError):
    """Case-local input prevents a safe broker observation."""


class LifecycleObservationGenerationChanged(RuntimeError):
    """The lifecycle facts changed after the due decision was prepared."""


@dataclass(frozen=True)
class SettlementObservationCollector:
    repo: Any
    broker_gateway: Any | None
    quote_gateway: Any | None
    quote_dependency_error: str | None
    allowed_futu_account_ids: frozenset[str]
    trd_env: str
    now_ms_fn: Callable[[], int]
    source_id: str
    contract: SettlementCollectorContract
    capability: SettlementCapabilitySnapshot

    def __call__(
        self,
        lifecycle_case: dict[str, Any],
        read_model: dict[str, Any],
        *,
        before_first_provider_io: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        case_account_id = str(
            lifecycle_case.get("futu_account_id") or ""
        ).strip()
        if case_account_id not in self.allowed_futu_account_ids:
            raise SettlementObservationDataError(
                "lifecycle case Futu account binding is outside the source binding set"
            )
        return collect_broker_settlement_observation(
            self.repo,
            lifecycle_case=lifecycle_case,
            read_model=read_model,
            broker_gateway=self.broker_gateway,
            quote_gateway=self.quote_gateway,
            quote_dependency_error=self.quote_dependency_error,
            futu_account_id=case_account_id,
            trd_env=self.trd_env,
            now_ms=int(self.now_ms_fn()),
            before_first_provider_io=before_first_provider_io,
        )

    def collect_outcome(
        self,
        lifecycle_case: dict[str, Any],
        read_model: dict[str, Any],
        *,
        before_first_provider_io: Callable[[], None] | None = None,
    ) -> SettlementAttemptOutcome:
        case_id = str(lifecycle_case.get("case_id") or "").strip()
        account = str(
            lifecycle_case.get("account") or ""
        ).strip().lower()
        if not self.capability.supported:
            return SettlementAttemptOutcome(
                kind="blocked_static",
                source_id=self.source_id,
                account=account,
                case_id=case_id,
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                reason_code="missing_static_capability",
                error_class="missing_static",
            )
        provider_io_started = False
        provider_start_failed = False

        def mark_provider_io_started() -> None:
            nonlocal provider_io_started, provider_start_failed
            if provider_io_started:
                return
            try:
                if before_first_provider_io is not None:
                    before_first_provider_io()
            except Exception:
                provider_start_failed = True
                raise
            provider_io_started = True

        try:
            observation = self(
                lifecycle_case,
                read_model,
                before_first_provider_io=mark_provider_io_started,
            )
        except LifecycleObservationGenerationChanged:
            if provider_start_failed:
                raise
            return SettlementAttemptOutcome(
                kind="stale_generation",
                source_id=self.source_id,
                account=account,
                case_id=case_id,
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                reason_code="lifecycle_generation_changed",
                error_class="stale_generation",
            )
        except SettlementObservationDataError:
            if provider_start_failed:
                raise
            return SettlementAttemptOutcome(
                kind="unknown_error",
                source_id=self.source_id,
                account=account,
                case_id=case_id,
                contract_version=self.contract.contract_version,
                capability_fingerprint=(
                    self.capability.capability_fingerprint
                ),
                reason_code="settlement_observation_data_invalid",
                error_class="case_data",
            )
        except Exception as exc:
            if provider_start_failed:
                raise
            return classify_exception_outcome(
                exc,
                source_id=self.source_id,
                account=account,
                case_id=case_id,
                contract=self.contract,
                capability=self.capability,
            )
        return classify_observation_outcome(
            observation,
            source_id=self.source_id,
            account=account,
            case_id=case_id,
            contract=self.contract,
            capability=self.capability,
        )


def collect_broker_settlement_observation(
    repo: Any,
    *,
    lifecycle_case: dict[str, Any],
    read_model: dict[str, Any],
    gateway: Any | None = None,
    broker_gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    futu_account_id: str,
    trd_env: str = "REAL",
    now_ms: int,
    before_first_provider_io: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Collect one frozen, fail-closed settlement observation."""

    broker_gateway = broker_gateway or gateway
    quote_gateway = quote_gateway or gateway
    if broker_gateway is None:
        raise SettlementObservationDataError(
            "settlement observation requires a broker gateway"
        )
    environment = str(trd_env or "").strip().upper()
    if environment not in {"REAL", "SIMULATE"}:
        raise SettlementObservationDataError(
            "settlement observation trade environment is invalid"
        )

    case_id = str(lifecycle_case.get("case_id") or "").strip()
    account = str(lifecycle_case.get("account") or "").strip().lower()
    account_id = str(futu_account_id or "").strip()
    if not case_id or not account or not account_id:
        raise SettlementObservationDataError(
            "settlement observation account identity is incomplete"
        )
    if (
        str(lifecycle_case.get("futu_account_id") or "").strip()
        != account_id
    ):
        raise SettlementObservationDataError(
            "lifecycle case Futu account binding mismatch"
        )
    prepared_facts = read_model.get(SETTLEMENT_OBSERVATION_CONTEXT_KEY)
    facts = (
        dict(prepared_facts)
        if isinstance(prepared_facts, Mapping)
        else lifecycle_case_coherent_facts(repo, case_id=case_id)
    )
    prepared_token = str(
        read_model.get("lifecycle_generation_token") or ""
    ).strip()
    observed_token = str(
        facts["generation_token"].get("generation_token") or ""
    ).strip()
    if not prepared_token or prepared_token != observed_token:
        raise LifecycleObservationGenerationChanged(
            "lifecycle generation changed before provider collection"
        )
    frozen_case = dict(facts["lifecycle_case"])
    if frozen_case != lifecycle_case:
        raise LifecycleObservationGenerationChanged(
            "lifecycle case changed before provider collection"
        )
    timing_policy = facts.get("timing_policy")
    if not isinstance(timing_policy, dict):
        raise SettlementObservationDataError(
            "lifecycle timing policy is unavailable"
        )
    deadline_ms = int(
        timing_policy.get("settlement_deadline_ms") or 0
    )
    if deadline_ms <= 0:
        raise SettlementObservationDataError(
            "settlement deadline is unavailable"
        )
    case_resolution = dict(facts["case_resolution"])
    if case_resolution.get("status") == "conflict":
        raise SettlementObservationDataError(
            "option close anchor is invalid: "
            + ",".join(
                str(item)
                for item in case_resolution.get("reason_codes") or []
            )
        )
    evidence_rows = [
        dict(item) for item in facts["validated_anchors"]
    ]
    option_rows = sorted(
        (
            item
            for item in evidence_rows
            if str(item.get("evidence_type") or "").strip().lower()
            == "option_zero_price_close"
        ),
        key=lambda item: (
            int(item.get("received_at_ms") or 0),
            str(item.get("evidence_id") or ""),
        ),
    )
    if not option_rows:
        raise SettlementObservationDataError(
            "option close anchor evidence is unavailable"
        )
    anchor = option_rows[0]
    if str(anchor.get("futu_account_id") or "").strip() != account_id:
        raise SettlementObservationDataError(
            "option close anchor account mismatch"
        )
    anchor_key = str(anchor.get("source_event_id") or "").strip()
    anchor_execution_ms = int(
        anchor.get("event_time_ms")
        or anchor.get("trade_time_ms")
        or 0
    )
    if not anchor_key or anchor_execution_ms <= 0:
        raise SettlementObservationDataError(
            "option close anchor identity is incomplete"
        )

    timezone = ZoneInfo(str(timing_policy.get("timezone") or "UTC"))
    anchor_local = datetime.fromtimestamp(
        anchor_execution_ms / 1000,
        tz=timezone,
    )
    observed_local = datetime.fromtimestamp(
        int(now_ms) / 1000,
        tz=timezone,
    )
    start_ymd = anchor_local.date().isoformat()
    end_ymd = observed_local.date().isoformat()
    market = str(timing_policy.get("market") or "").strip().upper()
    contract_code = _extract_option_contract_code(anchor)
    query_base = {
        "start": start_ymd,
        "end": end_ymd,
        "trd_env": environment,
        "acc_id": account_id,
    }
    anchor_reason_codes: set[str] = set()
    expected_source_prefix = f"futu:{account}:{account_id}:"
    if (
        not anchor_key.startswith(expected_source_prefix)
        or anchor_key == expected_source_prefix
    ):
        anchor_reason_codes.add("option_anchor_source_key_invalid")
    if not _is_exact_zero(anchor.get("price")):
        anchor_reason_codes.add("option_anchor_price_not_zero")
    anchor_contracts = _positive_integer(anchor.get("contracts"))
    if anchor_contracts is None:
        anchor_reason_codes.add("option_anchor_contracts_invalid")
    if not _anchor_contract_matches_case(
        anchor,
        lifecycle_case=lifecycle_case,
    ):
        anchor_reason_codes.add("option_anchor_contract_identity_mismatch")
    receipts: dict[str, dict[str, Any]] = {}
    receipts["anchor_option_close"] = (
        build_settlement_source_receipt(
            source="anchor_option_close",
            query_input={"source_key": anchor_key},
            rows=[_allowlisted_anchor(anchor)],
            observed_at_ms=now_ms,
            retcode=0,
            coverage_complete=not anchor_reason_codes,
            pagination_complete=True,
            error=(
                ",".join(sorted(anchor_reason_codes))
                if anchor_reason_codes
                else None
            ),
        )
    )
    history_deals = _query_receipt(
        source="history_deals",
        query_input=query_base,
        observed_at_ms=now_ms,
        query=lambda: broker_gateway.get_history_deals(
            **query_base,
        ),
        before_first_provider_io=before_first_provider_io,
    )
    receipts["history_deals"] = history_deals
    history_orders = _query_receipt(
        source="history_orders",
        query_input=query_base,
        observed_at_ms=now_ms,
        query=lambda: broker_gateway.get_history_orders(
            **query_base,
        ),
        before_first_provider_io=before_first_provider_io,
    )
    receipts["history_orders"] = history_orders
    positions = _query_receipt(
        source="fresh_positions",
        query_input={
            "trd_env": environment,
            "acc_id": account_id,
            "refresh_cache": True,
        },
        observed_at_ms=now_ms,
        query=lambda: broker_gateway.get_positions_with_receipt(
            trd_env=environment,
            acc_id=account_id,
            refresh_cache=True,
        ),
        before_first_provider_io=before_first_provider_io,
    )
    receipts["fresh_positions"] = positions

    business_days = [
        str(item.get("date") or "")
        for item in list(timing_policy.get("trading_days") or [])
        if isinstance(item, dict)
        and str(item.get("date") or "")
        > str(lifecycle_case.get("expiration_ymd") or "")
        and str(item.get("type") or "").upper()
        in {"WHOLE", "TRADING"}
    ][:2]
    frozen_calendar_days = [
        str(item.get("date") or "").strip()
        for item in list(timing_policy.get("trading_days") or [])
        if isinstance(item, dict)
        and str(item.get("date") or "").strip()
    ]
    calendar_start = (
        min(frozen_calendar_days)
        if frozen_calendar_days
        else (
            date.fromisoformat(
                str(lifecycle_case.get("expiration_ymd") or "")
            )
            - timedelta(days=1)
        ).isoformat()
    )
    calendar_end = (
        max(frozen_calendar_days)
        if frozen_calendar_days
        else end_ymd
    )
    calendar_input = {
        "market": market,
        "start": calendar_start,
        "end": calendar_end,
    }
    if quote_gateway is None:
        calendar = build_settlement_source_receipt(
            source="trading_calendar",
            query_input=calendar_input,
            rows=[],
            observed_at_ms=now_ms,
            retcode="dependency_unavailable",
            coverage_complete=False,
            pagination_complete=False,
            error=(
                str(quote_dependency_error or "").strip()
                or "Futu quote dependency is unavailable"
            ),
            error_class="dependency_unavailable",
        )
    else:
        calendar = _query_receipt(
            source="trading_calendar",
            query_input=calendar_input,
            observed_at_ms=now_ms,
            query=lambda: quote_gateway.get_trading_days_with_receipt(
                market=market,
                start=calendar_start,
                end=calendar_end,
            ),
            before_first_provider_io=before_first_provider_io,
        )
    receipts["trading_calendar"] = calendar
    receipts["contract_metadata"] = (
        build_settlement_source_receipt(
            source="contract_metadata",
            query_input={
                "case_id": case_id,
                "policy_schema": timing_policy.get("policy_schema"),
            },
            rows=[
                {
                    "settlement_style": timing_policy.get(
                        "settlement_style"
                    ),
                    "underlying_security_type": timing_policy.get(
                        "underlying_security_type"
                    ),
                    "last_trade_cutoff_ms": timing_policy.get(
                        "last_trade_cutoff_ms"
                    ),
                    "last_trade_cutoff_source": timing_policy.get(
                        "last_trade_cutoff_source"
                    ),
                    "calendar_hash": timing_policy.get("calendar_hash"),
                }
            ],
            observed_at_ms=now_ms,
            retcode=0,
            coverage_complete=True,
            pagination_complete=True,
        )
    )

    target_manifest = {
        str(key): int(value)
        for key, value in dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ).items()
    }
    allocations = [
        dict(item)
        for item in facts.get("case_allocations") or []
        if isinstance(item, dict)
    ]
    void_event_ids = tuple(
        facts.get("effective_void_event_ids") or ()
    )
    resolution = resolve_allocations(
        target_manifest,
        allocations,
        void_event_ids=void_event_ids,
    )
    frozen_remaining = dict(
        resolution.remaining_contracts_by_lot
    )
    lot_fields_by_id = dict(
        facts.get("position_lot_fields_by_id") or {}
    )
    projection_remaining = {
        lot_id: int(
            (
                lot_fields_by_id.get(lot_id)
                if isinstance(
                    lot_fields_by_id.get(lot_id),
                    dict,
                )
                else {}
            ).get("contracts_open")
            or 0
        )
        for lot_id in target_manifest
    }
    reservation_exclusive = (
        dict(case_resolution.get("effective_reservations_by_lot") or {})
        == frozen_remaining
        and bool(case_resolution.get("anchor_facts"))
    )
    extra_incomplete: set[str] = set()
    extra_incomplete.update(anchor_reason_codes)
    if not contract_code:
        extra_incomplete.add("option_contract_code_missing")
    if len(business_days) < 2:
        extra_incomplete.add("two_business_days_unavailable")
    if int(read_model.get("pending_until_ms") or 0) != deadline_ms:
        extra_incomplete.add("read_model_timing_policy_mismatch")
    if calendar.get("status") == "complete":
        normalized_calendar = _normalize_calendar_rows(
            calendar.get("rows") or []
        )
        if canonical_hash(normalized_calendar) != str(
            timing_policy.get("calendar_hash") or ""
        ):
            extra_incomplete.add("calendar_hash_mismatch")

    anchor_deal_matches = [
        item
        for item in list(history_deals.get("rows") or [])
        if _row_matches_anchor_deal(
            item,
            anchor_key=anchor_key,
            contract_code=contract_code,
            futu_account_id=account_id,
        )
    ]
    if not anchor_deal_matches:
        extra_incomplete.add("option_anchor_history_deal_missing")
    elif len(anchor_deal_matches) > 1:
        extra_incomplete.add("option_anchor_history_deal_ambiguous")
    elif not _history_anchor_economics_match(
        anchor_deal_matches[0],
        anchor=anchor,
    ):
        extra_incomplete.add("option_anchor_history_deal_mismatch")

    order_rows = list(history_orders.get("rows") or [])
    normal_order_present, order_ambiguous = _normal_order_facts(
        order_rows,
        anchor_order_id=str(anchor.get("order_id") or ""),
    )
    if order_ambiguous:
        extra_incomplete.add("anchor_order_classification_ambiguous")
    stock_candidates = [
        candidate
        for item in list(history_deals.get("rows") or [])
        for candidate in [
            _stock_settlement_candidate(
                item,
                lifecycle_case=lifecycle_case,
                account=account,
                futu_account_id=account_id,
                timezone=timezone,
                settlement_deadline_ms=deadline_ms,
            )
        ]
        if candidate is not None
    ]
    option_position_absent = not any(
        _row_matches_option_contract(
            item,
            lifecycle_case=lifecycle_case,
            contract_code=contract_code,
        )
        and _nonzero_position(item)
        for item in list(positions.get("rows") or [])
    )
    source_claims = [
        dict(item)
        for item in facts.get("anchor_source_claims") or []
        if isinstance(item, dict)
    ]
    option_claims = [
        item
        for item in source_claims
        if str(item.get("source_role") or "") == "option_anchor"
    ]
    matching_option_claims = [
        item
        for item in option_claims
        if (
            str(item.get("source_key") or "").strip() == anchor_key
            and str(item.get("case_id") or "").strip()
            == str(
                anchor.get("source_owner_case_id") or case_id
            ).strip()
            and str(item.get("owner_evidence_id") or "").strip()
            == str(
                anchor.get("source_owner_evidence_id")
                or anchor.get("evidence_id")
                or ""
            ).strip()
            and isinstance(item.get("source_payload"), dict)
            and str(item.get("source_payload_hash") or "").strip()
            == canonical_hash(item["source_payload"])
        )
    ]
    if len(option_claims) != 1 or len(matching_option_claims) != 1:
        extra_incomplete.add("option_anchor_claim_not_unique")
    if (
        anchor_contracts is not None
        and anchor_contracts > sum(frozen_remaining.values())
    ):
        extra_incomplete.add(
            "option_anchor_contracts_exceed_frozen_remaining"
        )
    competing_consumption = (
        resolution.status != "ok"
        or case_resolution.get("status") == "conflict"
    )
    latest_settlement = latest_trade_lifecycle_settlement_evidence(
        repo,
        case_id=case_id,
    )
    previous_settlement_evidence_id = (
        str((latest_settlement or {}).get("evidence_id") or "").strip()
        or None
    )
    observation = build_broker_settlement_observation(
        case_id=case_id,
        account=account,
        futu_account_id=account_id,
        market=market,
        contract_identity={
            "symbol": lifecycle_case.get("symbol"),
            "option_contract_code": contract_code,
            "option_type": lifecycle_case.get("option_type"),
            "position_side": lifecycle_case.get("position_side"),
            "strike": lifecycle_case.get("strike"),
            "expiration_ymd": lifecycle_case.get("expiration_ymd"),
            "multiplier": lifecycle_case.get("multiplier"),
        },
        target_contracts_by_lot=target_manifest,
        frozen_preterminal_remaining_by_lot=frozen_remaining,
        anchor_option_deal_key=anchor_key,
        anchor_execution_time_ms=anchor_execution_ms,
        observed_at_ms=now_ms,
        settlement_deadline_ms=deadline_ms,
        query_window={"start": start_ymd, "end": end_ymd},
        source_receipts=receipts,
        calendar_hash=str(timing_policy.get("calendar_hash") or ""),
        broker_option_position_absent=option_position_absent,
        projection_matches_frozen_remaining=(
            projection_remaining == frozen_remaining
        ),
        reservation_exclusive=reservation_exclusive,
        competing_effective_consumption=competing_consumption,
        stock_settlement_present=bool(stock_candidates),
        stock_settlement_candidates=stock_candidates,
        normal_order_present=normal_order_present,
        additional_incomplete_reason_codes=extra_incomplete,
        observation_start_ms=(
            int(lifecycle_case["observation_start_ms"])
            if lifecycle_case.get("observation_start_ms") is not None
            else None
        ),
        expected_lifecycle_generation_token=observed_token,
        previous_settlement_evidence_id=(
            previous_settlement_evidence_id
        ),
    )
    return {
        **observation,
        "expected_lifecycle_generation_token": observed_token,
    }


def build_settlement_observation_collector(
    *,
    repo: Any,
    gateway: Any | None = None,
    broker_gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    futu_account_id: str | None = None,
    futu_account_ids: Iterable[str] | None = None,
    trd_env: str = "REAL",
    now_ms_fn: Callable[[], int],
    source_id: str = "settlement",
    required_capability_keys: Iterable[str] | None = None,
    additional_capability_requirements: (
        dict[str, tuple[str, str]] | None
    ) = None,
) -> SettlementObservationCollector:
    broker = broker_gateway or gateway
    quote = quote_gateway or gateway
    allowed = {
        str(value or "").strip()
        for value in ([futu_account_id] if futu_account_id else list(futu_account_ids or []))
        if str(value or "").strip()
    }

    contract = SettlementCollectorContract(
        required_capability_keys=(
            tuple(str(item) for item in required_capability_keys)
            if required_capability_keys is not None
            else SettlementCollectorContract().required_capability_keys
        )
    )
    capability = inspect_settlement_capabilities(
        broker_gateway=broker,
        quote_gateway=quote,
        contract=contract,
        additional_requirements=(
            additional_capability_requirements
        ),
    )
    return SettlementObservationCollector(
        repo=repo,
        broker_gateway=broker,
        quote_gateway=quote,
        quote_dependency_error=quote_dependency_error,
        allowed_futu_account_ids=frozenset(allowed),
        trd_env=str(trd_env or "REAL").strip().upper(),
        now_ms_fn=now_ms_fn,
        source_id=str(source_id or "settlement").strip(),
        contract=contract,
        capability=capability,
    )


def _query_receipt(
    *,
    source: str,
    query_input: dict[str, Any],
    observed_at_ms: int,
    query: Callable[[], Any],
    before_first_provider_io: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if before_first_provider_io is not None:
        before_first_provider_io()
    try:
        result = query()
    except Exception as exc:
        provider_code = str(
            getattr(exc, "code", "") or ""
        ).strip().upper()
        error_class = {
            "TRANSIENT": "transient",
            "RATE_LIMIT": "rate_limit",
            "AUTH_EXPIRED": "auth_expired",
            "NEED_2FA": "need_2fa",
            "TIMEOUT": "timeout",
            "PROVIDER_UNAVAILABLE": "provider_unavailable",
        }.get(provider_code, "timeout" if isinstance(exc, TimeoutError) else "unknown")
        return build_settlement_source_receipt(
            source=source,
            query_input=query_input,
            rows=[],
            observed_at_ms=observed_at_ms,
            retcode="error",
            coverage_complete=False,
            pagination_complete=False,
            error=f"{type(exc).__name__}: {exc}",
            error_class=error_class,
            provider_code=provider_code or None,
            retry_after_ms=_positive_integer(
                getattr(exc, "retry_after_ms", None)
            ),
        )
    if not isinstance(result, dict) or not isinstance(
        result.get("rows"), list
    ):
        return build_settlement_source_receipt(
            source=source,
            query_input=query_input,
            rows=[],
            observed_at_ms=observed_at_ms,
            retcode="coverage_unproven",
            coverage_complete=False,
            pagination_complete=False,
            error="gateway query receipt is unavailable",
            error_class="malformed_response",
        )
    retcode = result.get("retcode")
    error = str(result.get("error") or "") or None
    error_class = str(result.get("error_class") or "") or None
    provider_code = str(result.get("provider_code") or "") or None
    if (
        not error_class
        and not provider_code
        and (
            error is not None
            or not settlement_receipt_retcode_succeeded(retcode)
        )
    ):
        error_class = "unknown"
    return build_settlement_source_receipt(
        source=source,
        query_input=query_input,
        rows=[
            dict(item)
            for item in result.get("rows") or []
            if isinstance(item, dict)
        ],
        observed_at_ms=observed_at_ms,
        retcode=retcode,
        coverage_complete=bool(result.get("coverage_complete")),
        pagination_complete=bool(
            result.get("pagination_complete")
        ),
        stale=bool(result.get("stale")),
        fallback_cache=bool(result.get("fallback_cache")),
        error=error,
        error_class=error_class,
        provider_code=provider_code,
        retry_after_ms=_positive_integer(
            result.get("retry_after_ms")
        ),
    )


def _extract_option_contract_code(anchor: dict[str, Any]) -> str:
    raw = anchor.get("raw")
    raw_payload = (
        dict(raw.get("raw_payload") or {})
        if isinstance(raw, dict)
        and isinstance(raw.get("raw_payload"), dict)
        else {}
    )
    for value in (
        raw_payload.get("code"),
        raw_payload.get("stock_code"),
        raw.get("broker_symbol") if isinstance(raw, dict) else None,
    ):
        text = str(value or "").strip().upper()
        if text:
            return text
    return ""


def _is_exact_zero(value: Any) -> bool:
    if isinstance(value, bool) or value in (None, ""):
        return False
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return numeric.is_finite() and numeric == 0


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        return None
    return parsed


def _row_matches_anchor_deal(
    row: dict[str, Any],
    *,
    anchor_key: str,
    contract_code: str,
    futu_account_id: str,
) -> bool:
    deal_id = str(anchor_key or "").split(":", 3)[-1]
    row_deal_id = str(
        row.get("deal_id")
        or row.get("dealID")
        or row.get("id")
        or ""
    ).strip()
    if not deal_id or row_deal_id != deal_id:
        return False
    row_account_id = str(
        row.get("acc_id")
        or row.get("futu_account_id")
        or ""
    ).strip()
    if row_account_id and row_account_id != futu_account_id:
        return False
    row_code = str(
        row.get("code")
        or row.get("stock_code")
        or row.get("symbol")
        or ""
    ).strip().upper()
    return not contract_code or row_code == contract_code.upper()


def _history_anchor_economics_match(
    row: dict[str, Any],
    *,
    anchor: dict[str, Any],
) -> bool:
    row_contracts = _positive_integer(
        row.get("qty")
        or row.get("quantity")
        or row.get("contracts")
    )
    anchor_contracts = _positive_integer(anchor.get("contracts"))
    return (
        _is_exact_zero(
            row.get("price")
            if row.get("price") is not None
            else row.get("deal_price")
        )
        and row_contracts is not None
        and row_contracts == anchor_contracts
    )


def _anchor_contract_matches_case(
    anchor: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
) -> bool:
    text_fields = (
        "symbol",
        "option_type",
        "position_side",
        "expiration_ymd",
    )
    for key in text_fields:
        left = str(anchor.get(key) or "").strip().lower()
        right = str(lifecycle_case.get(key) or "").strip().lower()
        if not left or not right or left != right:
            return False
    try:
        anchor_strike = Decimal(str(anchor.get("strike")))
        case_strike = Decimal(str(lifecycle_case.get("strike")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        anchor_strike.is_finite()
        and case_strike.is_finite()
        and anchor_strike == case_strike
    )


def _allowlisted_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        key: anchor.get(key)
        for key in (
            "evidence_id",
            "source_event_id",
            "account",
            "futu_account_id",
            "symbol",
            "option_type",
            "position_side",
            "strike",
            "expiration_ymd",
            "contracts",
            "price",
            "event_time_ms",
            "received_at_ms",
            "order_id",
            "clearing_date",
        )
    }


def _normalize_calendar_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: dict[str, str] = {}
    for row in rows:
        day = str(
            row.get("date")
            or row.get("time")
            or row.get("trade_date")
            or ""
        ).strip()
        kind = str(
            row.get("type")
            or row.get("trade_date_type")
            or row.get("trade_type")
            or ""
        ).strip().upper()
        if day:
            normalized[day] = kind
    return [
        {"date": day, "type": normalized[day]}
        for day in sorted(normalized)
    ]


def _normal_order_facts(
    rows: Iterable[dict[str, Any]],
    *,
    anchor_order_id: str,
) -> tuple[bool, bool]:
    order_id = str(anchor_order_id or "").strip()
    if not order_id:
        return False, True
    matches = [
        row
        for row in rows
        if str(
            row.get("order_id")
            or row.get("orderID")
            or row.get("id")
            or ""
        ).strip()
        == order_id
    ]
    if len(matches) != 1:
        return False, True
    row = matches[0]
    automatic = row.get("is_broker_auto")
    if isinstance(automatic, bool):
        return not automatic, False
    origin = str(
        row.get("order_origin")
        or row.get("source")
        or row.get("order_source")
        or ""
    ).strip().lower()
    if origin in {"broker_auto", "expiry", "settlement"}:
        return False, False
    if origin in {"client", "manual", "api", "app"}:
        return True, False
    return False, True


def _looks_like_stock_settlement(
    row: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
) -> bool:
    return (
        _stock_settlement_candidate(
            row,
            lifecycle_case=lifecycle_case,
            account=str(
                lifecycle_case.get("account") or ""
            ).strip().lower(),
            futu_account_id=str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip(),
            timezone=ZoneInfo("UTC"),
            settlement_deadline_ms=2**63 - 1,
        )
        is not None
    )


def _stock_settlement_candidate(
    row: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
    account: str,
    futu_account_id: str,
    timezone: ZoneInfo,
    settlement_deadline_ms: int,
) -> dict[str, Any] | None:
    code = str(
        row.get("code")
        or row.get("stock_code")
        or row.get("symbol")
        or ""
    ).strip().upper()
    symbol = str(lifecycle_case.get("symbol") or "").strip().upper()
    if code and symbol and code.split(".", 1)[-1] != symbol.split(".", 1)[-1]:
        return None
    deal_id = str(
        row.get("deal_id")
        or row.get("dealID")
        or row.get("id")
        or ""
    ).strip()
    row_account_id = str(
        row.get("acc_id")
        or row.get("futu_account_id")
        or ""
    ).strip()
    if (
        not deal_id
        or not account
        or not futu_account_id
        or (
            row_account_id
            and row_account_id != futu_account_id
        )
    ):
        return None
    side = _stock_side(
        row.get("side")
        or row.get("trd_side")
        or row.get("trade_side")
    )
    expected_side = _expected_stock_side(lifecycle_case)
    if not side or side != expected_side:
        return None
    try:
        price = Decimal(
            str(
                row.get("price")
                if row.get("price") is not None
                else row.get("deal_price")
            )
        )
        strike = Decimal(str(lifecycle_case.get("strike")))
        quantity = abs(
            int(
                row.get("qty")
                or row.get("quantity")
                or row.get("shares")
                or 0
            )
        )
        multiplier = int(lifecycle_case.get("multiplier") or 100)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    trade_time_ms = _row_trade_time_ms(
        row,
        timezone=timezone,
    )
    observation_start_ms = int(
        lifecycle_case.get("observation_start_ms") or 0
    )
    if not (
        price.is_finite()
        and strike.is_finite()
        and price == strike
        and quantity > 0
        and multiplier > 0
        and quantity % multiplier == 0
        and trade_time_ms > 0
        and (
            observation_start_ms <= 0
            or trade_time_ms >= observation_start_ms
        )
        and trade_time_ms <= int(settlement_deadline_ms)
    ):
        return None
    source_event_id = (
        f"futu:{account}:{futu_account_id}:{deal_id}"
    )
    economic_seed = {
        "source_event_id": source_event_id,
        "symbol": symbol,
        "side": side,
        "stock_qty": quantity,
        "stock_price": str(price),
        "trade_time_ms": trade_time_ms,
        "order_id": str(
            row.get("order_id") or row.get("orderID") or ""
        ).strip()
        or None,
        "clearing_date": str(
            row.get("clearing_date")
            or row.get("settlement_date")
            or ""
        ).strip()
        or None,
    }
    return {
        "evidence_id": "ev_"
        + canonical_hash(
            {
                "evidence_type": "stock_settlement_leg",
                "source_event_id": source_event_id,
            }
        )[:24],
        "case_id": None,
        "observed_case_id": str(
            lifecycle_case.get("case_id") or ""
        ).strip(),
        "source_type": "futu_broker_deal",
        "source_event_id": source_event_id,
        "evidence_type": "stock_settlement_leg",
        "account": account,
        "futu_account_id": futu_account_id,
        **economic_seed,
        "raw": {
            key: row.get(key)
            for key in (
                "deal_id",
                "dealID",
                "id",
                "acc_id",
                "code",
                "stock_code",
                "symbol",
                "side",
                "trd_side",
                "trade_side",
                "qty",
                "quantity",
                "shares",
                "price",
                "deal_price",
                "trade_time_ms",
                "event_time_ms",
                "create_time",
                "trade_time",
                "order_id",
                "orderID",
                "clearing_date",
                "settlement_date",
            )
            if key in row
        },
    }


def _stock_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.endswith("buy"):
        return "buy"
    if text.endswith("sell"):
        return "sell"
    return ""


def _expected_stock_side(
    lifecycle_case: dict[str, Any],
) -> str:
    option_type = str(
        lifecycle_case.get("option_type") or ""
    ).strip().lower()
    position_side = str(
        lifecycle_case.get("position_side") or ""
    ).strip().lower()
    return {
        ("put", "short"): "buy",
        ("call", "short"): "sell",
        ("call", "long"): "buy",
        ("put", "long"): "sell",
    }.get((option_type, position_side), "")


def _row_trade_time_ms(
    row: dict[str, Any],
    *,
    timezone: ZoneInfo,
) -> int:
    for key in ("trade_time_ms", "event_time_ms"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return 0
            return parsed if parsed > 0 else 0
    text = str(
        row.get("create_time")
        or row.get("trade_time")
        or ""
    ).strip()
    if not text:
        return 0
    try:
        parsed_time = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        return 0
    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=timezone)
    return int(parsed_time.timestamp() * 1000)


def _row_matches_option_contract(
    row: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
    contract_code: str,
) -> bool:
    row_code = str(
        row.get("code")
        or row.get("stock_code")
        or row.get("symbol")
        or ""
    ).strip().upper()
    if contract_code:
        return row_code == contract_code.upper()
    return (
        str(row.get("option_type") or "").strip().lower()
        == str(lifecycle_case.get("option_type") or "").strip().lower()
        and str(
            row.get("expiration_ymd")
            or row.get("expiration")
            or ""
        ).strip()
        == str(lifecycle_case.get("expiration_ymd") or "").strip()
        and str(row.get("strike") or "")
        == str(lifecycle_case.get("strike") or "")
    )


def _nonzero_position(row: dict[str, Any]) -> bool:
    for key in ("qty", "quantity", "position_qty", "can_sell_qty"):
        if row.get(key) in (None, ""):
            continue
        try:
            return Decimal(str(row[key])) != 0
        except (InvalidOperation, TypeError, ValueError):
            return True
    return True


__all__ = [
    "SettlementObservationCollector",
    "build_settlement_observation_collector",
    "collect_broker_settlement_observation",
]
