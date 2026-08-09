from __future__ import annotations

from domain.domain.daily_decision_brief import (
    diff_daily_decision_briefs,
    normalize_daily_decision_brief,
)


def _brief(*, revision: int, ai_section: dict | None) -> dict:
    return {
        "schema_version": "daily_decision_brief.v1",
        "market": "US",
        "market_trading_date": "2026-08-09",
        "account": "lx",
        "revision": revision,
        "run_id": f"run-{revision}",
        "generated_at_utc": "2026-08-09T12:00:00+00:00",
        "data_as_of_utc": "2026-08-09T11:59:00+00:00",
        "valid_until_utc": "2026-08-09T20:00:00+00:00",
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": "",
        "actions": [],
        "positions": [],
        "capacity": {},
        "candidates": {},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
        **({"ai_decision_advice": ai_section} if ai_section is not None else {}),
    }


def _completed(*, sp_action: str, cc: list[dict] | None = None) -> dict:
    return {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-08-09T11:00:00+00:00",
        "sell_put": {
            "action": sp_action,
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": "put-1" if sp_action == "keep" else None,
            "rationale": {"risk_mechanism": "m"},
            "source_refs": {"external_evidence_refs": []},
        },
        "covered_call": cc or [],
        "zero_candidate": {"sell_put": False, "covered_call": False},
        "reused": False,
        "advice_record_id": "adv-1",
    }


def _cc(symbol: str, action: str) -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "baseline_candidate_id": f"call-{symbol}",
        "selected_candidate_id": None,
    }


def test_normalize_absent_section_stays_none():
    brief = normalize_daily_decision_brief(_brief(revision=1, ai_section=None))
    assert brief["ai_decision_advice"] is None


def test_normalize_completed_section():
    brief = normalize_daily_decision_brief(
        _brief(revision=1, ai_section=_completed(sp_action="keep", cc=[_cc("NVDA", "keep")]))
    )
    section = brief["ai_decision_advice"]
    assert section["status"] == "completed"
    assert section["sell_put"]["action"] == "keep"
    assert section["covered_call"][0]["symbol"] == "NVDA"


def test_action_migration_is_material():
    prev = _brief(revision=1, ai_section=_completed(sp_action="keep"))
    cur = _brief(revision=2, ai_section=_completed(sp_action="defer"))
    diff = diff_daily_decision_briefs(prev, cur)
    changes = [c for c in diff["changes"] if c["change_type"] == "ai_decision_advice_action_changed"]
    assert len(changes) == 1
    assert changes[0]["material"] is True
    assert changes[0]["ai_advice_scope"] == "sell_put"
    assert changes[0]["before"] == "keep"
    assert changes[0]["after"] == "defer"
    assert diff["material"] is True


def test_needs_review_to_keep_is_material():
    prev = _brief(revision=1, ai_section=_completed(sp_action="needs_review"))
    cur = _brief(revision=2, ai_section=_completed(sp_action="keep"))
    diff = diff_daily_decision_briefs(prev, cur)
    assert any(
        c["change_type"] == "ai_decision_advice_action_changed" and c["material"]
        for c in diff["changes"]
    )


def test_cc_scope_migration_is_material():
    prev = _brief(revision=1, ai_section=_completed(sp_action="keep", cc=[_cc("NVDA", "keep")]))
    cur = _brief(revision=2, ai_section=_completed(sp_action="keep", cc=[_cc("NVDA", "switch")]))
    diff = diff_daily_decision_briefs(prev, cur)
    changes = [c for c in diff["changes"] if c["change_type"] == "ai_decision_advice_action_changed"]
    assert len(changes) == 1
    assert changes[0]["ai_advice_scope"] == "covered_call:NVDA"


def test_unavailable_transition_is_not_material():
    completed = _brief(revision=1, ai_section=_completed(sp_action="keep"))
    unavailable = _brief(
        revision=2,
        ai_section={
            "status": "unavailable",
            "unavailable_reason": "timeout",
            "evidence_as_of": None,
            "sell_put": None,
            "covered_call": None,
            "zero_candidate": {"sell_put": False, "covered_call": False},
            "reused": False,
            "advice_record_id": None,
        },
    )
    diff = diff_daily_decision_briefs(completed, unavailable)
    assert not any(
        c["change_type"] == "ai_decision_advice_action_changed" for c in diff["changes"]
    )
    back = diff_daily_decision_briefs(unavailable, _brief(revision=3, ai_section=_completed(sp_action="keep")))
    assert not any(
        c["change_type"] == "ai_decision_advice_action_changed" for c in back["changes"]
    )


def test_identical_actions_produce_no_changes():
    prev = _brief(revision=1, ai_section=_completed(sp_action="keep", cc=[_cc("NVDA", "defer")]))
    cur = _brief(revision=2, ai_section=_completed(sp_action="keep", cc=[_cc("NVDA", "defer")]))
    diff = diff_daily_decision_briefs(prev, cur)
    assert diff["changes"] == []
    assert diff["material"] is False


def test_unavailable_reason_change_is_not_material():
    a = _brief(
        revision=1,
        ai_section={
            "status": "unavailable",
            "unavailable_reason": "timeout",
            "zero_candidate": {"sell_put": False, "covered_call": False},
        },
    )
    b = _brief(
        revision=2,
        ai_section={
            "status": "unavailable",
            "unavailable_reason": "no_evidence",
            "zero_candidate": {"sell_put": False, "covered_call": False},
        },
    )
    diff = diff_daily_decision_briefs(a, b)
    assert diff["changes"] == []
