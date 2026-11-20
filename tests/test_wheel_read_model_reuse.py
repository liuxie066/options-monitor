from copy import deepcopy
from dataclasses import replace

import pytest

from domain.domain.wheel import build_legacy_wheel_event, build_wheel_event
from src.application.ledger import assigned_stock_projection as assigned
from src.application.ledger import publisher
from src.application.wheel import read_model as model
from tests.test_performance_assignment import _assign_put, _trade


def _mixed_rows():
    events, wheel_events = [], []
    for account, symbol in [('lx', 'NVDA'), ('lx', '0700.HK'), ('sy', 'NVDA')]:
        key = f'{account}-{symbol}'
        contract = replace(_trade().contract_key, account=account, underlying_symbol=symbol)
        currency = 'HKD' if symbol.endswith('.HK') else 'USD'
        opened = _trade(event_id=f'open-{key}', lot_id=f'lot-{key}', contract_key=contract, currency=currency)
        assigned_event = _assign_put(event_id=f'assign-{key}', target_lot_id=f'lot-{key}',
                                     contract_key=contract, currency=currency)
        call = _trade(event_id=f'call-{key}', lot_id=f'call-lot-{key}', event_time_ms=3000,
                      contract_key=replace(contract, option_type='call'), currency=currency)
        events.extend({**item.to_dict(), "position_side": "short"} for item in (opened, assigned_event, call))
        if account == 'lx':
            wheel_events.append(build_legacy_wheel_event(
                event_id=f'wheel-{key}',
                account=account, lot_id=f'assigned-stock-assign-{key}', event_type='wheel_started',
                occurred_at_ms=2000, recorded_at_ms=2001, source_trade_event_id=f'assign-{key}',
                payload={'request_id': f'assignment:assign-{key}'},
            ))
    return {'trade_events': events, 'account_wheel_events': wheel_events}


@pytest.mark.parametrize('market', [None, 'us', 'hk'])
@pytest.mark.parametrize('readiness', [None, {'monitoring_gate': 'enabled'},
                                      {'monitoring_gate': 'config_mismatch', 'reason_code': 'policy_drift'}])
def test_pair_matches_separate_views_and_shares_base(monkeypatch, market, readiness):
    rows = _mixed_rows()
    original = deepcopy(rows)
    kwargs = dict(account='lx', as_of_ms=4000)
    expected = (model.build_wheel_read_model_from_rows(rows, **kwargs, market=market,
                                                      monitoring_readiness=readiness),
                model.build_wheel_read_model_from_rows(rows, **kwargs))
    project, branches = publisher._collect_projection_inputs, assigned.project_wheel_branches
    calls = {'project': 0, 'branches': 0}
    def counted_project(events):
        calls['project'] += 1
        return project(events)
    def counted_branches(*args, **kwargs):
        calls['branches'] += 1
        return branches(*args, **kwargs)
    monkeypatch.setattr(publisher, '_collect_projection_inputs', counted_project)
    monkeypatch.setattr(assigned, 'project_wheel_branches', counted_branches)
    actual = model.build_wheel_read_model_with_capacity_from_rows(
        rows, **kwargs, market=market, monitoring_readiness=readiness)
    assert actual == expected
    assert calls == {'project': 1, 'branches': 1}
    assert rows == original
    assert {row['symbol'] for row in actual[1]['wheel_branches']} == {'NVDA', '0700.HK'}
    assert all(row['account'] == 'lx' for row in actual[1]['wheel_branches'])
    assert all(row['phase'] == 'linkage_unresolved' for row in actual[1]['wheel_branches'])


@pytest.mark.parametrize("market", ["us", "hk"])
def test_historical_wheel_keeps_coverage_without_hashing_call_candidates(monkeypatch, market):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    kwargs = {"account": "lx", "as_of_ms": 4000, "market": market}
    full = model.build_wheel_read_model_from_rows(rows, **kwargs)
    assert full["linkage_candidates"]
    canonical_sha256 = projection.canonical_sha256

    def hash_without_call_candidates(payload):
        assert "call_record_id" not in payload
        return canonical_sha256(payload)

    monkeypatch.setattr(projection, "canonical_sha256", hash_without_call_candidates)
    historical = model.build_wheel_read_model_from_rows(rows, **kwargs, _historical=True)
    assert historical["linkage_candidates"] == []
    assert historical["wheel_branches"] == full["wheel_branches"]
    assert historical["batches"] == full["batches"]


