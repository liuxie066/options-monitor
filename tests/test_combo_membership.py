from __future__ import annotations

import json

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.combo_membership import (
    resolve_combo_group_membership,
    validate_combo_group_membership,
)


GROUP_ID = "Combo:Opaque:1"


def _contract(
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    option_type: str = "put",
) -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type=option_type,
        position_side="short" if option_type == "put" else "long",
        strike=100 if option_type == "put" else 110,
        expiration_ymd="2026-08-21",
    )


def _open(
    event_id: str,
    record_id: str,
    *,
    group_id: str = GROUP_ID,
    account: str = "lx",
    symbol: str = "NVDA",
    option_type: str = "put",
    role: str = "funding_put",
    event_time_ms: int = 1_700_000_000_000,
) -> dict:
    contract = _contract(
        account=account,
        symbol=symbol,
        option_type=option_type,
    )
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=contract,
        contracts=2,
        price=2,
        currency="USD",
        source="test",
        lot_id=record_id,
        raw_payload={
            "fields": {
                "account": account,
                "symbol": symbol,
                "strategy": "combo_yield",
                "strategy_group_id": group_id,
                "leg_role": role,
            }
        },
    ).to_dict()


def _adjust(
    event_id: str,
    record_id: str,
    *,
    group_id: str | None,
    option_type: str = "put",
    event_time_ms: int = 1_700_000_000_100,
) -> dict:
    return TradeEvent(
        event_id=event_id,
        event_type="adjust",
        event_time_ms=event_time_ms,
        contract_key=_contract(option_type=option_type),
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_lot_id=record_id,
        raw_payload={"patch": {"strategy_group_id": group_id}},
    ).to_dict()


def _void(event_id: str, target_event_id: str) -> dict:
    return TradeEvent(
        event_id=event_id,
        event_type="void",
        event_time_ms=1_700_000_000_200,
        contract_key=_contract(),
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_event_id=target_event_id,
    ).to_dict()


def _lot(
    record_id: str,
    open_event_id: str,
    *,
    group_id: str = GROUP_ID,
    account: str = "lx",
    symbol: str = "NVDA",
    role: str = "funding_put",
    contracts_open: int = 2,
) -> dict:
    return {
        "record_id": record_id,
        "fields": {
            "account": account,
            "symbol": symbol,
            "strategy": "combo_yield",
            "strategy_group_id": group_id,
            "leg_role": role,
            "source_event_id": open_event_id,
            "contracts": 2,
            "contracts_open": contracts_open,
        },
    }


def _exact_events_and_lots() -> tuple[list[dict], list[dict]]:
    return (
        [
            _open("put-open", "lot-put"),
            _open(
                "call-open",
                "lot-call",
                option_type="call",
                role="participation_call",
            ),
        ],
        [
            _lot("lot-put", "put-open"),
            _lot(
                "lot-call",
                "call-open",
                role="participation_call",
            ),
        ],
    )


def test_exact_membership_is_order_stable_and_allows_closed_identity() -> None:
    events, lots = _exact_events_and_lots()
    first = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    )
    closed_lots = [
        {**item, "fields": {**item["fields"], "contracts_open": 0}}
        for item in reversed(lots)
    ]
    second = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=list(reversed(events)),
        projected_position_lots=list(reversed(lots)),
    )
    closed = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=closed_lots,
    )

    assert first.fact == second.fact
    assert first.generation_hash == second.generation_hash
    assert first.fact["status"] == "exact"
    assert closed.fact["status"] == "exact"
    assert closed.global_live_record_ids == ()
    assert validate_combo_group_membership(first.fact).status == "valid"


def test_closed_third_member_retagged_away_remains_history_conflict() -> None:
    events, lots = _exact_events_and_lots()
    events.extend(
        [
            _open(
                "third-open",
                "lot-third",
                event_time_ms=1_700_000_000_050,
            ),
            _adjust(
                "third-retag",
                "lot-third",
                group_id="another-group",
                event_time_ms=1_700_000_000_100,
            ),
        ]
    )
    lots.append(
        _lot(
            "lot-third",
            "third-open",
            group_id="another-group",
            contracts_open=0,
        )
    )

    resolved = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    )

    assert resolved.fact["status"] == "conflict"
    assert resolved.fact["global_current_member_count"] == 2
    assert resolved.fact["global_historical_member_count"] == 3
    assert resolved.fact["retag_event_count"] == 1
    assert "lot-third" in resolved.global_historical_record_ids


def test_voided_retag_does_not_enter_effective_history() -> None:
    events, lots = _exact_events_and_lots()
    retag = _adjust("retag", "lot-put", group_id="another-group")
    events.extend([retag, _void("void-retag", "retag")])

    resolved = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    )

    assert resolved.fact["status"] == "exact"
    assert resolved.fact["retag_event_count"] == 0


def test_external_member_is_counted_but_identity_is_redacted() -> None:
    events, lots = _exact_events_and_lots()
    events.append(
        _open(
            "external-open",
            "secret-external-record",
            account="sy",
            symbol="TSLA",
            event_time_ms=1_700_000_000_050,
        )
    )
    lots.append(
        _lot(
            "secret-external-record",
            "external-open",
            account="sy",
            symbol="TSLA",
        )
    )

    resolved = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    )
    encoded = json.dumps(resolved.fact, sort_keys=True)

    assert resolved.fact["status"] == "conflict"
    assert resolved.fact["external_member_count"] == 1
    assert resolved.fact["cross_account_member_present"] is True
    assert resolved.fact["cross_symbol_member_present"] is True
    assert "secret-external-record" not in encoded


def test_membership_validator_rejects_tampered_hash() -> None:
    events, lots = _exact_events_and_lots()
    fact = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    ).fact
    tampered = {**fact, "global_historical_member_count": 3}

    validation = validate_combo_group_membership(tampered)

    assert validation.status == "conflict"
    assert "combo_group_membership_hash_mismatch" in validation.reason_codes


def test_membership_validator_rejects_duplicate_exact_roles() -> None:
    events, lots = _exact_events_and_lots()
    fact = resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    ).fact
    duplicate_roles = {
        **fact,
        "member_bindings_for_current_account": [
            {
                **binding,
                "role": "funding_put",
            }
            for binding in fact["member_bindings_for_current_account"]
        ],
    }
    duplicate_roles["membership_hash"] = canonical_sha256(
        {
            key: value
            for key, value in duplicate_roles.items()
            if key != "membership_hash"
        }
    )

    validation = validate_combo_group_membership(duplicate_roles)

    assert validation.status == "conflict"
    assert (
        "combo_group_exact_membership_invalid"
        in validation.reason_codes
    )
