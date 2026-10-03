from __future__ import annotations

from cash_evidence_helpers import cash_portfolio

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]

FAKE_FUTU_ACC_ID_LX = "123456789012345678"


@contextmanager
def _patched(module, **attributes):
    """Replace ``module`` attributes for the duration of the block."""
    originals = {name: getattr(module, name) for name in attributes}
    for name, value in attributes.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)


def test_query_sell_put_cash_uses_futu_portfolio_context_when_runtime_config_allows_it(tmp_path: Path) -> None:
    import src.application.cash_headroom_query as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        return cash_portfolio({
            "cash_by_currency": {"CNY": 130000.0, "USD": 1000.0},
            "cash_components_by_currency": {
                "CNY": {"cn_cash": 130000.0},
                "USD": {"us_cash": 1000.0},
            },
            "cash_source": "futu_cash_like_assets",
            "cash_power_by_currency": {"CNY": 150000.0},
            "cash_power_source": "futu_net_cash_power",
            "stocks_by_symbol": {},
            "portfolio_source_name": "futu",
            "context_source": "futu_direct",
            "source_observed_at": datetime.now(timezone.utc).isoformat(),
        }, account_id=FAKE_FUTU_ACC_ID_LX)

    with _patched(
        m,
        fetch_futu_portfolio_context=fake_fetch_futu_portfolio_context,
        open_position_ledger=lambda *_a, **_k: object(),
        _load_option_position_records=lambda *_a, **_k: (object(), []),
        decision_state_snapshot=lambda *_a, **_k: {},
        build_option_positions_context=lambda *_a, **_k: {
            "decision_snapshot_status": "trusted",
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        },
    ):
        out_dir = tmp_path / "test_query_sell_put_cash_futu"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "_resolved": {"market": "us"},
                "account_settings": {"lx": {"futu": {"account_id": FAKE_FUTU_ACC_ID_LX, "trd_env": "REAL"}}},
                "portfolio": {"source": "auto", "base_currency": "CNY"},
                "trade_intake": {"account_mapping": {"futu": {FAKE_FUTU_ACC_ID_LX: "lx"}}},
            },
            no_exchange_rates=True,
        )

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 130000.0
    assert result["cash_free_cny"] == 58000.0
    assert result["freshness"]["status"] == "fresh"
    assert result["cash_source"] == "futu_cash_like_assets"
    assert result["cash_components_by_currency"] == {
        "CNY": {"cn_cash": 130000.0},
        "USD": {"us_cash": 1000.0},
    }
    assert result["cash_power_by_currency"] == {"CNY": 150000.0}
    assert result["cash_power_total_cny"] == 150000.0
    assert result["cash_power_source"] == "futu_net_cash_power"


def test_holdings_explicit_zero_cash_row_remains_reliable() -> None:
    from src.application.portfolio_context_builder import build_context

    portfolio = build_context(
        [{
            "last_modified_time": "2026-08-22T01:00:00Z",
            "fields": {
                "broker": "富途",
                "account": "lx",
                "asset_type": "cash",
                "asset_id": "CNY-CASH",
                "currency": "CNY",
                "quantity": 0,
            },
        }],
        broker="富途",
        account="lx",
    )

    assert portfolio["cash_by_currency"] == {"CNY": 0.0}
    assert portfolio["cash_balance_reliable"] is True
    assert portfolio["cash_balance_unavailable_by_row"] == {}


def test_query_sell_put_cash_uses_futu_context_for_second_account(tmp_path: Path) -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "sy"
        return cash_portfolio({"cash_by_currency": {"CNY": 90000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "futu", "context_source": "futu_direct", "source_observed_at": datetime.now(timezone.utc).isoformat()})

    with _patched(
        m,
        load_account_portfolio_context=fake_load_account_portfolio_context,
        open_position_ledger=lambda *_a, **_k: object(),
        _load_option_position_records=lambda *_a, **_k: (object(), []),
        decision_state_snapshot=lambda *_a, **_k: {},
        build_option_positions_context=lambda *_a, **_k: {
            "decision_snapshot_status": "trusted",
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": 12000.0,
        },
    ):
        out_dir = tmp_path / "test_query_sell_put_cash_futu_second"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="sy",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {
                    "source": "auto",
                    "base_currency": "CNY",
                },
            },
            no_exchange_rates=True,
        )

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 90000.0
    assert result["cash_free_cny"] == 78000.0


def test_query_sell_put_cash_uses_configured_futu_account(tmp_path: Path) -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "sy"
        return cash_portfolio({"cash_by_currency": {"CNY": 50000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "futu", "context_source": "futu_direct", "source_observed_at": datetime.now(timezone.utc).isoformat()})

    with _patched(
        m,
        load_account_portfolio_context=fake_load_account_portfolio_context,
        open_position_ledger=lambda *_a, **_k: object(),
        _load_option_position_records=lambda *_a, **_k: (object(), []),
        decision_state_snapshot=lambda *_a, **_k: {},
        build_option_positions_context=lambda *_a, **_k: {
            "decision_snapshot_status": "trusted",
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 8000.0}},
            "cash_secured_total_by_ccy": {"CNY": 8000.0},
            "cash_secured_total_cny": 8000.0,
        },
    ):
        out_dir = tmp_path / "test_query_sell_put_cash_configured_futu"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="sy",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "accounts": ["user1", "sy"],
                "account_settings": {
                    "sy": {"type": "futu", "futu": {"account_id": "REAL_87654321"}},
                },
                "portfolio": {
                    "source": "auto",
                    "base_currency": "CNY",
                },
            },
            no_exchange_rates=True,
        )

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 50000.0
    assert result["cash_free_cny"] == 42000.0