def test_call_linkage_reads_batch_capacity_once_for_competing_lots():
    from domain.domain.wheel.projection import _project_wheel_call_linkage_candidates

    reads = []

    class Shares:
        def __int__(self):
            reads.append(True)
            return 200

    batch = {
        "stock_lot_id": "stock", "account": "lx", "symbol": "NVDA",
        "lifecycle_status": "active", "integrity_status": "trusted",
        "shares_remaining": Shares(), "active_option_committed_shares": 100,
        "batch_generation_hash": "generation",
    }
    lots = [
        {"record_id": event, "fields": {
            "open_event_id": event, "position_side": "short", "status": "open",
            "contracts_open": contracts, "multiplier": 100,
            "contract_key": {"account": "lx", "underlying_symbol": "NVDA", "option_type": "call"},
        }}
        for event, contracts in (("rejected", 1), ("too-large", 2), ("first", 1), ("second", 1))
    ]
    rejected = [{"event_type": "wheel_call_linkage_rejected", "stock_lot_id": "stock",
                 "payload": {"call_open_event_id": "rejected"}}]
    batches = [batch, {**batch, "account": "sy"}, {**batch, "symbol": "0700.HK"},
               {**batch, "lifecycle_status": "closed"}, {**batch, "integrity_status": "conflict"}]
    candidates = _project_wheel_call_linkage_candidates(batches, lots, rejected)
    assert [item["call_open_event_id"] for item in candidates] == ["first", "second"]
    assert len(reads) == 1

    matched = set()
    assert _project_wheel_call_linkage_candidates(batches, lots, rejected, _matched_stock_lots=matched) == []
    assert matched == {"stock"}
    assert len(reads) == 2


@pytest.mark.parametrize("market", ["us", "hk"])
def test_historical_attribution_keeps_branch_evidence_without_legacy_batches(monkeypatch, market):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    before = deepcopy(rows)
    kwargs = {"account": "lx", "as_of_ms": 4000, "market": market}
    full = model.build_wheel_read_model_from_rows(rows, **kwargs, _historical=True)
    assert full["wheel_branches"] and full["batches"]

    def unexpected_batches(*args, **kwargs):
        pytest.fail("historical attribution must not build compatibility batches")

    canonical_sha256 = projection.canonical_sha256

    def generation_hash_only(payload):
        assert "derived" not in payload
        return canonical_sha256(payload)

    monkeypatch.setattr(assigned, "_legacy_call_batches", unexpected_batches)
    monkeypatch.setattr(projection, "canonical_sha256", generation_hash_only)
    actual = assigned._historical_attribution_branches(
        rows, **kwargs, historical_rows=assigned._HistoricalTradeRows.from_rows(rows),
    )
    assert actual == {
        branch["wheel_branch_id"]: {
            "lifecycle_status": branch["lifecycle_status"],
            "integrity_status": branch["integrity_status"],
            "active_option_lot_ids": branch.get("active_option_lot_ids", []),
            "coverage": {"status": branch["coverage"]["status"]},
        }
        for branch in full["wheel_branches"]
    }
    assert rows == before


def test_historical_attribution_normalizes_wheel_rows_once_and_respects_void_time(monkeypatch):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    created = build_wheel_event(
        event_id="put-created", account="lx", lot_id=None, wheel_branch_id="put-branch",
        event_type="wheel_branch_created", occurred_at_ms=2000, recorded_at_ms=2001,
        payload={"direction": "put", "symbol": "NVDA"},
    )
    voided = build_wheel_event(
        event_id="put-voided", account="lx", lot_id=None, wheel_branch_id="put-branch",
        event_type="wheel_event_voided", occurred_at_ms=3500, recorded_at_ms=3501,
        payload={"target_wheel_event_id": "put-created"},
    )
    rows["account_wheel_events"].extend([created, voided])
    before = deepcopy(rows)
    instants = (3000, 4000, 3200)
    kwargs = {"account": "lx", "market": "us"}
    expected = [assigned._historical_attribution_branches(rows, as_of_ms=instant,
                historical_rows=None, **kwargs) for instant in instants]
    assert ["put-branch" in branches for branches in expected] == [True, False, True]
    normalize = projection.normalize_persisted_wheel_event
    normalized_ids = []

    def tracked(event):
        normalized_ids.append(event["event_id"])
        return normalize(event)

    monkeypatch.setattr(projection, "normalize_persisted_wheel_event", tracked)
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    actual = [assigned._historical_attribution_branches(rows, as_of_ms=instant,
              historical_rows=scope, **kwargs) for instant in instants]
    assert actual == expected
    assert sorted(normalized_ids) == sorted(event["event_id"] for event in rows["account_wheel_events"])
    assert rows == before


