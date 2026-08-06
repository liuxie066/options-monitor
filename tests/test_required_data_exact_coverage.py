from __future__ import annotations

from copy import deepcopy

import pandas as pd
import pytest

from src.application.required_data_coverage import (
    evaluate_required_data_frame_fetch_plan_debug,
    required_data_frame_covers_fetch_plan,
    required_data_frame_covers_fetch_plan_debug,
)
from src.application.required_data_plan_identity import required_data_request_sha256
from src.application.opend_symbol_chain_fetching import (
    OptionExpirationDiscoveryResult,
)
from src.application.required_data_planning import (
    OptionSideFetchPlan,
    RequiredDataFetchPlanBundle,
    RequiredDataFetchSpec,
    StrikeWindowPlan,
)


EXPIRATION = "2026-08-21"
TRADING_DATE = "2026-07-27"


def _side_plan(
    *,
    option_type: str = "put",
    expirations: list[str] | None = None,
    minimum: object = 100.0,
    maximum: object = 100.0,
    base_minimum: object = 100.0,
    base_maximum: object = 100.0,
    min_dte: object = 20,
    max_dte: object = 30,
    exact_strikes_by_expiration: dict[str, list[object]] | None = None,
) -> dict[str, object]:
    return {
        "option_type": option_type,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "explicit_expirations": list(
            [EXPIRATION] if expirations is None else expirations
        ),
        "strike_window": {
            "min_strike": minimum,
            "max_strike": maximum,
            "source": "test",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": base_minimum,
            "base_max_strike": base_maximum,
        },
        "required_exact_strikes_by_expiration": dict(
            exact_strikes_by_expiration or {}
        ),
    }


def _request(
    *,
    option_type: str = "put",
    expirations: list[str] | None = None,
    minimum: object = 100.0,
    maximum: object = 100.0,
    base_minimum: object = 100.0,
    base_maximum: object = 100.0,
    min_dte: object = 20,
    max_dte: object = 30,
    require_rv: object = False,
    exact_strikes_by_expiration: dict[str, list[object]] | None = None,
) -> dict[str, object]:
    request_expirations = list(
        [EXPIRATION] if expirations is None else expirations
    )
    return {
        "symbol": "NVDA",
        "host": "127.0.0.1",
        "port": 11111,
        "option_types": [option_type],
        "explicit_expirations": request_expirations,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "side_strike_windows": {
            option_type: {
                "min_strike": minimum,
                "max_strike": maximum,
            }
        },
        "include_realized_volatility": require_rv,
        "side_plans": [
            _side_plan(
                option_type=option_type,
                expirations=request_expirations,
                minimum=minimum,
                maximum=maximum,
                base_minimum=base_minimum,
                base_maximum=base_maximum,
                min_dte=min_dte,
                max_dte=max_dte,
                exact_strikes_by_expiration=exact_strikes_by_expiration,
            )
        ],
        "trading_date": TRADING_DATE,
    }


def _plan(*, requests: list[dict[str, object]] | None = None) -> dict[str, object]:
    request_items = list(requests or [_request()])
    return {
        "symbol": "NVDA",
        "spot_reference": 110.0,
        "merged_requests": request_items,
        "require_realized_volatility": (
            request_items[0]["include_realized_volatility"]
            if request_items
            else False
        ),
        "expiration_discovery": {
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": TRADING_DATE,
            }
        },
    }


def _frame(
    *,
    strike: object = 100.0,
    dte: object = 25,
    spot: object = 110.0,
    rv: object | None = None,
) -> pd.DataFrame:
    row: dict[str, object] = {
        "option_type": "put",
        "expiration": EXPIRATION,
        "dte": dte,
        "strike": strike,
        "spot": spot,
    }
    if rv is not None:
        row["term_matched_rv"] = rv
        row["term_matched_rv_status"] = "ok"
        row["term_matched_rv_reason"] = None
    return pd.DataFrame([row])


