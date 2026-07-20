from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _action(
    *,
    account: str = "lx",
    symbol: str = "NVDA",
    priority: str = "P1",
    mid: float = 1.0,
    contracts_available: int | None = 1,
) -> dict:
    return {
        "priority": priority,
        "state": "active",
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "account": account,
        "symbol": symbol,
        "option_type": "put",
        "side": "short",
        "expiration": "2026-08-21",
        "strike": 100,
        "contract_symbol": f"{symbol}260821P00100000",
        "metrics": {
            "mid": mid,
            **(
                {"capacity": {"contracts_available": contracts_available}}
                if contracts_available is not None
                else {}
            ),
        },
    }


def _brief(
    *,
    run_id: str,
    account: str = "lx",
    market: str = "US",
    market_date: str = "2026-07-17",
    actions: list[dict] | None = None,
    put_contracts: int = 1,
) -> dict:
    return {
        "market": market,
        "market_trading_date": market_date,
        "account": account,
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-17T13:40:00+00:00",
        "data_as_of_utc": "2026-07-17T13:39:00+00:00",
        "valid_until_utc": "2026-07-17T20:00:00+00:00",
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": "test",
        "actions": list(actions or []),
        "positions": [],
        "capacity": {
            "sell_put": {"contracts_available": put_contracts},
            "covered_call": {"contracts_available": 0},
        },
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def _confirm(tmp_path: Path, lifecycle: dict) -> dict:
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery

    brief = lifecycle["brief"]
    return confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market=brief["market"],
        market_trading_date=brief["market_trading_date"],
        account=brief["account"],
        revision=brief["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
        confirmed_at_utc=datetime(2026, 7, 17, 13, 41, tzinfo=timezone.utc),
    )


def test_prepare_isolates_account_and_market_and_preserves_shared_index(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    us_lx = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-shared"))
    hk_lx = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-shared", market="HK", actions=[_action(symbol="0700.HK")]),
    )
    us_sy = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-us-sy", account="sy", actions=[_action(account="sy", symbol="PDD")]),
    )

    assert us_lx["current_revision"] == hk_lx["current_revision"] == us_sy["current_revision"] == 0
    assert us_lx["paths"]["current"] != hk_lx["paths"]["current"]
    assert us_lx["paths"]["run_brief"] != hk_lx["paths"]["run_brief"]
    assert us_lx["paths"]["run_diff"] != hk_lx["paths"]["run_diff"]
    assert us_lx["paths"]["run_brief"].name == "daily_decision_brief.US.json"
    assert hk_lx["paths"]["run_brief"].name == "daily_decision_brief.HK.json"
    assert _confirm(tmp_path, us_lx)["pointer"]["market"] == "US"
    assert _confirm(tmp_path, hk_lx)["pointer"]["market"] == "HK"
    index = json.loads(us_sy["paths"]["shared_index"].read_text(encoding="utf-8"))
    assert set(index["items"]) == {"US/lx", "HK/lx", "US/sy"}


def test_same_day_revision_is_monotonic_and_new_day_resets_to_zero(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    first = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-0"))
    second = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-1"))
    next_day = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-next", market_date="2026-07-20"),
    )

    assert first["current_revision"] == 0
    assert second["current_revision"] == 1
    assert next_day["current_revision"] == 0
    assert next_day["paths"]["revision"].name.endswith("2026-07-20.r0000.json")


@pytest.mark.parametrize("seed_current", [False, True])
def test_prepare_advances_past_orphan_revision_after_interrupted_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_current: bool,
) -> None:
    import src.application.daily_decision_brief_repository as repository

    if seed_current:
        repository.prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-seed"))

    original_write = repository.atomic_write_json
    write_count = 0

    def interrupt_after_revision(path: Path, payload: object, **kwargs: object) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise RuntimeError("injected interruption after immutable revision write")
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(repository, "atomic_write_json", interrupt_after_revision)
    with pytest.raises(RuntimeError, match="injected interruption"):
        repository.prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-interrupted"))
    monkeypatch.setattr(repository, "atomic_write_json", original_write)

    recovered = repository.prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-recovered"))

    orphan_revision = 1 if seed_current else 0
    expected_revision = orphan_revision + 1
    assert recovered["current_revision"] == expected_revision
    assert recovered["paths"]["revision"].name.endswith(f".r{expected_revision:04d}.json")
    listed = repository.list_daily_decision_brief_revisions(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
    )
    assert listed["revisions"] == list(range(expected_revision + 1))


