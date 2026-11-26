from datetime import date

import pytest

from domain.domain.withdrawal_reserve import correct, reconcile, reserve_summary, safe_after_save_failure


TODAY = date(2026, 11, 26)
SOURCE = dict(record_id="pm1", account="lx", broker="富途", currency="HKD",
              flow_date="2026-11-25", amount="-10000")
FLOW = dict(cashflow_id="flow1", currency="HKD", cashflow_type="其他", cashflow_remark="",
            cashflow_direction="OUT", cashflow_amount="-10000", clearing_date="2026-11-25")


def run(previous=None, sources=None, flows=None, **kwargs):
    return reconcile(previous, [SOURCE] if sources is None else sources,
                     [] if flows is None else flows, today=TODAY, account="lx",
                     binding="futu:REAL:123", flow_complete=True, funds_usable=True, **kwargs)


def test_reserve_then_settle_does_not_create_cash():
    pending = run()
    assert reserve_summary(pending)["reserved_by_currency"] == {"HKD": 10000}
    settled = run(pending, flows=[FLOW])
    assert reserve_summary(settled)["reserved_by_currency"] == {}
    assert settled["claimed"] == {"flow1": "pm1"}
    assert run(settled, flows=[FLOW])["records"] == settled["records"]


@pytest.mark.parametrize("change", [{"amount": "-15000"}, {"amount": "-8000"},
                                   {"currency": "USD"}, {"broker": "other"}, {"account": "sy"}])
def test_settled_financial_revision_blocks_instead_of_guessing_difference(change):
    settled = run(flows=[FLOW])
    changed = run(settled, sources=[{**SOURCE, **change}], flows=[FLOW])
    assert reserve_summary(changed)["status"] == "unavailable"
    assert changed["claimed"] == settled["claimed"]


def test_remark_change_is_not_financial_revision():
    settled = run(flows=[FLOW])
    assert run(settled, sources=[{**SOURCE, "remark": "new note"}])["records"] == settled["records"]


def test_new_and_increased_reserve_save_failure_cannot_use_old_balance():
    empty = run(sources=[])
    assert not safe_after_save_failure(empty, run(empty))
    pending = run()
    assert not safe_after_save_failure(pending, run(pending, sources=[{**SOURCE, "amount": "-20000"}]))
    assert safe_after_save_failure(pending, run(pending, flows=[FLOW]))


@pytest.mark.parametrize("flow", [{**FLOW, "cashflow_type": "基金申购"},
                                 {**FLOW, "cashflow_remark": "Opt ASS-P"},
                                 {**FLOW, "currency": "USD"},
                                 {**FLOW, "cashflow_amount": "-9999"}])
def test_other_transactions_never_release(flow):
    assert reserve_summary(run(flows=[flow]))["reserved_by_currency"] == {"HKD": 10000}


def test_bidirectional_ambiguity_and_claims():
    state = run(sources=[SOURCE, {**SOURCE, "record_id": "pm2"}], flows=[FLOW])
    assert reserve_summary(state)["reserved_by_currency"] == {"HKD": 20000}
    corrected = correct(state, record_id="pm1", action="settle", flow_ids=["flow1"],
                        flows={"flow1": FLOW}, today=TODAY, reason="bank receipt")
    assert reserve_summary(run(corrected, sources=[SOURCE, {**SOURCE, "record_id": "pm2"}],
                               flows=[FLOW]))["reserved_by_currency"] == {"HKD": 10000}


def test_restore_is_manual_and_source_deletion_is_not_cancellation():
    settled = run(flows=[FLOW])
    restored = correct(settled, record_id="pm1", action="restore", flow_ids=[], flows={},
                       today=TODAY, reason="wrong match")
    assert run(restored, flows=[FLOW])["records"]["pm1"]["status"] == "manual"
    assert not safe_after_save_failure(settled, restored)
    assert reserve_summary(run(settled, sources=[]))["status"] == "unavailable"


def test_old_pending_survives_discovery_window():
    pending = run()
    pending["covered_since"] = "2026-12-20"
    updated = reconcile(pending, [SOURCE], [], today=date(2026, 12, 20), account="lx",
                        binding="futu:REAL:123", flow_complete=True, funds_usable=True)
    assert reserve_summary(updated)["reserved_by_currency"] == {"HKD": 10000}


def test_mixed_save_failure_is_unavailable():
    before = run()
    after = run(before, sources=[SOURCE, {**SOURCE, "record_id": "new", "amount": "-50000"}], flows=[FLOW])
    assert not safe_after_save_failure(before, after)


