from __future__ import annotations

import json
import pytest

from domain.domain.combo_identity import build_combo_identity
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.combo_membership import (
    resolve_combo_group_membership,
    resolve_combo_assignment_proof,
    validate_combo_group_membership,
)
from src.application.ledger.queries import project_trade_event_log


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
        strike=100 if option_type == "put" else 110,
        expiration_ymd="2026-08-21",
    )


def _open(
    event_id: str,
    lot_id: str,
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
        multiplier=100,
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=contract,
        contracts=2,
        price=2,
        currency="USD",
        source="test",
        lot_id=lot_id,
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
    lot_id: str,
    *,
    group_id: str | None,
    option_type: str = "put",
    event_time_ms: int = 1_700_000_000_100,
) -> dict:
    return TradeEvent(
        multiplier=100,
        event_id=event_id,
        event_type="adjust",
        event_time_ms=event_time_ms,
        contract_key=_contract(option_type=option_type),
        contracts=0,
        price=0,
        currency="USD",
        source="test",
        target_lot_id=lot_id,
        raw_payload={"patch": {"strategy_group_id": group_id}},
    ).to_dict()


def _void(event_id: str, target_event_id: str) -> dict:
    return TradeEvent(
        multiplier=100,
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
    lot_id: str,
    open_event_id: str,
    *,
    group_id: str = GROUP_ID,
    account: str = "lx",
    symbol: str = "NVDA",
    role: str = "funding_put",
    contracts_open: int = 2,
) -> dict:
    return {
        "record_id": lot_id,
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
            _open("call-open", "lot-call", option_type="call", role="participation_call"),
        ],
        [
            _lot("lot-put", "put-open"),
            _lot("lot-call", "call-open", role="participation_call"),
        ],
    )


def _resolve(events: list[dict], lots: list[dict]):
    return resolve_combo_group_membership(
        group_id=GROUP_ID,
        account="lx",
        expected_symbol="NVDA",
        trade_events=events,
        projected_position_lots=lots,
    )


def test_exact_membership_is_order_stable_and_allows_closed_identity() -> None:
    events, lots = _exact_events_and_lots()
    first = _resolve(events, lots)
    closed_lots = [
        {**item, "fields": {**item["fields"], "contracts_open": 0}}
        for item in reversed(lots)
    ]
    second = _resolve(list(reversed(events)), list(reversed(lots)))
    closed = _resolve(events, closed_lots)

    assert first.fact == second.fact
    assert first.generation_hash == second.generation_hash
    assert first.fact["status"] == "exact"
    assert closed.fact["status"] == "exact"
    assert closed.global_live_lot_ids == ()
    assert validate_combo_group_membership(first.fact).status == "valid"


def test_closed_third_member_retagged_away_remains_history_conflict() -> None:
    events, lots = _exact_events_and_lots()
    events.extend(
        [
            _open("third-open", "lot-third", event_time_ms=1_700_000_000_050),
            _adjust(
                "third-retag", "lot-third", group_id="another-group", event_time_ms=1_700_000_000_100
            ),
        ]
    )
    lots.append(_lot("lot-third", "third-open", group_id="another-group", contracts_open=0))

    resolved = _resolve(events, lots)

    assert resolved.fact["status"] == "conflict"
    assert resolved.fact["global_current_member_count"] == 2
    assert resolved.fact["global_historical_member_count"] == 3
    assert resolved.fact["retag_event_count"] == 1
    assert "lot-third" in resolved.global_historical_lot_ids


def test_voided_retag_does_not_enter_effective_history() -> None:
    events, lots = _exact_events_and_lots()
    retag = _adjust("retag", "lot-put", group_id="another-group")
    events.extend([retag, _void("void-retag", "retag")])

    resolved = _resolve(events, lots)

    assert resolved.fact["status"] == "exact"
    assert resolved.fact["retag_event_count"] == 0


def test_external_member_is_counted_but_identity_is_redacted() -> None:
    events, lots = _exact_events_and_lots()
    events.append(
        _open(
            "external-open", "secret-external-record", account="sy",
            symbol="TSLA", event_time_ms=1_700_000_000_050,
        )
    )
    lots.append(_lot("secret-external-record", "external-open", account="sy", symbol="TSLA"))

    resolved = _resolve(events, lots)
    encoded = json.dumps(resolved.fact, sort_keys=True)

    assert resolved.fact["status"] == "conflict"
    assert resolved.fact["external_member_count"] == 1
    assert resolved.fact["cross_account_member_present"] is True
    assert resolved.fact["cross_symbol_member_present"] is True
    assert "secret-external-record" not in encoded


