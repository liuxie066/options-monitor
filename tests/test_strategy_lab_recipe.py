from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.performance.models import FXRateFact
from domain.domain.symbol_identity import OPTION_CODE_RE
from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle
from src.application.strategy_lab.contracts import (
    RECIPE_ID,
    build_strategy_lab_timer_binding,
    canonical_sha256,
)
from src.application.strategy_lab.recipe import (
    _history_k_authority,
    _terminal_fx_bindings,
    build_concentration_arms,
    build_validation_plan,
    project_validation_arms,
    select_research_window,
)
from src.application.strategy_lab.service import preview_experiment
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def _candidate(candidate_id: str, symbol: str, contract: str, period_return: float) -> dict[str, object]:
    match = OPTION_CODE_RE.fullmatch(contract)
    assert match is not None
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "contract_symbol": contract,
        "option_type": "put",
        "expiration": "2026-08-28",
        "strike": int(match.group("strike")) / 1000,
        "currency": "HKD",
        "sell_limit": 1.0,
        "multiplier": 100,
        "period_net_return_on_cash_basis": period_return,
        "net_assignment_discount_pct": 0.01,
        "spread_ratio": 0.1,
        "open_interest": 100,
        "net_income": 100,
    }


def _formal_point() -> dict[str, object]:
    return {
        "recommendation_point_id": "p" * 64,
        "content_sha256": "f" * 64,
        "captured_at_utc": "2026-08-26T01:42:15Z",
        "source_binding": {
            "scheduled_scan_target_market": "2026-08-26T01:40:00Z"
        },
        "recommendation_point": {
            "opening_snapshot_sha256": "o" * 64,
            "scheduled_scan_target_market": "2026-08-26T01:40:00Z",
            "decision_at_utc": "2026-08-26T01:41:00Z",
            "formal_point_time_coherence": {
                "maximum_observed_at_utc": "2026-08-26T01:42:00Z"
            },
            "producer_accepted_candidate_ids": ["baseline", "lower-concentration", "other"],
            "prepared_context_manifest_ref": "output_runs/run/accounts/lx/prepared.json",
            "prepared_context_manifest_sha256": "m" * 64,
            "prepared_context_payload_sha256": "n" * 64,
            "source_commit_sha": "c" * 40,
        },
        "opening_snapshot": {
            "content_sha256": "o" * 64,
            "sealed_at_utc": "2026-08-26T01:41:30Z",
        },
        "option_position_evidence_binding": {
            "open_option_positions": [],
            "valuation_mark_facts": [],
            "fx_rate_facts": [
                {
                    "fact_id": "fx-opening-hkd-cny",
                    "base_currency": "HKD",
                    "quote_currency": "CNY",
                    "rate": "0.92",
                    "rate_kind": "spot",
                    "effective_at_ms": 1_777_000_000_000,
                    "observed_at_ms": 1_777_000_000_001,
                    "source": "prepared_option_positions_context",
                    "source_id": "fx-source",
                    "revision": 1,
                    "supersedes_fact_id": None,
                    "source_fact_sha256": "x" * 64,
                }
            ],
        },
    }


