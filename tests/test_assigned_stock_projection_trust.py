from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.option_position_identity import normalize_broker
from src.application.agent_tools.materialization_impl import capture_option_performance_evidence
from src.application.agent_tools.operations_impl import _assigned_stock_action
from src.application.ledger.assigned_stock_projection import project_assigned_stock_lifecycle_from_rows
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from tests.test_performance_assignment import _assign_put, _trade


class _Repo:
    def __init__(self, rows):
        self.rows = rows

    def list_position_lots(self):
        return []

    def list_trade_events(self):
        return self.rows

    def list_assigned_stock_events(self):
        return []


def _void(**overrides):
    fields = dict(event_id="void", event_type="void", event_time_ms=3000,
                  contracts=0, price=0, lot_id=None, target_event_id="assign-put", raw_payload={})
    fields.update(overrides)
    return _trade(**fields)


def _unexpected(*args, **kwargs):
    pytest.fail("untrusted projection must stop before external collection or evidence writes")


def _run(surface, rows):
    repo = _Repo(rows)
    if surface == "projection":
        return project_assigned_stock_lifecycle_from_rows({"trade_events": rows}, as_of_ms=4000, account="lx")
    if surface == "oracle":
        from src.application.ledger.current_decision_oracle import _oracle_assigned_stock_report

        return _oracle_assigned_stock_report({"trade_events": rows}, now_ms=4000, account="lx")
    if surface == "view":
        return build_assigned_stock_view(repo, as_of_ms=4000, account="lx")
    if surface == "operations":
        return _assigned_stock_action(
            repo, {"account": "lx", "as_of_ms": 4000}, cfg={}, repo_base=lambda: Path("/tmp"),
            quote_state_base_dir=None, normalize_broker=normalize_broker,
            normalize_account=lambda value: str(value).lower(), refresh_assigned_stock_quotes=_unexpected,
        )
    return capture_option_performance_evidence(
        {"config_key": "us", "account": "lx"}, apply=True, now_ms=1788400000000,
        load_runtime_config=lambda **kwargs: (Path("/tmp/test.config.json"), {}),
        resolve_public_data_config_path=lambda *args: Path("/tmp/test.data.json"),
        normalize_broker=normalize_broker, resolve_option_positions_repo=lambda **kwargs: (None, repo),
        open_performance_evidence_repository=lambda repo: SimpleNamespace(
            read_all=lambda: SimpleNamespace(valuation_marks=()), import_envelope=_unexpected,
        ),
        repo_base=lambda: Path("/tmp"), mask_path=str, evidence_collector=_unexpected,
    )


@pytest.mark.parametrize("surface", ["projection", "oracle", "view", "operations", "capture"])
@pytest.mark.parametrize("invalid", ["account", "broker", "contract", "time", "unknown"])
def test_assigned_stock_public_consumers_reject_invalid_control_graph(surface, invalid):
    void = _void()
    if invalid == "account":
        void = replace(void, contract_key=replace(void.contract_key, account="sy"))
    elif invalid == "broker":
        void = replace(void, contract_key=replace(void.contract_key, broker="ibkr"))
    elif invalid == "contract":
        void = replace(void, contract_key=replace(void.contract_key, underlying_symbol="AAPL"))
    elif invalid == "time":
        void = replace(void, event_time_ms=1500)
    else:
        void = replace(void, target_event_id="missing")
    with pytest.raises(ValueError, match="assigned-stock ledger projection is untrusted: target_event_"):
        _run(surface, [_trade().to_dict(), _assign_put().to_dict(), void.to_dict()])


def test_assigned_stock_does_not_ignore_unattributable_import_error():
    rows = [_trade().to_dict(), _assign_put().to_dict(), {"event_id": "unknown", "event_type": "open"}]
    with pytest.raises(ValueError, match="non_canonical_trade_event_schema"):
        _run("view", rows)


def test_assigned_stock_keeps_unrelated_account_errors_isolated():
    unrelated = _trade(event_id="sy-open", lot_id="sy-lot", multiplier=None,
                       contract_key=replace(_trade().contract_key, account="sy"))
    rows = [_trade().to_dict(), _assign_put().to_dict(), unrelated.to_dict()]
    report = _run("view", rows)
    assert [row["shares_remaining"] for row in report["assigned_stock_lots"]] == [100]
    assert report["assigned_stock_lots"][0]["account"] == "lx"


def test_assigned_stock_preserves_later_valid_void_and_voided_bad_multiplier():
    bad_open = _trade(event_id="bad-open", lot_id="bad-lot", multiplier=None).to_dict()
    void_bad = _void(event_id="void-bad", event_time_ms=6000, target_event_id="bad-open")
    # Late historical corrections still restate the earlier reporting boundary.
    rows = [_trade().to_dict(), _assign_put().to_dict(), bad_open, void_bad.to_dict(),
            _void(event_time_ms=6000).to_dict()]
    report = _run("view", rows)
    assert report["assigned_stock_lots"] == []
    assert report["assignment_lifecycle_rows"] == []


@pytest.mark.parametrize("valid", [False, True])
def test_current_decision_migration_oracle_rejects_bad_graph_and_keeps_valid_void(tmp_path, valid):
    from src.application.ledger.current_decision_projection import (
        build_current_decision_projection_migration_inventory,
    )
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_trade())
    repo.upsert_trade_event(_assign_put())
    void = _void(event_time_ms=6000)
    if not valid:
        void = replace(void, contract_key=replace(void.contract_key, account="sy"))
    repo.upsert_trade_event(void)
    if valid:
        from src.application.ledger.position_projection_runtime import run_position_projection_forced_full

        run_position_projection_forced_full(repo, seed_checkpoint=True)
    manifest = build_current_decision_projection_migration_inventory(repo.db_path, now_ms=4000)
    oracle_reasons = [reason for reason in manifest["readiness_reasons"] if reason.startswith("oracle_unavailable:")]
    if valid:
        assert oracle_reasons == []
        assert any(row["account"] == "lx" for row in manifest["accounts"])
    else:
        assert manifest["readiness"] == "not_ready"
        assert "oracle_unavailable:lx:ValueError" in oracle_reasons
        assert not any(row["account"] == "lx" for row in manifest["accounts"])
    with repo._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM current_decision_projections").fetchone()[0] == 0


def test_cross_account_adjust_target_diagnostic_blocks_stock_economics():
    adjust = _trade(event_id="wrong-adjust", event_type="adjust", event_time_ms=3000,
                    contracts=0, lot_id=None, target_lot_id="lot-put",
                    contract_key=replace(_trade().contract_key, account="sy"),
                    raw_payload={"patch": {"premium": 4}})
    with pytest.raises(ValueError, match="assigned-stock ledger projection is untrusted"):
        _run("view", [_trade().to_dict(), _assign_put().to_dict(), adjust.to_dict()])
