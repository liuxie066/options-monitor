from __future__ import annotations

from scripts.option_positions_core.domain import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_open_adjustment_patch,
    build_expire_auto_close_patch,
    build_open_fields,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
)


def test_normalize_broker_maps_futu_aliases() -> None:
    assert normalize_broker("富途证券（香港）") == "富途"
    assert normalize_broker("富途證券(香港)") == "富途"
    assert normalize_broker("Futu Securities HK") == "富途"
    assert normalize_broker("其他券商") == "其他券商"


def test_normalize_option_position_enums() -> None:
    assert normalize_option_type("认沽") == "put"
    assert normalize_option_type("CALL") == "call"
    assert normalize_side("Sell To Open") == "short"
    assert normalize_side("买入") == "long"
    assert normalize_status("已平仓") == "close"
    assert normalize_status("OPEN") == "open"
    assert normalize_currency("港币") == "HKD"
    assert normalize_currency("rmb") == "CNY"
    assert normalize_close_type("买入平仓") == BUY_TO_CLOSE
    assert normalize_close_type("到期自动平仓") == EXPIRE_AUTO_CLOSE


def test_strict_enum_normalization_rejects_invalid_values() -> None:
    for fn, value in (
        (normalize_option_type, "straddle"),
        (normalize_side, "flat"),
        (normalize_status, "running"),
        (normalize_currency, "EUR"),
        (normalize_close_type, "manual_close"),
    ):
        try:
            fn(value, strict=True)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {fn.__name__}")


def test_build_open_fields_for_short_put_sets_open_contracts_and_cash() -> None:
    fields = build_open_fields(
        OpenPositionCommand(
            broker="富途证券（香港）",
            account="LX",
            symbol="nvda",
            option_type="认沽",
            side="Sell To Open",
            contracts=2,
            currency="美元",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-04-17",
            opened_at_ms=1000,
        )
    )

    assert fields["account"] == "lx"
    assert fields["broker"] == "富途"
    assert "market" not in fields
    assert fields["position_id"] == "NVDA_20260417_100P_short"
    assert fields["symbol"] == "NVDA"
    assert fields["option_type"] == "put"
    assert fields["side"] == "short"
    assert fields["currency"] == "USD"
    assert fields["contracts"] == 2
    assert fields["contracts_open"] == 2
    assert fields["contracts_closed"] == 0
    assert fields["strike"] == 100.0
    assert fields["expiration"] == 1776384000000
    assert fields["multiplier"] == 100
    assert "exp=" not in str(fields.get("note") or "")
    assert "strike=" not in str(fields.get("note") or "")
    assert "multiplier=" not in str(fields.get("note") or "")
    assert "premium" not in str(fields.get("note") or "")
    assert fields["cash_secured_amount"] == 20000.0
    assert fields["opened_at"] == 1000
    assert fields["last_action_at"] == 1000


def test_build_open_fields_canonicalizes_alias_symbol() -> None:
    fields = build_open_fields(
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="POP",
            option_type="put",
            side="short",
            contracts=1,
            currency="HKD",
            strike=135,
            multiplier=100,
            expiration_ymd="2026-04-29",
            opened_at_ms=1000,
        )
    )

    assert fields["symbol"] == "9992.HK"
    assert fields["position_id"] == "9992_HK_20260429_135P_short"


def test_build_open_fields_for_short_call_sets_locked_shares() -> None:
    fields = build_open_fields(
        OpenPositionCommand(
            broker="富途",
            account="sy",
            symbol="aapl",
            option_type="call",
            side="short",
            contracts=3,
            currency="USD",
            strike=200,
            opened_at_ms=1000,
        )
    )

    assert fields["underlying_share_locked"] == 300
    assert fields["contracts_open"] == 3


