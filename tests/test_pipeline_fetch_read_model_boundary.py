from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


_TEST_EVIDENCE_COMPLETED_AT = datetime.now(timezone.utc).replace(microsecond=0)
_TEST_EVIDENCE_OBSERVED_AT = _TEST_EVIDENCE_COMPLETED_AT - timedelta(seconds=1)
_TEST_TRADING_DATE = "2026-08-04"


def _trading_date_for_dte(*, expiration: str, dte: int) -> str:
    return (
        datetime.fromisoformat(expiration).date() - timedelta(days=dte)
    ).isoformat()


def _make_dirs(root: Path) -> tuple[Path, Path]:
    required = (root / "required_data").resolve()
    state_dir = (root / "state").resolve()
    (required / "parsed").mkdir(parents=True, exist_ok=True)
    (required / "raw").mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return required, state_dir


def _typed_success_empty_payload(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "underlier_code": f"US.{symbol}",
        "trading_date": _TEST_TRADING_DATE,
        "expirations": [],
        "expiration_count": 0,
        "rows": [],
        "meta": {
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": _TEST_TRADING_DATE,
            "status": "ok",
            "source_outcome": "success_empty",
            "reason_code": "no_expirations",
            "snapshot_complete": True,
            "snapshot_requested_codes": 0,
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [],
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "realized_volatility": {
                "status": "not_applicable_no_contracts",
                "reason": "not_applicable_no_contracts",
            },
            "source_observed_at": _TEST_EVIDENCE_OBSERVED_AT.isoformat(),
            "completed_at_utc": _TEST_EVIDENCE_COMPLETED_AT.isoformat(),
        },
    }


def _typed_success_row_payload(
    *,
    symbol: str,
    expiration: str,
    dte: int,
    contract_symbol: str,
    option_type: str = "put",
    strike: float = 100.0,
    source_observed_at: datetime | None = None,
    completed_at_utc: datetime | None = None,
) -> dict[str, object]:
    observed_at = source_observed_at or _TEST_EVIDENCE_OBSERVED_AT
    completed_at = completed_at_utc or _TEST_EVIDENCE_COMPLETED_AT
    trading_date = _trading_date_for_dte(
        expiration=expiration,
        dte=dte,
    )
    return {
        "symbol": symbol,
        "underlier_code": f"US.{symbol}",
        "trading_date": trading_date,
        "spot": 120.0,
        "expirations": [expiration],
        "expiration_count": 1,
        "rows": [
            {
                "symbol": symbol,
                "option_type": option_type,
                "expiration": expiration,
                "dte": dte,
                "contract_symbol": contract_symbol,
                "strike": strike,
                "spot": 120.0,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "volume": 10,
                "open_interest": 100,
                "implied_volatility": 0.4,
                "in_the_money": (
                    strike > 120.0 if option_type == "put" else strike < 120.0
                ),
                "currency": "USD",
                "otm_pct": (
                    (120.0 - strike) / 120.0
                    if option_type == "put"
                    else (strike - 120.0) / 120.0
                ),
                "delta": -0.2 if option_type == "put" else 0.2,
                "multiplier": 100,
            }
        ],
        "meta": {
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": trading_date,
            "status": "ok",
            "source_outcome": "success_rows",
            "reason_code": None,
            "snapshot_complete": True,
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [contract_symbol],
            "snapshot_returned_code_set": [contract_symbol],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "realized_volatility": {
                "status": "ok",
                "reason": None,
                "realized_volatility_estimate": 0.3,
            },
            "source_observed_at": observed_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
        },
    }


def _fetch_spec_for_side_plans(
    *,
    symbol: str,
    side_plans: list[object],
    limit_expirations: int,
    trading_date: str,
    include_realized_volatility: bool = False,
):  # type: ignore[no-untyped-def]
    from src.application.required_data_planning import RequiredDataFetchSpec

    assert side_plans
    expirations = list(side_plans[0].explicit_expirations)  # type: ignore[attr-defined]
    assert all(
        list(plan.explicit_expirations) == expirations  # type: ignore[attr-defined]
        for plan in side_plans
    )
    return RequiredDataFetchSpec(
        symbol=symbol,
        limit_expirations=limit_expirations,
        host="127.0.0.1",
        port=11111,
        option_types=tuple(
            plan.option_type for plan in side_plans  # type: ignore[attr-defined]
        ),
        explicit_expirations=expirations,
        min_dte=min(
            (
                plan.min_dte  # type: ignore[attr-defined]
                for plan in side_plans
                if plan.min_dte is not None  # type: ignore[attr-defined]
            ),
            default=None,
        ),
        max_dte=max(
            (
                plan.max_dte  # type: ignore[attr-defined]
                for plan in side_plans
                if plan.max_dte is not None  # type: ignore[attr-defined]
            ),
            default=None,
        ),
        side_strike_windows={
            plan.option_type: {  # type: ignore[attr-defined]
                "min_strike": plan.strike_window.min_strike,  # type: ignore[attr-defined]
                "max_strike": plan.strike_window.max_strike,  # type: ignore[attr-defined]
            }
            for plan in side_plans
        },
        include_realized_volatility=include_realized_volatility,
        side_plans=list(side_plans),  # type: ignore[arg-type]
        planning_reason="test fixture mirrors planner request ownership",
        trading_date=trading_date,
    )


def _nvda_put_fetch_plan(*, expirations: list[str]):  # type: ignore[no-untyped-def]
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    side_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=10,
        max_dte=60,
        explicit_expirations=list(expirations),
        strike_window=StrikeWindowPlan(
            min_strike=100.0,
            max_strike=100.0,
            source="test",
            base_min_strike=100.0,
            base_max_strike=100.0,
        ),
        planning_reason="required-data boundary regression",
    )
    return RequiredDataFetchPlanBundle(
        symbol="NVDA",
        spot_reference=120.0,
        side_plans=[side_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol="NVDA",
                side_plans=[side_plan],
                limit_expirations=len(expirations),
                trading_date=_TEST_TRADING_DATE,
            )
        ],
        expiration_discovery=OptionExpirationDiscoveryResult(
            outcome="success_rows",
            reason_code=None,
            expirations=list(expirations),
            observed_at_utc=_TEST_EVIDENCE_OBSERVED_AT.isoformat(),
            completed_at_utc=_TEST_EVIDENCE_COMPLETED_AT.isoformat(),
            request_identity={
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": _TEST_TRADING_DATE,
            },
        ),
        projection_outcome="success_rows",
        projected_expirations=list(expirations),
        require_realized_volatility=False,
    )