def _scope_evidence(
    *,
    request: dict[str, object],
    codes_by_scope: dict[tuple[str, str], list[str]],
    statuses: dict[str, str] | None = None,
) -> dict[str, object]:
    codes = sorted({code for values in codes_by_scope.values() for code in values})
    expirations = list(request["explicit_expirations"])
    option_types = list(request["option_types"])
    return {
        "status": "ok",
        "source_outcome": "success_rows",
        "errors": [],
        "stale_cache_expirations": [],
        "option_codes": len(codes),
        "snapshot_complete": True,
        "snapshot_requested_codes": len(codes),
        "snapshot_returned_codes": len(codes),
        "snapshot_missing_codes": 0,
        "snapshot_unexpected_codes": 0,
        "snapshot_requested_code_set": codes,
        "snapshot_returned_code_set": codes,
        "snapshot_missing_code_set": [],
        "snapshot_unexpected_code_set": [],
        "option_chain_scope_coverage": {
            "schema_version": "option_chain_scope_coverage.v1",
            "scopes": [
                {
                    "option_type": option_type,
                    "expiration": expiration,
                    "chain_status": (statuses or {}).get(expiration, "cache"),
                    "filtered_contract_codes": codes_by_scope.get(
                        (option_type, expiration), []
                    ),
                    "filtered_contract_count": len(
                        codes_by_scope.get((option_type, expiration), [])
                    ),
                }
                for option_type in option_types
                for expiration in expirations
            ],
        },
    }


def _typed_plan(
    *,
    exact_strike: float,
    minimum: float | None = 1.0,
    maximum: float | None = None,
) -> RequiredDataFetchPlanBundle:
    side_plan = OptionSideFetchPlan(
        option_type="put",
        min_dte=None,
        max_dte=None,
        explicit_expirations=[EXPIRATION],
        strike_window=StrikeWindowPlan(
            min_strike=minimum,
            max_strike=maximum,
            source="test",
            base_min_strike=minimum,
            base_max_strike=maximum,
        ),
        planning_reason="test",
        required_exact_strikes_by_expiration={
            EXPIRATION: [exact_strike]
        },
    )
    return RequiredDataFetchPlanBundle(
        symbol="NVDA",
        spot_reference=110.0,
        side_plans=[side_plan],
        merged_specs=[
            RequiredDataFetchSpec(
                symbol="NVDA",
                limit_expirations=0,
                host="127.0.0.1",
                port=11111,
                option_types=("put",),
                explicit_expirations=[EXPIRATION],
                min_dte=None,
                max_dte=None,
                side_strike_windows={
                    "put": {
                        "min_strike": minimum,
                        "max_strike": maximum,
                    }
                },
                side_plans=[side_plan],
                trading_date=TRADING_DATE,
            )
        ],
        expiration_discovery=OptionExpirationDiscoveryResult(
            outcome="success_rows",
            reason_code=None,
            expirations=[EXPIRATION],
            observed_at_utc="2026-07-27T01:00:00Z",
            completed_at_utc="2026-07-27T01:00:01Z",
            request_identity={
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": TRADING_DATE,
            },
        ),
        projection_outcome="success_rows",
        projected_expirations=[EXPIRATION],
        require_realized_volatility=False,
    )


def test_exact_coverage_rejects_empty_executable_child() -> None:
    empty_child = _request(
        option_type="call",
        expirations=[],
        minimum=120.0,
        maximum=130.0,
        base_minimum=120.0,
        base_maximum=130.0,
        require_rv=False,
    )

    assert not required_data_frame_covers_fetch_plan_debug(
        _frame(),
        _plan(requests=[_request(), empty_child]),
    )


@pytest.mark.parametrize(
    ("plan_rv", "request_rv"),
    [
        (True, False),
        (False, True),
        ("true", True),
        (1, True),
        (True, "true"),
        (True, 1),
    ],
)
def test_exact_coverage_rejects_rv_authority_drift_or_non_bool(
    plan_rv: object,
    request_rv: object,
) -> None:
    request = _request(require_rv=request_rv)
    plan = _plan(requests=[request])
    plan["require_realized_volatility"] = plan_rv

    assert not required_data_frame_covers_fetch_plan_debug(
        _frame(rv=0.24),
        plan,
    )