def test_historical_wheel_validates_unchanged_call_lots_once(monkeypatch):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    kwargs = {"account": "lx", "market": "us", "_historical": True}
    instants = (3500, 4000, 3200)
    expected = [model.build_wheel_read_model_from_rows(rows, as_of_ms=at, **kwargs) for at in instants]
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    validate = projection.canonical_json_bytes
    calls = []

    def observe(payload):
        calls.append(payload["open_event_id"])
        return validate(payload)

    monkeypatch.setattr(projection, "canonical_json_bytes", observe)
    assert [
        model.build_wheel_read_model_from_rows(rows, as_of_ms=at, _historical_rows=scope, **kwargs)
        for at in instants
    ] == expected
    assert calls == ["call-lx-NVDA"]


def test_call_validation_respects_changed_inputs_and_decimal_signals():
    from decimal import Rounded, localcontext
    from domain.domain.wheel.projection import _validate_call_linkage_input

    stable = {
        "lot_id": "lot", "open_event_id": "open", "position_side": "short",
        "contracts_open": 1, "multiplier": 100,
        "contract_key": {"account": "lx", "underlying_symbol": "NVDA", "option_type": "call",
                         "strike": "123.45", "expiration_ymd": "2026-11-27"},
    }
    validated = set()
    with localcontext() as context:
        context.prec = 10
        _validate_call_linkage_input(stable, "lot", "open", validated)
        _validate_call_linkage_input({**stable, "contracts_open": 2}, "lot", "open", validated)
        assert len(validated) == 2
        with pytest.raises(UnicodeEncodeError):
            _validate_call_linkage_input(stable, "lot\ud800", "open", validated)
        context.prec = 3
        context.traps[Rounded] = True
        with pytest.raises(Rounded):
            _validate_call_linkage_input(stable, "lot", "open", validated)
        context.traps[Rounded] = False
        context.clear_flags()
        _validate_call_linkage_input(stable, "lot", "open", validated)
        assert context.flags[Rounded]
        assert len(validated) == 2


@pytest.mark.parametrize("legacy_fee_alias", [False, True])
def test_historical_put_reuses_economics_only_for_equivalent_events(monkeypatch, legacy_fee_alias):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    branch_id = "wheel-put:historical"
    rows["account_wheel_events"].append(build_wheel_event(
        event_id="put-start", account="lx", lot_id=None, wheel_branch_id=branch_id,
        event_type="wheel_branch_created", occurred_at_ms=900, recorded_at_ms=900,
        payload={"direction": "put", "symbol": "NVDA", "market": "US",
                 "contracts": 1, "multiplier": 100},
    ))
    opening, closing = rows["trade_events"][:2]
    opening["raw_payload"].update(
        strategy="wheel", leg_role="wheel_put", source_wheel_branch_id=branch_id,
    )
    for row in (opening, closing):
        row["fees"] = 0
        fee_container = row if legacy_fee_alias else row["raw_payload"]
        fee_container["fee_provenance"] = {"basis": "actual", "source": "test"}

    instants = (2500, 4000, 3000)
    expected = [
        model.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=at, _historical=True)
        for at in instants
    ]
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    replay = projection.project_trade_events
    calls = []

    def count_replay(events):
        calls.append(len(events))
        return replay(events)

    monkeypatch.setattr(projection, "project_trade_events", count_replay)
    actual = [
        model.build_wheel_read_model_from_rows(
            rows, account="lx", as_of_ms=at, _historical=True, _historical_rows=scope,
        )
        for at in instants
    ]
    assert actual == expected
    pnl = next(branch["realized_put_net_pnl_in_current_stage"] for branch in actual[0]["wheel_branches"]
               if branch["wheel_branch_id"] == branch_id)
    assert pnl == (None if legacy_fee_alias else 200)
    assert calls == ([len(scope.select(at)) for at in instants] if legacy_fee_alias else [])