def test_full_not_delivered_reuses_key_for_same_semantic_content(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    first_source = _brief(run_id="run-0", actions=[_action(mid=1.0)])
    first_source["actions"][0].update(
        {"title": "first title", "reason": "first reason", "source": {"path": "run-0.csv"}}
    )
    first_source["strategy_summary"] = "first summary"
    first_source["source_artifacts"] = [{"kind": "candidate", "path": "run-0.csv"}]

    retry_source = deepcopy(first_source)
    retry_source.update(
        {
            "run_id": "run-1",
            "generated_at_utc": "2026-07-17T13:50:00+00:00",
            "data_as_of_utc": "2026-07-17T13:49:00+00:00",
            "strategy_summary": "rewritten summary",
            "source_artifacts": [{"kind": "candidate", "path": "run-1.csv"}],
        }
    )
    retry_source["actions"][0].update(
        {"title": "rewritten title", "reason": "rewritten reason", "source": {"path": "run-1.csv"}}
    )

    first = prepare_daily_decision_brief(base=tmp_path, brief=first_source)
    retry = prepare_daily_decision_brief(base=tmp_path, brief=retry_source)

    assert first["delivery_kind"] == retry["delivery_kind"] == "full"
    assert first["delivery_key"] == retry["delivery_key"]
    assert first["delivery_key"].startswith("daily-brief:US:2026-07-17:lx:full:")
    persisted_diff = json.loads(retry["paths"]["run_diff"].read_text(encoding="utf-8"))
    assert persisted_diff["full_semantic_digest"] == retry["delivery_key"].rsplit(":", 1)[-1]


def test_full_not_delivered_uses_new_key_for_latest_changed_content(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        confirm_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    first = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-0", actions=[_action(mid=1.0)]),
    )
    second = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-1", actions=[_action(mid=1.2)]),
    )

    assert first["delivery_kind"] == second["delivery_kind"] == "full"
    assert first["delivery_key"] != second["delivery_key"]
    assert second["delivery_key"].startswith("daily-brief:US:2026-07-17:lx:full:")
    assert second["current_revision"] == 1
    assert second["last_delivered_revision"] is None

    with pytest.raises(DailyDecisionBriefStateError, match="envelope does not match"):
        confirm_daily_decision_brief_delivery(
            base=tmp_path,
            market="US",
            market_trading_date="2026-07-17",
            account="lx",
            revision=1,
            delivery_kind="full",
            delivery_key=first["delivery_key"],
            brief_digest=second["current_brief_digest"],
        )

    confirmed = _confirm(tmp_path, second)
    assert confirmed["advanced"] is True
    assert confirmed["pointer"]["revision"] == 1


def test_full_confirmation_rejects_tampered_semantic_digest(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import DailyDecisionBriefStateError, prepare_daily_decision_brief

    lifecycle = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-0", actions=[_action(mid=1.0)]),
    )
    persisted_diff = json.loads(lifecycle["paths"]["run_diff"].read_text(encoding="utf-8"))
    persisted_diff["full_semantic_digest"] = "0" * 64
    lifecycle["paths"]["run_diff"].write_text(
        json.dumps(persisted_diff, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(DailyDecisionBriefStateError, match="semantic digest mismatch"):
        _confirm(tmp_path, lifecycle)


def test_delta_is_computed_against_last_delivered_not_previous_revision(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    delivered = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-0", actions=[_action(priority="P1")]),
    )
    _confirm(tmp_path, delivered)

    quiet = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-1", actions=[_action(priority="P1", mid=1.1)]),
    )
    assert quiet["delivery_kind"] == "none"

    changed = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-2", actions=[_action(priority="P0", mid=1.2)]),
    )
    assert changed["delivery_kind"] == "delta"
    assert changed["last_delivered_revision"] == 0
    assert changed["diff"]["from_revision"] == 0
    assert changed["diff"]["to_revision"] == 2
    assert "candidate_priority_upgraded_to_p0" in {item["change_type"] for item in changed["diff"]["changes"]}


def test_failed_delta_does_not_advance_pointer_and_is_not_lost(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief, read_daily_decision_brief_delivery

    delivered = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-0", put_contracts=1, actions=[_action(contracts_available=1)]),
    )
    _confirm(tmp_path, delivered)

    failed = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-1", put_contracts=2, actions=[_action(contracts_available=2)]),
    )
    retry = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-2", put_contracts=2, actions=[_action(contracts_available=2)]),
    )

    assert failed["delivery_kind"] == retry["delivery_kind"] == "delta"
    assert failed["delivery_key"] == retry["delivery_key"]
    assert retry["diff"]["from_revision"] == 0
    pointer = read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")
    assert pointer["pointer"]["revision"] == 0