def test_exact_coverage_uses_position_strike_when_base_bounds_are_none() -> None:
    position_request = _request(
        minimum=100.0,
        maximum=100.0,
        base_minimum=None,
        base_maximum=None,
        min_dte=None,
        max_dte=None,
        exact_strikes_by_expiration={EXPIRATION: [100.0]},
    )
    plan = _plan(requests=[position_request])

    assert not required_data_frame_covers_fetch_plan_debug(
        _frame(strike=90.0),
        plan,
    )
    assert required_data_frame_covers_fetch_plan_debug(
        _frame(strike=100.0),
        plan,
    )


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        (None, 100.0),
        (100.0, None),
    ],
)
def test_exact_coverage_accepts_exact_strike_with_one_unbounded_side(
    minimum: float | None,
    maximum: float | None,
) -> None:
    request = _request(
        minimum=minimum,
        maximum=maximum,
        base_minimum=minimum,
        base_maximum=maximum,
        min_dte=None,
        max_dte=None,
        exact_strikes_by_expiration={EXPIRATION: [100.0]},
    )

    assert required_data_frame_covers_fetch_plan_debug(
        _frame(strike=100.0),
        _plan(requests=[request]),
    )
    assert required_data_frame_covers_fetch_plan(
        df=_frame(strike=100.0),
        fetch_plan=_typed_plan(
            exact_strike=100.0,
            minimum=minimum,
            maximum=maximum,
        ),
    )


@pytest.mark.parametrize("invalid_exact_strike", [0.0, -1.0])
def test_exact_coverage_rejects_nonpositive_exact_strike(
    invalid_exact_strike: float,
) -> None:
    request = _request(
        minimum=None,
        maximum=None,
        base_minimum=None,
        base_maximum=None,
        min_dte=None,
        max_dte=None,
        exact_strikes_by_expiration={
            EXPIRATION: [invalid_exact_strike]
        },
    )

    assert not required_data_frame_covers_fetch_plan_debug(
        _frame(strike=100.0),
        _plan(requests=[request]),
    )
    assert not required_data_frame_covers_fetch_plan(
        df=_frame(strike=100.0),
        fetch_plan=_typed_plan(
            exact_strike=invalid_exact_strike,
            minimum=None,
            maximum=None,
        ),
    )


def test_exact_coverage_requires_interior_position_strike_independently_of_range() -> None:
    request = _request(
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
        max_dte=60,
        exact_strikes_by_expiration={EXPIRATION: [100.0]},
    )
    rows = pd.DataFrame(
        [
            {
                "option_type": "put",
                "expiration": EXPIRATION,
                "dte": 25,
                "strike": strike,
                "spot": 110.0,
            }
            for strike in (80.0, 120.0)
        ]
    )

    assert not required_data_frame_covers_fetch_plan_debug(
        rows,
        _plan(requests=[request]),
    )


def test_exact_coverage_accepts_complete_provider_strike_grid_without_numeric_edges() -> None:
    request = _request(
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
        max_dte=60,
        exact_strikes_by_expiration={},
    )
    frame = _frame(strike=100.0).assign(contract_symbol="NVDA-C1")
    plan = _plan(requests=[request])
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-C1"]},
    )

    assert not required_data_frame_covers_fetch_plan_debug(frame, plan)
    assert required_data_frame_covers_fetch_plan_debug(
        frame,
        plan,
        option_chain_evidence=evidence,
    )


def test_exact_coverage_keeps_legacy_global_chain_evidence_strict() -> None:
    request = _request(
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
    )
    frame = _frame(strike=100.0).assign(contract_symbol="NVDA-P1")
    legacy_evidence = {
        "status": "ok",
        "source_outcome": "success_rows",
        "expiration_statuses": {EXPIRATION: "fetched"},
        "option_codes": 1,
        "snapshot_complete": True,
        "snapshot_requested_codes": 1,
        "snapshot_requested_code_set": ["NVDA-P1"],
    }

    assert not required_data_frame_covers_fetch_plan_debug(
        frame,
        _plan(requests=[request]),
        option_chain_evidence=legacy_evidence,
    )


