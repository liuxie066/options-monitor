"""Synthetic pre-amendment oracle; never reads providers or production state."""

import math
from datetime import datetime
from pathlib import Path
import pandas as pd
import pytest
import test_strategy_core_integration as old
import cash_evidence_helpers
from test_opening_candidate_snapshot import _seal_snapshot, _scan_status

BASE = "59684da956c5f56dcb1fd894887bd107907d771a"
OMITTED = {"strategy_core", "candidate_id", "decision_hash", "risk_policy_hash", "normalized_input_hash"}


def clean(value, artifact_root):
    if isinstance(value, dict):
        return {
            k: str(Path(v).relative_to(artifact_root))
            if k == "earnings_artifact_path" and v
            else clean(v, artifact_root)
            for k, v in value.items()
            if k not in OMITTED
        }
    if isinstance(value, (list, tuple)):
        return [clean(v, artifact_root) for v in value]
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return clean(value.item(), artifact_root)
    return value


def capture(root):
    cases = {}

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return old.NOW if tz is None else old.NOW.astimezone(tz)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cash_evidence_helpers, "datetime", FixedDatetime)
        patch.setattr(old.scan_sell_put, "datetime", FixedDatetime)
        patch.setattr(old.scan_sell_call, "datetime", FixedDatetime)
        for case in ("funded", "zero", "missing", "occupied", "fx_fresh", "fx_stale", "demo"):
            base = root / ("put_" + case)
            base.mkdir()
            old._write_calendar(base)
            ctx = old._portfolio()
            if case == "zero":
                ctx["cash_by_currency"] = {"USD": 0}
            if case == "missing":
                ctx.pop("cash_by_currency")
            if case == "occupied":
                ctx["option_ctx"]["cash_secured_total_by_ccy"] = {"USD": 95000}
            if case.startswith("fx_"):
                ctx["cash_by_currency"] = {"CNY": 1000000}
                ctx["_sell_put_fx_status"] = "stale" if case == "fx_stale" else "fresh"
            with pytest.MonkeyPatch.context() as fx_patch:
                if case == "fx_stale":
                    fx_patch.setattr(old, "CONVERTER", old.CurrencyConverter(old.ExchangeRates()))
                if case == "demo":
                    finals = []
                    decisions = []
                    summaries = []
                    for sym in ("NVDA", "MSFT"):
                        summaries.extend(
                            old.run_sell_put_scan_and_summarize(
                                symbol=sym,
                                symbol_cfg={},
                                sp={"strategy": "insurance_underwriting"},
                                required_data_dir=base,
                                required_data_frame=pd.DataFrame([old._put_quote(sym, k, 3.0) for k in (95, 96)]),
                                portfolio_ctx=ctx,
                                exchange_rate_converter=old.CONVERTER,
                                demo_capacity=True,
                                final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
                                candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
                            )
                        )
                else:
                    finals, decisions, summaries = old._run_puts(base, injected=False, context=ctx)
            snap = _seal_snapshot(
                base,
                run_id="capture",
                scan_statuses=[_scan_status(s, "put", "completed") for s in ("NVDA", "MSFT")],
                final_candidates={"put": finals},
                candidate_evaluations={"put": decisions},
            )
            cases["put_" + case] = clean(
                {
                    "finals": finals,
                    "decisions": decisions,
                    "summaries": summaries,
                    "ranked": snap["ranked_candidates"],
                    "sealed_decisions": snap["candidate_decisions"],
                },
                root,
            )
        for case in ("covered", "locked", "multiple_rejects", "demo"):
            base = root / ("call_" + case)
            base.mkdir()
            old._write_calendar(base)
            finals = []
            decisions = []
            summaries = []
            for sym in ("NVDA", "MSFT"):
                cc = {"strategy": "insurance_underwriting"}
                if case == "multiple_rejects":
                    cc.update(min_strike=130, min_net_income=99999, min_annualized_return=0.9)
                summaries.append(
                    old.run_sell_call_scan_and_summarize(
                        symbol=sym,
                        symbol_cfg={},
                        cc=cc,
                        stock={"shares": 200, "can_sell_qty": 200, "avg_cost": 100},
                        portfolio_ctx={
                            "filters": {"account": "lx"},
                            "portfolio_source_name": "futu",
                            "capacity_authority": {"status": "available"},
                        },
                        locked_shares_status="available",
                        locked_shares_by_symbol={sym: 200} if case == "locked" else {},
                        locked_shares_unavailable_by_symbol={},
                        required_data_dir=base,
                        required_data_frame=pd.DataFrame([old._quote(sym, strike=k) for k in (110, 115, 120)]),
                        exchange_rate_converter=old.CONVERTER,
                        demo_capacity=(case == "demo"),
                        final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
                        candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
                    )
                )
            snap = _seal_snapshot(
                base,
                run_id="capture",
                scan_statuses=[_scan_status(s, "call", "completed") for s in ("NVDA", "MSFT")],
                final_candidates={"call": finals},
                candidate_evaluations={"call": decisions},
            )
            cases["call_" + case] = clean(
                {
                    "finals": finals,
                    "decisions": decisions,
                    "summaries": summaries,
                    "ranked": snap["ranked_candidates"],
                    "sealed_decisions": snap["candidate_decisions"],
                },
                root,
            )
    return cases
