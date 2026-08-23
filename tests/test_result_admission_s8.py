from __future__ import annotations

import hashlib

import pytest

from src.application.copilot.result_admission import admit_submit_answer


def _evidence(
    *,
    coverage_status: str = "complete",
    complete_for: str = "full_query",
    freshness_status: str = "current",
    as_of: str | None = "2026-08-22T09:30:00+08:00",
    observation_status: str = "complete",
    authorized_read: bool = True,
) -> dict:
    freshness = {"status": freshness_status}
    if as_of is not None:
        freshness["as_of"] = as_of
    return {
        "ok": True,
        "authorized_read": authorized_read,
        "observation_status": observation_status,
        "coverage": {
            "status": coverage_status,
            "complete_for": complete_for,
            "included_count": 10,
            "total_count": 20,
            "omitted_count": 10,
            "scope": {"account": "lx"},
        },
        "freshness": freshness,
    }


def _submit(
    *,
    mode: str = "evidence",
    status: str = "complete",
    claims: list[dict] | None = None,
    text: str = "结论",
) -> dict:
    return {
        "mode": mode,
        "status": status,
        "answer_markdown": text,
        "claims": claims
        if claims is not None
        else [
            {
                "text": "事实",
                "kind": "current_fact",
                "observation_ids": ["obv_a"],
                "required_scope": "point",
            }
        ],
    }


def test_conceptual_answer_accepts_exact_text_and_hash() -> None:
    payload = _submit(mode="conceptual", claims=[], text="概念说明。")

    accepted = admit_submit_answer(payload, {})

    approved = accepted["approved_answer"]
    assert accepted["observation"] == {"ok": True, "status": "answer_accepted"}
    assert approved["text"] == "概念说明。"
    assert approved["text_sha256"] == (
        "sha256:" + hashlib.sha256("概念说明。".encode()).hexdigest()
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda payload: payload.update(extra=True), "answer_schema_invalid"),
        (lambda payload: payload.update(claims=[]), "answer_mode_inconsistent"),
        (
            lambda payload: payload["claims"][0].update(extra=True),
            "claim_schema_invalid",
        ),
        (
            lambda payload: payload["claims"][0].update(observation_ids=[]),
            "claim_schema_invalid",
        ),
    ],
)
def test_submit_answer_schema_is_closed(mutation, reason: str) -> None:
    payload = _submit()
    mutation(payload)

    rejected = admit_submit_answer(payload, {"obv_a": _evidence()})

    assert rejected["observation"]["ok"] is False
    assert rejected["observation"]["reason"] == reason


@pytest.mark.parametrize(
    "kind",
    ["current_fact", "historical_fact", "derived_fact", "judgment"],
)
@pytest.mark.parametrize("required_scope", ["point", "requested_page", "full_query"])
def test_all_claim_kinds_and_scopes_use_current_request_evidence(
    kind: str,
    required_scope: str,
) -> None:
    claim = {
        "text": "事实",
        "kind": kind,
        "observation_ids": ["obv_a"],
        "required_scope": required_scope,
    }

    accepted = admit_submit_answer(
        _submit(claims=[claim]),
        {"obv_a": _evidence()},
    )

    assert accepted["observation"]["ok"] is True


@pytest.mark.parametrize(
    "registry,reason",
    [
        ({}, "observation_outside_request"),
        ({"obv_a": _evidence(authorized_read=False)}, "observation_not_authoritative"),
        (
            {"obv_a": {**_evidence(), "ok": False}},
            "observation_not_authoritative",
        ),
    ],
)
def test_only_successful_authorized_current_request_reads_are_admitted(
    registry: dict,
    reason: str,
) -> None:
    rejected = admit_submit_answer(_submit(), registry)

    assert rejected["observation"]["reason"] == reason


def test_current_fact_requires_current_timestamped_evidence() -> None:
    for evidence in (
        _evidence(freshness_status="stale"),
        _evidence(freshness_status="current", as_of=None),
        _evidence(freshness_status="unknown"),
    ):
        rejected = admit_submit_answer(_submit(), {"obv_a": evidence})
        assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


def test_complete_answer_cannot_overreach_coverage_scope() -> None:
    rejected = admit_submit_answer(
        _submit(),
        {"obv_a": _evidence(complete_for="point", coverage_status="partial")},
    )

    assert rejected["observation"]["reason"] == "claim_scope_not_covered"


def test_partial_answer_gets_non_removable_counts_scope_banner() -> None:
    accepted = admit_submit_answer(
        _submit(status="partial"),
        {"obv_a": _evidence()},
    )

    text = accepted["approved_answer"]["text"]
    assert "部分数据" in text
    assert "已纳入 10 条" in text
    assert '"account": "lx"' in text


