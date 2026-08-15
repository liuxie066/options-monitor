from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import src.application.strategy_lab.top1.research_runner as runner_module
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.lifecycle import (
    authorize_research,
    prepare_experiment,
    record_generation_revision,
    set_account_opt_in,
    start_research,
)
from src.application.strategy_lab.top1.research import evaluate_research
from src.application.strategy_lab.top1.research_runner import (
    ResearchRunnerError,
    run_research,
)
from src.application.strategy_lab.top1.terminal_projection import publish_exact_text
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.test_strategy_lab_top1_research import (
    AVAILABLE,
    SOURCE_SHA,
    _build_case,
    _fee_contract,
    _receipts,
    _set_variants,
)


NOW = "2026-08-15T12:00:00Z"


class FakeGateway:
    def __init__(
        self,
        *,
        remaining: int = 2,
        existing_codes: tuple[str, ...] = ("HK.0700",),
        close_value: float | None = 390.0,
        close_error: bool = False,
    ) -> None:
        self.remaining = remaining
        self.existing_codes = existing_codes
        self.close_value = close_value
        self.close_error = close_error
        self.quota_calls = 0
        self.close_calls: list[tuple[str, str]] = []

    def get_history_kl_quota(self) -> dict[str, Any]:
        self.quota_calls += 1
        return {
            "used_quota": len(self.existing_codes),
            "remain_quota": self.remaining,
            "detail_list": [
                {"code": code, "request_time": "2026-08-15 09:00:00"}
                for code in self.existing_codes
            ],
        }

    def get_exact_expiration_close(
        self, *, code: str, expiration: str
    ) -> dict[str, object] | None:
        self.close_calls.append((code, expiration))
        if self.close_error:
            raise RuntimeError("provider unavailable")
        if self.close_value is None:
            return None
        return {"code": code, "expiration": expiration, "close": self.close_value}


def _direct_limiter(*, call: Any, **_kwargs: Any) -> Any:
    return call()


def _publish_case(root: Path, case: dict[str, Any]) -> None:
    publish_exact_text(
        root,
        case["dataset_ref"],
        render_json_text(case["sealed_dataset"]).encode("utf-8"),
    )
    for item in case["ranking_projections"]:
        publish_exact_text(
            root,
            item["projection_ref"],
            render_json_text(item["projection"]).encode("utf-8"),
        )


def _prepared(
    tmp_path: Path, *, same_top1: bool = False
) -> tuple[ExperimentStore, Path, dict[str, Any], str]:
    case = _build_case(tmp_path / "source")
    if same_top1:
        _set_variants(case, (("same", "current_tie_break"),))
    root = tmp_path / "artifacts"
    _publish_case(root, case)
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    store.migrate(migrated_at_utc=NOW)
    set_account_opt_in(
        store,
        market="HK",
        account="lx",
        enabled=True,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="enable",
        artifact_root=root,
        environ=AVAILABLE,
    )
    prepared = prepare_experiment(
        store,
        case["experiment_spec"],
        provenance={"source_commit_sha": SOURCE_SHA},
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="prepare",
        artifact_root=root,
        environ=AVAILABLE,
    )
    research_hash = str(prepared["research_spec_sha256"])
    authorize_research(
        store,
        experiment_id=case["experiment_spec"]["experiment_id"],
        research_spec_sha256=research_hash,
        actor="human",
        occurred_at_utc=NOW,
        idempotency_key="authorize",
        artifact_root=root,
        environ=AVAILABLE,
    )
    return store, root, case, research_hash


def _run(
    store: ExperimentStore,
    root: Path,
    case: dict[str, Any],
    research_hash: str,
    gateway: FakeGateway,
    *,
    fee_contract: object | None = None,
    environ: dict[str, str] = AVAILABLE,
) -> dict[str, object]:
    return run_research(
        store,
        root,
        experiment_id=case["experiment_spec"]["experiment_id"],
        research_spec_sha256=research_hash,
        fee_contract=_fee_contract() if fee_contract is None else fee_contract,
        gateway=gateway,  # type: ignore[arg-type]
        config=None,
        actor="runner",
        occurred_at_utc=NOW,
        idempotency_key="run",
        environ=environ,
    )


