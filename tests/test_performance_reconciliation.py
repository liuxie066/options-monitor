from __future__ import annotations

from pathlib import Path

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.event_codec import trade_event_application_payload
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.reporting import build_monthly_income_report
from src.application.performance.reconciliation import (
    LEGACY_REFERENCE_ALLOWLIST,
    assess_replay_determinism,
    assess_report_coverage,
    reconcile_legacy_monthly_report,
    scan_legacy_references,
)
from src.application.performance.service import build_option_period_performance


def _amount(
    amounts: dict[str, float] | None = None,
    *,
    cny: float | None = 0.0,
    status: str = "observed",
    missing: list[str] | None = None,
) -> dict[str, object]:
    native = dict(amounts or {})
    has_non_cny = any(str(currency).upper() != "CNY" for currency in native)
    return {
        "by_currency": native,
        "cny": cny,
        "status": status,
        "missing": list(missing or []),
        "fx_fact_ids": ["fx:test"] if status == "observed" and cny is not None and has_non_cny else [],
    }


def _v1_report() -> dict[str, object]:
    zero = _amount()
    return {
        "schema_version": "option_performance_report.output.v1",
        "period": {
            "kind": "month",
            "requested_start_date": "2026-05-01",
            "requested_end_date": "2026-05-31",
            "reporting_timezone": "Asia/Shanghai",
        },
        "scope": {"account": "lx", "accounts": ["lx"]},
        "activity": {
            "premium_collected_gross": _amount({"USD": 250.0}, cny=1810.0),
            "premium_paid_gross": zero,
            "contracts_opened": 1,
            "contracts_closed": 1,
        },
        "cash": {
            "option_trade_cash_gross": _amount({"USD": 250.0}, cny=1810.0),
            "option_fee_cash": _amount({"USD": -5.0}, cny=-36.0),
            "stock_settlement_cash_gross": _amount({"USD": -10000.0}, cny=-72000.0),
            "stock_settlement_fee_cash": _amount({"USD": -2.0}, cny=-14.4),
            "assigned_stock_sale_cash_gross": _amount({"USD": 10500.0}, cny=75600.0),
            "assigned_stock_sale_fee_cash": _amount({"USD": -3.0}, cny=-21.6),
            "total_cash_change_net": _amount({"USD": 740.0}, cny=5338.0),
        },
        "pnl": {
            "realized_gross": _amount({"USD": 750.0}, cny=5410.0),
            "realized_net": _amount({"USD": 740.0}, cny=5338.0),
            "opening_unrealized_gross": zero,
            "opening_unrealized_net": zero,
            "ending_unrealized_gross": zero,
            "ending_unrealized_net": zero,
            "period_total_gross": _amount({"USD": 750.0}, cny=5410.0),
            "period_total_net": _amount({"USD": 740.0}, cny=5338.0),
        },
        "capital": {},
        "assignment_lifecycle": {},
        "breakdowns": {},
        "quality": {"status": "observed", "missing": [], "warnings": [], "evidence_fact_ids": []},
        "rows": [
            {
                "fact_kind": "realized_gross",
                "source_event_id": "assign-put",
                "allocation_id": "open-put:assign-put:1",
                "currency": "USD",
                "amount": 250.0,
            },
            {
                "fact_kind": "realized_gross",
                "source_event_id": "stock-sale",
                "allocation_id": None,
                "currency": "USD",
                "amount": 500.0,
            },
        ],
    }


