from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import cast

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import (
    FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
    calc_futu_hk_terminal_fee,
)
from domain.domain.performance.models import (
    FXRateFact,
    normalize_currency,
    to_decimal,
)


FX_RATE_BINDING_SCHEMA_VERSION = "fx_rate_binding.v1"
SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION = "sell_put_top1_economic_result.v2"

_FX_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "selected_at_ms",
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
)
_FX_PROJECTION_KEYS = _FX_BINDING_KEYS - {"schema_version", "selected_at_ms"}
_CONTRACT_IDENTITY_KEYS = frozenset(
    {
        "symbol",
        "contract_symbol",
        "option_type",
        "strike",
        "expiration",
        "multiplier",
        "currency",
    }
)
_V2_NO_FILL_KEYS = frozenset({"stage", "fill_status", "contract_identity"})
_V2_FILLED_KEYS = frozenset(
    {
        "stage",
        "fill_status",
        "contract_identity",
        "holding_start_date",
        "opening_net_premium_native",
        "expiry_underlier_close_native",
        "account_fee_plan",
        "opening_fx_binding",
        "terminal_fx_binding",
    }
)


_NO_FILL_KEYS = frozenset({"stage", "fill_status"})
_FILLED_KEYS = frozenset(
    {
        "stage",
        "fill_status",
        "holding_start_date",
        "expiration",
        "opening_net_premium",
        "net_cash_basis",
        "strike",
        "multiplier",
        "underlier_close",
        "account_fee_plan",
    }
)
_FEE_PLAN_KEYS = frozenset({"commission_free", "platform_fee", "fee_plan_ref"})


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    raw_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise ValueError(f"{label} keys must be strings")
    return cast(Mapping[str, object], raw_mapping)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != set(expected):
        raise ValueError(f"{label} keys are incomplete or unexpected")


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label} is invalid")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def build_fx_rate_binding(
    fact: FXRateFact,
    *,
    selected_at_ms: int,
) -> dict[str, object]:
    if not isinstance(fact, FXRateFact):
        raise ValueError("fact must be an FXRateFact")
    if (
        isinstance(selected_at_ms, bool)
        or not isinstance(selected_at_ms, int)
        or selected_at_ms <= 0
        or fact.effective_at_ms > selected_at_ms
        or fact.observed_at_ms > selected_at_ms
    ):
        raise ValueError("selected_at_ms does not cover the FX fact")
    return validate_fx_rate_binding(
        {
            "schema_version": FX_RATE_BINDING_SCHEMA_VERSION,
            "selected_at_ms": selected_at_ms,
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
    )


def build_fx_rate_binding_from_projection(
    fact: object,
    *,
    selected_at_ms: int,
) -> dict[str, object]:
    item = dict(_mapping(fact, "fx_rate_fact"))
    _exact_keys(item, _FX_PROJECTION_KEYS, "fx_rate_fact")
    return validate_fx_rate_binding(
        {
            "schema_version": FX_RATE_BINDING_SCHEMA_VERSION,
            "selected_at_ms": selected_at_ms,
            **item,
        }
    )


def validate_fx_rate_binding(value: object) -> dict[str, object]:
    item = dict(_mapping(value, "fx_rate_binding"))
    _exact_keys(item, _FX_BINDING_KEYS, "fx_rate_binding")
    if item["schema_version"] != FX_RATE_BINDING_SCHEMA_VERSION:
        raise ValueError("fx_rate_binding schema is invalid")
    fact_id = str(item["fact_id"] or "").strip()
    rate_kind = str(item["rate_kind"] or "").strip()
    source = str(item["source"] or "").strip()
    source_id = str(item["source_id"] or "").strip()
    source_hash = str(item["source_fact_sha256"] or "").strip()
    if not all((fact_id, rate_kind, source, source_id)):
        raise ValueError("fx_rate_binding identity is incomplete")
    if len(source_hash) != 64 or any(ch not in "0123456789abcdef" for ch in source_hash):
        raise ValueError("source_fact_sha256 is invalid")
    base = normalize_currency(item["base_currency"])
    quote = normalize_currency(item["quote_currency"])
    if base == quote or quote != "CNY":
        raise ValueError("fx_rate_binding must convert a non-CNY currency to CNY")
    rate = to_decimal(item["rate"], field_name="rate")
    if rate <= 0:
        raise ValueError("rate must be positive")
    selected_at_ms = _positive_milliseconds(item["selected_at_ms"], "selected_at_ms")
    effective_at_ms = _positive_milliseconds(item["effective_at_ms"], "effective_at_ms")
    observed_at_ms = _positive_milliseconds(item["observed_at_ms"], "observed_at_ms")
    if effective_at_ms > selected_at_ms or observed_at_ms > selected_at_ms:
        raise ValueError("FX fact occurs after selection")
    revision = item["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("revision must be a positive integer")
    supersedes = item["supersedes_fact_id"]
    if supersedes is not None and (
        not isinstance(supersedes, str) or not supersedes.strip()
    ):
        raise ValueError("supersedes_fact_id must be null or canonical text")
    item.update(
        {
            "fact_id": fact_id,
            "base_currency": base,
            "quote_currency": quote,
            "rate": str(rate),
            "rate_kind": rate_kind,
            "source": source,
            "source_id": source_id,
            "source_fact_sha256": source_hash,
        }
    )
    return item


def _positive_milliseconds(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _v2_contract_identity(value: object) -> dict[str, object]:
    item = dict(_mapping(value, "contract_identity"))
    _exact_keys(item, _CONTRACT_IDENTITY_KEYS, "contract_identity")
    for field in ("symbol", "contract_symbol"):
        if not isinstance(item[field], str) or not item[field].strip():
            raise ValueError(f"contract_identity.{field} is required")
    if item["option_type"] != "put":
        raise ValueError("contract_identity.option_type must be put")
    item["strike"] = _number(item["strike"], "contract_identity.strike", positive=True)
    item["multiplier"] = _positive_int(item["multiplier"], "contract_identity.multiplier")
    item["expiration"] = _iso_date(
        item["expiration"],
        "contract_identity.expiration",
    ).isoformat()
    item["currency"] = normalize_currency(item["currency"])
    return item


def _binding_rate(
    value: object,
    *,
    currency: str,
) -> tuple[Decimal | None, dict[str, object] | None]:
    if currency == "CNY":
        if value is not None:
            raise ValueError("CNY amounts must not carry an FX binding")
        return Decimal(1), None
    if value is None:
        return None, None
    binding = validate_fx_rate_binding(value)
    if binding["base_currency"] != currency:
        raise ValueError("FX binding base currency does not match contract")
    return to_decimal(binding["rate"], field_name="rate"), binding


def _fx_evidence_ref(binding: dict[str, object] | None) -> dict[str, object] | None:
    if binding is None:
        return None
    return {
        "fact_id": binding["fact_id"],
        "source_fact_sha256": binding["source_fact_sha256"],
    }


def _v2_result(
    *,
    status: str,
    reason_code: str | None,
    fill_status: str,
    contract_identity: dict[str, object],
    holding_calendar_days: int | None,
    return_capital_basis_native: float | None,
    return_capital_basis_cny: float | None,
    opening_net_premium_native: float | None,
    opening_net_premium_cny: float | None,
    terminal_fee_native: float | None,
    terminal_fee_cny: float | None,
    expiry_underlier_pnl_native: float | None,
    expiry_underlier_pnl_cny: float | None,
    economic_pnl_cny: float | None,
    annualized_return: float | None,
    opening_fx_binding: dict[str, object] | None,
    terminal_fx_binding: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": SELL_PUT_TOP1_ECONOMIC_RESULT_VERSION,
        "status": status,
        "reason_code": reason_code,
        "fill_status": fill_status,
        "contract_identity": contract_identity,
        "holding_calendar_days": holding_calendar_days,
        "return_capital_basis_native": return_capital_basis_native,
        "return_capital_basis_cny": return_capital_basis_cny,
        "opening_net_premium_native": opening_net_premium_native,
        "opening_net_premium_cny": opening_net_premium_cny,
        "terminal_fee_native": terminal_fee_native,
        "terminal_fee_cny": terminal_fee_cny,
        "expiry_underlier_pnl_native": expiry_underlier_pnl_native,
        "expiry_underlier_pnl_cny": expiry_underlier_pnl_cny,
        "economic_pnl_cny": economic_pnl_cny,
        "annualized_return": annualized_return,
        "opening_fx_evidence_ref": _fx_evidence_ref(opening_fx_binding),
        "terminal_fx_evidence_ref": _fx_evidence_ref(terminal_fx_binding),
    }


def calculate_sell_put_top1_economic_result(
    economic_facts: object,
) -> dict[str, object]:
    facts = _mapping(economic_facts, "economic_facts")
    contract = _v2_contract_identity(facts.get("contract_identity"))
    if set(facts) == set(_V2_NO_FILL_KEYS):
        if facts["stage"] != "validation" or facts["fill_status"] != "no_observed_fill":
            raise ValueError("no-fill facts must describe validation/no_observed_fill")
        return _v2_result(
            status="evaluable",
            reason_code=None,
            fill_status="no_observed_fill",
            contract_identity=contract,
            holding_calendar_days=None,
            return_capital_basis_native=None,
            return_capital_basis_cny=None,
            opening_net_premium_native=None,
            opening_net_premium_cny=None,
            terminal_fee_native=None,
            terminal_fee_cny=None,
            expiry_underlier_pnl_native=None,
            expiry_underlier_pnl_cny=None,
            economic_pnl_cny=0.0,
            annualized_return=0.0,
            opening_fx_binding=None,
            terminal_fx_binding=None,
        )

    _exact_keys(facts, _V2_FILLED_KEYS, "economic_facts")
    expected_fill = {"research": "t0_assumed_fill", "validation": "observed_fill"}
    stage = facts["stage"]
    fill_status = facts["fill_status"]
    if not isinstance(stage, str) or expected_fill.get(stage) != fill_status:
        raise ValueError("stage and fill_status are inconsistent")
    start = _iso_date(facts["holding_start_date"], "holding_start_date")
    expiration = date.fromisoformat(str(contract["expiration"]))
    holding_days = (expiration - start).days
    premium = to_decimal(
        facts["opening_net_premium_native"],
        field_name="opening_net_premium_native",
    )
    close = to_decimal(
        facts["expiry_underlier_close_native"],
        field_name="expiry_underlier_close_native",
    )
    strike = to_decimal(contract["strike"], field_name="strike")
    multiplier = to_decimal(contract["multiplier"], field_name="multiplier")
    if premium <= 0 or close <= 0:
        raise ValueError("premium and expiry close must be positive")
    cash_basis = strike * multiplier - premium
    if cash_basis <= 0:
        raise ValueError("return capital basis must be positive")
    opening_rate, opening_binding = _binding_rate(
        facts["opening_fx_binding"],
        currency=str(contract["currency"]),
    )
    terminal_rate, terminal_binding = _binding_rate(
        facts["terminal_fx_binding"],
        currency=str(contract["currency"]),
    )
    if holding_days <= 0 or opening_rate is None or terminal_rate is None:
        return _v2_result(
            status="not_evaluable",
            reason_code=(
                "holding_period_non_positive" if holding_days <= 0 else "required_fx_missing"
            ),
            fill_status=str(fill_status),
            contract_identity=contract,
            holding_calendar_days=holding_days,
            return_capital_basis_native=float(cash_basis),
            return_capital_basis_cny=None,
            opening_net_premium_native=float(premium),
            opening_net_premium_cny=None,
            terminal_fee_native=None,
            terminal_fee_cny=None,
            expiry_underlier_pnl_native=None,
            expiry_underlier_pnl_cny=None,
            economic_pnl_cny=None,
            annualized_return=None,
            opening_fx_binding=opening_binding,
            terminal_fx_binding=terminal_binding,
        )

    raw_fee = _mapping(
        calc_futu_hk_terminal_fee(
            "assignment" if close < strike else "expired_worthless",
            order_price=float(strike),
            shares=int(multiplier),
            contracts=1,
            account_fee_plan=(
                dict(_mapping(facts["account_fee_plan"], "account_fee_plan"))
                if facts["account_fee_plan"] is not None
                else None
            ),
        ),
        "terminal fee result",
    )
    if raw_fee.get("currency") != contract["currency"] or raw_fee.get("complete") is not True:
        return _v2_result(
            status="not_evaluable",
            reason_code="required_outcome_missing",
            fill_status=str(fill_status),
            contract_identity=contract,
            holding_calendar_days=holding_days,
            return_capital_basis_native=float(cash_basis),
            return_capital_basis_cny=float(cash_basis * opening_rate),
            opening_net_premium_native=float(premium),
            opening_net_premium_cny=float(premium * opening_rate),
            terminal_fee_native=None,
            terminal_fee_cny=None,
            expiry_underlier_pnl_native=None,
            expiry_underlier_pnl_cny=None,
            economic_pnl_cny=None,
            annualized_return=None,
            opening_fx_binding=opening_binding,
            terminal_fx_binding=terminal_binding,
        )
    fee = to_decimal(raw_fee.get("amount"), field_name="terminal_fee.amount")
    underlier_pnl = min(close - strike, Decimal(0)) * multiplier
    cash_basis_cny = cash_basis * opening_rate
    premium_cny = premium * opening_rate
    fee_cny = fee * terminal_rate
    underlier_pnl_cny = underlier_pnl * terminal_rate
    pnl_cny = premium_cny - fee_cny + underlier_pnl_cny
    annualized_return = pnl_cny / cash_basis_cny / Decimal(holding_days) * Decimal(365)
    return _v2_result(
        status="evaluable",
        reason_code=None,
        fill_status=str(fill_status),
        contract_identity=contract,
        holding_calendar_days=holding_days,
        return_capital_basis_native=float(cash_basis),
        return_capital_basis_cny=float(cash_basis_cny),
        opening_net_premium_native=float(premium),
        opening_net_premium_cny=float(premium_cny),
        terminal_fee_native=float(fee),
        terminal_fee_cny=float(fee_cny),
        expiry_underlier_pnl_native=float(underlier_pnl),
        expiry_underlier_pnl_cny=float(underlier_pnl_cny),
        economic_pnl_cny=float(pnl_cny),
        annualized_return=float(annualized_return),
        opening_fx_binding=opening_binding,
        terminal_fx_binding=terminal_binding,
    )


def _result(
    *,
    status: str,
    reason_code: str | None,
    reason_detail: str | None,
    stage: str,
    fill_status: str,
    assignment_proxy: bool | None,
    intrinsic_per_share: float | None,
    holding_calendar_days: int | None,
    terminal_fee_schedule_version: str | None,
    terminal_fee_reason: str | None,
    terminal_fee_amount: float | None,
    economic_pnl: float | None,
    efficiency: float | None,
) -> dict[str, object]:
    return {
        "status": status,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "stage": stage,
        "fill_status": fill_status,
        "assignment_proxy": assignment_proxy,
        "intrinsic_per_share": intrinsic_per_share,
        "holding_calendar_days": holding_calendar_days,
        "terminal_fee_schedule_version": terminal_fee_schedule_version,
        "terminal_fee_reason": terminal_fee_reason,
        "terminal_fee_amount": terminal_fee_amount,
        "economic_pnl": economic_pnl,
        "efficiency": efficiency,
    }


def calculate_expiry_efficiency(economic_facts: object) -> dict[str, object]:
    facts = _mapping(economic_facts, "economic_facts")

    if set(facts) == set(_NO_FILL_KEYS):
        if (
            facts["stage"] != "validation"
            or facts["fill_status"] != "no_observed_fill"
        ):
            raise ValueError("no-fill facts must describe validation/no_observed_fill")
        return _result(
            status="evaluable",
            reason_code=None,
            reason_detail=None,
            stage="validation",
            fill_status="no_observed_fill",
            assignment_proxy=None,
            intrinsic_per_share=None,
            holding_calendar_days=None,
            terminal_fee_schedule_version=None,
            terminal_fee_reason=None,
            terminal_fee_amount=None,
            economic_pnl=0.0,
            efficiency=0.0,
        )

    _exact_keys(facts, _FILLED_KEYS, "economic_facts")
    raw_stage = facts["stage"]
    raw_fill_status = facts["fill_status"]
    expected_fill = {"research": "t0_assumed_fill", "validation": "observed_fill"}
    if (
        not isinstance(raw_stage, str)
        or not isinstance(raw_fill_status, str)
        or expected_fill.get(raw_stage) != raw_fill_status
    ):
        raise ValueError("stage and fill_status are inconsistent")
    stage = raw_stage
    fill_status = raw_fill_status

    start = _iso_date(facts["holding_start_date"], "holding_start_date")
    expiration = _iso_date(facts["expiration"], "expiration")
    premium = _number(facts["opening_net_premium"], "opening_net_premium", positive=True)
    cash_basis = _number(facts["net_cash_basis"], "net_cash_basis", positive=True)
    strike = _number(facts["strike"], "strike", positive=True)
    multiplier = _positive_int(facts["multiplier"], "multiplier")
    close = _number(facts["underlier_close"], "underlier_close", positive=True)

    raw_fee_plan = facts["account_fee_plan"]
    fee_plan: dict[str, object] | None = None
    if raw_fee_plan is not None:
        fee_plan_mapping = _mapping(raw_fee_plan, "account_fee_plan")
        if not set(fee_plan_mapping).issubset(set(_FEE_PLAN_KEYS)):
            raise ValueError("account_fee_plan contains unexpected keys")
        fee_plan = dict(fee_plan_mapping)

    holding_days = (expiration - start).days
    intrinsic = max(strike - close, 0.0)
    assignment_proxy = intrinsic > 0
    if holding_days <= 0:
        return _result(
            status="not_evaluable",
            reason_code="holding_period_non_positive",
            reason_detail=None,
            stage=stage,
            fill_status=fill_status,
            assignment_proxy=assignment_proxy,
            intrinsic_per_share=intrinsic,
            holding_calendar_days=holding_days,
            terminal_fee_schedule_version=None,
            terminal_fee_reason=None,
            terminal_fee_amount=None,
            economic_pnl=None,
            efficiency=None,
        )

    raw_terminal_fee: object = calc_futu_hk_terminal_fee(
        "assignment" if assignment_proxy else "expired_worthless",
        order_price=strike,
        shares=multiplier,
        contracts=1,
        account_fee_plan=fee_plan,
    )
    terminal_fee = _mapping(raw_terminal_fee, "terminal fee result")
    if terminal_fee.get("currency") != "HKD":
        raise ValueError("terminal fee result must use HKD")
    schedule_version = terminal_fee.get("schedule_version")
    if (
        not isinstance(schedule_version, str)
        or schedule_version != FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
    ):
        raise ValueError("terminal fee schedule version mismatch")
    complete = terminal_fee.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("terminal fee result complete flag is invalid")
    reason = terminal_fee.get("reason")
    if not isinstance(reason, str) or not reason or reason != reason.strip():
        raise ValueError("terminal fee reason is invalid")

    if not complete:
        return _result(
            status="not_evaluable",
            reason_code="required_outcome_missing",
            reason_detail="expiry_fee_unavailable",
            stage=stage,
            fill_status=fill_status,
            assignment_proxy=assignment_proxy,
            intrinsic_per_share=intrinsic,
            holding_calendar_days=holding_days,
            terminal_fee_schedule_version=schedule_version,
            terminal_fee_reason=reason,
            terminal_fee_amount=None,
            economic_pnl=None,
            efficiency=None,
        )

    if terminal_fee.get("basis") != "estimated":
        raise ValueError("complete terminal fee result must be estimated")
    fee_amount = _number(terminal_fee.get("amount"), "terminal_fee.amount")
    if fee_amount < 0:
        raise ValueError("terminal_fee.amount must be non-negative")

    pnl = premium - fee_amount
    if assignment_proxy:
        pnl += (close - strike) * multiplier
    efficiency = pnl / cash_basis / holding_days * 365
    return _result(
        status="evaluable",
        reason_code=None,
        reason_detail=None,
        stage=stage,
        fill_status=fill_status,
        assignment_proxy=assignment_proxy,
        intrinsic_per_share=intrinsic,
        holding_calendar_days=holding_days,
        terminal_fee_schedule_version=schedule_version,
        terminal_fee_reason=reason,
        terminal_fee_amount=fee_amount,
        economic_pnl=pnl,
        efficiency=efficiency,
    )
