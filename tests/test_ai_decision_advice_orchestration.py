from __future__ import annotations

import json
from datetime import datetime, timezone

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ai_decision_advice.collector import ModelCallResult
from src.application.ai_decision_advice.evidence_store import (
    EvidenceIndex,
    SymbolEvidenceView,
)
from src.application.ai_decision_advice.orchestration import (
    _build_model_runner,
    run_or_reuse_ai_decision_advice,
)
from src.application.prepared_portfolio_distribution import (
    PreparedPortfolioDistribution,
)


NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
CONFIG_HASH = "a" * 64


def _snapshot(
    *,
    run_id: str = "run-1",
    account: str = "lx",
    with_candidate: bool = True,
) -> dict:
    dependencies = [
        {"kind": kind, "relpath": None, "sha256": character * 64}
        for kind, character in (
            ("required_data", "1"),
            ("portfolio", "2"),
            ("ledger", "3"),
            ("fx", "4"),
            ("earnings_rv", "5"),
        )
    ]
    ranked = (
        [
            {
                "candidate_id": "put-1",
                "rank": 1,
                "strategy_mode": "put",
                "facts": {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "strike": 100,
                    "expiration": "2026-09-18",
                    "multiplier": 100,
                    "currency": "USD",
                    "dte": 40,
                    "delta": -0.2,
                    "period_net_return_on_cash_basis": 0.03,
                    "annualized_net_return_on_cash_basis": 0.27,
                },
            }
        ]
        if with_candidate
        else []
    )
    payload = {
        "schema_version": "opening_candidate_snapshot.v1",
        "run_id": run_id,
        "account": account,
        "futu_account_id": "123456",
        "trade_env": "REAL",
        "market": "US",
        "strategy_modes": ["put", "call"],
        "account_config_sha256": CONFIG_HASH,
        "strategy_policy_sha256": "b" * 64,
        "required_data_manifest_sha256": "1" * 64,
        "dependencies": dependencies,
        "sealed_at_utc": NOW.isoformat(),
        "opening_status": (
            "candidates_found" if with_candidate else "no_candidate"
        ),
        "strategy_results": [
            {
                "strategy_mode": "put",
                "strategy_status": (
                    "candidates_found" if with_candidate else "no_candidate"
                ),
            },
            {"strategy_mode": "call", "strategy_status": "no_candidate"},
        ],
        "scope_results": [],
        "candidate_decisions": (
            [{"candidate_id": "put-1"}] if with_candidate else []
        ),
        "ranked_candidates": ranked,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def _rehash_snapshot(snapshot: dict) -> dict:
    snapshot["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in snapshot.items()
            if key != "content_sha256"
        }
    )
    return snapshot


def _portfolio(
    *,
    run_id: str = "run-1",
    account: str = "lx",
) -> PreparedPortfolioDistribution:
    return PreparedPortfolioDistribution(
        envelope={
            "authority": {
                "run_id": run_id,
                "account": account,
                "account_config_sha256": CONFIG_HASH,
                "status": "ready",
                "reason": "portfolio_ready",
            },
            "payload": {
                "observed_at_utc": NOW.isoformat(),
                "freshness_status": "fresh",
                "trust_status": "trusted",
                "assets": [
                    {
                        "code": "NVDA",
                        "normalized_type": "stock",
                        "currency": "USD",
                        "quantity": 100,
                        "value": 700_000,
                    }
                ],
                "derived": {
                    "total_value": 700_000,
                    "currency_weights": {"USD": 1.0},
                    "cash_and_mmf_weight": 0.0,
                },
            },
            "integrity": {},
        },
        artifact_path=None,
        artifact_sha256=None,
    )


def _option_context(
    *,
    run_id: str = "run-1",
    account: str = "lx",
) -> dict:
    return {
        "context_status": "available",
        "decision_snapshot_status": "trusted",
        "filters": {"account": account, "broker": "futu"},
        "prepared_authority": {
            "run_id": run_id,
            "account": account,
            "account_config_sha256": CONFIG_HASH,
            "source_observed_at": NOW.isoformat(),
            "fx_status": "ready",
        },
        "exchange_rates": {"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}},
        "open_positions_min": [],
        "decision_state_snapshot": {
            "account_combo_identities": [],
            "account_combo_group_memberships": [],
        },
    }


def _inputs(*, run_id: str = "run-1", with_candidate: bool = True) -> dict:
    return {
        "candidate_snapshot": _snapshot(
            run_id=run_id,
            with_candidate=with_candidate,
        ),
        "portfolio_distribution": _portfolio(run_id=run_id),
        "option_positions_context": _option_context(run_id=run_id),
    }


def _runner(output: dict):
    def run(instructions, payload, schema, timeout):
        return ModelCallResult(
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
            response_sha256="c" * 64,
        )

    return run


def _model_output(payload_bindings: dict, *, run_id: str, account_ref: str) -> dict:
    return {
        "schema": "ai_decision_advice.v1",
        "run_id": run_id,
        "account_ref": account_ref,
        "market": "US",
        "input_bindings": payload_bindings,
        "strategies": [
            {
                "strategy_family": "sell_put",
                "status": "completed",
                "decisions": [
                    {
                        "scope_symbol": None,
                        "baseline_candidate_id": "put-1",
                        "action": "defer",
                        "selected_candidate_id": None,
                        "rationale": {
                            "risk_mechanism": "m",
                            "candidate_effect": "e",
                            "decision_reason": "r",
                        },
                        "internal_fact_refs": [],
                        "external_evidence_refs": [],
                    }
                ],
            }
        ],
    }


def test_disabled_config_is_not_applicable_without_model(tmp_path):
    calls = []

    def runner(*args):  # pragma: no cover
        calls.append(args)

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": False}},
        **_inputs(),
        model_runner=runner,
        now=NOW,
    )
    assert view["status"] == "not_applicable"
    assert calls == []


