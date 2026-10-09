from copy import deepcopy
from src.application.portfolio_assignment_scenario import _futu_context_error, _futu_evidence, _futu_holdings


def with_sizing_evidence(context, *, quotes=(), positions=()):
    context = deepcopy(context)
    snapshot = context.get("position_snapshot_input")
    stocks = context.get("stocks_by_symbol")
    if isinstance(snapshot, dict) and not snapshot.get("rows") and isinstance(stocks, dict) and stocks:
        from src.application.futu_portfolio_context import build_futu_position_snapshot

        context["position_snapshot_input"] = build_futu_position_snapshot(
            rows=[
                {"code": code, "sec_type": "STOCK", "qty": stock["shares"], "currency": stock.get("currency", "USD")}
                for code, stock in stocks.items()
            ],
            broker_account_ref=snapshot["broker_account_ref"],
            markets=["US", "HK"],
            asset_types=["stock", "option"],
            observed_at_utc=snapshot["observed_at_utc"],
            completeness="complete",
        )
    data = _futu_evidence(["lx"], {"lx": context})
    error = _futu_context_error("lx", context)
    if error:
        data["status"] = "unavailable"
        data["warnings"] = [error]
    rows, warnings = ([], []) if error else _futu_holdings("lx", context, {q["code"]: q for q in quotes})
    data["holdings"] = rows
    data["quotes"] = list(quotes)
    data["warnings"] = list(data.get("warnings") or []) + warnings
    if warnings and not error:
        data["status"] = "partial"
    context["position_sizing_evidence"] = data
    context.setdefault("option_ctx", {}).update(
        context_status="available", decision_snapshot_status="trusted", assignment_positions=list(positions)
    )
    return context