def test_stale_delivery_completion_cannot_regress_pointer(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import confirm_daily_decision_brief_delivery, prepare_daily_decision_brief

    first = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-0", put_contracts=1, actions=[_action(contracts_available=1)]),
    )
    _confirm(tmp_path, first)
    second = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="run-1", put_contracts=2, actions=[_action(contracts_available=2)]),
    )
    _confirm(tmp_path, second)

    stale = confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date="2026-07-17",
        account="lx",
        revision=0,
        delivery_kind="full",
        delivery_key=first["delivery_key"],
        brief_digest=first["current_brief_digest"],
    )
    assert stale["advanced"] is False
    assert stale["reason"] == "stale_completion"
    assert stale["pointer"]["revision"] == 1


def test_confirmation_rejects_delivery_envelope_that_was_not_prepared(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        confirm_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    lifecycle = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-0"))

    with pytest.raises(DailyDecisionBriefStateError, match="does not match prepared lifecycle"):
        confirm_daily_decision_brief_delivery(
            base=tmp_path,
            market="US",
            market_trading_date="2026-07-17",
            account="lx",
            revision=0,
            delivery_kind="delta",
            delivery_key="wrong-key",
            brief_digest=lifecycle["current_brief_digest"],
        )


def test_read_apis_list_and_load_latest_day_and_delivery(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        list_daily_decision_brief_revisions,
        prepare_daily_decision_brief,
        read_daily_decision_brief,
        read_daily_decision_brief_delivery,
        read_latest_daily_decision_brief,
    )

    first = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-0"))
    prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-1"))
    _confirm(tmp_path, first)

    assert list_daily_decision_brief_revisions(
        base=tmp_path, account="lx", market="US", market_trading_date="2026-07-17"
    )["revisions"] == [0, 1]
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"]["revision"] == 1
    assert read_daily_decision_brief(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
        revision=0,
    )["brief"]["run_id"] == "run-0"
    assert read_daily_decision_brief(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-17",
    )["brief"]["revision"] == 1
    assert read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")["pointer"]["revision"] == 0


def test_malformed_or_legacy_state_fails_closed(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        prepare_daily_decision_brief,
        read_latest_daily_decision_brief,
    )

    state_dir = tmp_path / "output_accounts" / "lx" / "state"
    state_dir.mkdir(parents=True)
    current = state_dir / "daily_decision_brief.US.current.json"
    current.write_text('{"legacy": true}', encoding="utf-8")

    read = read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")
    assert read["available"] is False
    assert read["reason"] == "state_invalid"
    with pytest.raises(DailyDecisionBriefStateError, match="incompatible"):
        prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-new"))


def test_malformed_delivery_pointer_returns_structured_unavailable(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery

    state_dir = tmp_path / "output_accounts" / "lx" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "daily_decision_brief.US.delivery.json").write_text(
        json.dumps(
            {
                "schema_version": "daily_decision_brief_delivery.v1",
                "market": "US",
                "market_trading_date": "2026-07-17",
                "account": "lx",
                "revision": "bad",
                "brief_digest": "digest",
            }
        ),
        encoding="utf-8",
    )

    out = read_daily_decision_brief_delivery(base=tmp_path, account="lx", market="US")
    assert out["available"] is False
    assert out["reason"] == "state_invalid"


def test_orphan_current_revision_fails_closed_before_allocation(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        prepare_daily_decision_brief,
        read_latest_daily_decision_brief,
    )

    lifecycle = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-0"))
    lifecycle["paths"]["revision"].unlink()

    latest = read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")
    assert latest["available"] is False
    assert latest["reason"] == "state_invalid"

    with pytest.raises(DailyDecisionBriefStateError, match="references a missing revision"):
        prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="run-1"))


def test_no_material_revision_is_persisted_with_run_scoped_diff(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief

    source = _brief(run_id="run-0", actions=[_action(mid=1.0)])
    first = prepare_daily_decision_brief(base=tmp_path, brief=source)
    _confirm(tmp_path, first)

    updated = deepcopy(source)
    updated["run_id"] = "run-1"
    updated["actions"][0]["metrics"]["mid"] = 1.05
    lifecycle = prepare_daily_decision_brief(base=tmp_path, brief=updated)

    assert lifecycle["delivery_kind"] == "none"
    assert lifecycle["paths"]["revision"].exists()
    persisted_diff = json.loads(lifecycle["paths"]["run_diff"].read_text(encoding="utf-8"))
    assert persisted_diff["material"] is False
    assert persisted_diff["from_revision"] == 0
    assert persisted_diff["to_revision"] == 1