def test_exact_coverage_accepts_globally_proven_filtered_empty_plan() -> None:
    request = _request(
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
    )
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): []},
    )

    result = evaluate_required_data_frame_fetch_plan_debug(
        pd.DataFrame(),
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is True
    assert result.status == "success_empty"
    assert result.provider_coverage == "complete"
    assert result.strategy_readiness == "empty"
    assert required_data_frame_covers_fetch_plan_debug(
        pd.DataFrame(),
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )


def test_exact_coverage_accepts_futu_scope_order_independent_of_plan_order() -> None:
    second_expiration = "2026-09-18"
    request = _request(
        option_type="call",
        expirations=[second_expiration, EXPIRATION],
        minimum=100.0,
        maximum=140.0,
        base_minimum=100.0,
        base_maximum=140.0,
        max_dte=60,
    )
    frame = pd.DataFrame(
        [
            {
                "option_type": "call",
                "expiration": expiration,
                "dte": dte,
                "strike": strike,
                "spot": 110.0,
                "contract_symbol": code,
            }
            for expiration, dte, strike, code in (
                (EXPIRATION, 25, 120.0, "NVDA-C-AUG"),
                (second_expiration, 53, 125.0, "NVDA-C-SEP"),
            )
        ]
    )
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={
            ("call", EXPIRATION): ["NVDA-C-AUG"],
            ("call", second_expiration): ["NVDA-C-SEP"],
        },
    )
    scopes = evidence["option_chain_scope_coverage"]["scopes"]
    assert isinstance(scopes, list)
    scopes.reverse()
    evidence["snapshot_requested_code_set"] = ["NVDA-C-SEP", "NVDA-C-AUG"]
    evidence["snapshot_returned_code_set"] = ["NVDA-C-AUG", "NVDA-C-SEP"]

    result = evaluate_required_data_frame_fetch_plan_debug(
        frame,
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is True
    assert result.status == "success"


def test_exact_coverage_classifies_missing_snapshot_as_provider_incomplete() -> None:
    request = _request(minimum=80.0, maximum=120.0)
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P1"]},
    )
    evidence.update(
        {
            "snapshot_returned_codes": 0,
            "snapshot_missing_codes": 1,
            "snapshot_returned_code_set": [],
            "snapshot_missing_code_set": ["NVDA-P1"],
            "snapshot_complete": False,
        }
    )

    result = evaluate_required_data_frame_fetch_plan_debug(
        _frame(strike=100.0).assign(contract_symbol="NVDA-P1"),
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is False
    assert result.reason_code == "provider_incomplete"


def test_exact_coverage_warns_on_quarantined_unexpected_snapshot_code() -> None:
    request = _request(minimum=80.0, maximum=120.0)
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P1"]},
    )
    evidence.update(
        {
            "snapshot_returned_codes": 2,
            "snapshot_unexpected_codes": 1,
            "snapshot_returned_code_set": ["NVDA-FOREIGN", "NVDA-P1"],
            "snapshot_unexpected_code_set": ["NVDA-FOREIGN"],
        }
    )

    result = evaluate_required_data_frame_fetch_plan_debug(
        _frame(strike=100.0).assign(contract_symbol="NVDA-P1"),
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is True
    assert result.warnings == ("unexpected_snapshot_codes:1",)


def test_exact_coverage_classifies_snapshot_count_drift_as_internal_error() -> None:
    request = _request(minimum=80.0, maximum=120.0)
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P1"]},
    )
    evidence["snapshot_requested_codes"] = 2

    result = evaluate_required_data_frame_fetch_plan_debug(
        _frame(strike=100.0).assign(contract_symbol="NVDA-P1"),
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is False
    assert result.reason_code == "internal_contract_error"


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("require_realized_volatility", "false"),
        ("required_exact_strikes_by_expiration", {EXPIRATION: [True]}),
    ],
)
def test_structured_coverage_classifies_malformed_plan_as_internal_error(
    field: str,
    invalid: object,
) -> None:
    request = _request(minimum=80.0, maximum=120.0)
    plan = _plan(requests=[request])
    if field == "require_realized_volatility":
        plan[field] = invalid
    else:
        request["side_plans"][0][field] = invalid
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P1"]},
    )

    result = evaluate_required_data_frame_fetch_plan_debug(
        _frame(strike=100.0).assign(contract_symbol="NVDA-P1"),
        plan,
        option_chain_evidence=evidence,
    )

    assert result.accepted is False
    assert result.reason_code == "internal_contract_error"