def _nvda_split_side_fetch_plan():  # type: ignore[no-untyped-def]
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    put_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=10,
        max_dte=60,
        explicit_expirations=["2026-08-21"],
        strike_window=StrikeWindowPlan(
            min_strike=100.0,
            max_strike=100.0,
            source="test",
            base_min_strike=100.0,
            base_max_strike=100.0,
        ),
        planning_reason="test put child request",
    )
    call_plan = OptionSideFetchPlan(
        option_type="call",
        min_dte=10,
        max_dte=60,
        explicit_expirations=["2026-09-18"],
        strike_window=StrikeWindowPlan(
            min_strike=130.0,
            max_strike=130.0,
            source="test",
            base_min_strike=130.0,
            base_max_strike=130.0,
        ),
        planning_reason="test call child request",
    )
    expirations = ["2026-08-21", "2026-09-18"]
    return RequiredDataFetchPlanBundle(
        symbol="NVDA",
        spot_reference=120.0,
        side_plans=[put_plan, call_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol="NVDA",
                side_plans=[put_plan],
                limit_expirations=1,
                trading_date=_TEST_TRADING_DATE,
            ),
            _fetch_spec_for_side_plans(
                symbol="NVDA",
                side_plans=[call_plan],
                limit_expirations=1,
                trading_date=_TEST_TRADING_DATE,
            ),
        ],
        expiration_discovery=OptionExpirationDiscoveryResult(
            outcome="success_rows",
            reason_code=None,
            expirations=expirations,
            observed_at_utc=_TEST_EVIDENCE_OBSERVED_AT.isoformat(),
            completed_at_utc=_TEST_EVIDENCE_COMPLETED_AT.isoformat(),
            request_identity={
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": _TEST_TRADING_DATE,
            },
        ),
        projection_outcome="success_rows",
        projected_expirations=expirations,
        require_realized_volatility=False,
    )


def _success_rows_expiration_discovery(
    *,
    symbol: str,
    expirations: list[str],
    trading_date: str,
):  # type: ignore[no-untyped-def]
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )

    from src.application.opend_utils import normalize_underlier

    return OptionExpirationDiscoveryResult(
        outcome="success_rows",
        reason_code=None,
        expirations=list(expirations),
        observed_at_utc=_TEST_EVIDENCE_OBSERVED_AT.isoformat(),
        completed_at_utc=_TEST_EVIDENCE_COMPLETED_AT.isoformat(),
        request_identity={
            "symbol": symbol,
            "underlier": normalize_underlier(symbol).code,
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": trading_date,
        },
    )


def test_pipeline_success_empty_payload_binds_discovery_trading_date() -> None:
    import src.application.required_data_steps as mod
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )
    from src.application.required_data_planning import (
        RequiredDataFetchPlanBundle,
    )

    discovery = OptionExpirationDiscoveryResult(
        outcome="success_empty",
        reason_code="no_expirations",
        expirations=[],
        observed_at_utc=_TEST_EVIDENCE_OBSERVED_AT.isoformat(),
        completed_at_utc=_TEST_EVIDENCE_COMPLETED_AT.isoformat(),
        request_identity={
            "symbol": "NVDA",
            "underlier": "US.NVDA",
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": _TEST_TRADING_DATE,
        },
    )
    fetch_plan = RequiredDataFetchPlanBundle(
        symbol="NVDA",
        spot_reference=None,
        side_plans=[],
        merged_specs=[],
        expiration_discovery=discovery,
        projection_outcome="success_empty",
        projected_expirations=[],
        require_realized_volatility=False,
    )

    payload = mod._success_empty_payload_from_plan(
        symbol="NVDA",
        fetch_plan=fetch_plan,
        fetch_source="opend",
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )

    assert payload["meta"]["trading_date"] == _TEST_TRADING_DATE
    assert payload["meta"]["trading_date"] == discovery.request_identity[
        "trading_date"
    ]


def test_ensure_required_data_uses_read_model_error_to_force_refetch() -> None:
    from src.application import pipeline_fetch_models as models
    import src.application.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_read_model_error").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "AAPL"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text("dte\n12\n", encoding="utf-8")

    models.record_fetch_snapshot(
        state_dir=state_dir,
        symbol=symbol,
        source="opend",
        status="error",
        reason="previous_failed",
    )

    old_execute = mod.execute_required_data_opend
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or _typed_success_empty_payload(symbol))  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]

    assert len(called) == 1
    request = called[0]["request"]
    assert request.symbol == symbol
    assert request.option_types == "put"


def test_ensure_required_data_skips_when_read_model_is_ok_and_dte_satisfies() -> None:
    from src.application import pipeline_fetch_models as models
    import src.application.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_read_model_ok").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "AAPL"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text("dte\n12\n", encoding="utf-8")

    models.record_fetch_snapshot(
        state_dir=state_dir,
        symbol=symbol,
        source="opend",
        status="ok",
    )

    old_execute = mod.execute_required_data_opend
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or _typed_success_empty_payload("AAPL"))  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            min_dte=5,
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]

    assert called == []


def test_ensure_required_data_treats_futu_source_as_opend_path() -> None:
    import src.application.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_futu_source").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)

    old_execute = mod.execute_required_data_opend
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or _typed_success_empty_payload("AAPL"))  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol="AAPL",
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="futu",
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]

    assert len(called) == 1
    request = called[0]["request"]
    assert request.symbol == "AAPL"
    assert request.option_types == "put"


def test_manual_fetch_without_run_id_saves_without_publishing_quote_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod

    required, _state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    payload = _typed_success_row_payload(
        symbol=symbol,
        expiration="2026-09-18",
        dte=45,
        contract_symbol="US.NVDA.2026-09-18.P100",
    )
    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        lambda **_kwargs: payload,
    )
    receipt_reads: list[object] = []
    monkeypatch.setattr(
        mod,
        "resolve_exact_fresh_required_data_quote_receipt",
        lambda **kwargs: receipt_reads.append(kwargs),
    )

    result = mod.ensure_required_data(
        py="python3",
        base=tmp_path,
        symbol=symbol,
        required_data_dir=required,
        limit_expirations=1,
        want_put=True,
        want_call=False,
        timeout_sec=5,
        is_scheduled=True,
        fetch_source="opend",
        fetch_host="127.0.0.1",
        fetch_port=11111,
        min_dte=10,
        max_dte=60,
        fetch_plan=None,
        position_advice_producer_run_id=None,
    )

    assert result is None
    assert (required / "raw" / f"{symbol}_required_data.json").is_file()
    assert (required / "parsed" / f"{symbol}_required_data.csv").is_file()
    quote_root = required / "position_advice_sources" / "quotes"
    assert list(quote_root.glob("**/receipt.json")) == []
    assert list(quote_root.glob("**/payload.json")) == []
    assert receipt_reads == []


def test_planned_cached_data_without_run_id_never_adopts_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod
    from src.application.opend_symbol_outputs import save_outputs

    required, _state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    expiration = "2026-09-18"
    payload = _typed_success_row_payload(
        symbol=symbol,
        expiration=expiration,
        dte=45,
        contract_symbol="US.NVDA.2026-09-18.P100",
    )
    save_outputs(tmp_path, symbol, payload, output_root=required)
    provider_calls: list[object] = []
    receipt_reads: list[object] = []
    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        lambda **kwargs: provider_calls.append(kwargs),
    )
    monkeypatch.setattr(
        mod,
        "resolve_exact_fresh_required_data_quote_receipt",
        lambda **kwargs: receipt_reads.append(kwargs),
    )

    result = mod.ensure_required_data(
        py="python3",
        base=tmp_path,
        symbol=symbol,
        required_data_dir=required,
        limit_expirations=1,
        want_put=True,
        want_call=False,
        timeout_sec=5,
        is_scheduled=True,
        fetch_source="opend",
        fetch_host="127.0.0.1",
        fetch_port=11111,
        fetch_plan=_nvda_put_fetch_plan(
            expirations=[expiration],
        ),
        position_advice_producer_run_id=None,
    )

    assert result is None
    assert provider_calls == []
    assert receipt_reads == []