def test_arms_use_explicit_sealed_rank_one_and_all_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    rows = {
        "baseline": _candidate("baseline", "0700.HK", "HK.TCH260828P300000", 0.0200),
        "lower-concentration": _candidate("lower-concentration", "9992.HK", "HK.POP260828P145000", 0.0190),
        "other": _candidate("other", "3690.HK", "HK.MET260828P90000", 0.0100),
    }
    # List order is deliberately unrelated to the sealed rank.
    monkeypatch.setattr(
        recipe,
        "ranked_opening_candidate_decisions",
        lambda _opening: [
            {
                "candidate_id": candidate_id,
                "strategy_mode": "put",
                "opening_snapshot_rank": rank,
                "normalized_input": rows[candidate_id],
                "opening_decision": {"accepted": True},
            }
            for candidate_id, rank in (("other", 3), ("baseline", 1), ("lower-concentration", 2))
        ],
    )
    concentrations = {"baseline": 0.80, "lower-concentration": 0.20, "other": 0.10}
    monkeypatch.setattr(
        recipe,
        "calculate_option_market_concentration_after",
        lambda *, candidate, **_kwargs: {
            "option_market_concentration_after": concentrations[candidate["candidate_id"]],
            "option_market_value_cny": 100.0,
            "metric_version": "option_market_concentration.v1",
            "evidence_refs": {
                "position_lot_ids": [],
                "valuation_mark_fact_ids": [],
                "fx_rate_fact_ids": [],
            },
        },
    )

    result = build_concentration_arms(_formal_point())

    assert result["arms"][0]["candidate_id"] == "baseline"
    assert result["recommendation_available_at_utc"] == "2026-08-26T01:42:15Z"
    assert [arm["near_return_threshold"] for arm in result["arms"][1:]] == [
        0.002,
        0.004,
        0.006,
    ]
    assert all(arm["candidate_id"] == "lower-concentration" for arm in result["arms"][1:])
    assert result["accepted_candidate_ids"] == ["baseline", "lower-concentration", "other"]
    validation = project_validation_arms(
        _formal_point(),
        {
            "variant_id": "challenger_0.004",
            "near_return_threshold": 0.004,
            "comparison_sha256": "a" * 64,
        },
    )
    assert [arm["arm_id"] for arm in validation["arms"]] == [
        "baseline",
        "challenger_0.004",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expiration", "2026-09-25"),
        ("currency", "USD"),
        ("symbol", "9992.HK"),
        ("contract_symbol", "HK.TCH260828C300000"),
    ],
)
def test_recipe_rejects_mismatched_hk_put_contract_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    rows = {
        "baseline": _candidate("baseline", "0700.HK", "HK.TCH260828P300000", 0.0200),
        "lower-concentration": _candidate(
            "lower-concentration",
            "9992.HK",
            "HK.POP260828P145000",
            0.0190,
        ),
        "other": _candidate("other", "3690.HK", "HK.MET260828P90000", 0.0100),
    }
    rows["baseline"][field] = value
    seal_opening_candidate_fixture(
        tmp_path,
        run_id="identity-mismatch",
        market="HK",
        accepted_rows=rows.values(),
    )
    opening = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id="identity-mismatch",
        account="lx",
    )["owners"]["opening"]
    decisions = recipe.ranked_opening_candidate_decisions(opening)
    formal_point = _formal_point()
    recommendation = formal_point["recommendation_point"]
    assert isinstance(recommendation, dict)
    recommendation["opening_snapshot_sha256"] = opening["content_sha256"]
    recommendation["producer_accepted_candidate_ids"] = [
        item["candidate_id"] for item in decisions
    ]
    formal_point["opening_snapshot"] = opening
    monkeypatch.setattr(
        recipe,
        "calculate_option_market_concentration_after",
        lambda **_kwargs: {
            "option_market_concentration_after": 0.5,
            "option_market_value_cny": 100.0,
            "metric_version": "option_market_concentration.v1",
            "evidence_refs": {
                "position_lot_ids": [],
                "valuation_mark_fact_ids": [],
                "fx_rate_fact_ids": [],
            },
        },
    )

    with pytest.raises(recipe.StrategyLabRecipeError) as raised:
        build_concentration_arms(formal_point)

    assert raised.value.reason_code == "recipe_evidence_incomplete"


def _trading_dates(count: int) -> list[str]:
    out: list[str] = []
    current = date(2026, 7, 1)
    while len(out) < count:
        if current.weekday() < 5:
            out.append(current.isoformat())
        current += timedelta(days=1)
    return out