def test_exact_coverage_accepts_proven_partial_empty_expiration() -> None:
    second_expiration = "2026-09-18"
    request = _request(
        expirations=[EXPIRATION, second_expiration],
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
        max_dte=60,
    )
    frame = _frame(strike=100.0).assign(contract_symbol="NVDA-P1")
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={
            ("put", EXPIRATION): ["NVDA-P1"],
            ("put", second_expiration): [],
        },
    )

    assert required_data_frame_covers_fetch_plan_debug(
        frame, _plan(requests=[request]), option_chain_evidence=evidence
    )


def test_exact_coverage_accepts_proven_empty_child_when_another_child_has_rows() -> None:
    put_request = _request(
        option_type="put",
        minimum=80.0,
        maximum=100.0,
        base_minimum=80.0,
        base_maximum=100.0,
    )
    call_request = _request(
        option_type="call",
        minimum=120.0,
        maximum=140.0,
        base_minimum=120.0,
        base_maximum=140.0,
    )
    frame = _frame(strike=90.0).assign(contract_symbol="NVDA-P90")
    put_evidence = _scope_evidence(
        request=put_request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P90"]},
    )
    call_evidence = _scope_evidence(
        request=call_request,
        codes_by_scope={("call", EXPIRATION): []},
    )
    for index, (request, child) in enumerate(
        ((put_request, put_evidence), (call_request, call_evidence))
    ):
        child["request_index"] = index
        child["planned_request_sha256"] = required_data_request_sha256(request)
    evidence = deepcopy(put_evidence)
    # OpenD/merge output ordering is not semantic identity. The preserved
    # request_index values remain valid compatibility evidence.
    evidence["requests"] = [call_evidence, put_evidence]
    evidence["request_count"] = 2

    assert required_data_frame_covers_fetch_plan_debug(
        frame,
        _plan(requests=[put_request, call_request]),
        option_chain_evidence=evidence,
    )

    call_evidence["request_index"] = 0
    put_evidence["request_index"] = 1
    result = evaluate_required_data_frame_fetch_plan_debug(
        frame,
        _plan(requests=[put_request, call_request]),
        option_chain_evidence=evidence,
    )
    assert result.accepted is False
    assert result.reason_code == "internal_contract_error"


def test_exact_coverage_scopes_same_side_multi_request_rows_by_proven_codes() -> None:
    lower_request = _request(
        minimum=80.0,
        maximum=100.0,
        base_minimum=80.0,
        base_maximum=100.0,
    )
    upper_request = _request(
        minimum=110.0,
        maximum=130.0,
        base_minimum=110.0,
        base_maximum=130.0,
    )
    frame = pd.DataFrame(
        [
            {
                "option_type": "put",
                "expiration": EXPIRATION,
                "dte": 25,
                "strike": strike,
                "spot": 110.0,
                "contract_symbol": code,
            }
            for strike, code in ((90.0, "NVDA-P90"), (120.0, "NVDA-P120"))
        ]
    )
    lower_evidence = _scope_evidence(
        request=lower_request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P90"]},
    )
    upper_evidence = _scope_evidence(
        request=upper_request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P120"]},
    )
    for index, (request, child) in enumerate(
        ((lower_request, lower_evidence), (upper_request, upper_evidence))
    ):
        child["request_index"] = index
        child["planned_request_sha256"] = required_data_request_sha256(request)
    evidence = deepcopy(lower_evidence)
    all_codes = ["NVDA-P120", "NVDA-P90"]
    evidence.update(
        {
            "option_codes": 2,
            "snapshot_requested_codes": 2,
            "snapshot_returned_codes": 2,
            "snapshot_requested_code_set": all_codes,
            "snapshot_returned_code_set": all_codes,
            "request_count": 2,
            "requests": [lower_evidence, upper_evidence],
        }
    )

    assert required_data_frame_covers_fetch_plan_debug(
        frame,
        _plan(requests=[lower_request, upper_request]),
        option_chain_evidence=evidence,
    )