def test_planned_fresh_data_without_run_id_never_adopts_or_signs_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod

    required, _state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    expiration = "2026-09-18"
    payload = _typed_success_row_payload(
        symbol=symbol,
        expiration=expiration,
        dte=45,
        contract_symbol="US.NVDA.2026-09-18.P100",
    )
    receipt_reads: list[object] = []
    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        lambda **_kwargs: payload,
    )
    monkeypatch.setattr(
        mod,
        "resolve_exact_fresh_required_data_quote_receipt",
        lambda **kwargs: receipt_reads.append(kwargs),
    )

    result = mod.ensure_required_data(
        py="python3",
        base=tmp_path,
        symbol=symbol,
        required_data_dir=required,
        limit_expirations=1,
        want_put=True,
        want_call=False,
        timeout_sec=5,
        is_scheduled=True,
        fetch_source="opend",
        fetch_host="127.0.0.1",
        fetch_port=11111,
        fetch_plan=_nvda_put_fetch_plan(
            expirations=[expiration],
        ),
        position_advice_producer_run_id=None,
    )

    assert result is None
    assert receipt_reads == []
    quote_root = required / "position_advice_sources" / "quotes"
    assert list(quote_root.glob("**/receipt.json")) == []
    assert list(quote_root.glob("**/payload.json")) == []


def test_manual_fetch_with_run_id_fails_before_cache_or_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod
    from src.application.position_advice_source_receipts import (
        PositionAdviceSourceError,
    )

    required, _state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    parsed = required / "parsed" / f"{symbol}_required_data.csv"
    parsed_before = b"dte\n12\n"
    parsed.write_bytes(parsed_before)
    provider_calls: list[object] = []
    receipt_reads: list[object] = []

    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        lambda **kwargs: (
            provider_calls.append(kwargs)
            or _typed_success_empty_payload(symbol)
        ),
    )
    monkeypatch.setattr(
        mod,
        "resolve_exact_fresh_required_data_quote_receipt",
        lambda **kwargs: (
            receipt_reads.append(kwargs)
            or {"snapshot_id": "stale-symbol-receipt"}
        ),
    )

    with pytest.raises(PositionAdviceSourceError, match="fetch plan"):
        mod.ensure_required_data(
            py="python3",
            base=tmp_path,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=1,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=True,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=None,
            position_advice_producer_run_id="run-manual-must-fail",
        )

    assert provider_calls == []
    assert receipt_reads == []
    assert parsed.read_bytes() == parsed_before
    quote_root = required / "position_advice_sources" / "quotes"
    assert list(quote_root.glob("**/receipt.json")) == []
    assert list(quote_root.glob("**/payload.json")) == []


def test_ensure_required_data_does_not_read_raw_fetch_file_on_main_path() -> None:
    import pathlib
    import src.application.required_data_steps as mod

    root = (BASE / "tests" / ".tmp_pipeline_fetch_read_model_no_raw").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "AAPL"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text("dte\n12\n", encoding="utf-8")
    (required / "raw" / f"{symbol}_required_data.json").write_text(
        '{"meta": {"error": "legacy_error"}}',
        encoding="utf-8",
    )

    old_execute = mod.execute_required_data_opend
    old_read_text = pathlib.Path.read_text
    called: list[object] = []
    raw_touched: list[Path] = []

    def _guard_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        p = str(self)
        if p.endswith(f"{symbol}_required_data.json"):
            raw_touched.append(self)
        return old_read_text(self, *args, **kwargs)

    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        pathlib.Path.read_text = _guard_read_text  # type: ignore[assignment]
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]
        pathlib.Path.read_text = old_read_text  # type: ignore[assignment]

    assert called == []
    assert raw_touched == []


def test_ensure_required_data_records_error_when_fetch_payload_reports_error(tmp_path: Path) -> None:
    from src.application import pipeline_fetch_models as models
    import src.application.required_data_steps as mod

    required, state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"

    old_execute = mod.execute_required_data_opend
    try:
        def _fake_execute_required_data_opend(**_kwargs):  # type: ignore[no-untyped-def]
            return {
                "symbol": symbol,
                "rows": [
                    {
                        "symbol": symbol,
                        "option_type": "put",
                        "expiration": "2026-06-19",
                        "dte": 44,
                        "contract_symbol": "US.NVDA.2026-06-19.P100",
                        "strike": 100,
                        "spot": 120,
                    }
                ],
                "expiration_count": 1,
                "meta": {"status": "error", "error_code": "RATE_LIMIT", "error": "snapshot rate limited"},
            }

        mod.execute_required_data_opend = _fake_execute_required_data_opend  # type: ignore[assignment]
        try:
            mod.ensure_required_data(
                py="python3",
                base=BASE,
                symbol=symbol,
                required_data_dir=required,
                limit_expirations=2,
                want_put=True,
                want_call=False,
                timeout_sec=5,
                is_scheduled=False,
                state_dir=state_dir,
                fetch_source="opend",
                fetch_host="127.0.0.1",
                fetch_port=11111,
            )
        except RuntimeError as exc:
            assert "snapshot rate limited" in str(exc)
        else:
            raise AssertionError("expected required_data fetch error to propagate")
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]

    current = models.read_symbol_fetch_current(state_dir=state_dir, symbol=symbol)
    assert current is not None
    assert current["status"] == "error"
    assert "snapshot rate limited" in current["reason"]
    assert not list(
        (required / "position_advice_sources" / "quotes").glob(
            "*/*/*/receipt.json"
        )
    )


def test_ensure_required_data_rejects_direct_contract_coverage_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod
    from src.application.position_advice_source_receipts import (
        PositionAdviceSourceError,
    )
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    required, _state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    side_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=20,
        max_dte=60,
        explicit_expirations=["2026-06-19"],
        strike_window=StrikeWindowPlan(
            min_strike=90.0,
            max_strike=100.0,
            source="test",
            base_min_strike=90.0,
            base_max_strike=100.0,
        ),
        planning_reason="coverage regression",
    )
    plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=120.0,
        side_plans=[side_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol=symbol,
                limit_expirations=1,
                side_plans=[side_plan],
                trading_date=_trading_date_for_dte(
                    expiration="2026-06-19",
                    dte=44,
                ),
            )
        ],
        expiration_discovery=OptionExpirationDiscoveryResult(
            outcome="success_rows",
            reason_code=None,
            expirations=["2026-06-19"],
            observed_at_utc=_TEST_EVIDENCE_OBSERVED_AT.isoformat(),
            completed_at_utc=_TEST_EVIDENCE_COMPLETED_AT.isoformat(),
            request_identity={
                "symbol": symbol,
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": _trading_date_for_dte(
                    expiration="2026-06-19",
                    dte=44,
                ),
            },
        ),
        projection_outcome="success_rows",
        projected_expirations=["2026-06-19"],
        require_realized_volatility=False,
    )
    code = "US.NVDA.2026-06-19.P50"
    payload = {
        "symbol": symbol,
        "underlier_code": "US.NVDA",
        "trading_date": _trading_date_for_dte(
            expiration="2026-06-19",
            dte=44,
        ),
        "spot": 120.0,
        "expirations": ["2026-06-19"],
        "expiration_count": 1,
        "rows": [
            {
                "symbol": symbol,
                "option_type": "put",
                "expiration": "2026-06-19",
                "dte": 44,
                "contract_symbol": code,
                "strike": 50.0,
                "spot": 120.0,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "volume": 10,
                "open_interest": 100,
                "implied_volatility": 0.4,
                "currency": "USD",
                "multiplier": 100,
            }
        ],
        "meta": {
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
            "trading_date": _trading_date_for_dte(
                expiration="2026-06-19",
                dte=44,
            ),
            "status": "ok",
            "source_outcome": "success_rows",
            "reason_code": None,
            "snapshot_complete": True,
            "snapshot_requested_codes": 1,
            "snapshot_returned_codes": 1,
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": [code],
            "snapshot_returned_code_set": [code],
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "source_observed_at": _TEST_EVIDENCE_OBSERVED_AT.isoformat(),
            "completed_at_utc": _TEST_EVIDENCE_COMPLETED_AT.isoformat(),
        },
    }
    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        lambda **_kwargs: payload,
    )

    with pytest.raises(
        PositionAdviceSourceError,
        match=r"^invalid_row_identity:",
    ):
        mod.ensure_required_data(
            py="python3",
            base=tmp_path,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=1,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=True,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=plan,
            position_advice_producer_run_id="run-coverage-gap",
        )

    assert not list(
        (required / "position_advice_sources" / "quotes").glob(
            "*/*/*/receipt.json"
        )
    )


