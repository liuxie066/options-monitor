from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _make_dirs(root: Path) -> tuple[Path, Path]:
    required = (root / "required_data").resolve()
    state_dir = (root / "state").resolve()
    (required / "parsed").mkdir(parents=True, exist_ok=True)
    (required / "raw").mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    return required, state_dir


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
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
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
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
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
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
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
        RequiredDataFetchSpec,
        StrikeWindowPlan,
    )

    root = (BASE / "tests" / ".tmp_pipeline_fetch_opend_config").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    required, state_dir = _make_dirs(root)
    symbol = "0700.HK"

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=470.0,
        side_plans=[
            OptionSideFetchPlan(
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
        ],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol=symbol,
                limit_expirations=1,
                host="127.0.0.1",
                port=11111,
                option_types=("call",),
                explicit_expirations=["2026-05-29"],
                min_dte=10,
                max_dte=60,
                side_strike_windows={"call": {"min_strike": 505.0, "max_strike": 561.0}},
            )
        ],
    )

    old_execute = mod.execute_required_data_opend
    old_save = mod.save_outputs
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.save_outputs = lambda *args, **kwargs: None  # type: ignore[assignment]
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
        mod.save_outputs = old_save  # type: ignore[assignment]

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
        RequiredDataFetchSpec,
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

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=470.0,
        side_plans=[
            OptionSideFetchPlan(
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
        ],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol=symbol,
                limit_expirations=1,
                host="127.0.0.1",
                port=11111,
                option_types=("call",),
                explicit_expirations=["2026-05-29"],
                min_dte=10,
                max_dte=60,
                side_strike_windows={"call": {"min_strike": 505.0, "max_strike": 561.0}},
            )
        ],
    )

    old_execute = mod.execute_required_data_opend
    old_save = mod.save_outputs
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.save_outputs = lambda *args, **kwargs: None  # type: ignore[assignment]
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
        mod.save_outputs = old_save  # type: ignore[assignment]

    assert len(called) == 1


def test_ensure_required_data_fetches_yield_enhancement_call_side_when_local_cache_has_only_puts() -> None:
    import src.application.required_data_steps as mod
    from src.application.required_data_planning import (
        OptionSideFetchPlan,
        RequiredDataFetchPlanBundle,
        RequiredDataFetchSpec,
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

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=100.0,
        side_plans=[
            OptionSideFetchPlan(
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
            ),
            OptionSideFetchPlan(
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
            ),
        ],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol=symbol,
                limit_expirations=1,
                host="127.0.0.1",
                port=11111,
                option_types=("put", "call"),
                explicit_expirations=["2026-06-19"],
                min_dte=20,
                max_dte=60,
                side_strike_windows={
                    "put": {"min_strike": 90.0, "max_strike": 96.0},
                    "call": {"min_strike": 103.0, "max_strike": 127.5},
                },
            )
        ],
    )

    old_execute = mod.execute_required_data_opend
    old_save = mod.save_outputs
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.save_outputs = lambda *args, **kwargs: None  # type: ignore[assignment]
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
        mod.save_outputs = old_save  # type: ignore[assignment]

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
        RequiredDataFetchSpec,
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

    fetch_plan = RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=470.0,
        side_plans=[
            OptionSideFetchPlan(
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
        ],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol=symbol,
                limit_expirations=2,
                host="127.0.0.1",
                port=11111,
                option_types=("call",),
                explicit_expirations=["2026-05-29", "2026-06-26"],
                min_dte=10,
                max_dte=60,
                side_strike_windows={"call": {"min_strike": 505.0, "max_strike": 561.0}},
            )
        ],
    )

    old_execute = mod.execute_required_data_opend
    old_save = mod.save_outputs
    called: list[object] = []
    try:
        mod.execute_required_data_opend = lambda **kwargs: (called.append(kwargs) or {"rows": [], "expirations": [], "meta": {"status": "ok"}})  # type: ignore[assignment]
        mod.save_outputs = lambda *args, **kwargs: None  # type: ignore[assignment]
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
        mod.save_outputs = old_save  # type: ignore[assignment]

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
                    "realized_volatility_estimate": 0.3,
                    "currency": "USD",
                    "multiplier": 100,
                }
            )
    return rows


@pytest.mark.parametrize("account_order", [("lx", "sy"), ("sy", "lx")])
def test_tcom_shared_required_data_is_account_order_invariant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    account_order: tuple[str, str],
) -> None:
    from datetime import date

    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as planning
    import src.application.required_data_steps as steps

    monkeypatch.setattr(planning, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21", "2026-09-18"])
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 43.07)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 7, 22))

    fetch_requests: list[object] = []

    def _fake_execute_required_data_opend(*, base: Path, request):  # type: ignore[no-untyped-def]
        fetch_requests.append(request)
        return {
            "symbol": "TCOM",
            "spot": 43.07,
            "expirations": ["2026-08-21", "2026-09-18"],
            "expiration_count": 2,
            "rows": _tcom_required_rows(),
            "meta": {"status": "ok"},
        }

    monkeypatch.setattr(steps, "execute_required_data_opend", _fake_execute_required_data_opend)
    shared_required = tmp_path / "shared_required_data"
    plans: dict[str, dict[str, object]] = {}
    visible_contracts: dict[str, list[tuple[str, float]]] = {}

    for account in account_order:
        plan = _build_tcom_put_plan(required_data_dir=shared_required, account=account)
        plans[account] = plan.to_debug_dict()
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
    import src.application.required_data_planning as planning
    from src.application.tick_account_execution import run_account_outcomes

    monkeypatch.setattr(planning, "list_option_expirations", lambda *args, **kwargs: ["2026-08-21", "2026-09-18"])
    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: 43.07)
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: date(2026, 7, 22))

    plans = run_account_outcomes(
        account_ids=["lx", "sy"],
        max_workers=2,
        run_account_fn=lambda account: _build_tcom_put_plan(
            required_data_dir=tmp_path / "shared_required_data",
            account=account,
        ).to_debug_dict(),
    )

    assert plans[0] == plans[1]
    assert plans[0]["side_plans"][0]["min_strike"] == 34.456
    assert plans[0]["side_plans"][0]["max_strike"] == 43.07


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
