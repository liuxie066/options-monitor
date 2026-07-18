from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.strategy_vocab import STRATEGY_COMBO_YIELD
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.api import (
    preview_manual_position_adjust,
    record_manual_position_adjustments,
)
from src.application.strategy_policy import YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE

STAGGERED_EXPIRY_PAIR = "staggered_expiry_pair"
FUNDING_PUT_ROLE = "funding_put"
PARTICIPATION_CALL_ROLE = "participation_call"


@dataclass(frozen=True)
class ComboYieldPairLots:
    put_record_id: str
    call_record_id: str
    put_fields: dict[str, Any]
    call_fields: dict[str, Any]
    account: str
    symbol: str
    currency: str
    put_expiration: str
    call_expiration: str
    put_strike: float
    call_strike: float
    contracts: int
    multiplier: float


def _required_record_fields(repo: Any, record_id: str, *, leg_name: str) -> dict[str, Any]:
    normalized_id = str(record_id or "").strip()
    if not normalized_id:
        raise ValueError(f"{leg_name}_record_id is required")
    try:
        fields = repo.get_record_fields(normalized_id)
    except ValueError as exc:
        raise ValueError(f"{leg_name} position lot not found: {normalized_id}") from exc
    if not isinstance(fields, dict):
        raise ValueError(f"{leg_name} position lot fields are invalid: {normalized_id}")
    return dict(fields)


def _expiration_date(value: str | None, *, leg_name: str, record_id: str) -> date:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{leg_name} position lot missing expiration: {record_id}")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{leg_name} position lot has invalid expiration: {record_id}={text}") from exc


def _canonical_symbol(fields: dict[str, Any]) -> str:
    return canonical_contract_symbol(fields.get("symbol"))


def resolve_staggered_combo_yield_pair(
    repo: Any,
    *,
    put_record_id: str,
    call_record_id: str,
) -> ComboYieldPairLots:
    put_id = str(put_record_id or "").strip()
    call_id = str(call_record_id or "").strip()
    if put_id == call_id:
        raise ValueError("put_record_id and call_record_id must identify different position lots")

    put_fields = _required_record_fields(repo, put_id, leg_name="put")
    call_fields = _required_record_fields(repo, call_id, leg_name="call")

    put_account = normalize_account(put_fields.get("account"))
    call_account = normalize_account(call_fields.get("account"))
    if not put_account or put_account != call_account:
        raise ValueError(
            f"Combo Yield legs must use the same account: put={put_account or '-'} call={call_account or '-'}"
        )

    put_symbol = _canonical_symbol(put_fields)
    call_symbol = _canonical_symbol(call_fields)
    if not put_symbol or put_symbol != call_symbol:
        raise ValueError(
            f"Combo Yield legs must use the same canonical symbol: put={put_symbol or '-'} call={call_symbol or '-'}"
        )

    put_currency = normalize_currency(put_fields.get("currency"))
    call_currency = normalize_currency(call_fields.get("currency"))
    if not put_currency or put_currency != call_currency:
        raise ValueError(
            f"Combo Yield legs must use the same currency: put={put_currency or '-'} call={call_currency or '-'}"
        )

    put_option_type = str(put_fields.get("option_type") or "").strip().lower()
    put_side = str(put_fields.get("side") or "").strip().lower()
    if put_option_type != "put" or put_side != "short":
        raise ValueError(
            f"funding Put must be an open short put: record_id={put_id} option_type={put_option_type or '-'} side={put_side or '-'}"
        )
    call_option_type = str(call_fields.get("option_type") or "").strip().lower()
    call_side = str(call_fields.get("side") or "").strip().lower()
    if call_option_type != "call" or call_side != "long":
        raise ValueError(
            f"participation Call must be an open long call: record_id={call_id} option_type={call_option_type or '-'} side={call_side or '-'}"
        )

    put_contracts = effective_contracts_open(put_fields)
    call_contracts = effective_contracts_open(call_fields)
    if put_contracts <= 0 or call_contracts <= 0:
        raise ValueError(
            f"Combo Yield legs must both be open: put_contracts_open={put_contracts} call_contracts_open={call_contracts}"
        )
    if put_contracts != call_contracts or put_contracts != 1:
        raise ValueError(
            "staggered Combo Yield V1 requires exactly 1 open Put contract and 1 open Call contract: "
            f"put={put_contracts} call={call_contracts}"
        )

    put_multiplier = effective_multiplier(put_fields)
    call_multiplier = effective_multiplier(call_fields)
    if put_multiplier is None or call_multiplier is None or float(put_multiplier) != float(call_multiplier):
        raise ValueError(
            f"Combo Yield leg multipliers must match: put={put_multiplier} call={call_multiplier}"
        )

    put_expiration = effective_expiration_ymd(put_fields)
    call_expiration = effective_expiration_ymd(call_fields)
    put_exp_date = _expiration_date(put_expiration, leg_name="put", record_id=put_id)
    call_exp_date = _expiration_date(call_expiration, leg_name="call", record_id=call_id)
    if put_exp_date >= call_exp_date:
        raise ValueError(
            "staggered Combo Yield requires Put expiration earlier than Call expiration: "
            f"put={put_expiration} call={call_expiration}"
        )

    put_strike = effective_strike(put_fields)
    call_strike = effective_strike(call_fields)
    if put_strike is None or call_strike is None:
        raise ValueError(
            f"Combo Yield legs require strikes: put={put_strike} call={call_strike}"
        )
    if float(put_strike) >= float(call_strike):
        raise ValueError(
            "staggered Combo Yield requires Put strike lower than Call strike: "
            f"put={put_strike} call={call_strike}"
        )

    return ComboYieldPairLots(
        put_record_id=put_id,
        call_record_id=call_id,
        put_fields=put_fields,
        call_fields=call_fields,
        account=put_account,
        symbol=put_symbol,
        currency=put_currency,
        put_expiration=str(put_expiration),
        call_expiration=str(call_expiration),
        put_strike=float(put_strike),
        call_strike=float(call_strike),
        contracts=put_contracts,
        multiplier=float(put_multiplier),
    )