def _legacy_report() -> dict[str, object]:
    return {
        "summary": [
            {
                "month": "2026-05",
                "account": "lx",
                "currency": "USD",
                "premium_received_gross": 250.0,
                "premium_received_gross_cny": 1800.0,
                "net_cashflow_gross": -9750.0,
                "net_cashflow_gross_cny": -70200.0,
                "assignment_stock_net_cashflow_gross": -10000.0,
                "assignment_stock_net_cashflow_gross_cny": -72000.0,
                "realized_pnl_gross": 250.0,
                "realized_pnl_gross_cny": 1800.0,
            }
        ],
        "rows": [
            {
                "event_id": "assign-put",
                "currency": "USD",
                "realized_pnl_gross": 250.0,
            }
        ],
        "cashflow_rows": [
            {"trade_action": "sell_open", "contracts": 1},
            {"trade_action": "assignment_option_close", "contracts": 1},
            {"trade_action": "assignment_stock_buy", "contracts": 0},
        ],
        "return_summary": [
            {
                "realized_return_rate": 0.01,
                "net_return_rate": 0.0,
                "premium_return_rate": 0.01,
            }
        ],
    }


def test_reconciliation_accepts_current_legacy_and_v1_builders_over_the_same_events(tmp_path: Path) -> None:
    key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-06-19",
    )
    open_event = TradeEvent(
        event_id="open-put",
        event_type="open",
        event_time_ms=1777773600000,
        contract_key=key,
        contracts=1,
        price=2.5,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        lot_id="lot-open-put",
        raw_payload={"fee_provenance": {"basis": "actual", "source": "test"}},
    )
    close_event = TradeEvent(
        event_id="close-put",
        event_type="close",
        event_time_ms=1779242400000,
        contract_key=key,
        contracts=1,
        price=1.0,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        target_lot_id="lot-open-put",
        raw_payload={"fee_provenance": {"basis": "actual", "source": "test"}},
    )
    repo = SQLiteOptionPositionsRepository(tmp_path / "reconciliation.sqlite3")
    repo.upsert_trade_event(open_event)
    repo.upsert_trade_event(close_event)
    legacy = build_monthly_income_report(
        [],
        account="lx",
        broker="futu",
        month="2026-05",
        rates={"USDCNY": 7.2},
        trade_events=[trade_event_application_payload(open_event), trade_event_application_payload(close_event)],
    )
    v1 = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-05"},
        account="lx",
        broker="futu",
        now_ms=1784300000000,
        include_rows=True,
        scope_proven=True,
    )

    result = reconcile_legacy_monthly_report(legacy, v1, scope_proven=True)

    assert result["status"] == "pass"
    assert all(item["status"] == "pass" for item in result["exact_checks"])
    assert all(item["status"] == "pass" for item in result["quantity_checks"])


def test_reconciliation_separates_exact_native_metrics_from_expected_semantic_deltas() -> None:
    result = reconcile_legacy_monthly_report(_legacy_report(), _v1_report(), scope_proven=True)

    assert result["status"] == "pass"
    exact = {item["name"]: item for item in result["exact_checks"]}
    assert exact["premium_collected_gross.native"]["status"] == "pass"
    assert exact["option_trade_cash_gross.native"]["status"] == "pass"
    assert exact["option_trade_cash_gross.native"]["legacy"] == {"USD": 250.0}
    assert exact["option_realized_gross.by_close_event"]["status"] == "pass"
    deltas = {item["name"]: item for item in result["expected_deltas"]}
    assert deltas["realized_net_vs_legacy_gross"]["code"] == "actual_fee_delta"
    assert deltas["realized_net_vs_legacy_gross"]["delta_by_currency"] == {"USD": -10.0}
    assert deltas["premium_cny"]["status"] == "classified"
    assert deltas["premium_cny"]["code"] == "effective_time_fx_vs_legacy_static_fx"
    assert deltas["period_total_gross_vs_legacy_option_realized"]["status"] == "classified"
    assert deltas["generic_return_rates"]["code"] == "intentional_removal_use_explicit_capital_efficiency"


def test_reconciliation_requires_realized_detail_rows_and_fails_one_sided_identity() -> None:
    missing_rows = _v1_report()
    missing_rows.pop("rows")
    omitted = reconcile_legacy_monthly_report(_legacy_report(), missing_rows, scope_proven=True)

    one_sided = _v1_report()
    one_sided["rows"] = []
    lost = reconcile_legacy_monthly_report(_legacy_report(), one_sided, scope_proven=True)

    assert omitted["status"] == "fail"
    assert omitted["exact_checks"][2]["missing"] == ["v1.rows"]
    assert lost["status"] == "fail"
    assert lost["exact_checks"][2]["delta"] == {"assign-put|USD": -250.0}


