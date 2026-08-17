from __future__ import annotations

from copy import deepcopy
import math

import pytest

from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
    required_data_plan_id,
)


HOST = "127.0.0.1"
PORT = 11111
TRADING_DATE = "2026-07-27"


def _side_plan(
    option_type: str,
    *,
    expirations: list[str],
    min_dte: int,
    max_dte: int,
    min_strike: float | None,
    max_strike: float | None,
) -> dict[str, object]:
    return {
        "option_type": option_type,
        "min_dte": min_dte,
        "max_dte": max_dte,
        "explicit_expirations": list(expirations),
        "strike_window": {
            "min_strike": min_strike,
            "max_strike": max_strike,
            "source": f"fixture.{option_type}",
            "buffer_applied": False,
            "buffer_pct": 0.0,
            "base_min_strike": min_strike,
            "base_max_strike": max_strike,
        },
        "planning_reason": f"fixture {option_type}",
        "source_fields": [f"{option_type}.min_dte", f"{option_type}.max_dte"],
        "spot_reference": 110.0,
        "min_strike": min_strike,
        "max_strike": max_strike,
        "expiration_count": len(expirations),
        "required_exact_strikes_by_expiration": {},
    }


def _request(
    side_plan: dict[str, object],
    *,
    require_rv: object = True,
) -> dict[str, object]:
    option_type = str(side_plan["option_type"])
    strike_window = side_plan["strike_window"]
    assert isinstance(strike_window, dict)
    return {
        "symbol": "NVDA",
        "limit_expirations": 0,
        "host": HOST,
        "port": PORT,
        "option_types": [option_type],
        "explicit_expirations": list(side_plan["explicit_expirations"]),
        "min_dte": side_plan["min_dte"],
        "max_dte": side_plan["max_dte"],
        "side_strike_windows": {
            option_type: {
                "min_strike": strike_window["min_strike"],
                "max_strike": strike_window["max_strike"],
            }
        },
        "include_realized_volatility": require_rv,
        "side_plans": [deepcopy(side_plan)],
        "planning_reason": "single-side request",
        "trading_date": TRADING_DATE,
    }


def _success_rows_plan() -> dict[str, object]:
    # Keep this fixture fully active so request-index mutation tests remain
    # focused on the field they intend to contradict.
    put = _side_plan(
        "put",
        expirations=["2026-08-21"],
        min_dte=20,
        max_dte=30,
        min_strike=90.0,
        max_strike=100.0,
    )
    call = _side_plan(
        "call",
        expirations=["2026-09-18"],
        min_dte=30,
        max_dte=60,
        min_strike=120.0,
        max_strike=140.0,
    )
    return {
        "symbol": "NVDA",
        "spot_reference": 110.0,
        "side_plans": [put, call],
        "merged_requests": [_request(put), _request(call)],
        "require_realized_volatility": True,
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_rows",
            "reason_code": None,
            "expirations": ["2026-08-21", "2026-09-18"],
            "observed_at_utc": "2026-07-27T10:00:00+00:00",
            "completed_at_utc": "2026-07-27T10:00:01+00:00",
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": HOST,
                "port": PORT,
                "trading_date": TRADING_DATE,
            },
            "error": None,
        },
        "projection_outcome": "success_rows",
        "projected_expirations": ["2026-08-21", "2026-09-18"],
    }


def _success_rows_plan_with_empty_top_side() -> dict[str, object]:
    plan = _success_rows_plan()
    side_plans = plan["side_plans"]
    requests = plan["merged_requests"]
    assert isinstance(side_plans, list) and isinstance(side_plans[0], dict)
    assert isinstance(requests, list)
    empty_put = side_plans[0]
    empty_put["explicit_expirations"] = []
    empty_put["expiration_count"] = 0
    plan["merged_requests"] = [requests[1]]
    plan["projected_expirations"] = ["2026-09-18"]
    return plan