@pytest.mark.parametrize(
    "coverage,expected",
    [
        (
            {
                "status": "complete",
                "complete_for": "requested_page",
                "included_count": 10,
                "total_count": None,
                "omitted_count": None,
                "has_more": True,
                "scope": {"account": "lx"},
            },
            "本页已返回 10 条记录，仍有更多记录；请继续查询下一页",
        ),
        (
            {
                "status": "complete",
                "complete_for": "full_query",
                "included_count": 20,
                "total_count": 20,
                "omitted_count": 0,
                "has_more": False,
                "scope": {"account": "lx"},
            },
            "已返回当前条件下全部 20 条记录，没有更多记录",
        ),
    ],
)
def test_complete_answer_gets_non_removable_pagination_banner(
    coverage: dict,
    expected: str,
) -> None:
    evidence = _evidence()
    evidence["coverage"] = coverage
    required_scope = str(coverage["complete_for"])
    accepted = admit_submit_answer(
        _submit(
            claims=[
                {
                    "text": "交易记录范围",
                    "kind": "current_fact",
                    "observation_ids": ["obv_a"],
                    "required_scope": required_scope,
                }
            ]
        ),
        {"obv_a": evidence},
    )

    assert accepted["observation"]["ok"] is True
    assert expected in accepted["approved_answer"]["text"]


def test_partial_banner_does_not_sum_duplicate_or_overlapping_evidence() -> None:
    duplicate = _evidence(observation_status="partial")
    accepted = admit_submit_answer(
        _submit(
            status="partial",
            claims=[
                {
                    "text": "同一范围被重复读取",
                    "kind": "current_fact",
                    "observation_ids": ["obv_a", "obv_b"],
                    "required_scope": "point",
                }
            ],
        ),
        {"obv_a": duplicate, "obv_b": dict(duplicate)},
    )

    text = accepted["approved_answer"]["text"]
    assert text.count("部分数据") == 1
    assert "已纳入 10 条" in text
    assert "总数 20" in text
    assert "遗漏 10" in text
    assert "已纳入 20 条" not in text
    assert "总数 40" not in text


def test_complete_answer_cannot_overstate_partial_observation() -> None:
    rejected = admit_submit_answer(
        _submit(),
        {"obv_a": _evidence(observation_status="partial")},
    )

    assert rejected["observation"]["reason"] == "answer_status_overstates_evidence"


def test_complete_judgment_cannot_overstate_unknown_freshness() -> None:
    claim = {
        "text": "时效性无法确认",
        "kind": "judgment",
        "observation_ids": ["obv_a"],
        "required_scope": "point",
    }
    rejected = admit_submit_answer(
        _submit(claims=[claim]),
        {"obv_a": _evidence(freshness_status="unknown")},
    )

    assert rejected["observation"]["reason"] == "answer_status_overstates_evidence"


def test_partial_point_banner_does_not_fabricate_zero_rows() -> None:
    evidence = _evidence(observation_status="partial")
    evidence["coverage"] = {
        "status": "complete",
        "complete_for": "point",
        "scope": {"account": "lx"},
    }
    accepted = admit_submit_answer(
        _submit(status="partial"),
        {"obv_a": evidence},
    )

    text = accepted["approved_answer"]["text"]
    assert "已纳入条数未知" in text
    assert "已纳入 0 条" not in text


def test_unknown_diagnostic_judgment_forces_completeness_banner() -> None:
    claim = {
        "text": "证据完整性无法确认",
        "kind": "judgment",
        "observation_ids": ["obv_a"],
        "required_scope": "point",
    }
    accepted = admit_submit_answer(
        _submit(status="insufficient_evidence", claims=[claim]),
        {
            "obv_a": _evidence(
                coverage_status="unknown",
                freshness_status="unknown",
            )
        },
    )

    text = accepted["approved_answer"]["text"]
    assert "证据不足" in text
    assert "完整性未知" in text


def test_needs_narrowing_requires_matching_status_and_fixed_banner() -> None:
    claim = {
        "text": "结果范围过大",
        "kind": "judgment",
        "observation_ids": ["obv_a"],
        "required_scope": "point",
    }
    evidence = _evidence(
        coverage_status="partial",
        freshness_status="unknown",
        observation_status="needs_narrowing",
    )
    rejected = admit_submit_answer(
        _submit(status="partial", claims=[claim]),
        {"obv_a": evidence},
    )
    accepted = admit_submit_answer(
        _submit(status="needs_narrowing", claims=[claim]),
        {"obv_a": evidence},
    )

    assert rejected["observation"]["reason"] == "narrowing_status_required"
    assert "请指定账户、时间、标的或结果范围" in accepted["approved_answer"]["text"]


def test_invalid_markdown_and_oversized_answer_are_rejected() -> None:
    malformed = admit_submit_answer(
        _submit(mode="conceptual", claims=[], text="```markdown\n未闭合"),
        {},
    )
    oversized = admit_submit_answer(
        _submit(mode="conceptual", claims=[], text="字" * 12_001),
        {},
    )

    assert malformed["observation"]["reason"] == "answer_markdown_invalid"
    assert oversized["observation"]["reason"] == "answer_schema_invalid"