def test_fresh_candidate_missing_dte_preserves_existing_required_data_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod
    from src.application.position_advice_source_receipts import (
        PositionAdviceSourceError,
    )

    required, _state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    raw_path = required / "raw" / f"{symbol}_required_data.json"
    csv_path = required / "parsed" / f"{symbol}_required_data.csv"
    existing_payload = _typed_success_row_payload(
        symbol=symbol,
        expiration="2026-08-21",
        dte=17,
        contract_symbol="US.NVDA.2026-08-21.P100",
    )
    raw_before = (
        json.dumps(existing_payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    existing_rows = existing_payload["rows"]
    assert isinstance(existing_rows, list)
    csv_before = pd.DataFrame(existing_rows).to_csv(index=False).encode("utf-8")
    raw_path.write_bytes(raw_before)
    csv_path.write_bytes(csv_before)

    candidate = _typed_success_row_payload(
        symbol=symbol,
        expiration="2026-09-18",
        dte=45,
        contract_symbol="US.NVDA.2026-09-18.P100",
    )
    candidate_rows = candidate["rows"]
    assert isinstance(candidate_rows, list)
    assert isinstance(candidate_rows[0], dict)
    candidate_rows[0].pop("dte")
    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        lambda **_kwargs: candidate,
    )

    with pytest.raises(
        PositionAdviceSourceError,
        match="rows would be dropped during persistence",
    ):
        mod.ensure_required_data(
            py="python3",
            base=tmp_path,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=1,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=True,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=_nvda_put_fetch_plan(
                expirations=["2026-09-18"],
            ),
            position_advice_producer_run_id="run-missing-dte",
        )

    assert raw_path.read_bytes() == raw_before
    assert csv_path.read_bytes() == csv_before
    quote_root = required / "position_advice_sources" / "quotes"
    assert list(quote_root.glob("**/receipt.json")) == []
    assert list(quote_root.glob("**/payload.json")) == []


def test_multi_request_merge_preserves_union_and_stable_evidence_window() -> None:
    import src.application.required_data_steps as mod

    rv = {
        "status": "ok",
        "realized_volatility_estimate": 0.3,
    }

    def _payload(code: str, observed: str, completed: str) -> dict[str, object]:
        return {
            "symbol": "NVDA",
            "rows": [{"contract_symbol": code}],
            "meta": {
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "status": "ok",
                "source_outcome": "success_rows",
                "reason_code": None,
                "snapshot_complete": True,
                "snapshot_requested_codes": 1,
                "snapshot_returned_codes": 1,
                "snapshot_missing_codes": 0,
                "snapshot_unexpected_codes": 0,
                "snapshot_requested_code_set": [code],
                "snapshot_returned_code_set": [code],
                "snapshot_missing_code_set": [],
                "snapshot_unexpected_code_set": [],
                "realized_volatility": rv,
                "source_observed_at": observed,
                "completed_at_utc": completed,
            },
        }

    first_observed_at = _TEST_EVIDENCE_COMPLETED_AT - timedelta(seconds=3)
    second_observed_at = _TEST_EVIDENCE_COMPLETED_AT - timedelta(seconds=2)
    first_completed_at = _TEST_EVIDENCE_COMPLETED_AT - timedelta(seconds=1)
    second_completed_at = _TEST_EVIDENCE_COMPLETED_AT
    payloads = [
        _payload(
            "US.NVDA.2026-06-19.P100",
            first_observed_at.isoformat(),
            first_completed_at.isoformat(),
        ),
        _payload(
            "US.NVDA.2026-07-17.P100",
            second_observed_at.isoformat(),
            second_completed_at.isoformat(),
        ),
    ]
    merged = mod.merge_required_data_payloads(
        symbol="NVDA",
        payloads=payloads,
    )

    mod._bind_merged_payload_evidence(
        merged_payload=merged,
        payloads=payloads,
    )

    meta = merged["meta"]
    assert meta["status"] == "ok"
    assert meta["snapshot_requested_code_set"] == [
        "US.NVDA.2026-06-19.P100",
        "US.NVDA.2026-07-17.P100",
    ]
    assert meta["snapshot_returned_codes"] == 2
    assert meta["snapshot_missing_code_set"] == []
    assert meta["source_observed_at"] == first_observed_at.isoformat()
    assert meta["completed_at_utc"] == second_completed_at.isoformat()


@pytest.mark.parametrize("conflicting", [False, True])
def test_multi_request_merge_rejects_duplicate_contract_within_child(
    conflicting: bool,
) -> None:
    import src.application.required_data_fetching as fetching

    payload = _typed_success_row_payload(
        symbol="NVDA",
        expiration="2026-08-21",
        dte=17,
        contract_symbol="US.NVDA.2026-08-21.P100",
    )
    duplicate = dict(payload["rows"][0])
    if conflicting:
        duplicate["mid"] = 9.9
    payload["rows"].append(duplicate)

    with pytest.raises(RuntimeError, match="duplicate contract identity"):
        fetching.merge_required_data_payloads(
            symbol="NVDA",
            payloads=[payload],
        )


def test_multi_request_merge_explicitly_reconciles_identical_cross_child_overlap() -> None:
    import src.application.required_data_fetching as fetching

    first = _typed_success_row_payload(
        symbol="NVDA",
        expiration="2026-08-21",
        dte=17,
        contract_symbol="US.NVDA.2026-08-21.P100",
    )
    second = json.loads(json.dumps(first))

    merged = fetching.merge_required_data_payloads(
        symbol="NVDA",
        payloads=[first, second],
    )

    assert len(merged["rows"]) == 1
    assert merged["meta"]["reconciled_contract_overlap_count"] == 1
    assert merged["meta"]["reconciled_contract_overlaps"] == [
        "US.NVDA.2026-08-21.P100"
    ]


def test_multi_request_merge_rejects_conflicting_cross_child_overlap() -> None:
    import src.application.required_data_fetching as fetching

    first = _typed_success_row_payload(
        symbol="NVDA",
        expiration="2026-08-21",
        dte=17,
        contract_symbol="US.NVDA.2026-08-21.P100",
    )
    second = json.loads(json.dumps(first))
    second["rows"][0]["mid"] = 9.9

    with pytest.raises(RuntimeError, match="conflicting contract overlap"):
        fetching.merge_required_data_payloads(
            symbol="NVDA",
            payloads=[first, second],
        )


def test_child_request_evidence_binds_actual_payload_and_rejects_collision() -> None:
    import src.application.required_data_fetching as fetching

    fetch_plan = _nvda_put_fetch_plan(expirations=["2026-08-21"])
    planned_request = fetch_plan.merged_specs[0].to_debug_dict()
    payload = _typed_success_row_payload(
        symbol="NVDA",
        expiration="2026-08-21",
        dte=17,
        contract_symbol="US.NVDA.2026-08-21.P100",
    )

    bound = fetching.bind_required_data_child_request_evidence(
        payload=payload,
        planned_request=planned_request,
        request_index=0,
    )

    assert bound is not payload
    assert bound["meta"] is not payload["meta"]
    assert "request_index" not in payload["meta"]
    assert bound["meta"]["request_index"] == 0
    assert bound["meta"]["request_symbol"] == "NVDA"
    assert bound["meta"]["request_underlier_code"] == "US.NVDA"

    collided = {**payload, "meta": dict(payload["meta"])}
    collided["meta"]["planned_request_sha256"] = "provider-forged"
    with pytest.raises(RuntimeError, match="reserved fields"):
        fetching.bind_required_data_child_request_evidence(
            payload=collided,
            planned_request=planned_request,
            request_index=0,
        )


@pytest.mark.parametrize(
    ("invalid_time_kind", "error_match"),
    [
        (
            "observation_after_completion",
            "completion precedes source observation",
        ),
        ("future_observation", "quote observation is in the future"),
        ("future_completion", "completion is in the future"),
    ],
)
def test_multi_spec_rejects_invalid_child_time_without_publishing_receipt(
    invalid_time_kind: str,
    error_match: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod
    from src.application.position_advice_source_receipts import (
        PositionAdviceSourceError,
    )

    required, _state_dir = _make_dirs(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    normal_payload = _typed_success_row_payload(
        symbol="NVDA",
        expiration="2026-08-21",
        dte=17,
        contract_symbol="US.NVDA.2026-08-21.P100",
        source_observed_at=now - timedelta(seconds=4),
        completed_at_utc=now - timedelta(seconds=3),
    )
    if invalid_time_kind == "observation_after_completion":
        invalid_observed_at = now - timedelta(seconds=1)
        invalid_completed_at = now - timedelta(seconds=2)
    elif invalid_time_kind == "future_observation":
        invalid_observed_at = now + timedelta(seconds=60)
        invalid_completed_at = now + timedelta(seconds=61)
    else:
        invalid_observed_at = now - timedelta(seconds=1)
        invalid_completed_at = now + timedelta(seconds=60)
    invalid_payload = _typed_success_row_payload(
        symbol="NVDA",
        expiration="2026-09-18",
        dte=45,
        contract_symbol="US.NVDA.2026-09-18.C130",
        option_type="call",
        strike=130.0,
        source_observed_at=invalid_observed_at,
        completed_at_utc=invalid_completed_at,
    )
    payload_by_expiration = {
        "2026-08-21": normal_payload,
        "2026-09-18": invalid_payload,
    }

    def _execute_required_data_opend(*, base: Path, request):  # type: ignore[no-untyped-def]
        del base
        return payload_by_expiration[request.explicit_expirations[0]]

    monkeypatch.setattr(
        mod,
        "execute_required_data_opend",
        _execute_required_data_opend,
    )

    with pytest.raises(PositionAdviceSourceError, match=error_match):
        mod.ensure_required_data(
            py="python3",
            base=tmp_path,
            symbol="NVDA",
            required_data_dir=required,
            limit_expirations=2,
            want_put=True,
            want_call=True,
            timeout_sec=5,
            is_scheduled=True,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=_nvda_split_side_fetch_plan(),
            position_advice_producer_run_id=f"run-child-time-{invalid_time_kind}",
        )

    quote_root = required / "position_advice_sources" / "quotes"
    assert list(quote_root.glob("**/receipt.json")) == []
    assert list(quote_root.glob("**/payload.json")) == []


def test_ensure_required_data_rejects_partial_payload_with_usable_rows(
    tmp_path: Path,
) -> None:
    import src.application.required_data_steps as mod

    required, state_dir = _make_dirs(tmp_path)
    symbol = "NVDA"
    old_execute = mod.execute_required_data_opend
    try:
        mod.execute_required_data_opend = lambda **_kwargs: {  # type: ignore[assignment]
            "symbol": symbol,
            "rows": [
                {
                    "symbol": symbol,
                    "option_type": "put",
                    "expiration": "2026-06-19",
                    "dte": 44,
                    "contract_symbol": "US.NVDA.2026-06-19.P100",
                    "strike": 100,
                    "spot": 120,
                }
            ],
            "expiration_count": 2,
            "meta": {
                "status": "partial",
                "error": "one expiration unavailable",
            },
        }
        with pytest.raises(RuntimeError, match="one expiration unavailable"):
            mod.ensure_required_data(
                py="python3",
                base=BASE,
                symbol=symbol,
                required_data_dir=required,
                limit_expirations=2,
                want_put=True,
                want_call=False,
                timeout_sec=5,
                is_scheduled=False,
                state_dir=state_dir,
            )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]


def test_fetch_required_data_opend_normalizes_timestamp_explicit_expirations(tmp_path: Path) -> None:
    from src.application.required_data_fetching import RequiredDataFetchRequest
    import src.application.required_data_fetching as mod

    old_fetch = mod.fetch_symbol_request
    old_save = mod.save_outputs
    captured: dict[str, object] = {}
    try:
        def _fake_fetch_symbol_request(request):  # type: ignore[no-untyped-def]
            captured["symbol"] = request.symbol
            captured["explicit_expirations"] = request.explicit_expirations
            return {"rows": [], "expiration_count": 0}

        def _fake_save_outputs(base, symbol, payload, *, output_root=None):  # type: ignore[no-untyped-def]
            return Path(base) / "raw.json", Path(base) / "parsed.csv"

        mod.fetch_symbol_request = _fake_fetch_symbol_request  # type: ignore[assignment]
        mod.save_outputs = _fake_save_outputs  # type: ignore[assignment]
        mod.fetch_required_data_opend(
            base=tmp_path,
            request=RequiredDataFetchRequest(
                symbol="FUTU",
                limit_expirations=2,
                explicit_expirations=[1777420800, "1781740800000"],
            ),
        )
    finally:
        mod.fetch_symbol_request = old_fetch  # type: ignore[assignment]
        mod.save_outputs = old_save  # type: ignore[assignment]

    assert captured["symbol"] == "FUTU"
    assert captured["explicit_expirations"] == ["2026-04-29", "2026-06-18"]


def test_fetch_required_data_opend_forwards_side_strike_windows(tmp_path: Path) -> None:
    from src.application.required_data_fetching import RequiredDataFetchRequest
    import src.application.required_data_fetching as mod

    old_fetch = mod.fetch_symbol_request
    old_save = mod.save_outputs
    captured: dict[str, object] = {}
    try:
        def _fake_fetch_symbol_request(request):  # type: ignore[no-untyped-def]
            captured["symbol"] = request.symbol
            captured["side_strike_windows"] = request.side_strike_windows
            return {"rows": [], "expiration_count": 0}

        def _fake_save_outputs(base, symbol, payload, *, output_root=None):  # type: ignore[no-untyped-def]
            return Path(base) / "raw.json", Path(base) / "parsed.csv"

        mod.fetch_symbol_request = _fake_fetch_symbol_request  # type: ignore[assignment]
        mod.save_outputs = _fake_save_outputs  # type: ignore[assignment]
        mod.fetch_required_data_opend(
            base=tmp_path,
            request=RequiredDataFetchRequest(
                symbol="0700.HK",
                limit_expirations=2,
                option_types="put,call",
                side_strike_windows={
                    "put": {"min_strike": 420.0, "max_strike": 460.0},
                    "call": {"min_strike": 505.0, "max_strike": 560.0},
                },
            ),
        )
    finally:
        mod.fetch_symbol_request = old_fetch  # type: ignore[assignment]
        mod.save_outputs = old_save  # type: ignore[assignment]

    assert captured["symbol"] == "0700.HK"
    assert captured["side_strike_windows"] == {
        "put": {"min_strike": 420.0, "max_strike": 460.0},
        "call": {"min_strike": 505.0, "max_strike": 560.0},
    }


def test_build_fetch_request_from_spec_applies_opend_fetch_config() -> None:
    from src.application.required_data_fetching import build_fetch_request_from_spec
    from src.application.required_data_planning import RequiredDataFetchSpec

    request = build_fetch_request_from_spec(
        spec=RequiredDataFetchSpec(
            symbol="0700.HK",
            limit_expirations=1,
            host="127.0.0.1",
            port=11111,
            option_types=("call",),
            explicit_expirations=["2026-05-29"],
            min_dte=10,
            max_dte=60,
            side_strike_windows={"call": {"min_strike": 505.0, "max_strike": 560.0}},
            trading_date="2026-05-09",
        ),
        spot_override=470.0,
        opend_fetch_config={
            "max_wait_sec": 11,
            "option_chain_window_sec": 12,
            "option_chain_max_calls": 13,
            "snapshot_max_wait_sec": 21,
            "snapshot_window_sec": 22,
            "snapshot_max_calls": 23,
            "expiration_max_wait_sec": 31,
            "expiration_window_sec": 32,
            "expiration_max_calls": 33,
        },
    )

    assert request.spot_override == 470.0
    assert request.max_wait_sec == 11
    assert request.option_chain_window_sec == 12
    assert request.option_chain_max_calls == 13
    assert request.snapshot_max_wait_sec == 21
    assert request.snapshot_window_sec == 22
    assert request.snapshot_max_calls == 23
    assert request.expiration_max_wait_sec == 31
    assert request.expiration_window_sec == 32
    assert request.expiration_max_calls == 33


def test_ensure_required_data_passes_opend_fetch_config_into_fetch_plan_requests() -> None:
    import src.application.required_data_steps as mod
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    root = (BASE / "tests" / ".tmp_pipeline_fetch_opend_config").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "0700.HK"
    side_plan = OptionSideFetchPlan(
        option_type="call",
        min_dte=10,
        max_dte=60,
        explicit_expirations=["2026-05-29"],
        strike_window=StrikeWindowPlan(
            min_strike=505.0,
            max_strike=561.0,
            source="test",
            base_min_strike=505.0,
            base_max_strike=550.0,
        ),
        planning_reason="test",
    )

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=470.0,
        side_plans=[side_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol=symbol,
                limit_expirations=1,
                side_plans=[side_plan],
                trading_date="2026-05-09",
            )
        ],
        expiration_discovery=_success_rows_expiration_discovery(
            symbol=symbol,
            expirations=["2026-05-29"],
            trading_date="2026-05-09",
        ),
        projection_outcome="success_rows",
        projected_expirations=["2026-05-29"],
        require_realized_volatility=False,
    )

    old_execute = mod.execute_required_data_opend
    old_finalize = mod.finalize_required_data_quote_candidate
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = (  # type: ignore[assignment]
            lambda **_kwargs: {"evidence": None}
        )
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=1,
            want_put=False,
            want_call=True,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=fetch_plan,
            report_dir=root / "reports",
            opend_fetch_config={
                "max_wait_sec": 11,
                "option_chain_window_sec": 12,
                "option_chain_max_calls": 13,
                "snapshot_max_wait_sec": 21,
                "snapshot_window_sec": 22,
                "snapshot_max_calls": 23,
                "expiration_max_wait_sec": 31,
                "expiration_window_sec": 32,
                "expiration_max_calls": 33,
            },
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = old_finalize  # type: ignore[assignment]

    request = called[0]["request"]
    assert request.spot_override == 470.0
    assert request.max_wait_sec == 11
    assert request.option_chain_window_sec == 12
    assert request.option_chain_max_calls == 13
    assert request.snapshot_max_wait_sec == 21
    assert request.snapshot_window_sec == 22
    assert request.snapshot_max_calls == 23
    assert request.expiration_max_wait_sec == 31
    assert request.expiration_window_sec == 32
    assert request.expiration_max_calls == 33