def test_exact_coverage_rejects_empty_scope_with_required_exact_strike() -> None:
    second_expiration = "2026-09-18"
    request = _request(
        expirations=[EXPIRATION, second_expiration],
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
        max_dte=60,
        exact_strikes_by_expiration={second_expiration: [100.0]},
    )
    frame = _frame(strike=100.0).assign(contract_symbol="NVDA-P1")
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={
            ("put", EXPIRATION): ["NVDA-P1"],
            ("put", second_expiration): [],
        },
    )

    result = evaluate_required_data_frame_fetch_plan_debug(
        frame,
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is False
    assert result.reason_code == "required_contract_missing"


@pytest.mark.parametrize("status", ["empty", "stale_cache", "error", ""])
def test_exact_coverage_rejects_unreliable_empty_scope_status(status: str) -> None:
    second_expiration = "2026-09-18"
    request = _request(
        expirations=[EXPIRATION, second_expiration],
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
        max_dte=60,
    )
    frame = _frame(strike=100.0).assign(contract_symbol="NVDA-P1")
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={
            ("put", EXPIRATION): ["NVDA-P1"],
            ("put", second_expiration): [],
        },
        statuses={second_expiration: status},
    )

    assert not required_data_frame_covers_fetch_plan_debug(
        frame, _plan(requests=[request]), option_chain_evidence=evidence
    )


def test_exact_coverage_classifies_stale_scope_as_stale_data() -> None:
    request = _request(
        minimum=80.0,
        maximum=120.0,
        base_minimum=80.0,
        base_maximum=120.0,
    )
    frame = _frame(strike=100.0).assign(contract_symbol="NVDA-P1")
    evidence = _scope_evidence(
        request=request,
        codes_by_scope={("put", EXPIRATION): ["NVDA-P1"]},
        statuses={EXPIRATION: "stale_cache"},
    )

    result = evaluate_required_data_frame_fetch_plan_debug(
        frame,
        _plan(requests=[request]),
        option_chain_evidence=evidence,
    )

    assert result.accepted is False
    assert result.reason_code == "stale_data"


def test_exact_coverage_is_expiration_local() -> None:
    second_expiration = "2026-09-18"
    request = _request(
        expirations=[EXPIRATION, second_expiration],
        minimum=1.0,
        maximum=None,
        base_minimum=1.0,
        base_maximum=None,
        exact_strikes_by_expiration={
            EXPIRATION: [100.0],
            second_expiration: [105.0],
        },
    )
    rows = pd.DataFrame(
        [
            {
                "option_type": "put",
                "expiration": expiration,
                "dte": dte,
                "strike": 100.0,
                "spot": 110.0,
            }
            for expiration, dte in (
                (EXPIRATION, 25),
                (second_expiration, 53),
            )
        ]
    )

    assert not required_data_frame_covers_fetch_plan_debug(
        rows,
        _plan(requests=[request]),
    )


@pytest.mark.parametrize(
    ("expected_strike", "actual_strike", "covered"),
    [
        (100.0, 100.0 + 0.5e-9, True),
        (100.0, 100.0 + 2.0e-9, False),
        (1_000_000.0, 1_000_000.0 + 0.5e-9, True),
        (1_000_000.0, 1_000_000.0 + 2.0e-9, False),
    ],
)
def test_typed_and_debug_exact_coverage_share_fixed_absolute_tolerance(
    expected_strike: float,
    actual_strike: float,
    covered: bool,
) -> None:
    frame = _frame(strike=actual_strike)
    debug_request = _request(
        minimum=1.0,
        maximum=None,
        base_minimum=1.0,
        base_maximum=None,
        min_dte=None,
        max_dte=None,
        exact_strikes_by_expiration={EXPIRATION: [expected_strike]},
    )

    assert (
        required_data_frame_covers_fetch_plan(
            df=frame,
            fetch_plan=_typed_plan(exact_strike=expected_strike),
        )
        is covered
    )
    assert (
        required_data_frame_covers_fetch_plan_debug(
            frame,
            _plan(requests=[debug_request]),
        )
        is covered
    )


def test_debug_coverage_rejects_missing_exact_strike_contract_field() -> None:
    plan = deepcopy(_plan())
    request = plan["merged_requests"][0]
    assert isinstance(request, dict)
    del request["side_plans"][0][
        "required_exact_strikes_by_expiration"
    ]

    assert not required_data_frame_covers_fetch_plan_debug(_frame(), plan)