def test_historical_wheel_reuses_decoded_rows_and_opening_lots_across_instants(monkeypatch):
    from domain.domain import assigned_stock
    from domain.domain.ledger import projection as ledger_projection
    from domain.domain.ledger.lots import PositionLot
    from domain.domain.wheel import projection
    rows = _mixed_rows()
    for row in rows["trade_events"]:
        if row["event_type"] == "assignment" or row["event_id"].startswith("open-"):
            row["fees"] = 2.5
            row["raw_payload"]["fee_provenance"] = {"basis": "actual", "source": "test"}
    original = deepcopy(rows)
    instants = (2500, 4000, 3000)
    expected = [
        model.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=instant, _historical=True)
        for instant in instants
    ]
    full = model.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=instants[0])
    assigned_lots = full["assigned_stock_projection"]["assigned_stock_lots"]
    assert [lot["fees_used"] for lot in assigned_lots] == [5.0, 5.0]
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    decode = assigned.stored_trade_event_to_ledger_event
    decoded_ids = []
    from_open = PositionLot.from_open_event
    opened_ids = []
    resolve_lot_id = ledger_projection.lot_id_for_open_event
    resolved_opening_ids = []
    metadata_sizes = []
    metadata = projection.lot_strategy_metadata_from_trade_events
    settlement_fee_calls = []
    option_fee_fact = assigned_stock._option_fee_fact

    def counted(row):
        decoded_ids.append(row["event_id"])
        return decode(row)

    def counted_open(event, *, lot_id):
        opened_ids.append(event.event_id)
        return from_open(event, lot_id=lot_id)

    def counted_opening_id(event):
        resolved_opening_ids.append(event.event_id)
        return resolve_lot_id(event)

    def counted_metadata(events, **kwargs):
        metadata_sizes.append(len(events))
        return metadata(events, **kwargs)

    def counted_fee(event, *, component):
        settlement_fee_calls.append((event["event_id"], component))
        return option_fee_fact(event, component=component)

    monkeypatch.setattr(assigned, "stored_trade_event_to_ledger_event", counted)
    monkeypatch.setattr(PositionLot, "from_open_event", staticmethod(counted_open))
    monkeypatch.setattr(ledger_projection, "lot_id_for_open_event", counted_opening_id)
    monkeypatch.setattr(assigned, "lot_strategy_metadata_from_trade_events", counted_metadata)
    monkeypatch.setattr(projection, "lot_strategy_metadata_from_trade_events", counted_metadata)
    monkeypatch.setattr(assigned_stock, "_option_fee_fact", counted_fee)
    actual = [
        model.build_wheel_read_model_from_rows(
            rows, account="lx", as_of_ms=instant, _historical=True, _historical_rows=scope,
        )
        for instant in instants
    ]
    assert actual == expected
    assert rows == original
    assert sorted(decoded_ids) == sorted(row["event_id"] for row in rows["trade_events"])
    assert sorted(opened_ids) == sorted(
        row["event_id"] for row in rows["trade_events"] if row["event_type"] == "open"
    )
    assert sorted(resolved_opening_ids) == sorted(opened_ids)
    assert metadata_sizes == [len(scope.select(instant)) for instant in instants]
    assert sorted(settlement_fee_calls) == [
        ("assign-lx-0700.HK", "put_assignment_option_fee"),
        ("assign-lx-NVDA", "put_assignment_option_fee"),
        ("open-lx-0700.HK", "put_open_option_fee"),
        ("open-lx-NVDA", "put_open_option_fee"),
    ]


