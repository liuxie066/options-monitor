"""Deterministic, provider-free inputs for the real Brief preparation benchmark.

Reuse the existing delivery-history, Meituan ledger and sealed-run fixture
contracts. Construction is deliberately excluded from measured samples.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from domain.domain.engine import build_candidate_decision
from src.application import daily_decision_brief_repository as repository
from src.application.opening_candidate_snapshot import seal_opening_candidate_snapshot
from src.application.trades.auto_intake import _process_payload
from test_daily_decision_brief_repository_v2 import _action, _brief
from test_daily_decision_brief_service import (
    _account_dir,
    _call_row,
    _config,
    _fixture_dependencies,
    _materialize_candidate_bundle_fixture,
    _write_close_report,
)
from test_trade_attribution_meituan import _call_payload, _meituan_repo

NOW = datetime(2026, 9, 28, 2, 0, 30, tzinfo=timezone.utc)
MARKET = "HK"
ACCOUNT = "lx"
RUN_ID = "run-1"
LAST_HISTORY_DATE = date(2026, 9, 27)


def seed_preparation_inputs(
    base: Path, *, days: int, fill_dates: int, fills_per_date: int,
    revisions_per_day: int = 2,
) -> dict:
    """Create real history DB, delivery envelopes, ledger and sealed run files.

    Historical envelopes follow seed_delivery_history's four-report/day shape;
    all dates are restored only after construction to keep fixture setup linear.
    The two revisions/day have distinct contents. No measured owner is patched.
    """
    ledger_repo, ledger_config, original_call = _meituan_repo(base)
    if original_call["status"] != "applied":
        raise AssertionError(f"real ingress rejected the original Meituan fill: {original_call}")
    for offset in range(fill_dates):
        day = LAST_HISTORY_DATE - timedelta(days=offset)
        for index in range(fills_per_date):
            # _meituan_repo already creates the first 2026-09-27 call.
            if offset == 0 and index == 0:
                continue
            payload = _call_payload(deal_id=f"baseline-call-{offset}-{index}", contracts=1)
            payload["occurred_at_utc"] = f"{day.isoformat()}T02:45:31Z"
            result = _process_payload(
                payload, repo=ledger_repo, state_path=base / "state.json", audit_path=base / "audit.jsonl",
                account_mapping={"1001": "lx"}, futu_account_ids=["1001"], apply_changes=True,
                host="127.0.0.1", port=11111, allow_external_lookup=False,
                config=ledger_config, runtime_root=base,
            )
            if result["status"] != "applied":
                raise AssertionError(f"real ingress rejected benchmark fill: {result}")

    collected: dict = {}
    delivery_path = repository._delivery_path(base, ACCOUNT, MARKET)
    for offset in range(days):
        day = (LAST_HISTORY_DATE - timedelta(days=offset)).isoformat()
        actions = [_action(symbol=f"{index:04d}.HK") for index in range(1, 25)]
        for action in actions:
            action.update(expiration="2026-11-27", contract_symbol=f"{action['symbol']}261127P00100000")
        for revision in range(revisions_per_day):
            brief = _brief(run_id=f"offline-{day}-{revision}", market=MARKET,
                           market_date=day, actions=actions)
            brief.update(generated_at_utc=f"{day}T02:00:00+00:00",
                         data_as_of_utc=f"{day}T01:59:00+00:00",
                         valid_until_utc=f"{day}T08:00:00+00:00",
                         strategy_summary=f"offline fixture revision {revision}")
            persisted = repository.persist_daily_decision_brief_success(base=base, brief=brief)
        for hour in (10, 11, 12, 13):
            prepared = repository.prepare_daily_decision_brief_delivery(
                base=base, account=ACCOUNT, market=MARKET, market_trading_date=day,
                run_id=brief["run_id"], delivery_kind="fixed_report", source_kind="successful_brief",
                source_digest=persisted["current_brief_digest"], revision=persisted["current_revision"],
                scheduled_target_market=f"{day}T{hour:02}:00:00+08:00",
                candidate_identities=persisted["current_candidate_identities"],
                rendered_message=("Offline option candidate facts. " * 540),
                prepared_at_utc=f"{day}T02:00:02+00:00",
            )
            delivery_path = prepared["paths"]["delivery"]
        raw = json.loads(delivery_path.read_text(encoding="utf-8"))
        collected.update(raw["days"])
        raw["days"] = {}
        delivery_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    raw["days"] = collected
    delivery_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    config = {**_config(timezone_name="Asia/Hong_Kong"), **ledger_config}
    config["notifications"].update(enabled=True, provider="feishu_app", channel="feishu_app",
                                   target="feishu:bot-user")
    config["symbols"] = ["3690.HK"]
    run_dir = _account_dir(base)
    stamp = (NOW - timedelta(seconds=30)).isoformat()
    for name in ("portfolio_context.json", "option_positions_context.json"):
        path = run_dir / "state" / name
        context = json.loads(path.read_text(encoding="utf-8"))
        context["as_of_utc"] = stamp
        if isinstance(context.get("exchange_rates"), dict):
            context["exchange_rates"]["timestamp"] = stamp
        path.write_text(json.dumps(context, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    candidate = _call_row(symbol="3690.HK", contract="HK.03690261127C80000")
    candidate.update(expiration="2026-11-27", strike=80, spot=80, dte=60,
                     multiplier=500, shares_total=2500, shares_locked=0,
                     shares_available_for_cover=2500, call_covered_contracts_available=5)
    seal_opening_candidate_snapshot(
        base=base, run_id=RUN_ID, account=ACCOUNT, market=MARKET,
        physical_account={"status": "available", "logical_account": ACCOUNT,
                          "futu_account_id": "1001", "trd_env": "REAL", "market": MARKET, "source": "opend"},
        account_config_sha256="f" * 64, strategy_policy_sha256="1" * 64,
        dependencies=_fixture_dependencies(),
        scan_statuses=[{"scope": "strategy", "symbol": "3690.HK", "strategy_mode": "call",
                        "status": "completed", "reason_code": None,
                        "quote_snapshot_id": None, "quote_receipt_relpath": None}],
        final_candidates={"call": [candidate]},
        candidate_evaluations={"call": [{"normalized_input": candidate,
            "opening_decision": build_candidate_decision(mode="call", symbol=candidate["symbol"],
                contract_symbol=candidate["contract_symbol"], accepted=True, normalized_input=candidate)}]},
        run_mode={"scan_mode": "standard", "executable": True}, sealed_at=stamp,
    )
    _write_close_report(run_dir, [], market=MARKET)
    _materialize_candidate_bundle_fixture(base)
    config_path = base / "benchmark_config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return config
