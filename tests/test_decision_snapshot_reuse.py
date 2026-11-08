from copy import deepcopy
from unittest.mock import patch

import pytest

from src.application.ledger import api as ledger_api, decision_snapshot as mod
from src.application.ledger.manual_trades import (
    persist_manual_adjust_event,
    persist_manual_open_event,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


OBSERVED = "2026-08-10T03:00:00+00:00"
SCOPES = {"lx": "scope:lx", "sy": "scope:sy"}


def _rows(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with patch("src.application.ledger.writer_trade_events.utc_now_ms", return_value=1_784_000_000_000):
        for index, account in enumerate(SCOPES):
            persist_manual_open_event(
                repo, broker="futu", account=account, symbol="NVDA", option_type="put",
                side="short", contracts=index + 1, currency="USD", strike=100,
                multiplier=100, expiration_ymd="2099-09-18", premium_per_share=2,
                opened_at_ms=1000 + index, request_id=f"fixture-{account}",
            )
    return mod.read_decision_state_rows_many(repo, accounts=list(SCOPES))


def _independent(rows_by_account, **kwargs):
    return {
        account: mod.decision_state_snapshot_from_rows(
            rows, account=account, portfolio_scope_id=SCOPES[account],
            source_observed_at=OBSERVED,
            current_projection=kwargs.get("current_projections", {}).get(account),
            current_decision_now_ms=kwargs.get("current_decision_now_ms"),
        ) for account, rows in rows_by_account.items()
    }


def _many(rows, **kwargs):
    return mod.decision_state_snapshots_from_rows_many(
        rows, portfolio_scope_ids=SCOPES, source_observed_at=OBSERVED, **kwargs,
    )


def test_shared_global_reduction_preserves_complete_snapshots_and_hashes(tmp_path, monkeypatch):
    rows = _rows(tmp_path)
    frozen = deepcopy(rows)
    expected = _independent(rows)
    calls = {"project": 0, "compare": 0, "hash": 0, "account_hash": 0}
    for name, key in (("project_stored_trade_events_to_position_lots", "project"),
                      ("compare_projection_lots", "compare"),
                      ("canonical_sha256", "hash"),
                      ("decision_state_snapshot_fingerprint", "account_hash")):
        original = getattr(mod, name)

        def counted(*args, _original=original, _key=key, **kwargs):
            calls[_key] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(mod, name, counted)
    actual = _many(rows)
    assert actual == expected
    # Complete serialized snapshot hashes for the event-time cash FX policy.
    # Compared with the prior daily-policy fixture, only conversion policy
    # metadata and its derived fingerprints change; economic fields stay intact.
    for snapshot in actual.values():
        for event in snapshot["trade_events"]:
            for conversion in event["raw_payload"]["cash_conversions"].values():
                assert conversion["status"] == "pending"
                assert conversion["method"] == "event_time_market_fx"
                assert "fx_policy" not in conversion and "cash_fx_date" not in conversion
    from domain.domain.decision_state_fingerprint import canonical_sha256
    assert {account: canonical_sha256(value) for account, value in actual.items()} == {
        "lx": "90bc665c6a5142da777c4f9e8c76206fd42b543c2b85789e4fb48b25e83288ef",
        "sy": "513d55afc2ab1633e58aba5653b82372b1d56a9c13a710595f854b045e88c28d",
    }
    assert calls == {"project": 1, "compare": 1, "hash": 3, "account_hash": 2}
    assert rows == frozen
    assert all(snapshot["snapshot_status"] == "trusted" for snapshot in actual.values())


@pytest.mark.parametrize("change", ["copy", "event", "stored_lot", "bool_int"])
def test_nonidentical_ordered_inputs_recompute_without_value_equality(tmp_path, monkeypatch, change):
    rows = _rows(tmp_path)
    if change == "copy":
        rows["sy"] = deepcopy(rows["sy"])
    elif change in {"event", "bool_int"}:
        rows["sy"]["trade_events"] = deepcopy(rows["sy"]["trade_events"])
        rows["lx"]["trade_events"][0]["unknown_fixture"] = True
        rows["sy"]["trade_events"][0]["unknown_fixture"] = 1 if change == "bool_int" else "changed"
    else:
        rows["sy"]["stored_position_lots"] = deepcopy(rows["sy"]["stored_position_lots"])
        rows["sy"]["stored_position_lots"][0]["fields"]["fixture"] = "changed"
    expected = _independent(rows)
    calls = []
    original = mod.project_stored_trade_events_to_position_lots

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "project_stored_trade_events_to_position_lots", counted)
    assert _many(rows) == expected
    assert len(calls) == 2
    if change == "bool_int":
        assert expected["lx"]["event_fingerprint"] != expected["sy"]["event_fingerprint"]


def test_account_failure_and_lazy_hash_failure_keep_original_error_priority(tmp_path):
    rows = _rows(tmp_path)
    del rows["lx"]["_account_trade_source_constraints"]
    rows["lx"]["trade_events"][0]["uncanonicalizable_fixture"] = float("nan")
    expected = _independent(rows)
    actual = _many(rows)
    assert actual == expected
    assert actual["lx"]["error"] == "trade source constraints were not frozen"
    assert actual["sy"]["snapshot_status"] == "snapshot_unavailable"
    assert actual["sy"]["error"] != actual["lx"]["error"]


@pytest.mark.parametrize("change", ["missing_global", "source", "lifecycle", "current", "global_mismatch"])
def test_bad_or_different_account_does_not_contaminate_healthy_sibling(tmp_path, change):
    rows = _rows(tmp_path)
    kwargs = {}
    if change == "missing_global":
        del rows["lx"]["trade_events"]
    elif change == "source":
        rows["lx"]["_account_trade_source_constraints"] = {
            "status": "source_untrusted", "reason_codes": ["fixture_source_unavailable"],
        }
    elif change == "lifecycle":
        rows["lx"]["account_lifecycle_allocations"] = [None]
    elif change == "current":
        kwargs = {"current_projections": {
            "lx": {"status": "data_unavailable", "reason": "fixture_lx"},
            "sy": {"status": "data_unavailable", "reason": "fixture_sy"},
        }, "current_decision_now_ms": 4000}
    else:
        rows["lx"]["stored_position_lots"] = []
    assert _many(rows, **kwargs) == _independent(rows, **kwargs)
    assert _many(rows, **kwargs)["sy"]["snapshot_status"] == "trusted"


def test_changed_input_on_next_call_and_escaped_projection_mutation_are_isolated(tmp_path):
    rows = _rows(tmp_path)
    source_before = deepcopy(rows)
    snapshots = _many(rows)
    sibling = deepcopy(snapshots["sy"])
    snapshots["lx"]["trade_events"].pop()
    snapshots["lx"]["trade_events"].append({"fixture": "private-list"})
    snapshots["lx"]["projection_comparison"]["summary"]["fixture"] = 99
    snapshots["lx"]["projection_diagnostics"].append({"fixture": True})
    snapshots["lx"]["projection_comparison"]["items"][0]["status"] = "mutated"
    snapshots["lx"]["account_reprojected_position_lots"].append({"fields": {"contracts": 99}})
    assert snapshots["sy"] == sibling
    assert rows == source_before
    assert _many(rows) == _independent(rows)
    rows["lx"]["trade_events"][0]["new_observation"] = "changed"
    refreshed = _many(rows)
    assert refreshed == _independent(rows)
    assert refreshed["sy"]["event_fingerprint"] != sibling["event_fingerprint"]


def test_empty_one_account_and_missing_scope_key(tmp_path):
    assert _many({}) == {}
    rows = _rows(tmp_path)
    assert _many({"sy": rows["sy"]}) == _independent({"sy": rows["sy"]})
    with pytest.raises(KeyError, match="sy"):
        mod.decision_state_snapshots_from_rows_many(rows, portfolio_scope_ids={"lx": "scope"},
                                                    source_observed_at=OBSERVED)
    with pytest.raises(ValueError, match="source_observed_at is required"):
        mod.decision_state_snapshots_from_rows_many(rows, portfolio_scope_ids=SCOPES,
                                                    source_observed_at="")


def test_hash_failure_is_not_memoized_for_later_account(tmp_path, monkeypatch):
    rows = _rows(tmp_path)
    rows["lx"]["trade_events"][0]["uncanonicalizable_fixture"] = float("nan")
    expected = _independent(rows)
    original = mod.canonical_sha256
    calls = []

    def counted(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(mod, "canonical_sha256", counted)
    assert _many(rows) == expected
    assert len(calls) == 2


def test_batch_preserves_caller_keys_without_normalization_collisions(tmp_path):
    rows = _rows(tmp_path)
    keyed = {"LX": rows["lx"], "lx": rows["lx"]}
    actual = mod.decision_state_snapshots_from_rows_many(
        keyed, portfolio_scope_ids={"LX": "scope:first", "lx": "scope:second"},
        source_observed_at=OBSERVED,
    )
    assert list(actual) == ["LX", "lx"]
    assert actual["LX"]["normalized_account"] == actual["lx"]["normalized_account"] == "lx"
    assert actual["LX"]["decision_state_fingerprint"] != actual["lx"]["decision_state_fingerprint"]


def _premium_adjustment_observations(tmp_path, *, adjusted_account="lx"):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with patch("src.application.ledger.writer_trade_events.utc_now_ms", return_value=5_000):
        for index, account in enumerate(SCOPES):
            opened = ledger_api.record_manual_position_open(
                repo, broker="futu", account=account, symbol="NVDA", option_type="put",
                side="short", contracts=index + 1, currency="USD", strike=100,
                multiplier=100, expiration_ymd="2099-09-18", premium_per_share=2,
                opened_at_ms=1_000 + index, request_id=f"shadow-{account}",
            )
            assert opened.result.created is True
    inventory = ledger_api.build_current_decision_projection_migration_inventory(
        repo.db_path, now_ms=10_000,
    )
    assert inventory["readiness"] == "ready", inventory["readiness_reasons"]
    migrated = ledger_api.apply_current_decision_projection_migration(repo.db_path, inventory)
    assert migrated["write_applied"] is True

    def observe():
        rows = ledger_api.read_decision_state_rows_many(repo, accounts=list(SCOPES))
        current = {
            account: ledger_api.read_current_decision_projection(
                repo, account=account, now_ms=20_000,
            ) for account in SCOPES
        }
        assert all(item["status"] == "trusted" for item in current.values()), current
        return rows, current

    before = observe()
    frozen_before = deepcopy(before)
    lot = before[0][adjusted_account]["account_position_lots"][0]
    adjusted = persist_manual_adjust_event(
        repo, lot_id=lot["record_id"], fields=lot["fields"],
        premium_per_share=3, as_of_ms=15_000,
    )
    assert adjusted.created is True
    after = observe()
    assert before == frozen_before
    return before, after


@pytest.mark.parametrize("observation", ["before", "after"])
def test_public_batch_current_shadow_matches_same_premium_observation(tmp_path, observation):
    before, after = _premium_adjustment_observations(tmp_path)
    rows, current = before if observation == "before" else after
    frozen = deepcopy((rows, current))
    replay = ledger_api.project_position_lots_from_trade_facts(rows["lx"]["trade_events"])
    assert not replay.diagnostics
    replayed_lots = {lot.lot_id: lot.to_dict()["fields"] for lot in replay.lots}
    snapshots = ledger_api.decision_state_snapshots_from_rows_many(
        rows, portfolio_scope_ids=SCOPES, source_observed_at=OBSERVED,
        current_projections=current, current_decision_now_ms=20_000,
    )
    for account, snapshot in snapshots.items():
        premium = "3" if observation == "after" and account == "lx" else "2"
        lot = snapshot["account_position_lots"][0]
        assert lot["fields"]["contract_key"]["account"] == account
        assert lot["fields"]["premium_open"] == premium
        assert replayed_lots[lot["record_id"]]["premium_open"] == premium
        assert snapshot["snapshot_status"] == "trusted"
        assert snapshot["actionable"] is True
        shadow = snapshot["current_decision_shadow"]
        assert shadow["status"] == "matched"
        assert shadow["mismatch_count"] == 0
        assert all(section["legacy_sha256"] == section["current_sha256"]
                   for section in shadow["sections"])
        assert mod.decision_state_snapshot_fingerprint(snapshot) == snapshot["decision_state_fingerprint"]
    assert (rows, current) == frozen


@pytest.mark.parametrize("adjusted_account", ["lx", "sy"])
def test_public_batch_current_shadow_detects_legal_premium_adjustment(tmp_path, adjusted_account):
    (rows, previous), (_fresh_rows, current) = _premium_adjustment_observations(
        tmp_path, adjusted_account=adjusted_account,
    )
    frozen = deepcopy((rows, previous, current))
    replay = ledger_api.project_position_lots_from_trade_facts(rows["lx"]["trade_events"])
    assert not replay.diagnostics
    replayed_lots = {lot.lot_id: lot.to_dict()["fields"] for lot in replay.lots}
    baseline = ledger_api.decision_state_snapshots_from_rows_many(
        rows, portfolio_scope_ids=SCOPES, source_observed_at=OBSERVED,
        current_projections=previous, current_decision_now_ms=20_000,
    )
    snapshots = ledger_api.decision_state_snapshots_from_rows_many(
        rows, portfolio_scope_ids=SCOPES, source_observed_at=OBSERVED,
        current_projections=current, current_decision_now_ms=20_000,
    )
    for account, snapshot in snapshots.items():
        lot = snapshot["account_position_lots"][0]
        assert lot["fields"]["contract_key"]["account"] == account
        assert lot["fields"]["premium_open"] == "2"
        assert replayed_lots[lot["record_id"]]["premium_open"] == "2"
        current_lot = current[account]["position_lots"][0]
        assert current_lot["record_id"] == lot["record_id"]
        assert current_lot["fields"]["contract_key"]["account"] == account
        assert current_lot["fields"]["premium_open"] == ("3" if account == adjusted_account else "2")
        assert baseline[account]["current_decision_shadow"]["status"] == "matched"
        assert snapshot["snapshot_status"] == "trusted"
        assert snapshot["actionable"] is True
        assert {key: value for key, value in snapshot.items()
                if key not in {"current_decision_shadow", "current_decision_read"}} == {
            key: value for key, value in baseline[account].items()
            if key not in {"current_decision_shadow", "current_decision_read"}
        }
        assert mod.decision_state_snapshot_fingerprint(snapshot) == baseline[account]["decision_state_fingerprint"]
        shadow = snapshot["current_decision_shadow"]
        positions = next(section for section in shadow["sections"] if section["section"] == "position_lots")
        if account == adjusted_account:
            assert shadow["status"] == "mismatch", shadow
            assert shadow["reason"] == "consumer_fact_mismatch"
            assert shadow["mismatch_count"] == positions["mismatch_count"] == 1
            assert positions["legacy_sha256"] != positions["current_sha256"]
            assert [sample["key"] for sample in positions["mismatch_samples"]] == [lot["record_id"]]
        else:
            assert shadow == baseline[account]["current_decision_shadow"]
            assert shadow["status"] == "matched"
            assert shadow["mismatch_count"] == 0
    assert (rows, previous, current) == frozen