def test_runner_deduplicates_closes_and_recovers_terminal_without_provider_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    gateway = FakeGateway()
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)
    recover = runner_module.recover_terminal_projection
    monkeypatch.setattr(
        runner_module,
        "recover_terminal_projection",
        lambda *_args, **_kwargs: {"recovered": 0, "pending": 1},
    )

    with pytest.raises(ResearchRunnerError) as exc_info:
        _run(store, root, case, research_hash, gateway)
    assert exc_info.value.reason_code == "research_terminal_pending"
    assert gateway.quota_calls == 1
    assert gateway.close_calls == sorted(set(gateway.close_calls))
    assert {code for code, _expiration in gateway.close_calls} == {
        "HK.0700",
        "HK.3690",
        "HK.9988",
    }
    first_counts = (gateway.quota_calls, len(gateway.close_calls))
    generation = store.generations(case["experiment_spec"]["experiment_id"])[0]
    assert generation["revision"] == 1
    assert generation["terminal_published_event_id"] is None

    monkeypatch.setattr(runner_module, "recover_terminal_projection", recover)
    result = _run(store, root, case, research_hash, gateway)

    assert result["selection"] == "research_leader"
    assert (gateway.quota_calls, len(gateway.close_calls)) == first_counts
    generation = store.generations(case["experiment_spec"]["experiment_id"])[0]
    assert generation["terminal_published_event_id"] is not None
    assert store.experiment(case["experiment_spec"]["experiment_id"])[
        "research_progress"
    ] == "ready_to_compare"


def test_runner_skips_provider_when_top1_does_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, case, research_hash = _prepared(tmp_path, same_top1=True)
    gateway = FakeGateway(remaining=0, existing_codes=())
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)

    result = _run(
        store,
        root,
        case,
        research_hash,
        gateway,
        fee_contract=_fee_contract(complete=False),
    )

    assert result["selection"] == "no_research_winner"
    assert gateway.quota_calls == 0
    assert gateway.close_calls == []


@pytest.mark.parametrize("tampered_bytes", [b"\xff", b'{"value": 1e999}\n'])
def test_runner_rejects_tampered_artifact_before_state_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tampered_bytes: bytes
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    dataset_path = root.joinpath(*case["dataset_ref"].split("/"))
    dataset_path.write_bytes(tampered_bytes)
    gateway = FakeGateway()
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)

    with pytest.raises(ResearchRunnerError) as exc_info:
        _run(store, root, case, research_hash, gateway)

    assert exc_info.value.reason_code == "research_artifact_invalid"
    assert store.generations(case["experiment_spec"]["experiment_id"]) == []
    assert gateway.quota_calls == 0
    assert gateway.close_calls == []


def test_runner_rejects_malformed_fee_before_state_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    fee_contract = _fee_contract()
    fee_contract["account_fee_plan"]["unexpected"] = True
    gateway = FakeGateway()
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)

    with pytest.raises(ResearchRunnerError) as exc_info:
        _run(
            store,
            root,
            case,
            research_hash,
            gateway,
            fee_contract=fee_contract,
        )

    assert exc_info.value.reason_code == "research_input_invalid"
    assert store.generations(case["experiment_spec"]["experiment_id"]) == []
    assert gateway.quota_calls == 0
    assert gateway.close_calls == []