def test_build_buy_to_close_patch_supports_partial_close() -> None:
    patch = build_buy_to_close_patch(
        {"contracts": 3, "contracts_open": 3, "contracts_closed": 0, "status": "open"},
        contracts_to_close=1,
        close_price=1.23,
        as_of_ms=2000,
    )

    assert patch == {
        "contracts_open": 2,
        "contracts_closed": 1,
        "last_action_at": 2000,
        "close_type": BUY_TO_CLOSE,
        "close_reason": "manual_buy_to_close",
        "close_price": 1.23,
        "status": "open",
    }


def test_build_buy_to_close_patch_supports_full_close() -> None:
    patch = build_buy_to_close_patch(
        {"contracts": 3, "contracts_open": 1, "contracts_closed": 2, "status": "open"},
        contracts_to_close=1,
        close_reason="manual",
        as_of_ms=3000,
    )

    assert patch["contracts_open"] == 0
    assert patch["contracts_closed"] == 3
    assert patch["status"] == "close"
    assert patch["closed_at"] == 3000
    assert patch["last_action_at"] == 3000
    assert patch["close_type"] == BUY_TO_CLOSE
    assert patch["close_reason"] == "manual"


def test_build_buy_to_close_patch_rejects_over_close() -> None:
    try:
        build_buy_to_close_patch(
            {"contracts": 1, "contracts_open": 1, "status": "open"},
            contracts_to_close=2,
        )
    except ValueError as exc:
        assert "exceeds open contracts" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_build_expire_auto_close_patch_closes_open_contracts() -> None:
    patch = build_expire_auto_close_patch(
        {
            "contracts": 2,
            "contracts_open": 1,
            "contracts_closed": 1,
            "status": "open",
            "note": "exp=2026-04-17",
        },
        as_of_ms=4000,
        exp_source="expiration",
        grace_days=1,
    )

    assert patch["contracts_open"] == 0
    assert patch["contracts_closed"] == 2
    assert patch["status"] == "close"
    assert patch["closed_at"] == 4000
    assert patch["last_action_at"] == 4000
    assert patch["close_type"] == EXPIRE_AUTO_CLOSE
    assert patch["close_reason"] == "expired"
    assert "auto_close_reason=expired" in patch["note"]


def test_build_open_adjustment_patch_updates_key_open_fields() -> None:
    patch = build_open_adjustment_patch(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "status": "open",
            "contracts": 2,
            "contracts_open": 2,
            "contracts_closed": 0,
            "currency": "USD",
            "strike": 100.0,
            "multiplier": 100,
            "expiration": 1781827200000,
            "premium": 2.5,
            "note": "exp=2026-06-19;multiplier=100;premium_per_share=2.5;strike=100",
        },
        contracts=3,
        strike=105.0,
        expiration_ymd="2026-07-17",
        premium_per_share=3.1,
        multiplier=100,
        opened_at_ms=2000,
        as_of_ms=3000,
    )

    assert patch["contracts"] == 3
    assert patch["contracts_open"] == 3
    assert patch["contracts_closed"] == 0
    assert patch["strike"] == 105.0
    assert patch["premium"] == 3.1
    assert patch["expiration"] > 0
    assert patch["opened_at"] == 2000
    assert patch["last_action_at"] == 3000
    assert patch["cash_secured_amount"] == 31500.0
    assert patch["position_id"] == "NVDA_20260717_105P_short"
    assert "exp=" not in patch["note"]
    assert "strike=" not in patch["note"]
    assert "multiplier=" not in patch["note"]
    assert "premium_per_share=" not in patch["note"]


def test_build_open_adjustment_patch_rejects_contracts_below_closed() -> None:
    try:
        build_open_adjustment_patch(
            {
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "status": "open",
                "contracts": 3,
                "contracts_open": 1,
                "contracts_closed": 2,
                "currency": "USD",
                "strike": 100.0,
                "multiplier": 100,
            },
            contracts=1,
        )
    except ValueError as exc:
        assert "contracts must be >= contracts_closed" in str(exc)
    else:
        raise AssertionError("expected ValueError")