def _validate_strategy_group_assignment(
    repo: Any,
    *,
    pair: ComboYieldPairLots,
    strategy_group_id: str,
) -> None:
    for record_id, fields in (
        (pair.put_record_id, pair.put_fields),
        (pair.call_record_id, pair.call_fields),
    ):
        existing_group_id = str(fields.get("strategy_group_id") or "").strip()
        if existing_group_id and existing_group_id != strategy_group_id:
            raise ValueError(
                f"position lot already belongs to another strategy group: record_id={record_id} "
                f"strategy_group_id={existing_group_id}"
            )

    selected_ids = {pair.put_record_id, pair.call_record_id}
    list_position_lots = getattr(repo, "list_position_lots", None)
    rows = list_position_lots() if callable(list_position_lots) else []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        record_id = str(row.get("record_id") or "").strip()
        fields = row.get("fields")
        if record_id in selected_ids or not isinstance(fields, dict):
            continue
        if str(fields.get("strategy_group_id") or "").strip() == strategy_group_id:
            raise ValueError(
                f"pair_intent_id is already assigned to another position lot: record_id={record_id}"
            )


def _strategy_snapshot(
    fields: dict[str, Any],
    *,
    leg_role: str,
    counterpart_record_id: str,
    pair_intent_id: str,
    strategy_group_id: str,
) -> dict[str, Any]:
    existing = fields.get("strategy_snapshot")
    snapshot = dict(existing) if isinstance(existing, dict) else {}
    snapshot.update(
        {
            "strategy": STRATEGY_COMBO_YIELD,
            "strategy_family": STRATEGY_COMBO_YIELD,
            "strategy_source": "manual_pair_confirmation",
            "structure_mode": STAGGERED_EXPIRY_PAIR,
            "leg_role": leg_role,
            "yield_enhancement_mode": YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
            "pair_intent_id": pair_intent_id,
            "strategy_group_id": strategy_group_id,
            "counterpart_record_id": counterpart_record_id,
        }
    )
    return snapshot


def _pair_metadata_matches(
    fields: dict[str, Any],
    *,
    leg_role: str,
    counterpart_record_id: str,
    pair_intent_id: str,
    strategy_group_id: str,
) -> bool:
    snapshot = fields.get("strategy_snapshot")
    return (
        str(fields.get("strategy") or "").strip() == STRATEGY_COMBO_YIELD
        and str(fields.get("leg_role") or "").strip() == leg_role
        and str(fields.get("strategy_group_id") or "").strip() == strategy_group_id
        and str(fields.get("yield_enhancement_mode") or "").strip()
        == YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE
        and isinstance(snapshot, dict)
        and str(snapshot.get("structure_mode") or "").strip() == STAGGERED_EXPIRY_PAIR
        and str(snapshot.get("pair_intent_id") or "").strip() == pair_intent_id
        and str(snapshot.get("strategy_group_id") or "").strip() == strategy_group_id
        and str(snapshot.get("leg_role") or "").strip() == leg_role
        and str(snapshot.get("counterpart_record_id") or "").strip() == counterpart_record_id
    )


