from itertools import permutations

from domain.domain.strategy_membership import resolve_trade_attribution


def test_attribution_arbitrates_all_strategies_without_priority():
    wheel = {"candidate_id": "wheel:a", "strategy": "wheel", "eligible": True}
    combo = {"candidate_id": "combo:a", "strategy": "combo_yield", "eligible": False,
             "reason_codes": ["confirmation_required"]}
    for candidates in permutations([wheel, combo]):
        result = resolve_trade_attribution(candidates=candidates, evidence_complete=True)
        assert result.status == "pending"
        assert result.selected_candidate_id is None
        assert result.reason_codes == ("multiple_strategy_candidates",)
    unique = resolve_trade_attribution(candidates=(wheel,), evidence_complete=True)
    assert unique.status == "pending"  # A proposal cannot claim a durable link.
    assert unique.selected_candidate_id == "wheel:a"
    incomplete = resolve_trade_attribution(candidates=(wheel,), evidence_complete=False)
    assert incomplete.selected_candidate_id is None
    assert resolve_trade_attribution(candidates=(), evidence_complete=True).status == "ordinary"


def test_attribution_preserves_manual_decision_and_reports_late_conflict():
    combo = {"candidate_id": "combo:a", "strategy": "combo_yield", "eligible": True}
    result = resolve_trade_attribution(candidates=(combo,), evidence_complete=True,
                                      existing={"status": "ordinary", "origin": "manual"})
    assert result.status == "ordinary"
    result = resolve_trade_attribution(candidates=(combo,), evidence_complete=True,
                                      existing={"status": "linked", "candidate_id": "wheel:a"})
    assert result.status == "conflict"
    result = resolve_trade_attribution(candidates=(), evidence_complete=False,
                                      existing={"status": "linked", "candidate_id": "wheel:a"})
    assert result.status == "linked"


