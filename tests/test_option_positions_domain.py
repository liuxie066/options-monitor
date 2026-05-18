from __future__ import annotations

from domain.domain.ledger.position_fields import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    OpenPositionCommand,
    PositionLotFields,
    PositionLotPatch,
    build_buy_to_close_patch,
    build_close_patch_contract,
    build_open_adjustment_patch,
    build_open_adjustment_patch_contract,
    build_expire_auto_close_patch,
    build_expire_auto_close_patch_contract,
    build_open_fields,
    build_position_lot_fields,
    effective_expiration_ymd,
    exp_ms_to_ymd,
    infer_currency_from_symbol,
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


def test_infer_currency_from_symbol_uses_canonical_market_suffix() -> None:
    assert infer_currency_from_symbol("0700.HK") == "HKD"
    assert infer_currency_from_symbol("PLTR") == "USD"
    assert infer_currency_from_symbol("") is None


def test_expiration_ms_to_ymd_uses_business_date_timezone() -> None:
    assert exp_ms_to_ymd(1777564800000) == "2026-05-01"
    assert exp_ms_to_ymd(1781712000000) == "2026-06-18"
    assert exp_ms_to_ymd(1778803200000) == "2026-05-15"
    assert effective_expiration_ymd({"expiration": 1777564800000}) == "2026-05-01"


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
            premium_per_share=1.235,
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
    assert fields["premium"] == 1.235
    assert "exp=" not in str(fields.get("note") or "")
    assert "strike=" not in str(fields.get("note") or "")
    assert "multiplier=" not in str(fields.get("note") or "")
    assert "premium" not in str(fields.get("note") or "")
    assert fields["cash_secured_amount"] == 20000.0
    assert fields["opened_at"] == 1000
    assert fields["last_action_at"] == 1000


def test_build_position_lot_fields_returns_typed_open_contract() -> None:
    command = OpenPositionCommand(
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
        premium_per_share=1.235,
        opened_at_ms=1000,
    )

    fields = build_position_lot_fields(command)

    assert isinstance(fields, PositionLotFields)
    assert fields.position_id == "NVDA_20260417_100P_short"
    assert fields.to_dict() == build_open_fields(command)


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
            premium_per_share=1.23,
            opened_at_ms=1000,
        )
    )

    assert fields["symbol"] == "9992.HK"
    assert fields["position_id"] == "9992_HK_20260429_135P_short"


def test_build_open_fields_infers_currency_from_symbol_when_missing() -> None:
    hk_fields = build_open_fields(
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="",
            strike=510,
            multiplier=100,
            expiration_ymd="2026-06-29",
            premium_per_share=1.23,
            opened_at_ms=1000,
        )
    )
    us_fields = build_open_fields(
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="PLTR",
            option_type="put",
            side="short",
            contracts=1,
            currency=None,
            strike=30,
            multiplier=100,
            expiration_ymd="2026-05-15",
            premium_per_share=1.23,
            opened_at_ms=1000,
        )
    )

    assert hk_fields["currency"] == "HKD"
    assert us_fields["currency"] == "USD"


def test_build_open_fields_explicit_currency_overrides_symbol_inference() -> None:
    fields = build_open_fields(
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=510,
            multiplier=100,
            expiration_ymd="2026-06-29",
            premium_per_share=1.23,
            opened_at_ms=1000,
        )
    )

    assert fields["currency"] == "USD"


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
            multiplier=100,
            expiration_ymd="2026-05-15",
            premium_per_share=1.23,
            opened_at_ms=1000,
        )
    )

    assert fields["underlying_share_locked"] == 300
    assert fields["contracts_open"] == 3
    assert fields["expiration"] == 1778803200000


def test_build_open_fields_enforces_core_write_rules() -> None:
    command = OpenPositionCommand(
        broker="富途",
        account="sy",
        symbol="aapl",
        option_type="call",
        side="long",
        contracts=1,
        currency="USD",
        strike=200,
        multiplier=100,
        expiration_ymd="2026-05-15",
        premium_per_share=1.235,
        opened_at_ms=1000,
    )

    invalid_cases = (
        (command.__class__(**{**command.__dict__, "broker": ""}), "broker is required"),
        (command.__class__(**{**command.__dict__, "account": ""}), "account is required"),
        (command.__class__(**{**command.__dict__, "symbol": ""}), "symbol is required"),
        (command.__class__(**{**command.__dict__, "option_type": ""}), "option_type must be one of"),
        (command.__class__(**{**command.__dict__, "side": ""}), "side must be one of"),
        (command.__class__(**{**command.__dict__, "contracts": 0}), "contracts must be > 0"),
        (command.__class__(**{**command.__dict__, "contracts": 1.5}), "contracts must be an integer"),
        (command.__class__(**{**command.__dict__, "strike": 0}), "strike must be > 0"),
        (command.__class__(**{**command.__dict__, "multiplier": 0}), "multiplier must be > 0"),
    )
    for bad_command, expected in invalid_cases:
        try:
            build_open_fields(bad_command)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected ValueError: {expected}")