def test_query_sell_put_cash_marks_free_cash_unknown_when_cash_secured_unavailable(tmp_path: Path) -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "lx"
        return cash_portfolio({"cash_by_currency": {"CNY": 130000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "futu"})

    with _patched(
        m,
        load_account_portfolio_context=fake_load_account_portfolio_context,
        open_position_ledger=lambda *_a, **_k: object(),
        _load_option_position_records=lambda *_a, **_k: (object(), []),
        decision_state_snapshot=lambda *_a, **_k: {},
        build_option_positions_context=lambda *_a, **_k: {
            "decision_snapshot_status": "trusted",
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": None,
            "cash_secured_unavailable_by_symbol": {
                "0700.HK": "short_put_cash_secured_basis_missing",
            },
        },
    ):
        out_dir = tmp_path / "test_query_sell_put_cash_unavailable"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {"source": "auto", "base_currency": "CNY"},
            },
            no_exchange_rates=True,
        )

    assert result["cash_secured_usage_reliable"] is False
    assert result["cash_secured_used_cny"] is None
    assert result["cash_free_cny"] is None
    assert result["cash_free_total_cny"] is None
    assert result["cash_secured_total_by_ccy"] == {}
    assert result["cash_secured_known_total_by_ccy"] == {"CNY": 12000.0}
    assert result["cash_secured_unavailable_reason"] == "0700.HK:short_put_cash_secured_basis_missing"


def test_query_sell_put_cash_keeps_closed_put_pending_after_newer_cash() -> None:
    import src.application.cash_headroom_query as m

    snapshot = {"snapshot_status": "trusted"}

    def build_context(*_args, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["decision_snapshot"] is snapshot
        return {
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": 12000.0,
            "cash_secured_unavailable_by_symbol": {"0700.HK": "option_close_settlement_pending"},
            "open_positions_min": [{
                "lot_id": "closed-put", "symbol": "0700.HK", "side": "short", "option_type": "put",
                "closure_fact": "option_leg_closed", "reserved_contracts_by_lot": {"closed-put": 1},
                "first_option_close_received_at_ms": 1_790_682_868_000,
                "last_option_close_received_at_ms": 1_790_682_868_000,
            }],
        }

    with _patched(
        m,
        load_account_portfolio_context=lambda **_kwargs: cash_portfolio({
            "cash_by_currency": {"CNY": 130000.0},
            "portfolio_source_name": "futu",
            "context_source": "futu_direct",
            "source_observed_at": "2026-09-30T03:00:48+00:00",
        }),
        _load_option_position_records=lambda *_a, **_k: (object(), []),
        decision_state_snapshot=lambda *_a, **_k: snapshot,
        build_option_positions_context=build_context,
    ):
        result = m.query_sell_put_cash(
            market="富途", account="lx", base_dir=BASE,
            runtime_config={"portfolio": {"base_currency": "CNY"}},
            no_exchange_rates=True, write_cache=False,
        )

    assert result["cash_secured_usage_reliable"] is False
    assert result["cash_free_cny"] is None


def test_query_sell_put_cash_does_not_offer_stale_broker_cash_as_free_capacity() -> None:
    import src.application.cash_headroom_query as m
    with _patched(
        m,
        load_account_portfolio_context=lambda **_kwargs: cash_portfolio({
            "cash_by_currency": {"CNY": 130_000.0},
            "portfolio_source_name": "futu", "context_source": "futu_direct",
            "source_observed_at": "2020-01-01T00:00:00+00:00", "cash_balance_reliable": True,
        }, evaluated_at=datetime.now(timezone.utc)),
        _load_option_position_records=lambda *_args, **_kwargs: (object(), []),
        decision_state_snapshot=lambda *_args, **_kwargs: {"snapshot_status": "trusted"},
        build_option_positions_context=lambda *_args, **_kwargs: {
            "decision_snapshot_status": "trusted", "cash_secured_total_by_ccy": {"CNY": 12_000.0},
            "cash_secured_total_cny": 12_000.0, "cash_secured_unavailable_by_symbol": {},
        },
    ):
        result = m.query_sell_put_cash(
            market="富途", account="lx", base_dir=BASE,
            runtime_config={"portfolio": {"base_currency": "CNY"}},
            no_exchange_rates=True, write_cache=False,
        )
    assert result["freshness"]["status"] == "stale"
    assert result["cash_free_cny"] is None
    assert result["cash_free_total_cny"] is None


def test_query_sell_put_cash_rejects_malformed_settlement_blockers() -> None:
    import src.application.cash_headroom_query as m
    with _patched(
        m,
        load_account_portfolio_context=lambda **_kwargs: cash_portfolio({
            "cash_by_currency": {"CNY": 130_000.0}, "portfolio_source_name": "futu",
            "context_source": "futu_direct", "source_observed_at": datetime.now(timezone.utc).isoformat(),
        }),
        _load_option_position_records=lambda *_args, **_kwargs: (object(), []),
        decision_state_snapshot=lambda *_args, **_kwargs: {"snapshot_status": "trusted"},
        build_option_positions_context=lambda *_args, **_kwargs: {
            "decision_snapshot_status": "trusted", "cash_secured_total_by_ccy": {},
            "cash_secured_total_cny": 0.0, "cash_secured_unavailable_by_symbol": ["invalid"],
        },
    ):
        result = m.query_sell_put_cash(
            market="富途", account="lx", base_dir=BASE,
            runtime_config={"portfolio": {"base_currency": "CNY"}},
            no_exchange_rates=True, write_cache=False,
        )
    assert result["cash_secured_usage_reliable"] is False
    assert result["cash_free_cny"] is None