def test_provider_runner_does_not_retain_raw_response(monkeypatch):
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"safe":true}'}],
            }
        ],
        "usage": {"total_tokens": 7, "debug": "must-not-persist"},
        "provider_private": "must-not-persist",
    }
    monkeypatch.setattr(
        "src.application.ai_decision_advice.orchestration.create_deepseek_response",
        lambda **kwargs: response,
    )

    result = _build_model_runner("not-a-real-key")("instructions", {}, None, 1)

    assert result.output_text == '{"safe":true}'
    assert result.usage == {"total_tokens": 7}
    assert len(result.response_sha256 or "") == 64
    assert not hasattr(result, "raw_response")
    assert "must-not-persist" not in repr(result)


def test_missing_candidate_snapshot_is_unavailable(tmp_path):
    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        candidate_snapshot=None,
        portfolio_distribution=_portfolio(),
        option_positions_context=_option_context(),
        model_runner=_runner({}),
        now=NOW,
    )
    assert view["status"] == "unavailable"
    assert view["unavailable_reason"] == "candidate_snapshot_missing"


def test_completed_run_flows_through_to_brief_view(tmp_path):
    # Evidence for NVDA is not present, so keep would demote; the model
    # outputs defer, which validation accepts (defer does not require
    # completed evidence).
    captured: dict = {}

    def runner(instructions, payload, schema, timeout):
        captured["payload"] = payload
        output = _model_output(
            payload["input_bindings"],
            run_id=payload["run_id"],
            account_ref=payload["account_ref"],
        )
        return ModelCallResult(
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
            response_sha256="c" * 64,
        )

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        **_inputs(),
        model_runner=runner,
        now=NOW,
    )
    assert view["status"] == "completed"
    assert view["evidence_as_of"] is None
    assert view["sell_put"]["action"] == "needs_review"
    assert view["zero_candidate"] == {"sell_put": False, "covered_call": True}
    assert view["covered_call"] is None
    # Frozen evidence index travels with the view for receipt source rendering.
    index = view["evidence_index"]
    assert index["frozen_at"] == NOW.isoformat()
    assert [
        {
            "symbol": item["symbol"],
            "coverage": item["coverage"],
            "evidence": item["evidence"],
        }
        for item in index["symbols"]
    ] == [{"symbol": "NVDA", "coverage": "no_evidence", "evidence": []}]
    # Privacy: no account label in model input; no NAV/totals keys.
    payload_without_anonymous_ref = dict(captured["payload"])
    anonymous_ref = payload_without_anonymous_ref.pop("account_ref")
    assert isinstance(anonymous_ref, str) and anonymous_ref
    text = json.dumps(payload_without_anonymous_ref, ensure_ascii=False)
    assert '"lx"' not in text
    assert "total_value" not in text
    assert '"quantity"' not in text
    assert '"shares"' not in text


def test_second_run_reuses_without_model_call(tmp_path):
    def runner(instructions, payload, schema, timeout):
        output = _model_output(
            payload["input_bindings"],
            run_id=payload["run_id"],
            account_ref=payload["account_ref"],
        )
        return ModelCallResult(
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
            response_sha256="c" * 64,
        )

    kwargs = dict(
        base=tmp_path,
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        model_runner=runner,
        now=NOW,
    )
    first = run_or_reuse_ai_decision_advice(
        run_id="run-1",
        **_inputs(run_id="run-1"),
        **kwargs,
    )
    assert first["status"] == "completed"

    def forbidden(*args):  # pragma: no cover
        raise AssertionError("reuse must not call the model")

    second = run_or_reuse_ai_decision_advice(
        **{**kwargs, "model_runner": forbidden},
        run_id="run-2",
        **_inputs(run_id="run-2"),
    )
    assert second["status"] == "completed"
    assert second["reused"] is True
    assert second["sell_put"]["action"] == "needs_review"


def test_model_failure_degrades_to_unavailable_without_raising(tmp_path):
    def runner(instructions, payload, schema, timeout):
        raise TimeoutError("slow")

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        **_inputs(),
        model_runner=runner,
        now=NOW,
    )
    assert view["status"] == "unavailable"
    assert "provider_error" in (view["unavailable_reason"] or "")