def test_manual_principal_plus_fee_and_disputed_claim_transfer():
    source = {**SOURCE, "amount": "-10020"}
    fee = {**FLOW, "cashflow_id": "fee1", "cashflow_type": "手续费", "cashflow_amount": "-20"}
    pending = run(sources=[source], flows=[FLOW, fee])
    assert reserve_summary(pending)["reserved_by_currency"] == {"HKD": 10020}
    settled = correct(pending, record_id="pm1", action="settle", flow_ids=["flow1", "fee1"],
                      flows={"flow1": FLOW, "fee1": fee}, today=TODAY, reason="verified receipt")
    restored = correct(settled, record_id="pm1", action="restore", flow_ids=[], flows={},
                       today=TODAY, reason="wrong source")
    second = {**source, "record_id": "pm2"}
    two = run(restored, sources=[source, second], flows=[FLOW, fee])
    moved = correct(two, record_id="pm2", action="settle", flow_ids=["flow1", "fee1"],
                    flows={"flow1": FLOW, "fee1": fee}, today=TODAY, reason="verified owner")
    assert moved["claimed"] == {"flow1": "pm2", "fee1": "pm2"}
    assert reserve_summary(moved)["reserved_by_currency"] == {"HKD": 10020}


def test_explicit_cancellation_of_deleted_source_survives_next_read():
    missing = run(run(), sources=[])
    assert reserve_summary(missing)["status"] == "unavailable"
    cancelled = correct(missing, record_id="pm1", action="cancel", flow_ids=[], flows={},
                        today=TODAY, reason="withdrawal cancelled and PM entry removed")
    assert run(cancelled, sources=[])["records"]["pm1"]["status"] == "cancelled"
    assert reserve_summary(run(cancelled, sources=[]))["status"] == "available"


def test_conflict_reversion_restores_current_pm_amount():
    settled = run(sources=[{**SOURCE, "amount": "-20000"}], flows=[{**FLOW, "cashflow_amount": "-20000"}])
    changed = run(settled, sources=[SOURCE])
    reverted = run(changed, sources=[{**SOURCE, "amount": "-20000"}])
    restored = correct(reverted, record_id="pm1", action="restore", flow_ids=[], flows={}, today=TODAY, reason="current PM")
    assert reserve_summary(restored)["reserved_by_currency"] == {"HKD": 20000}


def test_provisional_claim_does_not_hide_new_competitor():
    provisional = reconcile(None, [SOURCE], [FLOW], today=TODAY, account="lx", binding="futu:REAL:123",
                            flow_complete=True, funds_usable=False)
    next_state = run(provisional, sources=[SOURCE, {**SOURCE, "record_id": "pm2"}], flows=[FLOW])
    assert reserve_summary(next_state)["reserved_by_currency"] == {"HKD": 20000}
    assert next_state["records"]["pm1"]["status"] != "settled"


def test_provisional_claim_cannot_silently_change_flow():
    provisional = reconcile(None, [SOURCE], [FLOW], today=TODAY, account="lx", binding="futu:REAL:123",
                            flow_complete=True, funds_usable=False)
    next_state = run(provisional, flows=[{**FLOW, "cashflow_id": "replacement"}])
    assert next_state["records"]["pm1"]["flows"] == ["flow1"]
    assert next_state["records"]["pm1"]["status"] != "settled"


def test_disputed_claim_can_be_manually_transferred_after_original_cancelled():
    restored = correct(run(flows=[FLOW]), record_id="pm1", action="restore", flow_ids=[], flows={}, today=TODAY, reason="wrong match")
    cancelled = correct(restored, record_id="pm1", action="cancel", flow_ids=[], flows={}, today=TODAY, reason="cancelled")
    both = run(cancelled, sources=[SOURCE, {**SOURCE, "record_id": "pm2"}], flows=[FLOW])
    moved = correct(both, record_id="pm2", action="settle", flow_ids=["flow1"], flows={"flow1": FLOW}, today=TODAY, reason="verified owner")
    assert moved["claimed"] == {"flow1": "pm2"}


def test_settled_claim_does_not_hide_unique_new_outflow():
    settled = run(flows=[FLOW])
    next_state = run(settled, sources=[SOURCE, {**SOURCE, "record_id": "pm2"}],
                     flows=[FLOW, {**FLOW, "cashflow_id": "flow2"}])
    assert next_state["records"]["pm2"]["flows"] == ["flow2"]
    assert reserve_summary(next_state)["reserved_by_currency"] == {}


def test_money_scale_change_is_not_financial_revision():
    settled = run(flows=[FLOW])
    changed = run(settled, sources=[{**SOURCE, "amount": "-10000.0"}])
    assert changed["records"]["pm1"]["status"] == "settled"
