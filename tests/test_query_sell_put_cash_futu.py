from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAKE_FUTU_ACC_ID_LX = "123456789012345678"


def _cash_observation(result: dict) -> dict:
    from src.application.copilot import tools as copilot_tools

    return copilot_tools.compact_observation(
        "query_cash_headroom",
        {"ok": True, "data": result},
        {"config_key": "us", "account": "lx"},
    )


def _admit_current_cash_fact(observation: dict, text: str) -> dict:
    from src.application.copilot.result_admission import admit_submit_answer

    return admit_submit_answer(
        {
            "mode": "evidence",
            "status": "complete",
            "answer_markdown": text,
            "claims": [{
                "text": text.rstrip("。"),
                "kind": "current_fact",
                "observation_ids": ["obv_cash"],
                "required_scope": "point",
            }],
        },
        {"obv_cash": {
            "ok": True,
            "authorized_read": True,
            "observation_status": observation["status"],
            "coverage": observation["coverage"],
            "freshness": observation["freshness"],
        }},
    )


def _run_cash_query(
    monkeypatch,
    tmp_path: Path,
    portfolio: dict,
    *,
    option_context: dict | None = None,
    runtime_config: dict | None = None,
    no_exchange_rates: bool = True,
    state_dir: Path | None = None,
) -> dict:
    import src.application.cash_headroom_query as m

    monkeypatch.setattr(
        m, "load_account_portfolio_context", lambda **_kwargs: portfolio
    )
    monkeypatch.setattr(m, "_load_option_position_records", lambda *_args: [])
    monkeypatch.setattr(
        m,
        "build_option_positions_context",
        lambda *_args, **_kwargs: option_context
        if option_context is not None
        else {
            "cash_secured_total_by_ccy": {"CNY": 0.0},
            "cash_secured_total_cny": 0.0,
        },
    )
    return m.query_sell_put_cash(
        market="富途",
        account="lx",
        out_dir=state_dir or tmp_path / "state",
        base_dir=BASE,
        runtime_config=runtime_config
        or {"portfolio": {"source": "auto", "base_currency": "CNY"}},
        no_exchange_rates=no_exchange_rates,
        write_cache=False,
    )


def test_query_sell_put_cash_uses_futu_portfolio_context_when_runtime_config_allows_it() -> None:
    import src.application.cash_headroom_query as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        return {
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
        }

    old_fetch = m.fetch_futu_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    try:
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        }

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_futu"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {"source": "auto", "base_currency": "CNY"},
                "trade_intake": {"account_mapping": {"futu": {FAKE_FUTU_ACC_ID_LX: "lx"}}},
            },
            no_exchange_rates=True,
        )
    finally:
        m.fetch_futu_portfolio_context = old_fetch
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 130000.0
    assert result["cash_free_cny"] == 58000.0
    assert result["cash_source"] == "futu_cash_like_assets"
    assert result["cash_components_by_currency"] == {
        "CNY": {"cn_cash": 130000.0},
        "USD": {"us_cash": 1000.0},
    }
    assert result["cash_power_by_currency"] == {"CNY": 150000.0}
    assert result["cash_power_total_cny"] == 150000.0
    assert result["cash_power_source"] == "futu_net_cash_power"