def test_valid_zero_candidate_does_not_call_model(tmp_path):
    def forbidden(*_args):  # pragma: no cover
        raise AssertionError("zero candidates must not call the model")

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        **_inputs(with_candidate=False),
        model_runner=forbidden,
        now=NOW,
    )

    assert view["status"] == "not_applicable"
    assert view["unavailable_reason"] == "zero_candidate"
    assert view["zero_candidate"] == {
        "sell_put": True,
        "covered_call": True,
    }


def test_market_closed_empty_snapshot_is_not_a_legal_zero_candidate(tmp_path):
    snapshot = _snapshot(with_candidate=False)
    snapshot["opening_status"] = "market_closed"
    snapshot["strategy_results"] = [
        {"strategy_mode": "put", "strategy_status": "data_unavailable"},
        {"strategy_mode": "call", "strategy_status": "not_applicable"},
    ]
    _rehash_snapshot(snapshot)

    def forbidden(*_args):  # pragma: no cover
        raise AssertionError("unavailable candidates must not call the model")

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        candidate_snapshot=snapshot,
        portfolio_distribution=_portfolio(),
        option_positions_context=_option_context(),
        model_runner=forbidden,
        now=NOW,
    )

    assert view["status"] == "unavailable"
    assert view["unavailable_reason"] == "advice_input_unavailable"


def test_data_unavailable_family_is_not_a_legal_zero_candidate(tmp_path):
    snapshot = _snapshot(with_candidate=False)
    snapshot["opening_status"] = "partial_data"
    snapshot["strategy_results"][0]["strategy_status"] = "data_unavailable"
    _rehash_snapshot(snapshot)

    def forbidden(*_args):  # pragma: no cover
        raise AssertionError("unavailable candidates must not call the model")

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        candidate_snapshot=snapshot,
        portfolio_distribution=_portfolio(),
        option_positions_context=_option_context(),
        model_runner=forbidden,
        now=NOW,
    )

    assert view["status"] == "unavailable"
    assert view["unavailable_reason"] == "advice_input_unavailable"


def test_explicit_inputs_ignore_legacy_context_artifacts(tmp_path):
    legacy = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "state"
    legacy.mkdir(parents=True)
    (legacy / "portfolio_context.json").write_text(
        '{"stocks_by_symbol":{"PRIVATE":{"shares":999}}}',
        encoding="utf-8",
    )
    (legacy / "option_positions_context.json").write_text(
        '{"open_positions":[{"symbol":"PRIVATE"}]}',
        encoding="utf-8",
    )
    captured: dict = {}

    def runner(instructions, payload, schema, timeout):
        captured["payload"] = payload
        output = _model_output(
            payload["input_bindings"],
            run_id=payload["run_id"],
            account_ref=payload["account_ref"],
        )
        return ModelCallResult(
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
            response_sha256="c" * 64,
        )

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        **_inputs(),
        model_runner=runner,
        now=NOW,
    )

    assert view["status"] == "completed"
    assert "PRIVATE" not in json.dumps(captured["payload"])


def test_evidence_is_frozen_before_model_call(monkeypatch, tmp_path):
    evidence_success_at = "2026-08-09T06:00:00+00:00"
    index = EvidenceIndex(
        frozen_at=NOW.isoformat(),
        views={
            "NVDA": SymbolEvidenceView(
                symbol="NVDA",
                coverage="completed",
                last_checked_at=NOW.isoformat(),
                last_success_at=evidence_success_at,
                evidence=(
                    {
                        "content_fingerprint": "e" * 64,
                        "topic": "regulatory",
                        "claim": "frozen claim",
                        "event_status": "developing",
                        "source": {
                            "title": "Source",
                            "publisher": "Publisher",
                            "url": "https://example.com/evidence",
                        },
                    },
                ),
            )
        },
    )
    monkeypatch.setattr(
        "src.application.ai_decision_advice.orchestration.freeze_evidence_index",
        lambda *_args, **_kwargs: index,
    )
    captured: dict = {}

    def runner(instructions, payload, schema, timeout):
        index.views["NVDA"] = SymbolEvidenceView(
            symbol="NVDA",
            coverage="completed",
            evidence=({"claim": "late mutation"},),
        )
        captured["payload"] = payload
        output = _model_output(
            payload["input_bindings"],
            run_id=payload["run_id"],
            account_ref=payload["account_ref"],
        )
        return ModelCallResult(
            output_text=json.dumps(output, ensure_ascii=False),
            usage={},
            response_sha256="c" * 64,
        )

    view = run_or_reuse_ai_decision_advice(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        config={"ai_decision_advice": {"enabled": True}},
        **_inputs(),
        model_runner=runner,
        now=NOW,
    )

    assert view["status"] == "completed"
    assert view["evidence_as_of"] == evidence_success_at
    evidence = captured["payload"]["external_evidence"]["symbols"][0]
    assert evidence["evidence"][0]["claim"] == "frozen claim"