def test_exact_coverage_recomputes_dte_from_discovery_trading_date() -> None:
    plan = _plan()

    assert required_data_frame_covers_fetch_plan_debug(_frame(dte=25), plan)
    wrong_dte_frame = _frame(dte=999)
    assert not required_data_frame_covers_fetch_plan(
        df=wrong_dte_frame,
        fetch_plan=_typed_plan(exact_strike=100.0),
    )
    assert not required_data_frame_covers_fetch_plan_debug(
        wrong_dte_frame,
        plan,
    )

    request_outside_declared_range = _request(min_dte=26, max_dte=30)
    assert not required_data_frame_covers_fetch_plan_debug(
        _frame(dte=25),
        _plan(requests=[request_outside_declared_range]),
    )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("spot_reference", True),
        ("spot_reference", float("nan")),
        ("spot_reference", float("inf")),
        ("min_strike", True),
        ("min_strike", "bad"),
        ("min_strike", float("nan")),
        ("max_strike", float("inf")),
        ("min_dte", True),
        ("max_dte", 20.5),
    ],
)
def test_exact_coverage_rejects_invalid_numeric_declarations(
    field: str,
    invalid: object,
) -> None:
    plan = deepcopy(_plan())
    if field == "spot_reference":
        plan[field] = invalid
    else:
        request = plan["merged_requests"][0]
        assert isinstance(request, dict)
        if field in {"min_strike", "max_strike"}:
            window = request["side_strike_windows"]["put"]
            side_window = request["side_plans"][0]["strike_window"]
            window[field] = invalid
            side_window[field] = invalid
        else:
            request[field] = invalid
            request["side_plans"][0][field] = invalid

    assert not required_data_frame_covers_fetch_plan_debug(_frame(), plan)


def test_exact_coverage_rejects_reversed_strike_and_dte_ranges() -> None:
    for request in (
        _request(minimum=110.0, maximum=100.0),
        _request(min_dte=30, max_dte=20),
    ):
        assert not required_data_frame_covers_fetch_plan_debug(
            _frame(),
            _plan(requests=[request]),
        )


@pytest.mark.parametrize(
    ("column", "invalid"),
    [
        ("spot", True),
        ("spot", float("nan")),
        ("spot", float("inf")),
        ("spot", 0.0),
        ("strike", True),
        ("strike", float("nan")),
        ("strike", float("inf")),
        ("strike", 0.0),
        ("dte", True),
        ("dte", float("inf")),
        ("dte", 25.5),
    ],
)
def test_exact_coverage_rejects_invalid_row_values(
    column: str,
    invalid: object,
) -> None:
    kwargs = {column: invalid}
    frame = _frame(**kwargs)
    assert not required_data_frame_covers_fetch_plan_debug(
        frame,
        _plan(),
    )
    if column == "dte":
        assert not required_data_frame_covers_fetch_plan(
            df=frame,
            fetch_plan=_typed_plan(exact_strike=100.0),
        )


def test_typed_and_debug_coverage_reject_missing_dte_column() -> None:
    frame = _frame().drop(columns=["dte"])

    assert not required_data_frame_covers_fetch_plan(
        df=frame,
        fetch_plan=_typed_plan(exact_strike=100.0),
    )
    assert not required_data_frame_covers_fetch_plan_debug(frame, _plan())


@pytest.mark.parametrize("invalid_rv", [True, False, float("nan"), float("inf"), 0.0, -0.1])
def test_exact_coverage_rejects_nonfinite_nonpositive_or_bool_rv(
    invalid_rv: object,
) -> None:
    plan = _plan(requests=[_request(require_rv=True)])

    assert not required_data_frame_covers_fetch_plan_debug(
        _frame(rv=invalid_rv),
        plan,
    )


def test_exact_coverage_accepts_required_finite_positive_rv() -> None:
    plan = _plan(requests=[_request(require_rv=True)])

    assert required_data_frame_covers_fetch_plan_debug(
        _frame(rv=0.24),
        plan,
    )