def test_query_sell_put_cash_can_run_without_writing_cache(tmp_path: Path) -> None:
    import src.application.cash_headroom_query as m

    out_dir = tmp_path / "cash_headroom_state"
    portfolio_observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("write_cache") is False
        assert not out_dir.exists()
        return {
            "cash_by_currency": {"CNY": 130000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "holdings",
            "context_source": "direct_fetch",
            "source_observed_at": portfolio_observed_at,
            "source_observation_status": "trusted",
        }

    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        }
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={"portfolio": {"source": "auto", "base_currency": "CNY"}},
            no_exchange_rates=True,
            write_cache=False,
        )
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]

    assert result["cash_secured_used_cny"] == 72000.0
    assert result["cash_available_total_cny"] == 130000.0
    assert result["freshness"] == {
        "status": "fresh",
        "as_of": portfolio_observed_at,
        "kind": "source_snapshot",
    }
    from src.application.copilot import tools as copilot_tools

    observation = copilot_tools.compact_observation(
        "query_cash_headroom",
        {"ok": True, "data": result},
        {"config_key": "us", "account": "lx"},
    )
    assert observation["freshness"] == {
        "status": "fresh",
        "as_of": portfolio_observed_at,
    }
    assert observation["status"] == "complete"
    assert observation["coverage"] == {
        "status": "complete",
        "complete_for": "point",
        "scope": {"account": "lx", "config_key": "us"},
    }
    assert set(observation["value"]) == {
        "account",
        "cash_secured_used_cny",
        "cash_available_total_cny",
        "cash_free_total_cny",
        "cash_secured_total_by_ccy",
        "cash_secured_usage_reliable",
        "cash_available_by_currency",
        "cash_balance_reliable",
        "exchange_rates",
        "cny_conversion_complete",
    }
    assert not out_dir.exists()


