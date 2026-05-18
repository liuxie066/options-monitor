from __future__ import annotations


def _base_cfg() -> dict:
    # Minimal valid runtime config skeleton; mirrors test_validate_config_notifications._base_cfg.
    return {
        "accounts": ["user1"],
        "account_settings": {"user1": {"type": "futu"}},
        "portfolio": {
            "broker": "富途",
            "account": "user1",
            "source": "futu",
            "base_currency": "CNY",
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "market": "US",
                "fetch": {"source": "futu"},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ],
    }


def test_default_alert_policy_matches_legacy_hardcoded_thresholds() -> None:
    from domain.domain.alert_policy import DEFAULT_ALERT_POLICY

    assert DEFAULT_ALERT_POLICY.change_annual_threshold == 0.02
    assert DEFAULT_ALERT_POLICY.sell_put.high_annual == 0.20
    assert DEFAULT_ALERT_POLICY.sell_put.high_spread_max == 0.20
    assert DEFAULT_ALERT_POLICY.sell_put.medium_annual == 0.12
    assert DEFAULT_ALERT_POLICY.sell_call.high_annual == 0.10
    assert DEFAULT_ALERT_POLICY.sell_call.high_total == 0.15
    assert DEFAULT_ALERT_POLICY.sell_call.medium_annual == 0.06


def test_set_active_alert_policy_switches_thresholds_for_render() -> None:
    from domain.domain import alert_rules

    try:
        alert_rules.set_active_alert_policy(
            {"sell_put": {"high_annual": 0.99, "high_spread_max": 0.99, "medium_annual": 0.25}}
        )
        # annual=0.30 hits default high (>=0.20) but stricter override forces it down to medium (>=0.25).
        row = {"risk_label": "稳健", "annualized_net_return_on_cash_basis": 0.30, "spread_ratio": 0.10}
        assert alert_rules.render_sell_put_comment(row) == "收益尚可，整体可考虑。"
    finally:
        alert_rules.set_active_alert_policy(None)


def test_render_accepts_explicit_policy_kwarg_overriding_active() -> None:
    from domain.domain import alert_rules
    from domain.domain.alert_policy import resolve_alert_policy

    alert_rules.set_active_alert_policy(None)
    strict = resolve_alert_policy(
        {"sell_call": {"high_annual": 0.99, "high_total": 0.99, "medium_annual": 0.50}}
    )
    row = {"risk_label": "稳健", "annualized_net_premium_return": 0.20, "if_exercised_total_return": 0.20}
    # Default active policy would label this "优先"; explicit kwarg must win.
    assert alert_rules.render_sell_call_comment(row, policy=strict) == "可作为备选观察。"


def test_validator_rejects_negative_threshold() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["alert_policy"] = {"sell_put": {"high_annual": -0.1}}
    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "alert_policy.sell_put.high_annual must be >= 0" in str(exc)


def test_validator_rejects_unknown_subkey() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["alert_policy"] = {"sell_call": {"bogus_key": 0.1}}
    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "alert_policy.sell_call.bogus_key" in str(exc)


def test_validator_rejects_non_dict_subsection() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["alert_policy"] = {"sell_put": [0.1, 0.2]}
    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "alert_policy.sell_put must be an object" in str(exc)


def test_validator_accepts_nested_alert_policy() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["alert_policy"] = {
        "change_annual_threshold": 0.02,
        "sell_put": {"high_annual": 0.20, "high_spread_max": 0.20, "medium_annual": 0.12},
        "sell_call": {"high_annual": 0.10, "high_total": 0.15, "medium_annual": 0.06},
    }
    mod.validate_config(cfg)