def test_ensure_required_data_refetches_when_existing_bounds_do_not_cover_plan() -> None:
    import src.application.required_data_steps as mod
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    root = (BASE / "tests" / ".tmp_pipeline_fetch_plan_bounds_gap").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "0700.HK"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,contract_symbol,strike,spot,bid,ask,last_price,mid,volume,open_interest,implied_volatility,in_the_money,currency,otm_pct,delta,multiplier",
                "0700.HK,call,2026-05-29,20,C1,560,470,1,1,1,1,1,1,0.2,,HKD,0.19,0.1,100",
            ]
        ),
        encoding="utf-8",
    )
    side_plan = OptionSideFetchPlan(
        option_type="call",
        min_dte=10,
        max_dte=60,
        explicit_expirations=["2026-05-29"],
        strike_window=StrikeWindowPlan(
            min_strike=505.0,
            max_strike=561.0,
            source="test",
            base_min_strike=505.0,
            base_max_strike=550.0,
        ),
        planning_reason="test",
    )

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=470.0,
        side_plans=[side_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol=symbol,
                limit_expirations=1,
                side_plans=[side_plan],
                trading_date="2026-05-09",
            )
        ],
        expiration_discovery=_success_rows_expiration_discovery(
            symbol=symbol,
            expirations=["2026-05-29"],
            trading_date="2026-05-09",
        ),
        projection_outcome="success_rows",
        projected_expirations=["2026-05-29"],
        require_realized_volatility=False,
    )

    old_execute = mod.execute_required_data_opend
    old_finalize = mod.finalize_required_data_quote_candidate
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = (  # type: ignore[assignment]
            lambda **_kwargs: {"evidence": None}
        )
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=1,
            want_put=False,
            want_call=True,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=fetch_plan,
            report_dir=root / "reports",
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = old_finalize  # type: ignore[assignment]

    assert len(called) == 1