def test_validation_plan_freezes_exact_10_sessions_without_evaluation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.application.strategy_lab.recipe as recipe

    dates: list[str] = []
    current = date(2026, 9, 1)
    while len(dates) < 10:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    sessions = [
        {
            "trading_date": trading_date,
            "trade_date_type": (
                "MORNING" if index == 1 else "AFTERNOON" if index == 2 else "WHOLE"
            ),
        }
        for index, trading_date in enumerate(dates)
    ]
    monkeypatch.setattr(
        recipe,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "hk.v1",
            "snapshot_ref": "calendar/snapshot.json",
            "snapshot_content_sha256": "a" * 64,
            "snapshot_file_sha256": "b" * 64,
            "trading_dates": dates,
            "trading_sessions": sessions,
        },
    )
    leader = {
        "variant_id": "challenger_0.002",
        "near_return_threshold": 0.002,
        "comparison_sha256": "c" * 64,
    }
    manifest = [{"path": "owner.py", "sha256": "d" * 64}]
    experiment = {
        "experiment_id": "experiment-1",
        "spec": {
            "recipe": {"recipe_id": RECIPE_ID},
            "scope": {"market": "hk", "account": "lx", "strategy": "sell_put"},
        },
        "spec_sha256": "e" * 64,
        "source_commit_sha": "f" * 40,
        "behavior_manifest": manifest,
        "evaluator_behavior_sha256": canonical_sha256(manifest),
        "leader": leader,
        "research_receipt_ref": "experiments/experiment-1/receipts/research.json",
        "research_receipt_sha256": "1" * 64,
    }
    receipt = {
        "experiment_id": "experiment-1",
        "spec_sha256": "e" * 64,
        "conclusion": {"status": "leader", "leader": leader},
    }
    schedule = {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "run_window": {
            "start": "09:30",
            "end": "16:00",
            "breaks": [{"start": "12:00", "end": "13:00"}],
        },
        "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10},
    }
    opend_binding = {"host": "127.0.0.1", "port": 11111}
    provider_authority = {
        "provider": "futu_opend",
        "endpoint": "market_snapshot",
        "opend_binding": opend_binding,
    }
    kwargs = {
        "requested_start": dates[0],
        "schedule": schedule,
        "account_run_config_sha256": "2" * 64,
        "provider_source": {
            **provider_authority,
            "source_authority_sha256": canonical_sha256(provider_authority),
        },
        "timer_binding": build_strategy_lab_timer_binding(),
    }

    first = build_validation_plan(
        {"artifact_root": tmp_path, "opend_binding": opend_binding},
        experiment,
        receipt,
        {"confirmation_sha256": "3" * 64},
        occurred_at_utc="2026-08-31T00:00:00Z",
        **kwargs,
    )
    second = build_validation_plan(
        {"artifact_root": tmp_path, "opend_binding": opend_binding},
        experiment,
        receipt,
        {"confirmation_sha256": "3" * 64},
        occurred_at_utc="2026-08-31T01:00:00Z",
        **kwargs,
    )

    frozen = first["market_calendar"]["sessions"]
    assert first == second
    assert first["selected_trading_dates"] == dates
    assert [len(item["minute_grid_utc"]) for item in frozen[:3]] == [330, 150, 180]
    assert frozen[0]["breaks_utc"] == [
        {"start_utc": "2026-09-01T04:00:00Z", "end_utc": "2026-09-01T05:00:00Z"}
    ]
    assert frozen[1]["session_endpoint_utc"].endswith("04:00:00Z")
    assert "occurred_at_utc" not in first

    with pytest.raises(recipe.StrategyLabRecipeError) as raised:
        build_validation_plan(
            {"artifact_root": tmp_path, "opend_binding": opend_binding},
            experiment,
            receipt,
            {"confirmation_sha256": "3" * 64},
            occurred_at_utc="2026-09-01T02:00:00Z",
            **kwargs,
        )
    assert raised.value.reason_code == "validation_preview_blocked"


def test_window_uses_newest_mature_20_and_never_skips_a_hole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    dates = _trading_dates(24)
    monkeypatch.setattr(
        recipe,
        "read_market_calendar_binding",
        lambda *_args, **_kwargs: {
            "trading_dates": dates,
            "market_calendar_version": "hk.v1",
            "snapshot_ref": "calendar.json",
            "snapshot_content_sha256": "a" * 64,
            "snapshot_file_sha256": "b" * 64,
        },
    )

    def load_day(
        _context: object,
        trading_date: str,
        _cutoff: datetime,
        _cutoff_ms: int,
        _calendar: object,
    ):
        if trading_date in dates[-2:]:
            return None, (
                "research_outcome_immature"
                if trading_date == dates[-2]
                else "research_point_post_cutoff"
            )
        return {"trading_date": trading_date, "points": []}, None

    monkeypatch.setattr(recipe, "_load_window_day", load_day)
    context = {"artifact_root": Path("/does/not/read"), "runtime_root": Path("/does/not/read")}

    selected = select_research_window(context, "2026-09-01T00:00:00Z")

    assert selected["status"] == "available"
    assert selected["selected_trading_dates"] == dates[2:22]
    assert selected["ignored_immature_window_count"] == 2

    hole = dates[10]

    def load_with_hole(
        _context: object,
        trading_date: str,
        _cutoff: datetime,
        _cutoff_ms: int,
        _calendar: object,
    ):
        if trading_date == hole:
            return None, "formal_point_evidence_missing"
        if trading_date in dates[-2:]:
            return None, "research_outcome_immature"
        return {"trading_date": trading_date, "points": []}, None

    monkeypatch.setattr(recipe, "_load_window_day", load_with_hole)
    blocked = select_research_window(context, "2026-09-01T00:00:00Z")
    assert blocked["status"] == "blocked"
    assert blocked["blockers"][0]["reason_code"] == "formal_point_evidence_missing"
    assert hole in blocked["selected_trading_dates"]

    def load_with_interior_immature(
        _context: object,
        trading_date: str,
        _cutoff: datetime,
        _cutoff_ms: int,
        _calendar: object,
    ):
        if trading_date == hole or trading_date in dates[-2:]:
            return None, "research_outcome_immature"
        return {"trading_date": trading_date, "points": []}, None

    monkeypatch.setattr(recipe, "_load_window_day", load_with_interior_immature)
    interior = select_research_window(context, "2026-09-01T00:00:00Z")
    assert interior["status"] == "blocked"
    assert interior["blockers"][0]["reason_code"] == "research_outcome_immature"
    assert hole in interior["selected_trading_dates"]


