from __future__ import annotations

from pathlib import Path

from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.reconciliation import (
    assess_replay_determinism,
    assess_report_coverage,
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
