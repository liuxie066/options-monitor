from __future__ import annotations

from dataclasses import dataclass

from src.application.copilot import tools as copilot_tools
from src.application.copilot.result_admission import admit_submit_answer
from src.application.copilot.scene import load_general_scene


@dataclass
class _Definition:
    contract: dict

    def resolve_output_contract(self, _payload: dict) -> dict:
        return dict(self.contract)


def _install_contract(monkeypatch, **overrides) -> None:
    contract = {
        "schema_version": "test.output.v1",
        "evidence_type": "collection",
        "bounded_projection": "contract_fields",
        "coverage": "primary_rows",
        "freshness": "not_applicable",
        "pagination": {"mode": "none"},
        "primary_rows": "rows",
        "model_value_fields": ["rows"],
        **overrides,
    }
    monkeypatch.setattr(
        copilot_tools,
        "get_tool_definition",
        lambda _name: _Definition(contract),
    )


def test_primary_rows_are_complete_only_for_the_visible_requested_page(monkeypatch) -> None:
    _install_contract(monkeypatch)

    observation = copilot_tools.compact_observation(
        "test_read",
        {"ok": True, "data": {"rows": [{"id": 1}, {"id": 2}]}},
        {"account": "lx", "limit": 2},
    )

    assert observation["status"] == "complete"
    assert observation["coverage"] == {
        "status": "complete",
        "complete_for": "requested_page",
        "included_count": 2,
        "total_count": None,
        "omitted_count": None,
        "scope": {"account": "lx", "limit": 2},
    }
    assert observation["freshness"] == {"status": "not_applicable"}


def test_generic_collection_clipping_requires_narrowing_and_never_claims_full_query(
    monkeypatch,
) -> None:
    _install_contract(monkeypatch)

    observation = copilot_tools.compact_observation(
        "test_read",
        {"ok": True, "data": {"rows": [{"id": index} for index in range(21)]}},
    )

    assert observation["status"] == "needs_narrowing"
    assert observation["coverage"]["status"] == "partial"
    assert observation["coverage"]["complete_for"] == "requested_page"
    assert observation["coverage"]["needs_narrowing"] is True
    assert observation["result_contract"]["pagination"] == {"mode": "none"}


def test_invalid_source_coverage_and_missing_freshness_fail_closed(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        coverage="source_declared",
        freshness="source_declared",
        model_value_fields=["value"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "value": 42,
                "coverage": {"status": "complete", "matched": 42},
            },
        },
    )

    assert observation["status"] == "partial"
    assert observation["coverage"] == {
        "status": "unknown",
        "complete_for": "point",
    }
    assert observation["freshness"] == {"status": "unknown"}


def test_declared_full_query_requires_a_known_total(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        coverage="source_declared",
        freshness="not_applicable",
        model_value_fields=["value"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "value": 42,
                "coverage": {
                    "status": "complete",
                    "complete_for": "full_query",
                    "included_count": 1,
                    "total_count": None,
                    "omitted_count": None,
                },
            },
        },
    )
    complete_observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "value": 42,
                "coverage": {
                    "status": "complete",
                    "complete_for": "full_query",
                    "included_count": 1,
                    "total_count": 1,
                    "omitted_count": 0,
                    "has_more": False,
                },
            },
        },
    )

    assert observation["coverage"]["status"] == "unknown"
    assert complete_observation["coverage"] == {
        "status": "complete",
        "complete_for": "full_query",
        "included_count": 1,
        "total_count": 1,
        "omitted_count": 0,
        "has_more": False,
    }


def test_declared_collection_coverage_requires_included_count(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        coverage="source_declared",
        freshness="not_applicable",
        model_value_fields=["rows"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "rows": [{"id": 1}],
                "coverage": {
                    "status": "complete",
                    "complete_for": "requested_page",
                    "total_count": None,
                    "omitted_count": None,
                },
            },
        },
    )

    assert observation["status"] == "partial"
    assert observation["coverage"]["status"] == "unknown"


def test_inconsistent_declared_full_query_coverage_fails_closed(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        coverage="source_declared",
        freshness="not_applicable",
        model_value_fields=["rows"],
    )
    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "rows": [{"id": 1}],
                "coverage": {
                    "status": "complete",
                    "complete_for": "full_query",
                    "included_count": 1,
                    "total_count": 20,
                    "omitted_count": 19,
                    "has_more": True,
                },
            },
        },
    )
    evidence = {
        "ok": True,
        "authorized_read": True,
        "observation_status": observation["status"],
        "coverage": observation["coverage"],
        "freshness": observation["freshness"],
    }
    proposed = {
        "mode": "evidence",
        "status": "complete",
        "answer_markdown": "全部 20 条均已覆盖。",
        "claims": [
            {
                "text": "全部 20 条均已覆盖",
                "kind": "historical_fact",
                "observation_ids": ["obv_bad"],
                "required_scope": "full_query",
            }
        ],
    }

    rejected = admit_submit_answer(proposed, {"obv_bad": evidence})

    assert observation["status"] == "partial"
    assert observation["coverage"]["status"] == "unknown"
    assert rejected["observation"]["reason"] == "claim_scope_not_covered"