def test_reconciliation_accepts_explicitly_empty_realized_detail_sets() -> None:
    legacy = _legacy_report()
    legacy["rows"] = []
    report = _v1_report()
    report["rows"] = [row for row in report["rows"] if not row.get("allocation_id")]

    result = reconcile_legacy_monthly_report(legacy, report, scope_proven=True)

    assert result["exact_checks"][2]["status"] == "pass"
    assert result["exact_checks"][2]["delta"] == {}


def test_reconciliation_fails_if_assignment_principal_leaks_into_option_cash() -> None:
    report = _v1_report()
    report["cash"]["option_trade_cash_gross"] = _amount({"USD": -9750.0}, cny=-70200.0)

    result = reconcile_legacy_monthly_report(_legacy_report(), report, scope_proven=True)

    assert result["status"] == "fail"
    cash = next(item for item in result["exact_checks"] if item["name"] == "option_trade_cash_gross.native")
    assert cash["status"] == "fail"
    assert cash["delta"] == {"USD": -10000.0}


def test_replay_gate_hashes_canonical_json_and_detects_changes() -> None:
    first = _v1_report()
    reordered = {key: first[key] for key in reversed(list(first))}

    same = assess_replay_determinism(first, reordered)
    changed = assess_replay_determinism(first, {**first, "scope": {"account": "sy"}})

    assert same["status"] == "pass"
    assert same["first_sha256"] == same["second_sha256"]
    assert changed["status"] == "fail"
    assert changed["first_sha256"] != changed["second_sha256"]


def test_coverage_gate_preserves_gross_when_fee_fx_or_mark_evidence_is_missing() -> None:
    report = _v1_report()
    report["pnl"]["realized_net"] = _amount({}, cny=None, status="partial", missing=["fee:close-event"])
    report["pnl"]["realized_gross"] = _amount({"USD": 750.0}, cny=None, status="partial", missing=["fx:USD:realized"])
    report["pnl"]["ending_unrealized_gross"] = _amount({}, cny=None, status="partial", missing=["mark:open-lot"])
    report["pnl"]["ending_unrealized_net"] = _amount({}, cny=None, status="partial", missing=["mark:open-lot"])
    report["pnl"]["period_total_gross"] = _amount({}, cny=None, status="partial", missing=["mark:open-lot"])
    report["pnl"]["period_total_net"] = _amount(
        {}, cny=None, status="partial", missing=["fee:close-event", "mark:open-lot"]
    )

    result = assess_report_coverage(report, scope_proven=True)

    assert result["status"] == "pass"


def test_coverage_gate_checks_missing_as_zero_inside_breakdown_lists() -> None:
    report = _v1_report()
    report["breakdowns"] = {
        "monthly": [
            {
                "month": "2026-05",
                "pnl": {
                    "realized_net": _amount(
                        {},
                        cny=0.0,
                        status="partial",
                        missing=["fee:close-event"],
                    )
                },
            }
        ]
    }

    result = assess_report_coverage(report, scope_proven=True)

    assert result["status"] == "fail"
    assert "breakdowns.monthly[0].pnl.realized_net" in result["failures"]


def test_coverage_gate_rejects_observed_non_cny_metric_without_fx_evidence() -> None:
    report = _v1_report()
    report["pnl"]["realized_gross"] = {
        "by_currency": {"USD": 750.0},
        "cny": None,
        "status": "observed",
        "missing": [],
        "fx_fact_ids": [],
    }

    result = assess_report_coverage(report, scope_proven=True)

    assert result["status"] == "fail"
    check = next(item for item in result["checks"] if item["name"] == "pnl.realized_gross")
    assert check["reasons"] == [
        "observed_metric_missing_cny_value",
        "observed_non_cny_metric_missing_fx_evidence",
    ]


