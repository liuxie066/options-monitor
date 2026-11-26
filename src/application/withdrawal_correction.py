"""Preview and confirm manual corrections using the shared reservation store."""
from datetime import datetime, timedelta, timezone
import json
from contextlib import ExitStack

from domain.domain.withdrawal_reserve import correct, fingerprint, normalize_source, reconcile, reserve_summary
from src.application.portfolio_management import resolve_portfolio_management_client
from src.application.withdrawal_reserve import (
    transaction, database_path, read_state, save_state, validate_pm, collect_flows, pending_cash_component, foreign_record_ids,
)


def correction(*, cfg, account, account_id, environment, gateway=None, gateway_factory=None, record_id, action,
               flow_ids, reason, actor, expected_hash=None, operation_id=None, path=None, client=None,
               now=None):
    """Preview by default; confirmed mutation rechecks live PM and broker evidence."""
    supplied_now = now
    now = now or datetime.now(timezone.utc)
    today = (now + timedelta(hours=8)).date()
    if not actor or not reason:
        raise ValueError("actor and reason required")
    with ExitStack() as resources:
        conn = resources.enter_context(transaction(path or database_path(), persist=False))
        state = read_state(conn, account)
        foreign_ids = foreign_record_ids(conn, account)
        if not state or state.get("binding") != f"futu:{environment}:{account_id}":
            raise ValueError("no matching reservation account")
        request = dict(record_id=record_id, action=action, flow_ids=flow_ids, reason=reason, actor=actor)
        if operation_id and operation_id in state["operations"]:
            previous = state["operations"][operation_id]
            if previous["request"] != request or previous["preview_hash"] != expected_hash:
                raise ValueError("operation identity reused")
            return previous
        pm = resolve_portfolio_management_client(cfg, client=client)
        sources = validate_pm(pm.read_cash_flows(account=account, timeout=15), account,
                                  supplied_now or datetime.now(timezone.utc))
        current = reconcile(state, sources, [], today=today, account=account,
                            binding=state["binding"], flow_complete=False, funds_usable=False,
                            foreign_record_ids=foreign_ids)
        source = current["records"][record_id].get("proposed", current["records"][record_id]["source"])
        if source["account"] != account or source["broker"] != "富途":
            raise ValueError("correct PM account binding before resolution")
        if action == "settle" and gateway is None:
            gateway = gateway_factory()
            resources.callback(gateway.close)
        query_state = json.loads(json.dumps(current))
        query_state["records"][record_id]["status"] = "manual"
        flows = collect_flows(gateway, sources, query_state, account_id=account_id, environment=environment,
                              today=today, controls={}) if action == "settle" else []
        flow_map = {}
        for row in flows:
            fid = str(row.get("cashflow_id") or "")
            if not fid or (fid in flow_map and flow_map[fid] != row):
                raise ValueError("invalid or conflicting cash flow identity")
            flow_map[fid] = row
        funds = None
        if action == "settle":
            funds = gateway.get_account_balance(acc_id=account_id, trd_env=environment,
                                               currency="CNH", refresh_cache=False)
            if hasattr(funds, "to_dict"):
                funds = funds.to_dict("records")
            if not isinstance(funds, list) or len(funds) != 1:
                raise ValueError("current funds unavailable")
            pending_cash_component(funds)
            from src.application.futu_portfolio_context import build_futu_portfolio_context
            from src.application.portfolio_context_service import evaluate_account_cash_snapshot

            context = build_futu_portfolio_context(
                balance_rows=funds, position_rows=[], account=account,
                cash_source_observed_at=now.isoformat(), futu_account_id=str(account_id),
                broker_account_identifiers={str(account_id)},
                trd_env=environment, capacity_market=str(cfg.get("_resolved", {}).get("market", "us")),
            )
            verdict = evaluate_account_cash_snapshot(context, config=cfg, account=account, evaluated_at=now)
            if verdict["status"] != "fresh":
                raise ValueError("current funds qualification unavailable")
            if str(funds[0].get("acc_id", account_id)) != str(account_id):
                raise ValueError("current funds account mismatch")
        proposed = correct(current, record_id=record_id, action=action, flow_ids=flow_ids,
                           flows=flow_map, today=today, reason=reason)
        token = fingerprint({
            "state": {key: state[key] for key in ("binding", "records", "claimed")},
            "source": sorted((normalize_source(row) for row in sources), key=lambda row: row["record_id"]),
            "request": request, "flows": sorted(flows, key=lambda row: str(row["cashflow_id"])),
            "funds": [{key: row.get(key) for key in
                       ("acc_id", "currency", "cn_cash", "hk_cash", "us_cash", "fund_assets", "pending_asset")}
                      for row in funds] if funds else None,
        })
        result = {"preview_hash": token, "request": request, "before": reserve_summary(state),
                  "after": reserve_summary(proposed), "applied": False,
                  "cash_snapshot": funds, "opening_capacity": None,
                  "opening_capacity_reason": "collateral_not_loaded_in_correction",
                  "flow_claims_before": state["claimed"], "flow_claims_after": proposed["claimed"],
                  "stored_record": state["records"][record_id],
                  "current_record": current["records"][record_id],
                  "selected_flows": [row for row in flows if str(row["cashflow_id"]) in flow_ids]}
        if expected_hash is not None:
            if not operation_id or expected_hash != token:
                raise ValueError("correction preview changed or operation ID missing")
            result.update(applied=True, observed_at=now.isoformat())
            proposed["operations"][operation_id] = result
            with transaction(path or database_path()) as writer:
                latest = read_state(writer, account)
                receipt = (latest or {}).get("operations", {}).get(operation_id)
                if receipt:
                    if receipt["request"] != request or receipt["preview_hash"] != expected_hash:
                        raise ValueError("operation identity reused")
                    return receipt
                if latest != state or foreign_record_ids(writer, account) != foreign_ids:
                    raise ValueError("correction preview changed during confirmation")
                save_state(writer, account, proposed)
        return result