def _window_day_artifacts(timestamp: str) -> tuple[dict[str, object], dict[str, object]]:
    expectation = {
        "status": "available",
        "reason_code": None,
        "artifact_ref": "expectation.json",
        "artifact_content_sha256": "e" * 64,
        "artifact_file_sha256": "f" * 64,
        "expectation": {
            "sealed_at_utc": timestamp,
            "market_calendar_version": "old.v1",
            "market_calendar_sha256": "a" * 64,
            "scheduled_scan_targets_market": [timestamp],
            "expected_recommendation_point_ids": ["p" * 64],
        },
    }
    point = {
        "captured_at_utc": timestamp,
        "source_binding": {"scheduled_scan_target_market": timestamp},
        "recommendation_point": {
            "scheduled_scan_target_market": timestamp,
            "decision_at_utc": timestamp,
            "formal_point_time_coherence": {"maximum_observed_at_utc": timestamp},
        },
        "opening_snapshot": {"sealed_at_utc": timestamp},
    }
    loaded = {
        "status": "available",
        "reason_code": None,
        "artifact_ref": "point.json.gz",
        "artifact_file_sha256": "d" * 64,
        "point": point,
    }
    return expectation, loaded


@pytest.mark.parametrize(
    "path",
    [
        ("expectation", "sealed_at_utc"),
        ("point", "captured_at_utc"),
        ("point", "source_binding", "scheduled_scan_target_market"),
        ("point", "recommendation_point", "scheduled_scan_target_market"),
        ("point", "recommendation_point", "decision_at_utc"),
        ("point", "opening_snapshot", "sealed_at_utc"),
        (
            "point",
            "recommendation_point",
            "formal_point_time_coherence",
            "maximum_observed_at_utc",
        ),
    ],
)
def test_window_day_rejects_every_authoritative_time_after_exact_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
) -> None:
    import src.application.strategy_lab.recipe as recipe

    cutoff = datetime(2026, 8, 26, 3, tzinfo=timezone.utc)
    expectation, loaded = _window_day_artifacts(cutoff.isoformat().replace("+00:00", "Z"))
    root: object = expectation["expectation"] if path[0] == "expectation" else loaded["point"]
    assert isinstance(root, dict)
    for key in path[1:-1]:
        root = root[key]
        assert isinstance(root, dict)
    root[path[-1]] = "2026-08-26T03:00:00.000001Z"
    monkeypatch.setattr(recipe, "load_formal_expectation", lambda *_args, **_kwargs: expectation)
    monkeypatch.setattr(recipe, "load_formal_point", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(
        recipe,
        "read_expectation_bound_market_calendar_snapshot",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "old.v1",
            "snapshot_ref": "old.json",
            "snapshot_content_sha256": "a" * 64,
            "snapshot_file_sha256": "b" * 64,
            "trading_sessions": [
                {"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}
            ],
        },
    )
    current = {
        "trading_sessions": [
            {"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}
        ]
    }

    day, reason = recipe._load_window_day(
        {"runtime_root": Path("/not-read"), "artifact_root": Path("/not-read")},
        "2026-08-26",
        cutoff,
        int(cutoff.timestamp() * 1000),
        current,
    )

    assert day is None
    assert reason == "research_point_post_cutoff"


def test_window_day_classifies_future_missing_target_before_point_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    cutoff = datetime(2026, 8, 26, 3, tzinfo=timezone.utc)
    expectation, loaded = _window_day_artifacts("2026-08-26T02:00:00Z")
    payload = expectation["expectation"]
    assert isinstance(payload, dict)
    before_id = "a" * 64
    future_id = "b" * 64
    payload["scheduled_scan_targets_market"] = [
        "2026-08-26T02:00:00Z",
        "2026-08-26T04:00:00Z",
    ]
    payload["expected_recommendation_point_ids"] = [before_id, future_id]
    calls: list[str] = []

    def load_point(*_args: object, **kwargs: object) -> dict[str, object]:
        point_id = str(kwargs["recommendation_point_id"])
        calls.append(point_id)
        if point_id == future_id:
            return {
                "status": "missing",
                "reason_code": "formal_point_evidence_missing",
            }
        return loaded

    monkeypatch.setattr(recipe, "load_formal_expectation", lambda *_args, **_kwargs: expectation)
    monkeypatch.setattr(recipe, "load_formal_point", load_point)
    monkeypatch.setattr(
        recipe,
        "read_expectation_bound_market_calendar_snapshot",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "old.v1",
            "snapshot_ref": "old.json",
            "snapshot_content_sha256": "a" * 64,
            "snapshot_file_sha256": "b" * 64,
            "trading_sessions": [
                {"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}
            ],
        },
    )
    monkeypatch.setattr(
        recipe,
        "build_concentration_arms",
        lambda *_args, **_kwargs: {"arms": [{"candidate": {"expiration": "2026-08-01"}}]},
    )
    current = {
        "trading_sessions": [
            {"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}
        ]
    }
    context = {"runtime_root": Path("/not-read"), "artifact_root": Path("/not-read")}

    day, reason = recipe._load_window_day(
        context,
        "2026-08-26",
        cutoff,
        int(cutoff.timestamp() * 1000),
        current,
    )

    assert day is None
    assert reason == "research_point_post_cutoff"
    assert calls == [before_id]

    def missing_point(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append(str(kwargs["recommendation_point_id"]))
        return {
            "status": "missing",
            "reason_code": "formal_point_evidence_missing",
        }

    calls.clear()
    monkeypatch.setattr(recipe, "load_formal_point", missing_point)
    blocked, blocked_reason = recipe._load_window_day(
        context,
        "2026-08-26",
        cutoff,
        int(cutoff.timestamp() * 1000),
        current,
    )
    assert blocked is None
    assert blocked_reason == "formal_point_evidence_missing"
    assert calls == [before_id]

    payload["scheduled_scan_targets_market"] = ["2026-08-26T03:00:00Z"]
    payload["expected_recommendation_point_ids"] = [before_id]
    calls.clear()
    exact, exact_reason = recipe._load_window_day(
        context,
        "2026-08-26",
        cutoff,
        int(cutoff.timestamp() * 1000),
        current,
    )
    assert exact is None
    assert exact_reason == "formal_point_evidence_missing"
    assert calls == [before_id]


def test_window_day_uses_expectation_calendar_and_compares_only_session_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    timestamp = "2026-08-26T03:00:00Z"
    cutoff = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    expectation, loaded = _window_day_artifacts(timestamp)
    monkeypatch.setattr(recipe, "load_formal_expectation", lambda *_args, **_kwargs: expectation)
    monkeypatch.setattr(recipe, "load_formal_point", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(
        recipe,
        "read_expectation_bound_market_calendar_snapshot",
        lambda *_args, **_kwargs: {
            "market_calendar_version": "old.v1",
            "snapshot_ref": "old.json",
            "snapshot_content_sha256": "a" * 64,
            "snapshot_file_sha256": "b" * 64,
            "trading_sessions": [
                {"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}
            ],
        },
    )
    monkeypatch.setattr(
        recipe,
        "build_concentration_arms",
        lambda *_args, **_kwargs: {"arms": [{"candidate": {"expiration": "2026-08-01"}}]},
    )
    current = {
        "market_calendar_version": "new.v2",
        "snapshot_content_sha256": "c" * 64,
        "trading_sessions": [
            {"trading_date": "2026-08-26", "trade_date_type": "WHOLE"}
        ],
    }
    context = {"runtime_root": Path("/not-read"), "artifact_root": Path("/not-read")}

    day, reason = recipe._load_window_day(
        context,
        "2026-08-26",
        cutoff,
        int(cutoff.timestamp() * 1000),
        current,
    )

    assert reason is None
    assert day is not None
    assert day["market_calendar_binding"]["market_calendar_version"] == "old.v1"

    current["trading_sessions"][0]["trade_date_type"] = "MORNING"
    changed, changed_reason = recipe._load_window_day(
        context,
        "2026-08-26",
        cutoff,
        int(cutoff.timestamp() * 1000),
        current,
    )
    assert changed is None
    assert changed_reason == "market_calendar_session_changed"


def _arm(
    contract: str = "HK.POP260828P145000",
    symbol: str = "9992.HK",
    *,
    sample_date: str = "2026-08-27",
) -> dict[str, object]:
    return {
        "trading_date": sample_date,
        "candidate": {
            "contract_symbol": contract,
            "symbol": symbol,
            "expiration": "2026-08-28",
            "currency": "HKD",
            "sample_trading_date": sample_date,
        },
    }


def _ready_receipt(
    probe_sha256: str,
    *,
    remaining: int = 100,
    sample_quota_code: str = "HK.09992",
) -> dict[str, object]:
    return {
        "probe_sha256": probe_sha256,
        "receipt_ref": f"readiness/{probe_sha256}.json",
        "content_sha256": "c" * 64,
        "receipt_file_sha256": "d" * 64,
        "observed_at_utc": "2026-08-30T03:00:00Z",
        "expires_at_utc": "2026-08-31T03:00:00Z",
        "provider_observation": {
            "readiness_status": "ready",
            "pagination_complete": True,
            "no_trade_bar_semantics_observed": True,
            "quota": {
                "sample_quota_code_counted": True,
                "sample_quota_code": sample_quota_code,
                "remain_quota": remaining,
            },
        },
    }


def test_history_k_uses_one_deterministic_exact_probe_and_quota_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    calls: list[dict[str, object]] = []

    def read_receipt(_root: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return _ready_receipt(str(kwargs["probe_sha256"]), sample_quota_code="HK.00700")

    monkeypatch.setattr(recipe, "read_history_k_readiness_receipt", read_receipt)
    context = {
        "artifact_root": Path("/not-read"),
        "opend_binding": {"host": "127.0.0.1", "port": 11111},
    }
    authority, blockers = _history_k_authority(
        context,
        [
            _arm(),
            _arm("HK.TCH260828P300000", "0700.HK"),
            _arm(),
        ],
        maturity_cutoff_utc="2026-08-30T03:00:00Z",
        occurred_at_utc="2026-08-30T03:00:00.100000Z",
    )

    assert blockers == []
    assert authority is not None
    assert authority["representative"]["security_quota_identity"] == "HK.00700"
    assert authority["required_unique_security_identity_count"] == 2
    assert len(calls) == 1
    assert calls[0]["probe_sha256"] == authority["probe_sha256"]
    assert calls[0]["as_of_utc"] == "2026-08-30T03:00:00.100000Z"

    monkeypatch.setattr(
        recipe,
        "read_history_k_readiness_receipt",
        lambda _root, **kwargs: _ready_receipt(str(kwargs["probe_sha256"]), remaining=1, sample_quota_code="HK.00700"),
    )
    _, quota_blockers = _history_k_authority(
        context,
        [_arm(), _arm("HK.TCH260828P300000", "0700.HK")],
        maturity_cutoff_utc="2026-08-30T03:00:00Z",
        occurred_at_utc="2026-08-30T03:00:00Z",
    )
    assert quota_blockers[0]["reason_code"] == "history_k_readiness_insufficient"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pagination_complete", False),
        ("no_trade_bar_semantics_observed", False),
        ("sample_quota_code_counted", False),
    ],
)
def test_history_k_blocks_incomplete_receipt_semantics(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: bool,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    def read_receipt(_root: object, **kwargs: object) -> dict[str, object]:
        receipt = _ready_receipt(str(kwargs["probe_sha256"]))
        observation = receipt["provider_observation"]
        assert isinstance(observation, dict)
        target = observation["quota"] if field == "sample_quota_code_counted" else observation
        assert isinstance(target, dict)
        target[field] = value
        return receipt

    monkeypatch.setattr(recipe, "read_history_k_readiness_receipt", read_receipt)
    _, blockers = _history_k_authority(
        {
            "artifact_root": Path("/not-read"),
            "opend_binding": {"host": "127.0.0.1", "port": 11111},
        },
        [_arm()],
        maturity_cutoff_utc="2026-08-30T03:00:00Z",
        occurred_at_utc="2026-08-30T03:00:00Z",
    )
    assert blockers[0]["reason_code"] == "history_k_readiness_insufficient"


@dataclass(frozen=True)
class _Bundle:
    schema_state: str
    fx_rates: tuple[FXRateFact, ...]


def test_terminal_fx_freezes_exact_fact_and_blocks_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.recipe as recipe

    observation_ms = 1_777_593_600_000
    fresh = FXRateFact(
        fact_id="fx-fresh",
        base_currency="HKD",
        quote_currency="CNY",
        rate="0.92",
        rate_kind="spot",
        effective_at_ms=observation_ms,
        observed_at_ms=observation_ms,
        source="realtime_snapshot",
        source_id="fresh",
    )
    monkeypatch.setattr(recipe, "expiration_observation_start_ms", lambda *_args: observation_ms)
    monkeypatch.setattr(
        recipe,
        "PerformanceEvidenceSQLiteRepository",
        lambda _path: SimpleNamespace(read_all=lambda: _Bundle("initialized_v1", (fresh,))),
    )

    bindings, blockers = _terminal_fx_bindings({"ledger_path": Path("x")}, [_arm()])

    assert blockers == []
    assert bindings[0]["fact_ref"] == {"kind": "fx_rate", "fact_id": "fx-fresh"}
    assert bindings[0]["fact"]["rate"] == "0.92"

    old = FXRateFact(
        fact_id="fx-old",
        base_currency="HKD",
        quote_currency="CNY",
        rate="0.91",
        rate_kind="spot",
        effective_at_ms=observation_ms - 8 * 86_400_000,
        observed_at_ms=observation_ms - 8 * 86_400_000,
        source="realtime_snapshot",
        source_id="old",
    )
    monkeypatch.setattr(
        recipe,
        "PerformanceEvidenceSQLiteRepository",
        lambda _path: SimpleNamespace(read_all=lambda: _Bundle("initialized_v1", (old,))),
    )
    _, stale = _terminal_fx_bindings({"ledger_path": Path("x")}, [_arm()])
    assert stale[0]["reason_code"] == "terminal_fx_unavailable"


def test_preview_is_canonical_clock_independent_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.application.strategy_lab.service as service

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    fee = tmp_path / "fee.json"
    fee.write_text("unchanged", encoding="utf-8")
    readiness = {
        "status": "available",
        "blockers": [],
        "window": {"status": "available", "sessions": []},
        "fee_plan": {"source_receipt_sha256": "a" * 64},
        "terminal_fx_bindings": [],
        "history_k_authority": {"probe_sha256": "b" * 64},
    }
    invocations: list[str] = []

    def check(_context: object, _request: object, *, occurred_at_utc: str):
        invocations.append(occurred_at_utc)
        return readiness

    monkeypatch.setattr(service, "check_recipe_readiness", check)
    monkeypatch.setattr(service, "source_commit_sha", lambda _root: "c" * 40)
    monkeypatch.setattr(
        service,
        "build_evaluator_behavior_manifest",
        lambda _root: [{"path": "owner.py", "sha256": "d" * 64}],
    )
    request = {
        "hypothesis": "concentration helps",
        "recipe_id": RECIPE_ID,
        "market": "hk",
        "account": "lx",
        "maturity_cutoff_utc": "2026-08-30T03:00:00Z",
        "fee_plan_receipt_path": str(fee),
    }
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    first = preview_experiment(
        {
            "artifact_root": artifact_root,
            "repo_root": tmp_path,
            "market": "hk",
            "account": "lx",
        },
        request,
        occurred_at_utc="2026-08-30T04:00:00.100000Z",
    )
    second = preview_experiment(
        {
            "artifact_root": artifact_root,
            "repo_root": tmp_path,
            "market": "hk",
            "account": "lx",
        },
        request,
        occurred_at_utc="2026-08-30T04:00:00.900000Z",
    )
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert first["preview_sha256"] == second["preview_sha256"]
    assert first["spec"] == second["spec"]
    assert first["spec_sha256"] == canonical_sha256(first["spec"])
    assert "spec_sha256" not in first["spec"]
    assert invocations == ["2026-08-30T04:00:00.100000Z", "2026-08-30T04:00:00.900000Z"]
    assert before == after