def test_build_open_fields_enforces_risk_field_write_rules() -> None:
    short_call = OpenPositionCommand(
        broker="富途",
        account="sy",
        symbol="aapl",
        option_type="call",
        side="short",
        contracts=3,
        currency="USD",
        strike=200,
        multiplier=100,
        expiration_ymd="2026-05-15",
        premium_per_share=1.235,
        opened_at_ms=1000,
    )
    long_call = short_call.__class__(**{**short_call.__dict__, "side": "long"})
    short_put = short_call.__class__(**{**short_call.__dict__, "option_type": "put"})

    explicit_locked = build_open_fields(
        short_call.__class__(**{**short_call.__dict__, "underlying_share_locked": 300})
    )
    no_risk_lock = build_open_fields(long_call)
    short_put_fields = build_open_fields(short_put)

    assert explicit_locked["underlying_share_locked"] == 300
    assert "underlying_share_locked" not in no_risk_lock
    assert "cash_secured_amount" not in no_risk_lock
    assert "underlying_share_locked" not in short_put_fields
    assert short_put_fields["cash_secured_amount"] == 60000.0

    invalid_cases = (
        (
            short_call.__class__(**{**short_call.__dict__, "underlying_share_locked": 299}),
            "underlying_share_locked must equal contracts * multiplier for short call",
        ),
        (
            short_put.__class__(**{**short_put.__dict__, "underlying_share_locked": 300}),
            "underlying_share_locked only applies to short call",
        ),
    )
    for bad_command, expected in invalid_cases:
        try:
            build_open_fields(bad_command)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected ValueError: {expected}")


def test_build_open_fields_requires_option_strike_and_expiration() -> None:
    for command, expected in (
        (
            OpenPositionCommand(
                broker="富途",
                account="sy",
                symbol="aapl",
                option_type="call",
                side="short",
                contracts=1,
                currency="USD",
                expiration_ymd="2026-05-15",
            ),
            "call option requires strike",
        ),
        (
            OpenPositionCommand(
                broker="富途",
                account="sy",
                symbol="aapl",
                option_type="call",
                side="short",
                contracts=1,
                currency="USD",
                strike=200,
            ),
            "call option requires expiration_ymd",
        ),
        (
            OpenPositionCommand(
                broker="富途",
                account="sy",
                symbol="aapl",
                option_type="put",
                side="short",
                contracts=1,
                currency="USD",
                strike=200,
                expiration_ymd="not-a-date",
            ),
            "put option requires expiration_ymd",
        ),
    ):
        try:
            build_open_fields(command)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected ValueError: {expected}")


def test_build_open_fields_requires_premium_and_multiplier_and_preserves_three_decimal_premium() -> None:
    command = OpenPositionCommand(
        broker="富途",
        account="sy",
        symbol="aapl",
        option_type="call",
        side="long",
        contracts=1,
        currency="USD",
        strike=200,
        multiplier=100,
        expiration_ymd="2026-05-15",
        premium_per_share=1.235,
        opened_at_ms=1000,
    )

    fields = build_open_fields(command)

    assert fields["premium"] == 1.235

    invalid_cases = (
        (command.__class__(**{**command.__dict__, "premium_per_share": None}), "premium_per_share is required"),
        (command.__class__(**{**command.__dict__, "premium_per_share": 0}), "premium_per_share must be > 0"),
        (command.__class__(**{**command.__dict__, "premium_per_share": -1}), "premium_per_share must be > 0"),
        (
            command.__class__(**{**command.__dict__, "premium_per_share": 1.2345}),
            "premium_per_share supports at most 3 decimal places",
        ),
        (command.__class__(**{**command.__dict__, "multiplier": None}), "call option requires multiplier"),
    )
    for bad_command, expected in invalid_cases:
        try:
            build_open_fields(bad_command)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected ValueError: {expected}")