def _success_empty_plan() -> dict[str, object]:
    empty_put = _side_plan(
        "put",
        expirations=[],
        min_dte=20,
        max_dte=30,
        min_strike=90.0,
        max_strike=100.0,
    )
    return {
        "symbol": "NVDA",
        "spot_reference": 110.0,
        "side_plans": [empty_put],
        "merged_requests": [],
        "require_realized_volatility": True,
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_empty",
            "reason_code": "no_expirations",
            "expirations": [],
            "observed_at_utc": "2026-07-27T10:00:00+00:00",
            "completed_at_utc": "2026-07-27T10:00:01+00:00",
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": HOST,
                "port": PORT,
                "trading_date": TRADING_DATE,
            },
            "error": None,
        },
        "projection_outcome": "success_empty",
        "projected_expirations": [],
    }


def _build(plan: dict[str, object], *, port: object = PORT) -> dict[str, object]:
    return build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=plan,
        source="opend",
        host=HOST,
        port=port,  # type: ignore[arg-type]
    )


def test_expected_contract_accepts_empty_top_evidence_without_executable_child() -> None:
    plan = _success_rows_plan_with_empty_top_side()

    contract = _build(plan)

    assert contract["fetch_plan"] == plan
    assert contract["coverage_policy"]["require_realized_volatility"] is True
    assert contract["coverage_policy"] == {
        "schema_version": "required_data_coverage_policy.v2",
        "projection_outcome": "success_rows",
        "require_realized_volatility": True,
        "coverage_evaluator": "required_data_frame_covers_fetch_plan.v2",
        "completion_unit": "request_option_type_expiration",
        "allow_proven_empty_scopes": True,
    }


def test_expected_contract_accepts_closed_success_empty_projection() -> None:
    contract = _build(_success_empty_plan())

    assert contract["coverage_policy"]["projection_outcome"] == "success_empty"


@pytest.mark.parametrize("value", [None, "true", 1])
def test_expected_contract_rejects_non_bool_plan_rv_authority(
    value: object,
) -> None:
    plan = _success_rows_plan()
    plan["require_realized_volatility"] = value

    with pytest.raises(ValueError, match="plan RV authority"):
        _build(plan)


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_expected_contract_rejects_request_rv_drift_or_non_bool(
    value: object,
) -> None:
    plan = _success_rows_plan()
    requests = plan["merged_requests"]
    assert isinstance(requests, list) and isinstance(requests[1], dict)
    requests[1]["include_realized_volatility"] = value

    with pytest.raises(ValueError, match="request RV flag"):
        _build(plan)


def test_expected_contract_rejects_dte_range_excluding_explicit_expiration() -> None:
    plan = _success_rows_plan()
    top, nested = _active_side_pair(plan)
    requests = plan["merged_requests"]
    assert isinstance(requests, list) and isinstance(requests[1], dict)
    for item in (top, nested, requests[1]):
        item["min_dte"] = 1
        item["max_dte"] = 2

    with pytest.raises(
        ValueError,
        match="DTE range does not cover explicit expirations",
    ):
        _build(plan)