def test_historical_wheel_keeps_sales_and_blocking_reviews_without_stock_report(monkeypatch):
    from domain.domain import assigned_stock

    rows = _mixed_rows()
    sale = {
        "event_type": "sale", "stock_event_id": "partial-sale",
        "target_stock_lot_id": "assigned-stock-assign-lx-NVDA",
        "account": "lx", "broker": "futu", "symbol": "NVDA", "currency": "USD",
        "side": "sell", "shares": 20, "price": 110, "trade_time_ms": 3600,
    }
    rows["account_assigned_stock_events"] = [
        sale,
        {**sale, "stock_event_id": "oversale", "shares": 200, "trade_time_ms": 3800},
    ]
    instants = (3500, 3700, 3900)
    expected = [
        model.build_wheel_read_model_from_rows(rows, account="lx", market="us", as_of_ms=instant)
        for instant in instants
    ]
    branches = [item["wheel_branches"][0] for item in expected]
    assert [branch["shares_remaining"] for branch in branches] == [100, 80, 80]
    assert branches[0]["batch_generation_hash"] != branches[1]["batch_generation_hash"]
    assert "assigned_stock_projection_conflict" in branches[2]["reason_codes"]

    def unexpected_report(*args, **kwargs):
        pytest.fail("historical Wheel inventory should not build Covered Call allocations")

    monkeypatch.setattr(assigned_stock, "_attribute_covered_calls", unexpected_report)
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    for instant, full in zip(instants, expected):
        historical = model.build_wheel_read_model_from_rows(
            rows, account="lx", market="us", as_of_ms=instant, _historical=True, _historical_rows=scope,
        )
        assert historical["wheel_branches"] == full["wheel_branches"]
        assert historical["batches"] == full["batches"]


def test_historical_stock_inventory_reuses_only_the_same_settlement_evidence(monkeypatch):
    from domain.domain import assigned_stock

    rows = _mixed_rows()
    rows["trade_events"][1]["raw_payload"]["stock_settlement"]["event_time_ms"] = 3200
    rows["account_assigned_stock_events"] = [{
        "event_type": "sale", "stock_event_id": "partial-sale",
        "target_stock_lot_id": "assigned-stock-assign-lx-NVDA",
        "account": "lx", "broker": "futu", "symbol": "NVDA", "currency": "USD",
        "side": "sell", "shares": 20, "price": 110, "trade_time_ms": 3600,
    }]
    instants = (2500, 3000, 3300, 3400, 3700, 3800, 2900)
    kwargs = {"account": "lx", "market": "us", "_historical": True}
    expected = [
        model.build_wheel_read_model_from_rows(rows, as_of_ms=instant, **kwargs)
        for instant in instants
    ]
    stock_lots = [result["assigned_stock_projection"]["_all_assigned_stock_lots"] for result in expected]
    assert [
        [lot["shares_remaining"] for lot in lots if lot["symbol"] == "NVDA"]
        for lots in stock_lots
    ] == [[], [], [100], [100], [80], [80], []]

    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    stock_fee = assigned_stock.assigned_stock_fee_fact
    calls = []

    def count_stock_fee(*args, **kwargs):
        calls.append(kwargs["transaction_kind"])
        return stock_fee(*args, **kwargs)

    monkeypatch.setattr(assigned_stock, "assigned_stock_fee_fact", count_stock_fee)
    for at, reference in zip(instants, expected):
        actual = model.build_wheel_read_model_from_rows(rows, as_of_ms=at, _historical_rows=scope, **kwargs)
        assert actual == reference
        actual["assigned_stock_projection"]["_all_assigned_stock_lots"][0]["shares_remaining"] = -1
    assert calls == ["assignment", "assignment", "assignment", "assignment", "assignment", "sale", "assignment"]


def test_historical_opening_suffix_matches_full_replay_out_of_order(monkeypatch):
    rows = _mixed_rows()
    opening = rows["trade_events"][2]
    rows["trade_events"].extend(
        {**opening, "event_id": event_id, "lot_id": f"lot-{event_id}", "event_time_ms": at}
        for event_id, at in (("same-time-as-settlement", 2000), ("later-b", 3500),
                             ("later-a", 3500), ("latest", 4000))
    )
    rows["trade_events"].reverse()
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    instants = (4000, 3000, 3500, 4000)
    replay = assigned._project_decoded_trade_events
    calls = []

    def observe(events, *args, **kwargs):
        calls.append(tuple(event.event_id for event in events))
        return replay(events, *args, **kwargs)

    monkeypatch.setattr(assigned, "_project_decoded_trade_events", observe)
    for instant in instants:
        selected = scope.select(instant)
        expected = publisher._project_stored_trade_events_with_decoded_events(
            selected, with_publication_state=False,
        )
        assert scope.project(selected) == expected
    assert len(calls) == 7  # The shared prefix and six isolated opens.
    assert all(len(events) == 1 for events in calls[1:])