def test_query_sell_put_cash_invalid_portfolio_observation_is_not_current(
    tmp_path: Path, monkeypatch
) -> None:
    result = _run_cash_query(
        monkeypatch,
        tmp_path,
        {
            "cash_by_currency": {"CNY": 130000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "holdings",
            "context_source": "direct_fetch",
            "source_observed_at": None,
            "source_observation_status": "invalid",
        },
        option_context={
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        },
    )
    observation = _cash_observation(result)
    rejected = _admit_current_cash_fact(observation, "当前现金空间为 58000 元。")

    assert result["freshness"] == {
        "status": "unknown",
        "kind": "source_unknown",
        "reason_codes": ["PORTFOLIO_OBSERVATION_MISSING"],
    }
    assert observation["freshness"]["status"] == "unknown"
    assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


def test_query_sell_put_cash_missing_fx_is_partial_and_preserves_native_cash(
    tmp_path: Path, monkeypatch
) -> None:
    result = _run_cash_query(
        monkeypatch,
        tmp_path,
        {
            "cash_by_currency": {"USD": 1000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "holdings",
            "context_source": "direct_fetch",
            "source_observed_at": "2026-08-22T01:00:00Z",
            "source_observation_status": "trusted",
        },
        option_context={
            "cash_secured_total_by_ccy": {"USD": 100.0},
            "cash_secured_total_cny": None,
        },
    )
    observation = _cash_observation(result)
    rejected = _admit_current_cash_fact(observation, "当前现金空间为 0 元。")

    assert result["cash_available_total_cny"] is None
    assert result["cash_free_total_cny"] is None
    assert result["cny_conversion_complete"] is False
    assert result["cny_conversion_missing_rates"] == ["USDCNY"]
    assert observation["status"] == "partial"
    assert observation["missing_data"] == {
        "cny_conversion_missing_rates": ["USDCNY"]
    }
    assert observation["value"]["cash_available_by_currency"] == {"USD": 1000.0}
    assert observation["value"]["cash_secured_total_by_ccy"] == {"USD": 100.0}
    assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


@pytest.mark.parametrize(
    ("row_fields", "expected_unavailable"),
    [
        (
            {
                "asset_type": "cash",
                "asset_id": "USD-CASH",
                "currency": "USD",
                "quantity": invalid_cash,
            },
            {"cash_row_1": "USD:quantity_invalid"},
        )
        for invalid_cash in ("not-a-number", True, False)
    ]
    + [
        (
            {
                "asset_type": "stock",
                "asset_id": "NVDA",
                "currency": "USD",
                "quantity": 100,
                "avg_cost": 120,
            },
            {"cash_snapshot": "cash_rows_missing"},
        )
    ],
)
def test_query_sell_put_cash_unreliable_owner_cash_cannot_become_current_zero(
    tmp_path: Path,
    monkeypatch,
    row_fields: dict[str, object],
    expected_unavailable: dict[str, str],
) -> None:
    from src.application.portfolio_context_builder import build_context

    portfolio = build_context(
        [{
            "last_modified_time": "2026-08-22T01:00:00Z",
            "fields": {
                "broker": "富途",
                "account": "lx",
                **row_fields,
            },
        }],
        broker="富途",
        account="lx",
    )
    portfolio["context_source"] = "direct_fetch"
    result = _run_cash_query(monkeypatch, tmp_path, portfolio)
    observation = _cash_observation(result)
    rejected = _admit_current_cash_fact(observation, "当前可用现金为 0 元。")

    assert portfolio["source_observation_status"] == "trusted"
    assert portfolio["cash_by_currency"] == {}
    assert portfolio["cash_balance_reliable"] is False
    assert portfolio["cash_balance_unavailable_by_row"] == expected_unavailable
    assert result["cash_available_total_cny"] is None
    assert result["cash_free_total_cny"] is None
    assert observation["status"] == "partial"
    assert observation["freshness"]["status"] == "unknown"
    assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


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


@pytest.mark.parametrize(
    (
        "cash_time_offset",
        "stock_observation",
        "expected_status",
        "expected_reason",
        "is_accepted",
    ),
    [
        (timedelta(seconds=-30), None, "fresh", None, True),
        (
            timedelta(hours=-1),
            None,
            "stale",
            "PORTFOLIO_OBSERVATION_STALE",
            False,
        ),
        (
            timedelta(days=1),
            None,
            "unknown",
            "PORTFOLIO_OBSERVATION_IN_FUTURE",
            False,
        ),
        (timedelta(seconds=-30), timedelta(days=-30), "fresh", None, True),
        (timedelta(seconds=-30), "missing", "fresh", None, True),
        (
            timedelta(hours=-1),
            timedelta(seconds=-30),
            "stale",
            "PORTFOLIO_OBSERVATION_STALE",
            False,
        ),
    ],
)
def test_holdings_observation_age_controls_current_fact_admission(
    tmp_path: Path,
    monkeypatch,
    cash_time_offset: timedelta,
    stock_observation: timedelta | str | None,
    expected_status: str,
    expected_reason: str | None,
    is_accepted: bool,
) -> None:
    from src.application.portfolio_context_builder import build_context

    cash_observed_at = (datetime.now(timezone.utc) + cash_time_offset).isoformat()
    records = [{
        "last_modified_time": cash_observed_at,
        "fields": {
            "broker": "富途",
            "account": "lx",
            "asset_type": "cash",
            "asset_id": "CNY-CASH",
            "currency": "CNY",
            "quantity": 5000,
        },
    }]
    if stock_observation is not None:
        stock_record: dict[str, object] = {
            "fields": {
                "broker": "富途",
                "account": "lx",
                "asset_type": "stock",
                "asset_id": "NVDA",
                "currency": "USD",
                "quantity": 100,
                "avg_cost": 120,
            }
        }
        if isinstance(stock_observation, timedelta):
            stock_record["last_modified_time"] = (
                datetime.now(timezone.utc) + stock_observation
            ).isoformat()
        records.append(stock_record)
    portfolio = build_context(
        records,
        broker="富途",
        account="lx",
    )
    portfolio["context_source"] = "direct_fetch"
    result = _run_cash_query(
        monkeypatch,
        tmp_path,
        portfolio,
        runtime_config={
            "portfolio": {"source": "auto", "base_currency": "CNY"},
            "runtime": {"portfolio_context_ttl_sec": 900},
        },
    )
    observation = _cash_observation(result)
    admission = _admit_current_cash_fact(observation, "当前可用现金为 5000 元。")

    assert portfolio["cash_source_observation_status"] == "trusted"
    assert portfolio["cash_source_observed_at"] == cash_observed_at.replace(
        "+00:00", "Z"
    )
    assert portfolio["cash_balance_reliable"] is True
    assert result["freshness"]["status"] == expected_status
    if expected_reason is None:
        assert "reason_codes" not in result["freshness"]
    else:
        assert expected_reason in result["freshness"]["reason_codes"]
    if is_accepted:
        assert admission["observation"] == {"ok": True, "status": "answer_accepted"}
    else:
        assert admission["observation"]["reason"] == "claim_freshness_not_supported"


@pytest.mark.parametrize(
    ("balance_rows", "expected_unavailable"),
    [
        (
            [{"currency": "CNY", "cn_cash": float("nan")}],
            {"balance_row_1.cn_cash": "value_invalid"},
        ),
        (
            [{"currency": "CNY", "cn_cash": "bad"}],
            {"balance_row_1.cn_cash": "value_invalid"},
        ),
        ([], {"balance_snapshot": "balance_rows_empty"}),
    ],
)
def test_query_sell_put_cash_unreliable_futu_cash_cannot_become_current_fact(
    tmp_path: Path,
    monkeypatch,
    balance_rows: list[dict[str, object]],
    expected_unavailable: dict[str, str],
) -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    portfolio = build_futu_portfolio_context(
        balance_rows=balance_rows,
        position_rows=[],
        account="lx",
        source_observed_at="2026-08-22T01:00:00Z",
    )
    portfolio["context_source"] = "futu_direct"
    result = _run_cash_query(monkeypatch, tmp_path, portfolio)
    observation = _cash_observation(result)
    rejected = _admit_current_cash_fact(observation, "当前现金不可用。")

    assert portfolio["cash_by_currency"] == {}
    assert portfolio["cash_balance_reliable"] is False
    assert portfolio["cash_balance_unavailable_by_row"] == expected_unavailable
    assert result["cash_available_total_cny"] is None
    assert observation["status"] == "partial"
    assert observation["freshness"]["status"] == "unknown"
    assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


def test_query_sell_put_cash_sdk_missing_sentinels_preserve_valid_current_fact(
    tmp_path: Path, monkeypatch
) -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    portfolio = build_futu_portfolio_context(
        balance_rows=[{
            "currency": "CNY",
            "fund_assets": 0,
            "hk_cash": "N/A",
            "us_cash": "N/A",
            "cn_cash": 5000,
            "jp_cash": "N/A",
            "sg_cash": "N/A",
            "au_cash": "N/A",
            "ca_cash": "N/A",
            "my_cash": "N/A",
        }],
        position_rows=[],
        account="lx",
        source_observed_at=datetime.now(timezone.utc).isoformat(),
    )
    portfolio["context_source"] = "futu_direct"
    result = _run_cash_query(monkeypatch, tmp_path, portfolio)
    observation = _cash_observation(result)
    accepted = _admit_current_cash_fact(observation, "当前可用现金为 5000 元。")

    assert portfolio["cash_balance_reliable"] is True
    assert portfolio["cash_balance_unavailable_by_row"] == {}
    assert result["cash_available_total_cny"] == 5000.0
    assert observation["status"] == "complete"
    assert observation["freshness"]["status"] == "fresh"
    assert accepted["observation"] == {"ok": True, "status": "answer_accepted"}


@pytest.mark.parametrize("invalid_rate", [-7.0, True])
def test_query_sell_put_cash_corrupt_fx_cache_cannot_become_current_fact(
    tmp_path: Path, monkeypatch, invalid_rate: object
) -> None:
    from src.infrastructure import exchange_rates

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "rate_cache.json").write_text(
        json.dumps({
            "source": "opend_account_funds_conversion",
            "timestamp": "2026-08-22T01:00:00Z",
            "rates": {"USDCNY": invalid_rate},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(exchange_rates, "fetch_market_exchange_rates", lambda: None)
    result = _run_cash_query(
        monkeypatch,
        tmp_path,
        {
            "cash_by_currency": {"USD": 1000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "holdings",
            "context_source": "direct_fetch",
            "source_observed_at": "2026-08-22T01:00:00Z",
            "source_observation_status": "trusted",
        },
        option_context={
            "cash_secured_total_by_ccy": {"USD": 100.0},
            "cash_secured_total_cny": None,
        },
        no_exchange_rates=False,
        state_dir=state_dir,
    )
    observation = _cash_observation(result)
    rejected = _admit_current_cash_fact(observation, "当前现金为负 7000 元。")

    assert result["exchange_rates"] == {"USDCNY": None, "HKDCNY": None}
    assert result["cash_available_total_cny"] is None
    assert result["cny_conversion_missing_rates"] == ["USDCNY"]
    assert observation["status"] == "partial"
    assert observation["freshness"]["status"] == "unknown"
    assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


def test_query_sell_put_cash_does_not_promote_account_cache_to_current(
    tmp_path: Path, monkeypatch
) -> None:
    result = _run_cash_query(
        monkeypatch,
        tmp_path,
        {
            "cash_by_currency": {"CNY": 130000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "holdings",
            "context_source": "account_cache",
            "source_observed_at": "2026-08-20T01:00:00Z",
            "source_observation_status": "trusted",
        },
        option_context={
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        },
    )
    observation = _cash_observation(result)

    assert result["freshness"] == {
        "status": "stale",
        "as_of": "2026-08-20T01:00:00+00:00",
        "kind": "source_snapshot",
        "reason_codes": ["PORTFOLIO_ACCOUNT_CACHE_FALLBACK"],
    }
    assert observation["freshness"] == {
        "status": "stale",
        "as_of": "2026-08-20T01:00:00+00:00",
        "reason_codes": ["PORTFOLIO_ACCOUNT_CACHE_FALLBACK"],
    }


def test_query_sell_put_cash_uses_account_scoped_portfolio_source_override() -> None:
    import src.application.cash_headroom_query as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("futu portfolio context should not run for holdings override")

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "sy"
        return {"cash_by_currency": {"CNY": 90000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_fetch = m.fetch_futu_portfolio_context
    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    try:
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": 12000.0,
        }

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_holdings_override"
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
                    "source_by_account": {"sy": "holdings"},
                    "base_currency": "CNY",
                },
            },
            no_exchange_rates=True,
        )
    finally:
        m.fetch_futu_portfolio_context = old_fetch
        m.load_account_portfolio_context = old_load_portfolio
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 90000.0
    assert result["cash_free_cny"] == 78000.0


def test_query_sell_put_cash_uses_holdings_account_mapping_for_external_account() -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "ext1"
        return {"cash_by_currency": {"CNY": 50000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 8000.0}},
            "cash_secured_total_by_ccy": {"CNY": 8000.0},
            "cash_secured_total_cny": 8000.0,
        }

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_external_holdings"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="ext1",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "accounts": ["user1", "ext1"],
                "account_settings": {
                    "ext1": {"type": "external_holdings", "holdings_account": "Feishu EXT"},
                },
                "portfolio": {
                    "source": "auto",
                    "source_by_account": {"ext1": "holdings"},
                    "base_currency": "CNY",
                },
            },
            no_exchange_rates=True,
        )
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 50000.0
    assert result["cash_free_cny"] == 42000.0


def test_query_sell_put_cash_marks_free_cash_unknown_when_cash_secured_unavailable() -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "lx"
        return {"cash_by_currency": {"CNY": 130000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": None,
            "cash_secured_unavailable_by_symbol": {
                "0700.HK": "short_put_cash_secured_basis_missing",
            },
        }

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_unavailable"
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
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]

    assert result["cash_secured_usage_reliable"] is False
    assert result["cash_secured_used_cny"] is None
    assert result["cash_free_cny"] is None
    assert result["cash_free_total_cny"] is None
    assert result["cash_secured_total_by_ccy"] == {}
    assert result["cash_secured_known_total_by_ccy"] == {"CNY": 12000.0}
    assert result["cash_secured_unavailable_reason"] == "0700.HK:short_put_cash_secured_basis_missing"