def _active_side_pair(
    plan: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    side_plans = plan["side_plans"]
    requests = plan["merged_requests"]
    assert isinstance(side_plans, list) and isinstance(side_plans[1], dict)
    assert isinstance(requests, list) and isinstance(requests[1], dict)
    nested_plans = requests[1]["side_plans"]
    assert isinstance(nested_plans, list) and isinstance(nested_plans[0], dict)
    return side_plans[1], nested_plans[0]


def test_expected_contract_accepts_expiry_local_exact_strike_identity() -> None:
    plan = _success_rows_plan()
    top, nested = _active_side_pair(plan)
    exact = {"2026-09-18": [120.0, 130.0, 140.0]}
    top["required_exact_strikes_by_expiration"] = deepcopy(exact)
    nested["required_exact_strikes_by_expiration"] = deepcopy(exact)

    contract = _build(plan)

    assert contract["fetch_plan"] == plan


@pytest.mark.parametrize(
    ("minimum", "maximum", "exact_strike"),
    [
        (None, 140.0, 130.0),
        (120.0, None, 130.0),
    ],
)
def test_expected_contract_accepts_exact_strike_with_one_unbounded_side(
    minimum: float | None,
    maximum: float | None,
    exact_strike: float,
) -> None:
    plan = _success_rows_plan()
    top, nested = _active_side_pair(plan)
    requests = plan["merged_requests"]
    assert isinstance(requests, list) and isinstance(requests[1], dict)
    request_windows = requests[1]["side_strike_windows"]
    assert isinstance(request_windows, dict)
    request_window = request_windows["call"]
    assert isinstance(request_window, dict)

    for side_plan in (top, nested):
        window = side_plan["strike_window"]
        assert isinstance(window, dict)
        window["min_strike"] = minimum
        window["max_strike"] = maximum
        window["base_min_strike"] = minimum
        window["base_max_strike"] = maximum
        side_plan["min_strike"] = minimum
        side_plan["max_strike"] = maximum
        side_plan["required_exact_strikes_by_expiration"] = {
            "2026-09-18": [exact_strike]
        }
    request_window["min_strike"] = minimum
    request_window["max_strike"] = maximum

    contract = _build(plan)

    assert contract["fetch_plan"] == plan


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"2026-09-19": [130.0]},
        {"2026-09-18": []},
        {"2026-09-18": [True]},
        {"2026-09-18": [math.nan]},
        {"2026-09-18": [0.0]},
        {"2026-09-18": [-1.0]},
        {"2026-09-18": [130.0, 130.0]},
        {"2026-09-18": [140.0, 130.0]},
        {"2026-09-18": [141.0]},
    ],
)
def test_expected_contract_rejects_malformed_exact_strike_identity(
    value: object,
) -> None:
    plan = _success_rows_plan()
    top, nested = _active_side_pair(plan)
    if value is None:
        top.pop("required_exact_strikes_by_expiration")
        nested.pop("required_exact_strikes_by_expiration")
    else:
        top["required_exact_strikes_by_expiration"] = deepcopy(value)
        nested["required_exact_strikes_by_expiration"] = deepcopy(value)

    with pytest.raises(ValueError, match="exact strike|JSON compliant"):
        _build(plan)


def test_expected_contract_rejects_top_nested_exact_strike_drift() -> None:
    plan = _success_rows_plan()
    top, _nested = _active_side_pair(plan)
    top["required_exact_strikes_by_expiration"] = {
        "2026-09-18": [130.0]
    }

    with pytest.raises(ValueError, match="nested and top-level"):
        _build(plan)


def test_expected_contract_rejects_wrong_discovery_underlier() -> None:
    plan = _success_rows_plan()
    discovery = plan["expiration_discovery"]
    assert isinstance(discovery, dict)
    identity = discovery["request_identity"]
    assert isinstance(identity, dict)
    identity["underlier"] = "US.AAPL"

    with pytest.raises(ValueError, match="identity mismatch"):
        _build(plan)


@pytest.mark.parametrize(
    "case",
    [
        "request_expiration",
        "request_option_type",
        "request_strike_window",
        "request_dte",
        "nested_side_plan",
        "projected_expirations",
        "discovery_expirations",
    ],
)
def test_expected_contract_rejects_contradictory_plan_projections(case: str) -> None:
    plan = _success_rows_plan()
    requests = plan["merged_requests"]
    assert isinstance(requests, list)
    active_request = requests[1]
    assert isinstance(active_request, dict)
    if case == "request_expiration":
        active_request["explicit_expirations"] = ["2026-08-21"]
    elif case == "request_option_type":
        active_request["option_types"] = ["put"]
    elif case == "request_strike_window":
        active_request["side_strike_windows"] = {
            "call": {"min_strike": 121.0, "max_strike": 140.0}
        }
    elif case == "request_dte":
        active_request["min_dte"] = 31
    elif case == "nested_side_plan":
        nested = active_request["side_plans"]
        assert isinstance(nested, list) and isinstance(nested[0], dict)
        nested[0]["planning_reason"] = "forged nested demand"
    elif case == "projected_expirations":
        plan["projected_expirations"] = ["2026-08-21"]
    else:
        discovery = plan["expiration_discovery"]
        assert isinstance(discovery, dict)
        discovery["expirations"] = ["2026-08-21"]

    with pytest.raises(ValueError):
        _build(plan)


