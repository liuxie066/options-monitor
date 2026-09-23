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


def test_manual_ordinary_is_durable_idempotent_and_preserves_economics(tmp_path):
    import pytest
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.api import (assert_trade_attribution_unclaimed,
        read_trade_attribution_facts, record_trade_ordinary_attribution)
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
    args = dict(account="lx", execution_key=fact["execution_key"], expected_input_hash=fact["input_hash"],
                request_id="op:1", actor="wechat:user", now_ms=2000)
    record_trade_ordinary_attribution(repo, **args)
    assert len(repo.list_trade_events()) == 1
    result = record_trade_ordinary_attribution(repo, **args, apply_changes=True)
    assert result["status"] == "ordinary" and result["origin"] == "manual"
    repeated = record_trade_ordinary_attribution(repo, **args, apply_changes=True)
    assert repeated["write_applied"] is False
    equivalent = record_trade_ordinary_attribution(repo, **{**args, "request_id": "op:2"}, apply_changes=True)
    assert equivalent["ledger_event_ids"] == result["ledger_event_ids"]
    assert repo.list_trade_events()[0] == original
    assert len(repo.list_trade_events()) == 2
    with pytest.raises(ValueError, match="manually excluded"):
        assert_trade_attribution_unclaimed(repo.list_trade_events(), ["lot"])
    assert read_trade_attribution_facts(repo, account="sy") == []


def test_attribution_enable_is_explicit_append_only_and_not_retroactive(tmp_path):
    import sqlite3
    import time
    import pytest
    from src.application.ledger.api import enable_trade_attribution_policy, read_trade_attribution_policy
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    scope = dict(broker="futu", physical_account_id="1001", environment="REAL", account="lx", market="hk")
    instant = int(time.time() * 1000)
    args = dict(scope=scope, effective_from_ms=instant + 10000, now_ms=instant, actor="operator", request_id="enable:1")
    assert read_trade_attribution_policy(repo, scope=scope) is None
    preview = enable_trade_attribution_policy(repo, **args)
    assert preview["write_applied"] is False
    assert read_trade_attribution_policy(repo, scope=scope) is None
    applied = enable_trade_attribution_policy(repo, **args, apply_changes=True)
    assert applied["write_applied"] is True
    repeated = enable_trade_attribution_policy(repo, **{**args, "now_ms": instant + 20000}, apply_changes=True)
    assert repeated["write_applied"] is False
    assert repeated["effective_from_ms"] == instant + 10000
    with pytest.raises(ValueError, match="another request"):
        enable_trade_attribution_policy(repo, **{**args, "request_id": "enable:2"}, apply_changes=True)
    other_scope = {**scope, "physical_account_id": "2002"}
    assert read_trade_attribution_policy(repo, scope=other_scope) is None
    with pytest.raises(ValueError, match="retroactively"):
        enable_trade_attribution_policy(repo, **{**args, "scope": other_scope, "now_ms": instant + 20000}, apply_changes=True)
    with sqlite3.connect(repo.db_path) as conn:
        from src.application.ledger.repository_schema import initialize_ledger_connection
        initialize_ledger_connection(conn)
        for statement in ("UPDATE trade_attribution_policy_enablings SET effective_from_ms = 4000",
                          "DELETE FROM trade_attribution_policy_enablings"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(statement)


def test_old_store_is_not_implicitly_migrated_or_enabled(tmp_path):
    import sqlite3
    import time
    import pytest
    from src.application.ledger.api import enable_trade_attribution_policy, read_trade_attribution_policy
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    path = tmp_path / "ledger.sqlite3"
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE trade_attribution_policy_enablings")
    repo = SQLiteOptionPositionsRepository(path)
    scope = dict(broker="futu", physical_account_id="1001", environment="REAL", account="lx", market="hk")
    assert read_trade_attribution_policy(repo, scope=scope) is None
    with pytest.raises(ValueError, match="controlled.*migration"):
        enable_trade_attribution_policy(repo, scope=scope, effective_from_ms=int(time.time() * 1000) + 10000, now_ms=1000,
                                        actor="operator", request_id="enable:1", apply_changes=True)