def test_membership_validator_rejects_tampered_hash() -> None:
    events, lots = _exact_events_and_lots()
    fact = _resolve(events, lots).fact
    tampered = {**fact, "global_historical_member_count": 3}

    validation = validate_combo_group_membership(tampered)

    assert validation.status == "conflict"
    assert "combo_group_membership_hash_mismatch" in validation.reason_codes


def test_membership_validator_rejects_duplicate_exact_roles() -> None:
    events, lots = _exact_events_and_lots()
    fact = _resolve(events, lots).fact
    duplicate_roles = {
        **fact,
        "member_bindings_for_current_account": [
            {**binding, "role": "funding_put"}
            for binding in fact["member_bindings_for_current_account"]
        ],
    }
    duplicate_roles["membership_hash"] = canonical_sha256(
        {key: value for key, value in duplicate_roles.items() if key != "membership_hash"}
    )

    validation = validate_combo_group_membership(duplicate_roles)

    assert validation.status == "conflict"
    assert "combo_group_exact_membership_invalid" in validation.reason_codes


def _assignment_pair(kind: str, *, long_at: int = 1_100, call_expiry: str = "2026-08-21"):
    put_key = _contract()
    call_key = ContractKey.from_values(
        broker="futu", account="lx", underlying_symbol="NVDA",
        option_type="call", strike=110, expiration_ymd=call_expiry,
    )
    put_role, call_role = (
        ("funding_put", "participation_call")
        if kind == "csp_lc" else ("long_put", "short_call")
    )
    def opening(event_id, lot_id, key, role, side, at):
        return TradeEvent(
            event_id=event_id, event_type="open", event_time_ms=at,
            contract_key=key, contracts=1, price=2, currency="USD",
            source="test", multiplier=100, lot_id=lot_id,
            raw_payload={"side": side, "strategy": "combo_yield",
                         "strategy_group_id": GROUP_ID, "leg_role": role},
        )
    put = opening("put-open", "put-lot", put_key, put_role,
                  "sell" if kind == "csp_lc" else "buy", 1_000 if kind == "csp_lc" else long_at)
    call = opening("call-open", "call-lot", call_key, call_role,
                   "buy" if kind == "csp_lc" else "sell", long_at if kind == "csp_lc" else 1_000)
    short = put if kind == "csp_lc" else call
    assignment = TradeEvent(
        event_id="assignment", event_type="assignment", event_time_ms=2_000,
        contract_key=short.contract_key, contracts=1, price=0, currency="USD",
        source="test", multiplier=100, target_lot_id=short.lot_id,
        raw_payload={"side": "buy"},
    )
    identity = build_combo_identity({
        "group_id": GROUP_ID, "strategy": "combo_yield", "account": "lx", "symbol": "NVDA",
        "funding_put_record_id": "put-lot", "funding_put_open_event_id": "put-open",
        "funding_put_contract_key": put_key.to_dict(),
        "participation_call_record_id": "call-lot", "participation_call_open_event_id": "call-open",
        "participation_call_contract_key": call_key.to_dict(), "original_contracts": 1,
    })
    return assignment, [put.to_dict(), call.to_dict()], [identity] if kind == "csp_lc" else []


@pytest.mark.parametrize("kind", ["csp_lc", "cc_lp"])
def test_assignment_proof_requires_exact_effective_pair(kind: str) -> None:
    assignment, events, identities = _assignment_pair(kind)
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=identities,
    ) == (kind, None)


def test_cc_lp_assignment_proof_rejects_later_long_leg_and_expiry_mismatch() -> None:
    assignment, events, identities = _assignment_pair("cc_lp", long_at=2_100)
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=identities,
    )[0] is None
    assignment, events, identities = _assignment_pair("cc_lp", call_expiry="2026-09-18")
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=identities,
    ) == (None, "combo_assignment_structure_invalid")


def test_sp_lc_assignment_proof_rejects_valid_hash_for_wrong_long_contract() -> None:
    assignment, events, identities = _assignment_pair("csp_lc")
    identities[0] = build_combo_identity({
        **identities[0],
        "participation_call_contract_key": _contract(option_type="call", symbol="TSLA").to_dict(),
    })
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=identities,
    ) == (None, "combo_assignment_identity_mismatch")