@pytest.mark.parametrize(("event_id", "lot_id", "diagnostic"), [
    ("reused-lot", "lot-lx-NVDA", "duplicate_lot_id"),
    ("open-lx-NVDA", "new-lot", "duplicate_event_id"),
])
def test_historical_opening_suffix_replays_identity_collisions(event_id, lot_id, diagnostic):
    rows = _mixed_rows()
    rows["trade_events"].append({
        **rows["trade_events"][2], "event_id": event_id, "lot_id": lot_id, "event_time_ms": 3500,
    })
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    selected = scope.select(4000)
    expected = publisher._project_stored_trade_events_with_decoded_events(selected, with_publication_state=False)
    assert any(item.code == diagnostic for item in expected[0].diagnostics)
    assert scope.project(selected) == expected


def test_historical_opening_suffix_replays_combo_membership_changes():
    rows = _mixed_rows()
    opening = rows["trade_events"][0]
    opening["raw_payload"].update(
        strategy="combo_yield", strategy_group_id="combo_yield:fixture", leg_role="funding_put",
    )
    rows["trade_events"].append({
        **opening, "event_id": "second-funding-put", "lot_id": "second-put", "event_time_ms": 3500,
    })
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    assert not scope.project(scope.select(3000))[0].diagnostics
    selected = scope.select(4000)
    expected = publisher._project_stored_trade_events_with_decoded_events(selected, with_publication_state=False)
    assert any(item.code == "csp_lc_membership_incomplete" for item in expected[0].diagnostics)
    assert scope.project(selected) == expected


def test_legacy_wheel_reuses_first_event_validation_with_shuffled_sources(monkeypatch):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    expected = model.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=4000)
    rows["account_wheel_events"].reverse()
    calls = []
    effective = projection.effective_wheel_events

    def counted(events, **kwargs):
        if "trade_events" in kwargs:
            calls.append(kwargs["as_of_ms"])
        return effective(events, **kwargs)

    monkeypatch.setattr(projection, "effective_wheel_events", counted)
    assert model.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=4000) == expected
    assert calls == [4000]


@pytest.mark.parametrize("mixed", [False, True])
def test_historical_v2_wheel_only_builds_legacy_batches_for_mixed_sources(monkeypatch, mixed):
    from domain.domain.wheel import projection

    rows = _mixed_rows()
    legacy = rows["account_wheel_events"][0]
    created = build_wheel_event(
        event_id="v2-created", account="lx", lot_id=None, wheel_branch_id="v2-put-branch",
        event_type="wheel_branch_created", occurred_at_ms=2000, recorded_at_ms=2001,
        payload={"direction": "put", "symbol": "NVDA"},
    )
    voided = build_wheel_event(
        event_id="v2-void", account="lx", lot_id=None, wheel_branch_id="v2-put-branch",
        event_type="wheel_event_voided", occurred_at_ms=3500, recorded_at_ms=3501,
        payload={"target_wheel_event_id": "v2-created"},
    )
    rows["account_wheel_events"] = [created, voided] + ([legacy] if mixed else [])
    instants = [3000, 4000]
    expected = [
        model.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=at, _historical=True)
        for at in instants
    ]
    assert [len(item["wheel_branches"]) for item in expected] == [1 + int(mixed), int(mixed)]
    original = projection.project_wheel_lifecycles
    legacy_calls = []

    def counted(*args, **kwargs):
        legacy_calls.append(args[4])
        return original(*args, **kwargs)

    monkeypatch.setattr(projection, "project_wheel_lifecycles", counted)
    scope = assigned._HistoricalTradeRows.from_rows(rows)
    assert scope is not None
    assert [
        model.build_wheel_read_model_from_rows(
            rows, account="lx", as_of_ms=at, _historical=True, _historical_rows=scope,
        )
        for at in instants
    ] == expected
    assert legacy_calls == (instants if mixed else [])


@pytest.mark.parametrize('side', [0, 1])
def test_pair_nested_mutation_does_not_escape_and_next_observation_is_fresh(side):
    rows = _mixed_rows()
    original = deepcopy(rows)
    pair = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=4000, market='us')
    other = deepcopy(pair[1 - side])
    expected = deepcopy(pair)
    pair[side]['wheel_branches'][0]['coverage']['test_mutation'] = True
    pair[side]['batches'][0]['active_option_contracts'].append({'lot_id': 'mutated'})
    pair[side]['assigned_stock_projection']['assigned_stock_lots'][0]['shares_remaining'] = -1
    assert pair[1 - side] == other
    assert rows == original
    assert model.build_wheel_read_model_with_capacity_from_rows(
        rows, account='lx', as_of_ms=4000, market='us') == expected
    earlier = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=1500, market='us')
    assert earlier[1]['wheel_branches'] == []
    rows['account_wheel_events'] = []
    changed = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=4000, market='us')
    assert changed != expected