def test_reconciliation_classifies_missing_fee_as_incomplete_not_actual_delta() -> None:
    report = _v1_report()
    report["pnl"]["realized_net"] = _amount(
        {},
        cny=None,
        status="partial",
        missing=["fee:close-event"],
    )

    result = reconcile_legacy_monthly_report(_legacy_report(), report, scope_proven=True)

    delta = next(item for item in result["expected_deltas"] if item["name"] == "realized_net_vs_legacy_gross")
    assert delta["code"] == "fee_coverage_incomplete"
    assert delta["delta_by_currency"] is None


def test_coverage_gate_rejects_missing_evidence_encoded_as_zero() -> None:
    report = _v1_report()
    report["pnl"]["realized_net"] = _amount({}, cny=0.0, status="partial", missing=["fee:close-event"])

    result = assess_report_coverage(report, scope_proven=True)

    assert result["status"] == "fail"
    assert "pnl.realized_net" in result["failures"]


def test_proven_scope_does_not_promote_undecodable_event_to_observed_zero() -> None:
    class CorruptRepo:
        def list_position_lots(self) -> list[dict[str, object]]:
            return []

        def list_trade_events(self) -> list[dict[str, object]]:
            return [
                {
                    "event_id": "bad-open",
                    "event_type": "open",
                    "event_time_ms": 1777773600000,
                    "contract_key": {"account": "lx", "broker": "富途"},
                    "contracts": "invalid",
                }
            ]

        def list_assigned_stock_events(self) -> list[dict[str, object]]:
            return []

        def upsert_trade_event(self, *_args: object, **_kwargs: object) -> None:
            return None

        def replace_position_lots(self, *_args: object, **_kwargs: object) -> None:
            return None

    report = build_option_period_performance(
        CorruptRepo(),
        period={"period": "month", "month": "2026-05"},
        account="lx",
        now_ms=1784300000000,
        include_rows=False,
        scope_proven=True,
    )

    assert report["quality"]["status"] == "partial"
    assert any("decode_failed:bad-open" in item for item in report["quality"]["warnings"])
    assert report["activity"]["premium_collected_gross"]["status"] == "not_observed"
    assert report["activity"]["premium_collected_gross"]["cny"] is None


def test_proven_empty_scope_is_observed_zero_but_unproven_scope_remains_not_observed(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "empty.sqlite3")
    kwargs = {
        "period": {"period": "month", "month": "2026-05"},
        "account": "lx",
        "now_ms": 1784300000000,
        "include_rows": False,
    }

    unproven = build_option_period_performance(repo, **kwargs)
    proven = build_option_period_performance(repo, **kwargs, scope_proven=True)

    assert unproven["activity"]["premium_collected_gross"]["status"] == "not_observed"
    assert unproven["activity"]["premium_collected_gross"]["cny"] is None
    assert proven["activity"]["premium_collected_gross"] == {
        "by_currency": {},
        "cny": 0.0,
        "status": "observed",
        "missing": [],
        "fx_fact_ids": [],
    }
    assert proven["pnl"]["period_total_net"]["status"] == "observed"
    assert proven["quality"]["status"] == "observed"
    assert assess_report_coverage(proven, scope_proven=True)["status"] == "pass"


def test_legacy_reference_inventory_matches_explicit_allowlist() -> None:
    root = Path(__file__).resolve().parents[1] / "src"

    result = scan_legacy_references(root)

    assert result["status"] == "pass"
    assert result["unowned"] == []
    assert result["stale_allowlist"] == []
    assert set(result["matches"]) == set(LEGACY_REFERENCE_ALLOWLIST)
    assert {item["category"] for item in result["matches"].values()} == {
        "deprecated_adapter_rollback",
        "deprecated_compatibility_projection",
        "candidate_strategy_domain",
    }