def test_manual_ordinary_is_durable_idempotent_and_preserves_economics(tmp_path, monkeypatch):
    import pytest
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.api import (assert_trade_attribution_unclaimed,
        read_trade_attribution_facts, read_trade_attribution_snapshot)
    from src.application.trades import attribution
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_object

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = TradeEvent(event_id="fill", event_type="open", event_time_ms=1000,
                       contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA",
                                                           option_type="put", strike=100, expiration_ymd="2026-12-18"),
                       contracts=1, price=2, multiplier=100, currency="USD", source="futu", lot_id="lot",
                       raw_payload={"side": "sell", "execution_input": {
                           "external_id_namespace": "futu.deal", "external_execution_id": "123",
                           "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}}})
    from domain.domain.trade_execution import execution_identity_from_input
    event.raw_payload["execution_id"] = execution_identity_from_input(event.raw_payload["execution_input"])
    persist_trade_event_object(repo, event)
    original = repo.list_trade_events()[0]
    fact, = read_trade_attribution_facts(repo, account="lx")
    assert fact["ordinary_previewable"]
    context = dict(config={"accounts": ["lx"], "market": "us", "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}, market="us", combo_evidence={"complete": True},
                   capacity_observation={}, combo_mode="confirm")
    view = attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        account="lx", now_ms=2000, **context)
    args = dict(account="lx", execution_key=fact["execution_key"], expected_input_hash=view["rows"][0]["input_hash"],
                request_id="op:1", actor="wechat:user", candidate_id="ordinary", manual=True, **context)
    attribution.apply_trade_attribution(repo, **args, apply_changes=False)
    assert len(repo.list_trade_events()) == 1
    result = attribution.apply_trade_attribution(repo, **args)
    assert result["status"] == "ordinary" and result["origin"] == "manual"
    repeated = attribution.apply_trade_attribution(repo, **args)
    assert repeated["write_applied"] is False
    with pytest.raises(ValueError, match="evidence changed"):
        attribution.apply_trade_attribution(repo, **{**args, "request_id": "op:2"})
    assert repo.list_trade_events()[0] == original
    assert len(repo.list_trade_events()) == 2
    with pytest.raises(ValueError, match="manually excluded"):
        assert_trade_attribution_unclaimed(repo.list_trade_events(), ["lot"])
    assert read_trade_attribution_facts(repo, account="sy") == []


def test_legacy_futu_open_with_exact_scoped_deal_identity_can_be_marked_ordinary(tmp_path):
    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.trade_execution import (execution_identity_from_input,
        legacy_open_execution_input_from_event)
    from src.application.ledger.api import read_trade_attribution_facts, read_trade_attribution_snapshot
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.writer import persist_trade_event_object
    from src.application.trades.attribution import apply_trade_attribution, build_trade_attribution_view

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = TradeEvent(event_id="futu:lx:1001:123", event_type="open", event_time_ms=1000,
        contract_key=ContractKey.from_values(broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="put", strike=100, expiration_ymd="2026-12-18"),
        contracts=1, price=2, multiplier=100, currency="USD", source="opend_push",
        raw_payload={"side": "sell", "source_type": "broker_trade_event", "futu_account_id": "1001",
            "trd_env": "REAL", "source_deal_id": "123", "deal_id": 123})
    persist_trade_event_object(repo, event)
    original = repo.list_trade_events()[0]
    expected = execution_identity_from_input(legacy_open_execution_input_from_event(original))
    fact, = read_trade_attribution_facts(repo, account="lx")
    assert expected and fact["execution_key"] == expected and fact["ordinary_previewable"]
    context = dict(config={"accounts": ["lx"], "market": "us", "account_settings": {
        "lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}, market="us",
        combo_evidence={"complete": True}, capacity_observation={}, combo_mode="confirm")
    view = build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        account="lx", now_ms=2000, **context)
    args = dict(account="lx", execution_key=expected, expected_input_hash=view["rows"][0]["input_hash"],
        request_id="legacy-ordinary:123", actor="operator:lx", candidate_id="ordinary", manual=True, **context)
    preview = apply_trade_attribution(repo, **args, apply_changes=False)
    assert preview["write_applied"] is False and len(repo.list_trade_events()) == 1
    applied = apply_trade_attribution(repo, **args)
    assert applied["status"] == "ordinary" and applied["write_applied"] is True
    assert repo.list_trade_events()[0] == original
    assert len(repo.list_trade_events()) == 2
    assert read_trade_attribution_facts(repo, account="lx")[0]["status"] == "ordinary"


def test_legacy_execution_identity_requires_exact_source_alias():
    from copy import deepcopy
    from domain.domain.trade_execution import legacy_open_execution_input_from_event

    event = {"event_id": "futu:sy:1001:123", "event_type": "open",
        "contract_key": {"broker": "富途", "account": "sy"},
        "raw_payload": {"futu_account_id": "1001", "trd_env": "REAL", "source_deal_id": "123"}}
    assert legacy_open_execution_input_from_event(event)
    for field, value in (("futu_account_id", "1002"), ("trd_env", "SIMULATE"),
                         ("source_deal_id", "124"), ("execution_id", "execution:v1:wrong"),
                         ("internal_account", "lx"), ("external_id_namespace", "other.deal")):
        changed = deepcopy(event)
        changed["raw_payload"][field] = value
        assert legacy_open_execution_input_from_event(changed) == {}


def test_v2_cutover_preserves_v1_and_is_append_only(tmp_path):
    import sqlite3
    import time
    import pytest
    from src.application.ledger.api import read_trade_attribution_policy
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.trade_attribution_migration import (
        preview_trade_attribution_migration, apply_trade_attribution_migration,
    )

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    scope = dict(broker="futu", physical_account_id="1001", environment="REAL", account="lx", market="hk")
    t0 = int(time.time() * 1000)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("""INSERT INTO trade_attribution_policy_enablings
            (broker, physical_account_id, environment, account, market, policy_version,
             effective_from_ms, created_at_ms, actor, request_id, request_hash)
            VALUES ('futu', '1001', 'REAL', 'lx', 'hk', 'trade_attribution.v1', ?, ?, 'legacy', 'old', ?)""",
            (t0, t0, "a" * 64))
    t2 = t0 + 30_000
    with pytest.raises(ValueError, match="cannot precede v1"):
        preview_trade_attribution_migration(repo.db_path, scope=scope, effective_from_ms=t0 - 1)
    preview = preview_trade_attribution_migration(repo.db_path, scope=scope, effective_from_ms=t2)
    assert preview["t0_effective_from_ms"] == t0
    assert preview["v2_existing_effective_from_ms"] is None
    applied = apply_trade_attribution_migration(repo.db_path, manifest=preview,
        backup_path=tmp_path / "backup.sqlite3", writers_stopped=True)
    assert applied["rules_enabled"] is True
    assert read_trade_attribution_policy(repo, scope=scope)["effective_from_ms"] == t2
    repeat_preview = preview_trade_attribution_migration(repo.db_path, scope=scope, effective_from_ms=t2)
    duplicate_backup = tmp_path / "duplicate-backup.sqlite3"
    with pytest.raises(ValueError, match="already exists"):
        apply_trade_attribution_migration(repo.db_path, manifest=repeat_preview,
            backup_path=duplicate_backup, writers_stopped=True)
    assert not duplicate_backup.exists()
    with sqlite3.connect(repo.db_path) as conn:
        from src.application.ledger.repository_schema import initialize_ledger_connection
        initialize_ledger_connection(conn)
        assert conn.execute("SELECT COUNT(*) FROM trade_attribution_policy_enablings").fetchone()[0] == 2
        for statement in ("UPDATE trade_attribution_policy_enablings SET effective_from_ms = 4000",
                          "DELETE FROM trade_attribution_policy_enablings"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)



def test_old_store_is_not_implicitly_migrated_or_enabled(tmp_path):
    import sqlite3
    from src.application.ledger.api import read_trade_attribution_policy
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.ledger.trade_attribution_migration import preview_trade_attribution_migration

    path = tmp_path / "ledger.sqlite3"
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE trade_attribution_policy_enablings")
    repo = SQLiteOptionPositionsRepository(path)
    scope = dict(broker="futu", physical_account_id="1001", environment="REAL", account="lx", market="hk")
    assert read_trade_attribution_policy(repo, scope=scope) is None
    preview = preview_trade_attribution_migration(path, scope=scope, effective_from_ms=2_000)
    assert preview["policy_schema_present"] is False
    assert preview["v2_existing_effective_from_ms"] is None


def test_manual_wheel_choice_ignores_alternatives_but_preserves_real_conflicts():
    existing = {"status": "linked", "origin": "manual", "strategy": "wheel", "candidate_id": "wheel:a"}
    target = {"candidate_id": "wheel:a", "strategy": "wheel", "eligible": True}
    other = {"candidate_id": "wheel:b", "strategy": "wheel", "eligible": True}
    def resolve(candidates, **extra):
        return resolve_trade_attribution(
            candidates=tuple(candidates), evidence_complete=True, existing={**existing, **extra})
    assert resolve([target, other]).status == "linked"
    assert resolve([target, other], origin="inherited").reason_codes == ("late_competing_evidence",)
    assert resolve([target, {**other, "intent_id": "explicit-intent"}]).status == "conflict"
    for reason in ("multiple_or_invalid_wheel_intents", "wheel_intent_fill_mismatch_or_consumed"):
        assert resolve([target, {**other, "reason_codes": [reason]}]).status == "conflict"
    assert resolve([target, {"candidate_id": "combo:c", "strategy": "combo_yield"}]).status == "conflict"
    assert resolve([{**target, "reason_codes": ["account_stock_capacity_exceeded"]}, other]).reason_codes == ("late_capacity_conflict",)
    assert resolve([target, other], status="conflict").reason_codes == ("unresolved_attribution_conflict",)