def test_sp_lc_assignment_proof_accepts_exact_stored_open_contract_keys() -> None:
    assignment, events, identities = _assignment_pair("csp_lc")
    for opening in events:
        parsed = TradeEvent.from_dict(opening)
        raw_key = dict(opening["contract_key"])
        raw_key.pop("asset_type")
        raw_key.update({
            "strike": float(raw_key["strike"]),
            "position_side": parsed.position_side,
            "position_key": parsed.position_key,
        })
        opening["contract_key"] = raw_key
    identity = build_combo_identity({
        **identities[0],
        "funding_put_contract_key": events[0]["contract_key"],
        "participation_call_contract_key": events[1]["contract_key"],
    })
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=[identity],
    ) == ("csp_lc", None)

    tampered = build_combo_identity({
        **identity,
        "participation_call_contract_key": {
            **events[1]["contract_key"], "position_key": "wrong",
        },
    })
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=[tampered],
    ) == (None, "combo_assignment_identity_mismatch")


@pytest.mark.parametrize("bad_metadata", [False, True])
def test_sp_lc_assignment_proof_requires_valid_pair_and_unconflicted_open_metadata(
    bad_metadata: bool,
) -> None:
    assignment, events, identities = _assignment_pair("csp_lc")
    events[1]["contract_key"]["strike"] = "1"
    if bad_metadata:
        events[1]["raw_payload"]["strategy_snapshot"] = {"strategy": "wheel"}
    else:
        events[1]["raw_payload"]["leg_role"] = "enhancement_call"
    identities[0] = build_combo_identity({
        **identities[0],
        "participation_call_contract_key": events[1]["contract_key"],
    })
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=events, identities=identities,
    )[0] is None


def test_malformed_cc_lp_exact_membership_returns_conflict() -> None:
    _, events, _ = _assignment_pair("cc_lp")
    fact = resolve_combo_group_membership(
        group_id=GROUP_ID, account="lx", trade_events=events,
        projected_position_lots=project_trade_event_log(events).lots,
    ).fact
    fact["member_bindings_for_current_account"][0]["extra"] = True
    assert validate_combo_group_membership(fact).status == "conflict"


def test_cc_lp_assignment_proof_rejects_post_open_retagging() -> None:
    assignment, events, identities = _assignment_pair("cc_lp")
    retags = []
    for event in events:
        role = event["raw_payload"].pop("leg_role")
        event["raw_payload"].pop("strategy")
        event["raw_payload"].pop("strategy_group_id")
        retags.append(TradeEvent(
            multiplier=100,
            event_id=f"retag-{role}", event_type="adjust", event_time_ms=1_500,
            contract_key=_contract(option_type=event["contract_key"]["option_type"]),
            contracts=0, price=0, currency="USD", source="test",
            target_lot_id=event["lot_id"], raw_payload={"patch": {
                "strategy": "combo_yield", "strategy_group_id": GROUP_ID, "leg_role": role,
            }},
        ).to_dict())
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=[*events, *retags], identities=identities,
    ) == (None, "combo_assignment_open_identity_unproven")


def test_assignment_proof_rejects_known_void_of_long_open() -> None:
    assignment, events, identities = _assignment_pair("cc_lp")
    known_void = _void("void-long-open", "put-open")
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=[*events, known_void], identities=identities,
    )[0] is None


def test_sp_lc_assignment_proof_accepts_exact_pre_assignment_controlled_adoption() -> None:
    assignment, events, identities = _assignment_pair("csp_lc")
    inference_id = "combo-inference:v1:verified-pair"
    adoptions = []
    for opening in events:
        role = opening["raw_payload"]["leg_role"]
        for key in ("strategy", "strategy_group_id", "leg_role"):
            opening["raw_payload"].pop(key)
        event_id = "combo-adopt:v1:" + canonical_sha256(
            {"inference_id": inference_id, "role": role}
        )
        adoptions.append(TradeEvent(
            event_id=event_id, event_type="adjust", event_time_ms=1_500,
            contract_key=ContractKey.from_values(**opening["contract_key"]),
            contracts=0, price=0, currency="USD", source="post_trade_combo_reconciliation",
            multiplier=100, target_lot_id=opening["lot_id"],
            raw_payload={
                "source": "post_trade_combo_reconciliation",
                "source_type": "combo_pair_inference",
                "mode": "post_trade_combo_adoption",
                "inference_id": inference_id,
                "record_id": opening["lot_id"],
                "target_lot_id": opening["lot_id"],
                "adjust_target_source_event_id": opening["event_id"],
                "idempotency_key": event_id,
                "patch": {
                    "strategy": "combo_yield", "strategy_group_id": GROUP_ID,
                    "leg_role": role, "last_action_at": 1_500,
                },
            },
        ).to_dict())
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=[*events, *adoptions], identities=identities,
    ) == ("csp_lc", None)
    altered = [*events, {**adoptions[0], "source": "test"}, adoptions[1]]
    assert resolve_combo_assignment_proof(
        assignment=assignment, group_id=GROUP_ID,
        trade_events=altered, identities=identities,
    )[0] is None