@pytest.mark.parametrize('rows,account,instant,market', [
    ({}, '', 4000, 'us'), ({}, 'lx', 0, 'us'), ({}, 'lx', 'invalid', 'us'),
    ({}, 'lx', 4000, 'invalid'),
    ({'trade_events': [{'event_id': 'bad', 'event_type': 'open', 'event_time_ms': 'bad-time'}]},
     'lx', 4000, 'invalid'),
])
def test_pair_preserves_single_view_first_error(rows, account, instant, market):
    with pytest.raises((ValueError, TypeError)) as expected:
        model.build_wheel_read_model_from_rows(rows, account=account, as_of_ms=instant, market=market)
    with pytest.raises(type(expected.value)) as actual:
        model.build_wheel_read_model_with_capacity_from_rows(rows, account=account, as_of_ms=instant, market=market)
    assert str(actual.value) == str(expected.value)


def test_pair_preserves_selection_fallback(monkeypatch):
    row = _trade().to_dict()
    row.update(event_time_ms=0, trade_time_ms=6000)
    rows = {'trade_events': [row]}
    before = deepcopy(rows)
    expected = model.build_wheel_read_model_from_rows(rows, account='lx', as_of_ms=4000)
    calls = []
    original = publisher._collect_projection_inputs
    monkeypatch.setattr(publisher, '_collect_projection_inputs',
                        lambda events: (calls.append(deepcopy(events)), original(events))[1])
    pair = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=4000, market=None)
    assert pair == (expected, expected)
    # Canonical selection applies the trade_time fallback before the sole replay.
    assert calls == [[]]
    assert pair[0]['assigned_stock_projection']['assigned_stock_lots'] == []
    assert rows == before


@pytest.mark.parametrize("paired", [False, True])
def test_historical_wheel_keeps_later_void_of_repaired_terminal(paired):
    opened = _trade()
    expired = _trade(event_id="old-expiry", event_type="expire_close", event_time_ms=2000,
                     price=0, lot_id=None, target_lot_id="lot-put", raw_payload={})
    assignment = _assign_put(event_time_ms=2500)
    void = _trade(event_id="void-old-expiry", event_type="void", event_time_ms=5000,
                  contracts=0, price=0, lot_id=None, target_event_id="old-expiry", raw_payload={})
    rows = {"trade_events": [item.to_dict() for item in (opened, expired, assignment, void)]}
    assert assigned._HistoricalTradeRows.from_rows(rows) is None
    before = deepcopy(rows)
    read = (model.build_wheel_read_model_with_capacity_from_rows if paired
            else model.build_wheel_read_model_from_rows)
    result = read(rows, account="lx", as_of_ms=4000, market="us")
    views = result if paired else (result,)
    for view in views:
        assert [lot["shares_remaining"] for lot in view["assigned_stock_projection"]["assigned_stock_lots"]] == [100]
    assert rows == before


def test_historical_wheel_does_not_include_void_of_future_event():
    opened = _trade(event_time_ms=6000)
    void = _trade(event_id="void-future-open", event_type="void", event_time_ms=7000,
                  contracts=0, price=0, lot_id=None, target_event_id=opened.event_id, raw_payload={})
    out = model.build_wheel_read_model_from_rows(
        {"trade_events": [opened.to_dict(), void.to_dict()]}, account="lx", as_of_ms=4000)
    assert out["assigned_stock_projection"]["assigned_stock_lots"] == []


def test_historical_wheel_retained_void_still_checks_account_identity():
    opened = _trade()
    assigned = _assign_put()
    void = _trade(event_id="invalid-void", event_type="void", event_time_ms=5000,
                  contracts=0, price=0, lot_id=None, target_event_id=assigned.event_id,
                  contract_key=replace(assigned.contract_key, account="sy"), raw_payload={})
    with pytest.raises(ValueError, match="target_event_contract_mismatch"):
        model.build_wheel_read_model_from_rows(
            {"trade_events": [item.to_dict() for item in (opened, assigned, void)]},
            account="lx", as_of_ms=4000)