def test_consistent_requested_page_may_have_more_full_query_rows(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        coverage="source_declared",
        freshness="not_applicable",
        model_value_fields=["rows"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "rows": [{"id": index} for index in range(20)],
                "coverage": {
                    "status": "complete",
                    "complete_for": "requested_page",
                    "included_count": 20,
                    "total_count": 143,
                    "omitted_count": 123,
                    "has_more": True,
                },
            },
        },
    )

    assert observation["coverage"] == {
        "status": "complete",
        "complete_for": "requested_page",
        "included_count": 20,
        "total_count": 143,
        "omitted_count": 123,
        "has_more": True,
    }


def test_source_freshness_requires_status_and_exposes_as_of(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        evidence_type="point",
        coverage="point",
        freshness="source_declared",
        freshness_fields=["freshness.status", "freshness.latest_mtime_utc"],
        model_value_fields=["value", "freshness"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "value": 42,
                "freshness": {
                    "status": "fresh",
                    "latest_mtime_utc": "2026-08-22T01:30:00Z",
                },
            },
        },
    )

    assert observation["freshness"] == {
        "status": "fresh",
        "as_of": "2026-08-22T01:30:00Z",
    }
    assert observation["as_of"] == "2026-08-22T01:30:00Z"


def test_large_chinese_projection_is_replaced_before_crossing_four_k_tokens(
    monkeypatch,
) -> None:
    _install_contract(
        monkeypatch,
        evidence_type="point",
        coverage="point",
        model_value_fields=["text"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {"ok": True, "data": {"text": "我" * 20_000}},
    )

    assert observation["status"] == "needs_narrowing"
    assert copilot_tools.conservative_json_tokens(observation) < 4_000
    assert "我" * 100 not in str(observation)


def test_depth_clipping_cannot_retain_complete_coverage(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        evidence_type="point",
        coverage="point",
        model_value_fields=["nested"],
    )
    nested = {"leaf": "x" * 1_000}
    for index in range(8):
        nested = {f"level_{index}": nested}

    observation = copilot_tools.compact_observation(
        "test_read",
        {"ok": True, "data": {"nested": nested}},
    )

    assert observation["status"] == "needs_narrowing"
    assert observation["coverage"]["status"] == "partial"


def test_narrowing_fallback_itself_stays_within_tool_budget(monkeypatch) -> None:
    _install_contract(
        monkeypatch,
        evidence_type="point",
        coverage="point",
        model_value_fields=["value"],
    )

    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": True,
            "data": {
                "value": "x" * 20_000,
                "source": {f"source_{index}": "y" * 2_000 for index in range(20)},
                "scope": {f"scope_{index}": "z" * 2_000 for index in range(20)},
            },
        },
    )

    assert observation["status"] == "needs_narrowing"
    assert copilot_tools.conservative_json_tokens(observation) <= 4_000


def test_tool_failure_never_receives_success_coverage() -> None:
    observation = copilot_tools.compact_observation(
        "test_read",
        {
            "ok": False,
            "error": {"code": "READ_ERROR", "message": "unavailable"},
        },
    )

    assert observation["ok"] is False
    assert "coverage" not in observation


def test_failed_tool_observation_recursively_bounds_schema_error_details() -> None:
    invalid_value = "错" * 100_000
    response = copilot_tools.call_read_tool(
        "option_positions_read",
        {"config_key": invalid_value, "action": "list"},
        allowed_tools=("option_positions_read",),
    )

    observation = copilot_tools.compact_observation(
        "option_positions_read",
        response,
        {"config_key": invalid_value, "action": "list"},
    )

    assert observation["ok"] is False
    assert observation["status"] == "failed"
    assert copilot_tools.conservative_json_tokens(observation) <= 4_000
    assert invalid_value not in str(observation)


def test_s8_static_prompt_stays_within_pre_s8_budget() -> None:
    # Measured from origin/main before the S8 tool-rules change.  The dynamic
    # catalog and resident schemas are intentionally accounted elsewhere.
    baseline_chars = 11_796
    baseline_conservative_tokens = 3_314
    prompt = load_general_scene()["system_prompt"]

    assert len(prompt) <= baseline_chars
    assert copilot_tools.conservative_json_tokens(prompt) <= baseline_conservative_tokens