def test_ensure_required_data_fetches_yield_enhancement_call_side_when_local_cache_has_only_puts() -> None:
    import src.application.required_data_steps as mod
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    root = (BASE / "tests" / ".tmp_pipeline_fetch_yield_call_gap").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "NVDA"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,contract_symbol,strike,spot,bid,ask,last_price,mid,volume,open_interest,implied_volatility,in_the_money,currency,otm_pct,delta,multiplier",
                "NVDA,put,2026-06-19,44,P1,90,100,3,3.2,3.1,3.1,80,1200,0.42,,USD,0.10,-0.25,100",
            ]
        ),
        encoding="utf-8",
    )
    put_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=20,
        max_dte=60,
        explicit_expirations=["2026-06-19"],
        strike_window=StrikeWindowPlan(
            min_strike=90.0,
            max_strike=96.0,
            source="sell_put.configured_bounds",
            base_min_strike=90.0,
            base_max_strike=96.0,
        ),
        planning_reason="test put",
    )
    call_plan = OptionSideFetchPlan(
        option_type="call",
        min_dte=20,
        max_dte=60,
        explicit_expirations=["2026-06-19"],
        strike_window=StrikeWindowPlan(
            min_strike=103.0,
            max_strike=127.5,
            source="yield_enhancement.call.spot_derived_bounds",
            buffer_applied=True,
            buffer_pct=0.02,
            base_min_strike=103.0,
            base_max_strike=125.0,
        ),
        planning_reason="test yield enhancement call",
    )

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=100.0,
        side_plans=[put_plan, call_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol=symbol,
                limit_expirations=1,
                side_plans=[put_plan, call_plan],
                trading_date="2026-05-06",
            )
        ],
        expiration_discovery=_success_rows_expiration_discovery(
            symbol=symbol,
            expirations=["2026-06-19"],
            trading_date="2026-05-06",
        ),
        projection_outcome="success_rows",
        projected_expirations=["2026-06-19"],
        require_realized_volatility=False,
    )

    old_execute = mod.execute_required_data_opend
    old_finalize = mod.finalize_required_data_quote_candidate
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = (  # type: ignore[assignment]
            lambda **_kwargs: {"evidence": None}
        )
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=1,
            want_put=True,
            want_call=True,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=fetch_plan,
            report_dir=root / "reports",
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = old_finalize  # type: ignore[assignment]

    assert len(called) == 1
    request = called[0]["request"]
    assert request.option_types == "put,call"
    assert request.explicit_expirations == ["2026-06-19"]
    assert request.side_strike_windows["call"] == {"min_strike": 103.0, "max_strike": 127.5}
    assert request.chain_cache is True
    assert request.freshness_policy == "cache_first"