def test_build_buy_to_close_patch_supports_partial_close() -> None:
    patch = build_buy_to_close_patch(
        {"contracts": 3, "contracts_open": 3, "contracts_closed": 0, "status": "open"},
        contracts_to_close=1,
        close_price=1.235,
        as_of_ms=2000,
    )

    assert patch == {
        "contracts_open": 2,
        "contracts_closed": 1,
        "last_action_at": 2000,
        "close_type": BUY_TO_CLOSE,
        "close_reason": "manual_buy_to_close",
        "close_price": 1.235,
        "status": "open",
    }


def test_build_buy_to_close_patch_rejects_invalid_close_price_precision_when_provided() -> None:
    for close_price, expected in (
        (0, "close_price must be > 0"),
        (-1, "close_price must be > 0"),
        (1.2345, "close_price supports at most 3 decimal places"),
    ):
        try:
            build_buy_to_close_patch(
                {"contracts": 3, "contracts_open": 3, "contracts_closed": 0, "status": "open"},
                contracts_to_close=1,
                close_price=close_price,
            )
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected ValueError: {expected}")


def test_build_close_patch_contract_matches_legacy_dict_api() -> None:
    fields = {"contracts": 3, "contracts_open": 3, "contracts_closed": 0, "status": "open", "side": "short"}

    patch = build_close_patch_contract(
        fields,
        contracts_to_close=1,
        close_price=1.23,
        close_reason="manual_buy_to_close",
        close_type=BUY_TO_CLOSE,
        as_of_ms=2000,
    )

    assert isinstance(patch, PositionLotPatch)
    assert patch.to_dict() == build_buy_to_close_patch(
        fields,
        contracts_to_close=1,
        close_price=1.23,
        as_of_ms=2000,
    )


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


def test_build_expire_auto_close_patch_contract_matches_legacy_dict_api() -> None:
    fields = {
        "contracts": 2,
        "contracts_open": 1,
        "contracts_closed": 1,
        "status": "open",
        "note": "exp=2026-04-17",
    }

    patch = build_expire_auto_close_patch_contract(
        fields,
        as_of_ms=4000,
        exp_source="expiration",
        grace_days=1,
    )

    assert isinstance(patch, PositionLotPatch)
    assert patch.to_dict() == build_expire_auto_close_patch(
        fields,
        as_of_ms=4000,
        exp_source="expiration",
        grace_days=1,
    )


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


def test_build_open_adjustment_patch_contract_matches_legacy_dict_api() -> None:
    fields = {
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
    }

    patch = build_open_adjustment_patch_contract(
        fields,
        contracts=3,
        strike=105.0,
        expiration_ymd="2026-07-17",
        premium_per_share=3.1,
        multiplier=100,
        opened_at_ms=2000,
        as_of_ms=3000,
    )

    assert isinstance(patch, PositionLotPatch)
    assert patch.to_dict() == build_open_adjustment_patch(
        fields,
        contracts=3,
        strike=105.0,
        expiration_ymd="2026-07-17",
        premium_per_share=3.1,
        multiplier=100,
        opened_at_ms=2000,
        as_of_ms=3000,
    )


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


def test_build_open_adjustment_patch_enforces_core_write_rules() -> None:
    fields = {
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
    }

    invalid_cases = (
        ({"contracts": 1.5}, "contracts must be an integer"),
        ({"contracts": 0}, "contracts must be > 0"),
        ({"strike": 0}, "strike must be > 0"),
        ({"multiplier": 0}, "multiplier must be > 0"),
        ({"premium_per_share": 1.2345}, "premium_per_share supports at most 3 decimal places"),
    )
    for kwargs, expected in invalid_cases:
        try:
            build_open_adjustment_patch(fields, **kwargs)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"expected ValueError: {expected}")


def test_build_open_adjustment_patch_recalculates_risk_fields() -> None:
    short_call_patch = build_open_adjustment_patch(
        {
            "symbol": "AAPL",
            "option_type": "call",
            "side": "short",
            "status": "open",
            "contracts": 2,
            "contracts_open": 2,
            "contracts_closed": 0,
            "currency": "USD",
            "strike": 200.0,
            "multiplier": 100,
            "expiration": 1781827200000,
            "premium": 2.5,
        },
        contracts=3,
    )
    short_put_patch = build_open_adjustment_patch(
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
        },
        contracts=3,
    )

    assert short_call_patch["underlying_share_locked"] == 300
    assert "cash_secured_amount" not in short_call_patch
    assert short_put_patch["cash_secured_amount"] == 30000.0
    assert "underlying_share_locked" not in short_put_patch