@pytest.mark.parametrize(
    ("mode", "expected_reason", "expected_revision"),
    [
        ("quota", "research_history_quota_insufficient", 0),
        ("provider", "research_expiry_close_unavailable", 0),
        ("missing", None, 1),
    ],
)
def test_runner_fails_closed_for_quota_provider_and_missing_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_reason: str | None,
    expected_revision: int,
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    gateway = FakeGateway(
        remaining=0 if mode == "quota" else 2,
        close_error=mode == "provider",
        close_value=None if mode == "missing" else 390.0,
    )
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)

    if expected_reason is not None:
        with pytest.raises(ResearchRunnerError) as exc_info:
            _run(store, root, case, research_hash, gateway)
        assert exc_info.value.reason_code == expected_reason
    else:
        result = _run(store, root, case, research_hash, gateway)
        assert result["selection"] == "insufficient_evidence"
        assert result["reason_details"] == ["expiry_close_missing_after_deadline"]
    generation = store.generations(case["experiment_spec"]["experiment_id"])[0]
    assert generation["revision"] == expected_revision
    if mode == "quota":
        assert gateway.close_calls == []


def test_runner_checks_feature_gate_before_provider_on_open_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    start_research(
        store,
        experiment_id=case["experiment_spec"]["experiment_id"],
        research_spec_sha256=research_hash,
        actor="runner",
        occurred_at_utc=NOW,
        idempotency_key="manual-start",
        artifact_root=root,
        environ=AVAILABLE,
    )
    gateway = FakeGateway()
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)

    with pytest.raises(ResearchRunnerError) as exc_info:
        _run(store, root, case, research_hash, gateway, environ={})

    assert exc_info.value.reason_code == "feature_disabled"
    assert gateway.quota_calls == 0
    assert gateway.close_calls == []
    assert store.generations(case["experiment_spec"]["experiment_id"])[0][
        "revision"
    ] == 0


def test_runner_checks_feature_gate_before_completed_revision_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    gateway = FakeGateway()
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)
    _run(store, root, case, research_hash, gateway)
    first_counts = (gateway.quota_calls, len(gateway.close_calls))
    generation = store.generations(case["experiment_spec"]["experiment_id"])[0]
    published_event_id = generation["terminal_published_event_id"]

    with pytest.raises(ResearchRunnerError) as exc_info:
        _run(store, root, case, research_hash, gateway, environ={})

    assert exc_info.value.reason_code == "feature_disabled"
    assert (gateway.quota_calls, len(gateway.close_calls)) == first_counts
    generation = store.generations(case["experiment_spec"]["experiment_id"])[0]
    assert generation["terminal_published_event_id"] == published_event_id


def test_runner_rejects_hash_valid_foreign_revision_without_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root, case, research_hash = _prepared(tmp_path)
    experiment_id = case["experiment_spec"]["experiment_id"]
    start_research(
        store,
        experiment_id=experiment_id,
        research_spec_sha256=research_hash,
        actor="runner",
        occurred_at_utc=NOW,
        idempotency_key="manual-start",
        artifact_root=root,
        environ=AVAILABLE,
    )
    generation = store.generations(experiment_id)[0]
    foreign = evaluate_research(case, _receipts(), _fee_contract())
    foreign["experiment_id"] = "another-experiment"
    ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/generations/"
        "research.revision.1.json"
    )
    content = render_json_text(foreign).encode("utf-8")
    publish_exact_text(root, ref, content)
    record_generation_revision(
        store,
        experiment_id=experiment_id,
        generation_kind="research",
        revision=1,
        revision_ref=ref,
        revision_file_sha256=hashlib.sha256(content).hexdigest(),
        frozen_row_sha256=str(generation["frozen_row_content_sha256"]),
        actor="runner",
        occurred_at_utc=NOW,
        idempotency_key="foreign-revision",
        artifact_root=root,
        environ=AVAILABLE,
    )
    gateway = FakeGateway()
    monkeypatch.setattr(runner_module, "rate_limited_opend_call", _direct_limiter)

    with pytest.raises(ResearchRunnerError) as exc_info:
        _run(store, root, case, research_hash, gateway)

    assert exc_info.value.reason_code == "research_revision_conflict"
    assert gateway.quota_calls == 0
    assert gateway.close_calls == []
    generation = store.generations(experiment_id)[0]
    assert generation["terminal_request_event_id"] is None