def test_ensure_required_data_refetches_when_bounds_are_split_across_expirations() -> None:
    import src.application.required_data_steps as mod
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        StrikeWindowPlan,
    )

    root = (BASE / "tests" / ".tmp_pipeline_fetch_plan_split_exp").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "0700.HK"
    (required / "parsed" / f"{symbol}_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,contract_symbol,strike,spot,bid,ask,last_price,mid,volume,open_interest,implied_volatility,in_the_money,currency,otm_pct,delta,multiplier",
                "0700.HK,call,2026-05-29,20,C1,505,470,1,1,1,1,1,1,0.2,,HKD,0.07,0.1,100",
                "0700.HK,call,2026-06-26,48,C2,550,470,1,1,1,1,1,1,0.2,,HKD,0.17,0.1,100",
            ]
        ),
        encoding="utf-8",
    )
    side_plan = OptionSideFetchPlan(
        option_type="call",
        min_dte=10,
        max_dte=60,
        explicit_expirations=["2026-05-29", "2026-06-26"],
        strike_window=StrikeWindowPlan(
            min_strike=505.0,
            max_strike=561.0,
            source="test",
            base_min_strike=505.0,
            base_max_strike=550.0,
        ),
        planning_reason="test",
    )

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=470.0,
        side_plans=[side_plan],
        merged_specs=[
            _fetch_spec_for_side_plans(
                symbol=symbol,
                limit_expirations=2,
                side_plans=[side_plan],
                trading_date="2026-05-09",
            )
        ],
        expiration_discovery=_success_rows_expiration_discovery(
            symbol=symbol,
            expirations=["2026-05-29", "2026-06-26"],
            trading_date="2026-05-09",
        ),
        projection_outcome="success_rows",
        projected_expirations=["2026-05-29", "2026-06-26"],
        require_realized_volatility=False,
    )

    old_execute = mod.execute_required_data_opend
    old_finalize = mod.finalize_required_data_quote_candidate
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = (  # type: ignore[assignment]
            lambda **_kwargs: {"evidence": None}
        )
        mod.ensure_required_data(
            py="python3",
            base=BASE,
            symbol=symbol,
            required_data_dir=required,
            limit_expirations=2,
            want_put=False,
            want_call=True,
            timeout_sec=5,
            is_scheduled=False,
            state_dir=state_dir,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=fetch_plan,
            report_dir=root / "reports",
        )
    finally:
        mod.execute_required_data_opend = old_execute  # type: ignore[assignment]
        mod.finalize_required_data_quote_candidate = old_finalize  # type: ignore[assignment]

    assert len(called) == 1


def _tcom_portfolio_context(account: str) -> dict[str, object]:
    if account == "lx":
        return {
            "cash_by_currency": {"HKD": 666787.5, "USD": 10177.48},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 386500.0, "USD": 8000.0}},
        }
    return {
        "cash_by_currency": {"HKD": 1104646.19},
        "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 213000.0, "USD": 8500.0}},
    }


def _build_tcom_put_plan(*, required_data_dir: Path, account: str):  # type: ignore[no-untyped-def]
    from src.application.prefilters import apply_prefilters
    from src.application.required_data_planning import build_required_data_fetch_plan

    sell_put = {
        "enabled": True,
        "strategy": "insurance_underwriting",
        "min_dte": 7,
        "max_dte": 60,
        "max_strike": 45.0,
    }
    prefilters = apply_prefilters(
        symbol="TCOM",
        sp=sell_put,
        cc={"enabled": False},
        want_put=True,
        want_call=False,
        portfolio_ctx=_tcom_portfolio_context(account),
    )
    return build_required_data_fetch_plan(
        base=required_data_dir.parent,
        required_data_dir=required_data_dir,
        symbol="TCOM",
        limit_expirations=10,
        want_put=prefilters.want_put,
        want_call=False,
        sell_put_cfg=prefilters.sp,
        sell_call_cfg={"enabled": False},
        fetch_host="127.0.0.1",
        fetch_port=11111,
    )