@pytest.mark.parametrize("container", ["side_plans", "merged_requests", "targets"])
def test_success_empty_rejects_any_fetch_demand(container: str) -> None:
    plan = _success_empty_plan()
    active = _success_rows_plan()
    if container == "side_plans":
        active_side = deepcopy(active["side_plans"])[1]
        plan["side_plans"] = [active_side]
        active_request = deepcopy(active["merged_requests"])[1]
        plan["merged_requests"] = [active_request]
    elif container == "merged_requests":
        active_request = deepcopy(active["merged_requests"])[1]
        plan["merged_requests"] = [active_request]
    else:
        plan["projected_expirations"] = ["2026-09-18"]

    with pytest.raises(ValueError):
        _build(plan)


def test_success_rows_rejects_empty_executable_child() -> None:
    plan = _success_rows_plan()
    side_plans = plan["side_plans"]
    requests = plan["merged_requests"]
    assert isinstance(side_plans, list) and isinstance(requests, list)
    active_side = side_plans[1]
    active_request = requests[1]
    assert isinstance(active_side, dict) and isinstance(active_request, dict)
    active_side["explicit_expirations"] = []
    active_side["expiration_count"] = 0
    active_request["explicit_expirations"] = []
    nested = active_request["side_plans"]
    assert isinstance(nested, list) and isinstance(nested[0], dict)
    nested[0]["explicit_expirations"] = []
    nested[0]["expiration_count"] = 0
    plan["projected_expirations"] = ["2026-08-21"]

    with pytest.raises(
        ValueError,
        match="executable request lacks expiration targets",
    ):
        _build(plan)


def test_expected_contract_rejects_fetch_window_that_excludes_base_window() -> None:
    plan = _success_rows_plan()
    side_plans = plan["side_plans"]
    requests = plan["merged_requests"]
    assert isinstance(side_plans, list) and isinstance(side_plans[1], dict)
    assert isinstance(requests, list) and isinstance(requests[1], dict)
    top_window = side_plans[1]["strike_window"]
    nested_plans = requests[1]["side_plans"]
    assert isinstance(top_window, dict)
    assert isinstance(nested_plans, list) and isinstance(nested_plans[0], dict)
    nested_window = nested_plans[0]["strike_window"]
    assert isinstance(nested_window, dict)
    for window in (top_window, nested_window):
        window["base_min_strike"] = 100.0
        window["base_max_strike"] = 110.0

    with pytest.raises(ValueError, match="does not cover base window"):
        _build(plan)


@pytest.mark.parametrize(
    ("fetch_min", "fetch_max", "base_min", "base_max"),
    [
        (None, 100.0, 120.0, None),
        (120.0, None, None, 100.0),
    ],
)
def test_expected_contract_rejects_cross_source_effective_strike_inversion(
    fetch_min: float | None,
    fetch_max: float | None,
    base_min: float | None,
    base_max: float | None,
) -> None:
    plan = _success_rows_plan()
    top, nested = _active_side_pair(plan)
    requests = plan["merged_requests"]
    assert isinstance(requests, list) and isinstance(requests[1], dict)
    request_windows = requests[1]["side_strike_windows"]
    assert isinstance(request_windows, dict)
    request_window = request_windows["call"]
    assert isinstance(request_window, dict)

    for side_plan in (top, nested):
        window = side_plan["strike_window"]
        assert isinstance(window, dict)
        window["min_strike"] = fetch_min
        window["max_strike"] = fetch_max
        window["base_min_strike"] = base_min
        window["base_max_strike"] = base_max
        side_plan["min_strike"] = fetch_min
        side_plan["max_strike"] = fetch_max
    request_window["min_strike"] = fetch_min
    request_window["max_strike"] = fetch_max

    with pytest.raises(ValueError, match="effective strike window is inverted"):
        _build(plan)