def execute_staggered_combo_yield_pairing(
    repo: Any,
    *,
    put_record_id: str,
    call_record_id: str,
    pair_intent_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    intent_id = str(pair_intent_id or "").strip()
    if not intent_id:
        raise ValueError("pair_intent_id is required")

    pair = resolve_staggered_combo_yield_pair(
        repo,
        put_record_id=put_record_id,
        call_record_id=call_record_id,
    )
    strategy_group_id = f"{STRATEGY_COMBO_YIELD}:{pair.account}:{intent_id}"
    _validate_strategy_group_assignment(
        repo,
        pair=pair,
        strategy_group_id=strategy_group_id,
    )
    put_snapshot = _strategy_snapshot(
        pair.put_fields,
        leg_role=FUNDING_PUT_ROLE,
        counterpart_record_id=pair.call_record_id,
        pair_intent_id=intent_id,
        strategy_group_id=strategy_group_id,
    )
    call_snapshot = _strategy_snapshot(
        pair.call_fields,
        leg_role=PARTICIPATION_CALL_ROLE,
        counterpart_record_id=pair.put_record_id,
        pair_intent_id=intent_id,
        strategy_group_id=strategy_group_id,
    )
    adjustments = [
        {
            "record_id": pair.put_record_id,
            "contracts": None,
            "strike": None,
            "expiration_ymd": None,
            "premium_per_share": None,
            "multiplier": None,
            "opened_at_ms": None,
            "strategy": STRATEGY_COMBO_YIELD,
            "leg_role": FUNDING_PUT_ROLE,
            "strategy_group_id": strategy_group_id,
            "yield_enhancement_mode": YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
            "strategy_snapshot": put_snapshot,
        },
        {
            "record_id": pair.call_record_id,
            "contracts": None,
            "strike": None,
            "expiration_ymd": None,
            "premium_per_share": None,
            "multiplier": None,
            "opened_at_ms": None,
            "strategy": STRATEGY_COMBO_YIELD,
            "leg_role": PARTICIPATION_CALL_ROLE,
            "strategy_group_id": strategy_group_id,
            "yield_enhancement_mode": YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
            "strategy_snapshot": call_snapshot,
        },
    ]

    already_paired = _pair_metadata_matches(
        pair.put_fields,
        leg_role=FUNDING_PUT_ROLE,
        counterpart_record_id=pair.call_record_id,
        pair_intent_id=intent_id,
        strategy_group_id=strategy_group_id,
    ) and _pair_metadata_matches(
        pair.call_fields,
        leg_role=PARTICIPATION_CALL_ROLE,
        counterpart_record_id=pair.put_record_id,
        pair_intent_id=intent_id,
        strategy_group_id=strategy_group_id,
    )

    if already_paired:
        results = [
            {
                "fields": pair.put_fields,
                "patch": {},
                "result": {"event_id": None, "created": False},
            },
            {
                "fields": pair.call_fields,
                "patch": {},
                "result": {"event_id": None, "created": False},
            },
        ]
        mode = "already_paired"
    elif dry_run:
        results = [
            preview_manual_position_adjust(
                repo,
                record_id=pair.put_record_id,
                contracts=None,
                strike=None,
                expiration_ymd=None,
                premium_per_share=None,
                multiplier=None,
                opened_at_ms=None,
                strategy=STRATEGY_COMBO_YIELD,
                leg_role=FUNDING_PUT_ROLE,
                strategy_group_id=strategy_group_id,
                yield_enhancement_mode=YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
                strategy_snapshot=put_snapshot,
            ).to_payload(),
            preview_manual_position_adjust(
                repo,
                record_id=pair.call_record_id,
                contracts=None,
                strike=None,
                expiration_ymd=None,
                premium_per_share=None,
                multiplier=None,
                opened_at_ms=None,
                strategy=STRATEGY_COMBO_YIELD,
                leg_role=PARTICIPATION_CALL_ROLE,
                strategy_group_id=strategy_group_id,
                yield_enhancement_mode=YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
                strategy_snapshot=call_snapshot,
            ).to_payload(),
        ]
        mode = "dry_run"
    else:
        results = [result.to_payload() for result in record_manual_position_adjustments(repo, adjustments)]
        mode = "applied"

    return {
        "mode": mode,
        "operation": "pair_staggered_combo_yield",
        "strategy": STRATEGY_COMBO_YIELD,
        "structure_mode": STAGGERED_EXPIRY_PAIR,
        "pair_intent_id": intent_id,
        "strategy_group_id": strategy_group_id,
        "account": pair.account,
        "symbol": pair.symbol,
        "currency": pair.currency,
        "contracts": pair.contracts,
        "multiplier": pair.multiplier,
        "put": {
            "record_id": pair.put_record_id,
            "expiration_ymd": pair.put_expiration,
            "strike": pair.put_strike,
            "leg_role": FUNDING_PUT_ROLE,
            **results[0],
        },
        "call": {
            "record_id": pair.call_record_id,
            "expiration_ymd": pair.call_expiration,
            "strike": pair.call_strike,
            "leg_role": PARTICIPATION_CALL_ROLE,
            **results[1],
        },
    }


__all__ = [
    "ComboYieldPairLots",
    "execute_staggered_combo_yield_pairing",
    "resolve_staggered_combo_yield_pair",
]