def _tcom_required_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for expiration, dte in (("2026-08-21", 30), ("2026-09-18", 58)):
        for strike in (35.0, 40.0, 42.5):
            rows.append(
                {
                    "symbol": "TCOM",
                    "option_type": "put",
                    "expiration": expiration,
                    "dte": dte,
                    "contract_symbol": f"US.TCOM.{expiration}.P{strike:g}",
                    "strike": strike,
                    "spot": 43.07,
                    "bid": 1.0,
                    "ask": 1.1,
                    "last_price": 1.05,
                    "mid": 1.05,
                    "volume": 100,
                    "open_interest": 1000,
                    "implied_volatility": 0.4,
                    "realized_volatility_20": 0.3,
                    "realized_volatility_60": 0.3,
                    "realized_volatility_120": 0.3,
                    "realized_volatility_estimate": 0.3,
                    "currency": "USD",
                    "multiplier": 100,
                }
            )
    return rows


def _plan_semantics(plan) -> dict[str, object]:  # type: ignore[no-untyped-def]
    payload = plan.to_debug_dict()
    discovery = payload.get("expiration_discovery")
    if isinstance(discovery, dict):
        discovery.pop("observed_at_utc", None)
        discovery.pop("completed_at_utc", None)
    return payload


@pytest.mark.parametrize("account_order", [("lx", "sy"), ("sy", "lx")])
def test_tcom_shared_required_data_is_account_order_invariant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    account_order: tuple[str, str],
) -> None:
    from datetime import date

    import src.application.opend_utils as opend_utils
    import src.application.opend_symbol_chain_fetching as chain_fetching
    import src.application.required_data_planning as planning
    import src.application.required_data_steps as steps

    monkeypatch.setattr(planning, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21", "2026-09-18"])
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 43.07)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 7, 22))
    monkeypatch.setattr(chain_fetching, "get_trading_date", lambda market: date(2026, 7, 22))

    fetch_requests: list[object] = []

    def _fake_execute_required_data_opend(*, base: Path, request):  # type: ignore[no-untyped-def]
        fetch_requests.append(request)
        rows = _tcom_required_rows()
        codes = sorted(str(row["contract_symbol"]) for row in rows)
        return {
            "symbol": "TCOM",
            "underlier_code": "US.TCOM",
            "trading_date": "2026-07-22",
            "spot": 43.07,
            "expirations": ["2026-08-21", "2026-09-18"],
            "expiration_count": 2,
            "rows": rows,
            "meta": {
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": "2026-07-22",
                "status": "ok",
                "source_outcome": "success_rows",
                "reason_code": None,
                "snapshot_complete": True,
                "snapshot_requested_codes": len(codes),
                "snapshot_returned_codes": len(codes),
                "snapshot_missing_codes": 0,
                "snapshot_unexpected_codes": 0,
                "snapshot_requested_code_set": codes,
                "snapshot_returned_code_set": codes,
                "snapshot_missing_code_set": [],
                "snapshot_unexpected_code_set": [],
                "realized_volatility": {
                    "status": "ok",
                    "reason": None,
                    "realized_volatility_20": 0.3,
                    "realized_volatility_60": 0.3,
                    "realized_volatility_120": 0.3,
                    "realized_volatility_estimate": 0.3,
                },
                "source_observed_at": _TEST_EVIDENCE_OBSERVED_AT.isoformat(),
                "completed_at_utc": _TEST_EVIDENCE_COMPLETED_AT.isoformat(),
            },
        }

    monkeypatch.setattr(steps, "execute_required_data_opend", _fake_execute_required_data_opend)
    shared_required = tmp_path / "shared_required_data"
    plans: dict[str, dict[str, object]] = {}
    visible_contracts: dict[str, list[tuple[str, float]]] = {}

    for account in account_order:
        plan = _build_tcom_put_plan(required_data_dir=shared_required, account=account)
        plans[account] = _plan_semantics(plan)
        steps.ensure_required_data(
            py="python3.12",
            base=tmp_path,
            symbol="TCOM",
            required_data_dir=shared_required,
            limit_expirations=10,
            want_put=True,
            want_call=False,
            timeout_sec=5,
            is_scheduled=True,
            fetch_source="opend",
            fetch_host="127.0.0.1",
            fetch_port=11111,
            fetch_plan=plan,
            report_dir=tmp_path / "reports" / account,
        )
        frame = pd.read_csv(shared_required / "parsed" / "TCOM_required_data.csv")
        visible_contracts[account] = sorted(
            (str(row.expiration), float(row.strike))
            for row in frame.itertuples()
            if str(row.option_type).lower() == "put"
        )

    assert plans["lx"] == plans["sy"]
    assert plans["lx"]["side_plans"][0]["min_strike"] == 34.456
    assert plans["lx"]["side_plans"][0]["max_strike"] == 43.07
    assert visible_contracts["lx"] == visible_contracts["sy"] == [
        ("2026-08-21", 35.0),
        ("2026-08-21", 40.0),
        ("2026-08-21", 42.5),
        ("2026-09-18", 35.0),
        ("2026-09-18", 40.0),
        ("2026-09-18", 42.5),
    ]
    assert len(fetch_requests) == 1
    request = fetch_requests[0]
    assert request.option_types == "put"
    assert request.explicit_expirations == ["2026-08-21", "2026-09-18"]
    assert request.side_strike_windows == {"put": {"min_strike": 34.456, "max_strike": 43.07}}
    assert request.include_realized_volatility is True


def test_tcom_concurrent_plan_construction_is_account_invariant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import date

    import src.application.opend_utils as opend_utils
    import src.application.opend_symbol_chain_fetching as chain_fetching
    import src.application.required_data_planning as planning
    from src.application.tick_account_execution import run_account_outcomes

    monkeypatch.setattr(planning, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21", "2026-09-18"])
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 43.07)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 7, 22))
    monkeypatch.setattr(chain_fetching, "get_trading_date", lambda market: date(2026, 7, 22))

    plans = run_account_outcomes(
        account_ids=["lx", "sy"],
        max_workers=2,
        run_account_fn=lambda account: _build_tcom_put_plan(
            required_data_dir=tmp_path / "shared_required_data",
            account=account,
        ),
    )

    plan_semantics = [_plan_semantics(plan) for plan in plans]
    assert plan_semantics[0] == plan_semantics[1]
    assert plan_semantics[0]["side_plans"][0]["min_strike"] == 34.456
    assert plan_semantics[0]["side_plans"][0]["max_strike"] == 43.07


def main() -> None:
    from tempfile import TemporaryDirectory

    test_ensure_required_data_uses_read_model_error_to_force_refetch()
    test_ensure_required_data_skips_when_read_model_is_ok_and_dte_satisfies()
    test_ensure_required_data_treats_futu_source_as_opend_path()
    test_ensure_required_data_does_not_read_raw_fetch_file_on_main_path()
    with TemporaryDirectory() as td:
        test_ensure_required_data_records_error_when_fetch_payload_reports_error(Path(td))
    test_fetch_required_data_opend_normalizes_timestamp_explicit_expirations(Path("."))
    test_fetch_required_data_opend_forwards_side_strike_windows(Path("."))
    test_build_fetch_request_from_spec_applies_opend_fetch_config()
    test_ensure_required_data_passes_opend_fetch_config_into_fetch_plan_requests()
    test_ensure_required_data_refetches_when_existing_bounds_do_not_cover_plan()
    test_ensure_required_data_fetches_yield_enhancement_call_side_when_local_cache_has_only_puts()
    test_ensure_required_data_refetches_when_bounds_are_split_across_expirations()
    print("OK (pipeline-fetch-read-model-boundary)")


if __name__ == "__main__":
    main()