@pytest.mark.parametrize(
    ("case", "value"),
    [
        ("spot", True),
        ("spot", math.nan),
        ("spot", math.inf),
        ("spot", 0),
        ("strike", True),
        ("strike", math.nan),
        ("strike", math.inf),
        ("dte", True),
        ("port", True),
        ("port", 0),
        ("port", 65536),
    ],
)
def test_expected_contract_rejects_invalid_numeric_authority(
    case: str,
    value: object,
) -> None:
    plan = _success_rows_plan()
    if case == "spot":
        plan["spot_reference"] = value
    elif case == "strike":
        side_plans = plan["side_plans"]
        assert isinstance(side_plans, list) and isinstance(side_plans[1], dict)
        window = side_plans[1]["strike_window"]
        assert isinstance(window, dict)
        window["min_strike"] = value
    elif case == "dte":
        side_plans = plan["side_plans"]
        assert isinstance(side_plans, list) and isinstance(side_plans[1], dict)
        side_plans[1]["min_dte"] = value
    else:
        with pytest.raises(ValueError):
            _build(plan, port=value)
        return

    with pytest.raises(ValueError):
        _build(plan)


@pytest.mark.parametrize("case", ["spot", "strike", "dte"])
def test_expected_contract_rejects_inverted_ranges(case: str) -> None:
    plan = _success_rows_plan()
    side_plans = plan["side_plans"]
    assert isinstance(side_plans, list) and isinstance(side_plans[1], dict)
    side = side_plans[1]
    if case == "spot":
        plan["spot_reference"] = -1
    elif case == "strike":
        window = side["strike_window"]
        assert isinstance(window, dict)
        window["min_strike"] = 150.0
        window["max_strike"] = 140.0
    else:
        side["min_dte"] = 61
        side["max_dte"] = 60

    with pytest.raises(ValueError):
        _build(plan)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trading_date", "2026-02-30"),
        ("expiration", "2026-09-31"),
        ("expiration", "2026-9-18"),
    ],
)
def test_expected_contract_rejects_invalid_iso_dates(
    field: str,
    value: str,
) -> None:
    plan = _success_rows_plan()
    discovery = plan["expiration_discovery"]
    assert isinstance(discovery, dict)
    if field == "trading_date":
        identity = discovery["request_identity"]
        assert isinstance(identity, dict)
        identity["trading_date"] = value
    else:
        discovery["expirations"] = ["2026-08-21", value]

    with pytest.raises(ValueError):
        _build(plan)


def test_plan_identity_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        required_data_plan_id([{"symbol": "NVDA", "spot": math.nan}])


def test_expected_contract_tolerates_expiration_order_between_projection_and_side_plans(
    monkeypatch,
) -> None:
    """projected_expirations may be sorted while side_plans keep OpenD order.

    required_data_prefetch rebuilds projected_expirations with sorted({...})
    while side_plans use order-preserving de-duplication. The same set of
    expirations must not fail the fail-closed validation merely on order.
    """

    plan = _success_rows_plan()
    # put=08-21, call=09-18 -> natural side-plan order is ascending; make the
    # projected list descending to reproduce the production mismatch.
    plan["projected_expirations"] = ["2026-09-18", "2026-08-21"]

    contract = _build(plan)

    assert contract["fetch_plan"] == plan
