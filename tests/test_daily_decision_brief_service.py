from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
    STAGE_INPUT_NORMALIZATION,
    build_candidate_decision,
)
from src.application.multi_tick.misc import AccountResult
from src.application.close_advice_report_manifest import (
    publish_close_advice_report_manifest,
)
from src.application.opening_candidate_snapshot import (
    seal_opening_candidate_snapshot,
)


def _account_dir(base: Path, run_id: str = "run-1", account: str = "lx") -> Path:
    path = base / "output_runs" / run_id / "accounts" / account
    path.mkdir(parents=True, exist_ok=True)
    state_dir = path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "portfolio_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:00+00:00",
                "cash_by_currency": {"HKD": 480_000, "USD": 18_000},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {"HKD": 255_000, "USD": 3_000},
                "cash_secured_unavailable_by_symbol": {},
                "cash_secured_total_cny": 250_500.0,
                "exchange_rates": {
                    "rates": {"USDCNY": 7.0, "HKDCNY": 0.9},
                    "timestamp": "2026-07-17T13:59:30+00:00",
                },
                "filters": {"account": account},
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_close_report(
    account_dir: Path,
    rows: list[dict[str, Any]],
    *,
    run_id: str = "run-1",
    market: str = "US",
) -> None:
    csv_path = account_dir / "close_advice.csv"
    text_path = account_dir / "close_advice.txt"
    if rows:
        pd.DataFrame(rows).to_csv(csv_path, index=False)
    else:
        csv_path.write_text("", encoding="utf-8")
    text_path.write_text("", encoding="utf-8")
    context_path = account_dir / "state" / "option_positions_context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    publish_close_advice_report_manifest(
        csv_path=csv_path,
        text_path=text_path,
        context_path=context_path,
        context=context,
        rows=rows,
        markets_to_run=[market],
        run_id=run_id,
        quote_mode="frozen_snapshot",
    )


def _config(*, timezone_name: str = "America/New_York") -> dict:
    return {
        "schedule": {
            "timezone": timezone_name,
            "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        },
        "notifications": {"daily_brief": {"max_candidates_per_strategy": 3}},
    }


def _live_window_config(now_utc: datetime) -> dict:
    offset_hours = 12 - now_utc.hour
    if offset_hours > 0:
        timezone_name = f"Etc/GMT-{offset_hours}"
    elif offset_hours < 0:
        timezone_name = f"Etc/GMT+{abs(offset_hours)}"
    else:
        timezone_name = "Etc/GMT"
    return _config(timezone_name=timezone_name)


def _result(*, ran_scan: bool = True, reason: str = "ok") -> AccountResult:
    return AccountResult("lx", ran_scan, True, reason, "legacy markdown must not be parsed")


def test_brief_omits_retired_ai_decision_advice_section(tmp_path: Path) -> None:
    brief = _assemble(tmp_path)
    assert "ai_decision_advice" not in brief
    assert "ai_decision_advice_evidence_index" not in brief


def test_brief_uses_explicit_candidate_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import daily_decision_brief_service as service

    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    _materialize_opening_snapshot_fixture(tmp_path, market="US")
    _materialize_candidate_bundle_fixture(tmp_path)
    snapshot = json.loads(
        (account_dir / "state" / "opening_candidate_snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    bundle_loads: list[dict[str, Any]] = []
    original_load_bundle = service.load_candidate_snapshot_bundle

    def load_bundle(**kwargs):
        bundle_loads.append(dict(kwargs))
        return original_load_bundle(**kwargs)

    monkeypatch.setattr(service, "load_candidate_snapshot_bundle", load_bundle)

    brief = service.assemble_daily_decision_brief(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        scheduler_decision={"in_run_window": True},
        account_result=_result(),
        pipeline_succeeded=True,
        config=_config(),
        now_utc=datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
        opening_candidate_snapshot=snapshot,
    )

    assert brief["candidates"]["sell_put"]
    assert len(bundle_loads) == 1
    assert "ai_decision_advice" not in brief
    assert "ai_decision_advice_evidence_index" not in brief


def _assemble(
    base: Path,
    *,
    market: str = "US",
    result: AccountResult | None = None,
    pipeline_succeeded: bool = True,
    config: dict | None = None,
    now_utc: datetime | None = None,
):
    from src.application.daily_decision_brief_service import assemble_daily_decision_brief

    _materialize_opening_snapshot_fixture(base, market=market)
    _materialize_combo_snapshot_fixture(base, market=market)
    _materialize_candidate_bundle_fixture(base)
    return assemble_daily_decision_brief(
        base=base,
        run_id="run-1",
        account="lx",
        market=market,
        scheduler_decision={"in_run_window": True},
        account_result=result or _result(),
        pipeline_succeeded=pipeline_succeeded,
        config=config or _config(timezone_name="Asia/Hong_Kong" if market == "HK" else "America/New_York"),
        now_utc=now_utc
        or datetime(2026, 7, 17, 14, 0, tzinfo=timezone.utc),
    )


def _fixture_dependencies() -> list[dict[str, Any]]:
    return [
        {"kind": kind, "relpath": None, "sha256": char * 64}
        for kind, char in (
            ("required_data", "a"),
            ("portfolio", "b"),
            ("ledger", "c"),
            ("fx", "d"),
            ("earnings_rv", "e"),
        )
    ]


def _fixture_symbol_from_path(path: Path, marker: str) -> str:
    return path.name.split(marker, 1)[0].upper()


def _materialize_opening_snapshot_fixture(base: Path, *, market: str) -> None:
    """Translate legacy test setup into the current sealed-source contract.

    Individual scenarios below still use compact CSV setup as fixture syntax;
    production code never reads those files as opening-candidate authority.
    """

    account_dir = base / "output_runs" / "run-1" / "accounts" / "lx"
    if not account_dir.is_dir():
        return
    if (
        account_dir / "state" / "candidate_snapshot_manifest.v1.json"
    ).is_file():
        return
    path_groups = {
        "put": sorted(account_dir.glob("*_sell_put_candidates_labeled.csv")),
        "call": sorted(account_dir.glob("*_sell_call_candidates.csv")),
    }
    if not any(path_groups.values()):
        return

    rows_by_mode: dict[str, list[dict]] = {"put": [], "call": []}
    scopes_by_mode: dict[str, dict[str, dict[str, Any]]] = {
        "put": {},
        "call": {},
    }
    markers = {
        "put": "_sell_put_candidates_labeled.csv",
        "call": "_sell_call_candidates.csv",
    }
    for mode, paths in path_groups.items():
        for path in paths:
            path_symbol = _fixture_symbol_from_path(path, markers[mode])
            scope = {
                "scope": "strategy",
                "symbol": path_symbol,
                "strategy_mode": mode,
                "status": "completed",
                "reason_code": None,
                "quote_snapshot_id": None,
                "quote_receipt_relpath": None,
            }
            try:
                raw = path.read_bytes()
                if raw in {b"\n", b"\r\n"}:
                    frame = pd.DataFrame()
                else:
                    frame = pd.read_csv(path)
            except Exception:
                scope["status"] = "unavailable"
                scope["reason_code"] = "candidate_fixture_parse_failed"
                scopes_by_mode[mode][path_symbol] = scope
                continue
            frame_rows = json.loads(frame.to_json(orient="records"))
            for item in frame_rows:
                row = dict(item)
                raw_events = row.get("earnings_events")
                if isinstance(raw_events, str):
                    try:
                        parsed_events = json.loads(raw_events)
                    except json.JSONDecodeError:
                        parsed_events = []
                    row["earnings_events"] = (
                        parsed_events
                        if isinstance(parsed_events, list)
                        else []
                    )
                rows_by_mode[mode].append(row)
                row_symbol = str(row.get("symbol") or path_symbol).upper()
                scopes_by_mode[mode].setdefault(
                    row_symbol,
                    {
                        **scope,
                        "symbol": row_symbol,
                    },
                )
            if not frame_rows:
                scopes_by_mode[mode].setdefault(path_symbol, scope)

    market_norm = market.upper()
    for mode in scopes_by_mode:
        scopes_by_mode[mode] = {
            symbol: scope
            for symbol, scope in scopes_by_mode[mode].items()
            if ("HK" if symbol.endswith(".HK") else "US") == market_norm
        }
    for mode, rows in rows_by_mode.items():
        deduped: dict[tuple[str, str, str, str], dict] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            row_market = "HK" if symbol.endswith(".HK") else "US"
            if row_market != market_norm:
                continue
            identity = (
                symbol,
                str(row.get("contract_symbol") or row.get("code") or ""),
                str(row.get("expiration") or ""),
                str(row.get("strike") or ""),
            )
            deduped.setdefault(identity, row)
        rows_by_mode[mode] = list(deduped.values())

    for mode, scopes in scopes_by_mode.items():
        family = "sell_put" if mode == "put" else "covered_call"
        for scope in scopes.values():
            status_path = account_dir / f"{str(scope['symbol']).lower()}_{family}_scan_status.json"
            try:
                status_payload = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                status_payload.get("run_id") == "run-1"
                and status_payload.get("account") == "lx"
                and status_payload.get("strategy_family") == family
            ):
                scope["status"] = status_payload.get("status")
                scope["reason_code"] = (
                    status_payload.get("reason_code")
                    or status_payload.get("reason")
                )
                scope["quote_snapshot_id"] = status_payload.get("snapshot_id")
                scope["quote_receipt_relpath"] = status_payload.get("receipt_relpath")

    observed_modes = [
        mode for mode in ("put", "call") if scopes_by_mode[mode]
    ]
    scan_statuses: list[dict[str, Any]] = []
    candidate_evaluations: dict[str, list[dict[str, Any]]] = {
        mode: [] for mode in observed_modes
    }
    for mode in observed_modes:
        scan_statuses.extend(
            {
                "symbol": scope["symbol"],
                "strategy_mode": mode,
                "status": scope["status"],
                "reason": scope.get("reason_code"),
                "quote_snapshot_id": scope.get("quote_snapshot_id"),
                "quote_receipt_relpath": scope.get("quote_receipt_relpath"),
            }
            for scope in scopes_by_mode[mode].values()
        )
        for facts in rows_by_mode[mode]:
            candidate_evaluations[mode].append(
                {
                    "normalized_input": facts,
                    "opening_decision": build_candidate_decision(
                        mode=mode,
                        symbol=str(facts.get("symbol") or ""),
                        contract_symbol=str(facts.get("contract_symbol") or ""),
                        accepted=True,
                        normalized_input=facts,
                    ),
                }
            )

    seal_opening_candidate_snapshot(
        base=base,
        run_id="run-1",
        account="lx",
        market=market_norm,
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": market_norm,
            "source": "opend",
        },
        account_config_sha256="f" * 64,
        strategy_policy_sha256="1" * 64,
        dependencies=_fixture_dependencies(),
        scan_statuses=scan_statuses,
        final_candidates={
            mode: rows_by_mode[mode] for mode in observed_modes
        },
        candidate_evaluations=candidate_evaluations,
        sealed_at="2026-07-17T13:59:59Z",
    )


def _materialize_combo_snapshot_fixture(base: Path, *, market: str) -> None:
    """Translate legacy Combo CSV setup into the sealed Combo snapshot contract."""

    from src.application.combo_yield_candidate_snapshot import (
        seal_combo_yield_candidate_snapshot,
    )
    from domain.domain.engine import select_best_yield_enhancement_per_symbol

    account_dir = base / "output_runs" / "run-1" / "accounts" / "lx"
    if not account_dir.is_dir():
        return
    combo_paths = sorted(account_dir.glob("*_combo_yield_candidates.csv"))
    if not combo_paths:
        return
    snapshot_path = account_dir / "state" / "combo_yield_candidate_snapshot.json"
    if snapshot_path.is_file():
        return
    pairs: list[dict[str, Any]] = []
    scope_symbols: set[str] = set()
    for path in combo_paths:
        scope_symbols.add(
            _fixture_symbol_from_path(path, "_combo_yield_candidates.csv")
        )
        try:
            raw = path.read_bytes()
            if raw in {b"\n", b"\r\n"}:
                frame = pd.DataFrame()
            else:
                frame = pd.read_csv(path)
        except Exception:
            continue
        for item in json.loads(frame.to_json(orient="records")):
            row = dict(item)
            symbol = str(row.get("symbol") or "").upper()
            row_market = "HK" if symbol.endswith(".HK") else "US"
            if row_market != market.upper():
                continue
            scope_symbols.add(symbol)
            row.setdefault("candidate_pair_id", row.get("strategy_group_id") or "")
            pairs.append(row)
    ranked_pairs = select_best_yield_enhancement_per_symbol(pairs)
    rank_records = [
        {
            **pair,
            "baseline_rank": rank,
            "shadow_rank": rank,
            "baseline_selected": True,
            "shadow_selected": True,
            "rank_changed": False,
        }
        for rank, pair in enumerate(ranked_pairs, start=1)
    ]
    seal_combo_yield_candidate_snapshot(
        base=base,
        run_id="run-1",
        account="lx",
        market=market.lower(),
        account_config_sha256="f" * 64,
        strategy_policy_sha256="1" * 64,
        dependencies=_fixture_dependencies(),
        scan_statuses=[
            {
                "symbol": symbol,
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": "completed",
                "quote_snapshot_id": None,
                "quote_receipt_relpath": None,
            }
            for symbol in sorted(scope_symbols)
        ],
        pair_evaluations=[
            {
                **pair,
                "diagnostic_scope": "pair",
                "diagnostic_stage": "pair_filter",
                "accepted": True,
                "reject_reasons": "",
            }
            for pair in ranked_pairs
        ],
        rank_records=rank_records,
        ranked_pairs=ranked_pairs,
        sealed_at="2026-07-17T13:59:59Z",
    )


def _seal_combo_status_snapshot(
    base: Path,
    *,
    opening_status: str,
    ranked_pairs: list[dict[str, Any]] | None = None,
) -> None:
    from src.application.combo_yield_candidate_snapshot import (
        seal_combo_yield_candidate_snapshot,
    )

    status_map = {
        "partial_data": ("completed", "partial_data"),
        "data_unavailable": ("unavailable", "data_unavailable"),
        "not_applicable": ("not_applicable", "not_applicable_for_test"),
        "no_candidate": ("completed", None),
        "candidates_found": ("completed", None),
    }
    scope_status, scope_reason = status_map[opening_status]
    pairs = list(ranked_pairs or [])
    seal_combo_yield_candidate_snapshot(
        base=base,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="f" * 64,
        strategy_policy_sha256="1" * 64,
        dependencies=_fixture_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "combo_yield",
                "variant": "sp_lc",
                "status": scope_status,
                "reason": scope_reason,
            }
        ],
        pair_evaluations=[
            {
                **pair,
                "diagnostic_scope": "pair",
                "diagnostic_stage": "pair_filter",
                "accepted": True,
                "reject_reasons": "",
            }
            for pair in pairs
        ],
        rank_records=[
            {
                **pair,
                "baseline_rank": rank,
                "shadow_rank": rank,
                "baseline_selected": True,
                "shadow_selected": True,
                "rank_changed": False,
            }
            for rank, pair in enumerate(pairs, start=1)
        ],
        ranked_pairs=pairs,
        opening_status=opening_status,
        sealed_at="2026-07-17T13:59:59Z",
    )


def _materialize_candidate_bundle_fixture(base: Path) -> None:
    from src.application.candidate_snapshot_manifest import (
        CANDIDATE_SNAPSHOT_MANIFEST_FILE,
        publish_candidate_snapshot_manifest,
    )
    from src.application.strategy_scan_status import (
        publish_strategy_scan_status,
        publish_strategy_scan_status_index_v2,
        strategy_status_path,
    )

    account_dir = base / "output_runs" / "run-1" / "accounts" / "lx"
    if not account_dir.is_dir():
        return
    manifest_path = account_dir / "state" / CANDIDATE_SNAPSHOT_MANIFEST_FILE
    if manifest_path.is_file():
        return
    snapshot_paths = {
        "opening": account_dir / "state" / "opening_candidate_snapshot.json",
        "sp_lc": account_dir / "state" / "combo_yield_candidate_snapshot.json",
        "cc_lp": account_dir / "state" / "cc_lp_candidate_snapshot.json",
    }
    snapshots: dict[str, dict[str, Any]] = {}
    for owner, path in snapshot_paths.items():
        if path.is_file():
            snapshots[owner] = json.loads(path.read_text(encoding="utf-8"))

    expected: list[dict[str, str]] = []
    for owner, snapshot in snapshots.items():
        counts: dict[tuple[str, str], int] = {}
        selected = (
            snapshot.get("ranked_candidates")
            if owner == "opening"
            else snapshot.get("ranked_pairs")
        ) or []
        for row in selected:
            facts = row.get("facts") if owner == "opening" else row
            facts = facts if isinstance(facts, dict) else {}
            key = (
                str(facts.get("symbol") or row.get("symbol") or "").upper(),
                str(row.get("strategy_mode") or "combo_yield").lower(),
            )
            counts[key] = counts.get(key, 0) + 1
        for scope in snapshot.get("scope_results") or []:
            if not isinstance(scope, dict) or scope.get("scope") != "strategy":
                continue
            symbol = str(scope.get("symbol") or "").upper()
            mode = str(scope.get("strategy_mode") or "").lower()
            family = {
                "put": "sell_put",
                "call": "covered_call",
                "combo_yield": "combo_yield",
            }[mode]
            status = str(scope.get("status") or "").lower()
            reason = str(scope.get("reason_code") or "").strip() or None
            status_path = strategy_status_path(
                report_dir=account_dir,
                symbol=symbol,
                strategy_family=family,
            )
            keep_existing = False
            try:
                existing = json.loads(status_path.read_text(encoding="utf-8"))
                keep_existing = (
                    existing.get("run_id") == "run-1"
                    and existing.get("account") == "lx"
                    and existing.get("market") == str(snapshot.get("market") or "").upper()
                    and existing.get("symbol") == symbol
                    and existing.get("strategy_family") == family
                    and existing.get("status") == status
                    and (
                        status != "completed"
                        or int(existing.get("candidate_count", -1))
                        == counts.get((symbol, mode), 0)
                    )
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                keep_existing = False
            if not keep_existing:
                publish_strategy_scan_status(
                    report_dir=account_dir,
                    run_id="run-1",
                    account="lx",
                    market=str(snapshot.get("market") or "").upper(),
                    symbol=symbol,
                    strategy_family=family,
                    status=status,
                    candidate_count=(
                        counts.get((symbol, mode), 0)
                        if status == "completed"
                        else None
                    ),
                    reason=reason or (
                        f"{owner}_fixture_{status}"
                        if status != "completed"
                        else None
                    ),
                    snapshot_id=scope.get("quote_snapshot_id"),
                    receipt_relpath=scope.get("quote_receipt_relpath"),
                )
            expected.append(
                {
                    "market": str(snapshot.get("market") or "").upper(),
                    "symbol": symbol,
                    "strategy_family": family,
                    "strategy_mode": mode,
                    "candidate_owner": owner,
                    "account_config_sha256": "f" * 64,
                }
            )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        account_config_sha256="f" * 64,
        expected=expected,
    )
    publish_candidate_snapshot_manifest(
        base=base,
        run_id="run-1",
        account="lx",
        strategy_policy_sha256="1" * 64,
        sealed_at="2026-07-17T13:59:59Z",
    )


def _put_row(
    *,
    symbol: str = "NVDA",
    contract: str = "NVDA260821P00100000",
    annualized: float = 0.2,
    priority: str | None = None,
) -> dict:
    row = {
        "symbol": symbol,
        "option_type": "put",
        "contract_symbol": contract,
        "expiration": "2026-08-21",
        "strike": 100,
        "spot": 120,
        "dte": 35,
        "delta": -0.2,
        "annualized_net_return_on_cash_basis": annualized,
        "net_income": 200,
        "spread_ratio": 0.1,
        "open_interest": 500,
        "volume": 20,
        "cash_required_cny": 10_000,
        "cash_free_cny": 25_000,
        "earnings_snapshot_hash": "e" * 64,
    }
    row.update(_earnings_evidence())
    if priority is not None:
        row["tier"] = priority
    return row


def _earnings_evidence(
    *,
    event_date: str | None = None,
    expiration: str = "2026-08-21",
    event_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_day = date.fromisoformat("2026-07-17")
    expiration_day = date.fromisoformat(expiration)
    hard_start = max(
        market_day,
        expiration_day - timedelta(days=EARNINGS_NEAR_EXPIRY_WINDOW_DAYS),
    )
    event = None
    if event_date is not None:
        days_before_expiration = (
            expiration_day - date.fromisoformat(event_date)
        ).days
        blocking = days_before_expiration <= EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
        event = {
            **dict(event_extra or {}),
            "earnings_date": event_date,
            "days_before_expiration": days_before_expiration,
            "classification": "blocking" if blocking else "nonblocking",
            "blocking": blocking,
        }
    events = [] if event is None else [event]
    blocking_events = [item for item in events if item["blocking"]]
    nonblocking_events = [item for item in events if not item["blocking"]]
    soft_end = hard_start - timedelta(days=1) if hard_start > market_day else None
    return {
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": market_day.isoformat(),
        "earnings_hard_window_start": hard_start.isoformat(),
        "earnings_hard_window_end": expiration_day.isoformat(),
        "earnings_hard_coverage_status": "complete",
        "earnings_hard_reason_codes": [],
        "earnings_hard_failed_intervals": [],
        "earnings_soft_window_start": (
            market_day.isoformat() if soft_end is not None else None
        ),
        "earnings_soft_window_end": (
            soft_end.isoformat() if soft_end is not None else None
        ),
        "earnings_soft_coverage_status": (
            "complete" if soft_end is not None else "not_applicable"
        ),
        "earnings_soft_reason_codes": [],
        "earnings_soft_failed_intervals": [],
        "earnings_has_event": bool(events),
        "earnings_blocking_has_event": bool(blocking_events),
        "earnings_event_dates": ",".join(
            item["earnings_date"] for item in events
        ),
        "earnings_blocking_event_dates": ",".join(
            item["earnings_date"] for item in blocking_events
        ),
        "earnings_nonblocking_event_dates": ",".join(
            item["earnings_date"] for item in nonblocking_events
        ),
        "earnings_events": events,
        "earnings_blocking_events": blocking_events,
        "earnings_nonblocking_events": nonblocking_events,
    }


def _install_success_empty_strategy_evidence(
    base: Path,
    *,
    reason_code: str = "no_expirations",
) -> tuple[datetime, dict]:
    from src.application.opend_symbol_outputs import (
        publish_required_data_quote_snapshot,
        save_outputs,
    )
    from src.application.required_data_plan_identity import (
        build_required_data_expected_fetch_contract,
        required_data_plan_id,
    )
    from src.application.required_data_snapshot import (
        resolve_frozen_required_data,
        seal_required_data_snapshot,
    )
    from src.application.strategy_scan_status import (
        publish_strategy_scan_status,
    )

    account_dir = _account_dir(base)
    pd.DataFrame(columns=("symbol", "contract_symbol")).to_csv(
        account_dir / "nvda_sell_put_candidates.csv",
        index=False,
    )
    pd.DataFrame(columns=("symbol", "contract_symbol")).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )

    observed_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    completed_at = observed_at + timedelta(seconds=1)
    run_root = base / "output_runs" / "run-1"
    required_data_root = run_root / "required_data"
    required_data_root.mkdir(parents=True, exist_ok=True)
    trading_date = observed_at.date().isoformat()
    fetch_plan = {
        "symbol": "NVDA",
        "spot_reference": None,
        "require_realized_volatility": False,
        "side_plans": [],
        "merged_requests": [],
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_empty",
            "reason_code": reason_code,
            "expirations": [],
            "observed_at_utc": observed_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": trading_date,
            },
            "error": None,
        },
        "projection_outcome": "success_empty",
        "projected_expirations": [],
    }
    expected_contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=fetch_plan,
        source="futu",
        host="127.0.0.1",
        port=11111,
    )
    raw_path, csv_path = save_outputs(
        base,
        "NVDA",
        {
            "symbol": "NVDA",
            "underlier_code": "US.NVDA",
            "trading_date": trading_date,
            "expirations": [],
            "expiration_count": 0,
            "meta": {
                "status": "ok",
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": trading_date,
                "source_outcome": "success_empty",
                "reason_code": reason_code,
                "source_observed_at": observed_at.isoformat(),
                "completed_at_utc": completed_at.isoformat(),
                "snapshot_requested_codes": 0,
                "snapshot_returned_codes": 0,
                "snapshot_missing_codes": 0,
                "snapshot_unexpected_codes": 0,
                "snapshot_requested_code_set": [],
                "snapshot_returned_code_set": [],
                "snapshot_missing_code_set": [],
                "snapshot_unexpected_code_set": [],
                "snapshot_complete": True,
                "realized_volatility": {
                    "status": "not_applicable_no_contracts",
                },
            },
            "rows": [],
        },
        output_root=required_data_root,
    )
    quote_receipt_path, _quote_receipt = (
        publish_required_data_quote_snapshot(
            producer_root=required_data_root,
            producer_run_id="run-1",
            symbol="NVDA",
            raw_path=raw_path,
            csv_path=csv_path,
            fetch_plan=fetch_plan,
            fetch_policy={
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            expected_fetch_contract=expected_contract,
            source_observed_at=observed_at,
            completed_at=completed_at,
            now=completed_at,
        )
    )
    plan_items = [
        {
            "symbol": "NVDA",
            "source": "futu",
            "fetch_plan": fetch_plan,
            "fetch_binding": expected_contract["fetch_binding"],
            "expected_fetch_contract": expected_contract,
            "projection_outcome": "success_empty",
            "discovery_status": "complete",
        }
    ]
    plan_id = required_data_plan_id(plan_items)
    manifest_path = (
        run_root / "state" / "required_data_snapshot_manifest.json"
    )
    seal_required_data_snapshot(
        manifest_path=manifest_path,
        required_data_root=required_data_root,
        run_id="run-1",
        prefetch_summary={
            "schema_version": "1.0",
            "errors": 0,
            "global_required_data_plan": {
                "plan_id": plan_id,
                "symbols": plan_items,
                "symbols_count": 1,
                "discovery_complete": True,
            },
            "symbols": [],
            "results": [],
        },
        sealed_at=completed_at,
    )
    evidence = resolve_frozen_required_data(
        manifest_path=manifest_path,
        expected_run_id="run-1",
        symbol="NVDA",
        required_data_root=required_data_root,
        now=completed_at,
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="sell_put",
        status="completed",
        candidate_count=0,
        snapshot_id=evidence["snapshot_id"],
        receipt_relpath=evidence["receipt_relpath"],
        source_outcome="success_empty",
        reason_code=reason_code,
    )
    return completed_at + timedelta(seconds=1), evidence


def test_success_empty_opening_snapshot_remains_non_actionable(
    tmp_path: Path,
) -> None:
    now_utc, _evidence = _install_success_empty_strategy_evidence(
        tmp_path
    )

    brief = _assemble(
        tmp_path,
        now_utc=now_utc,
        config=_live_window_config(now_utc),
    )

    assert brief["status"] == "degraded"
    assert brief["actionability"] == "live_actionable"
    assert "notification_authority" not in brief
    assert brief["candidates"]["sell_put"] == []
    assert not any(
        item.get("strategy_family") == "sell_put"
        and item.get("actionable") is True
        for item in brief["data_gaps"]
    )
    assert not any(
        item.get("reason")
        == "strategy_status_projection_mismatch"
        for item in brief["data_gaps"]
    )


def test_success_empty_bundle_does_not_publish_v1_status_index(
    tmp_path: Path,
) -> None:
    now_utc, _evidence = _install_success_empty_strategy_evidence(tmp_path)

    brief = _assemble(
        tmp_path,
        now_utc=now_utc,
        config=_live_window_config(now_utc),
    )

    account_dir = _account_dir(tmp_path)
    assert brief["candidates"]["sell_put"] == []
    assert not (account_dir / "strategy_scan_status_index.v1.json").exists()
    assert (account_dir / "strategy_scan_status_index.v2.json").is_file()


def _call_row(*, symbol: str = "NVDA", contract: str = "NVDA260821C00140000", annualized: float = 0.1) -> dict:
    return {
        "symbol": symbol,
        "option_type": "call",
        "contract_symbol": contract,
        "expiration": "2026-08-21",
        "strike": 140,
        "spot": 120,
        "dte": 35,
        "delta": 0.2,
        "annualized_net_premium_return": annualized,
        "net_income": 100,
        "spread_ratio": 0.1,
        "open_interest": 500,
        "volume": 20,
        "shares_total": 350,
        "shares_locked": 100,
        "shares_available_for_cover": 250,
        "multiplier": 100,
        "call_covered_contracts_available": 2,
    }


def test_assembler_uses_structured_candidates_ranking_and_capacity(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(contract="NVDA_LOW", annualized=0.10),
            _put_row(contract="NVDA_HIGH", annualized=0.25),
            _put_row(contract="NVDA_HIGH", annualized=0.25),
        ]
    ).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)
    pd.DataFrame([_call_row()]).to_csv(account_dir / "nvda_sell_call_candidates.csv", index=False)
    (account_dir / "symbols_notification.txt").write_text("P0 fake action from markdown", encoding="utf-8")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert [
        item["contract_symbol"]
        for item in brief["candidates"]["sell_put"]
    ] == ["NVDA_HIGH", "NVDA_LOW"]
    assert brief["capacity"]["sell_put"]["contracts_available"] == 2
    assert brief["capacity"]["covered_call"]["contracts_available"] == 2
    assert len(
        [
            item
            for item in brief["actions"]
            if item["strategy_family"] == "sell_put"
        ]
    ) == 2
    assert all("fake" not in item.get("reason", "") for item in brief["actions"])


def test_assembler_projects_multicurrency_funds_from_run_scoped_context(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["funds"] == {
        "as_of_utc": "2026-07-17T13:59:30+00:00",
        "cash_total_by_currency": {"HKD": 480_000.0, "USD": 18_000.0},
        "option_opening_available_by_currency": {"HKD": 225_000.0, "USD": 15_000.0},
        "cash_total_cny": 558_000.0,
        "cash_secured_total_cny": 250_500.0,
        "option_opening_available_cny": 307_500.0,
        "available": True,
        "reason": "ok",
    }


def test_funds_cny_totals_cover_secured_currency_without_cash(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = account_dir / "state"
    (state_dir / "portfolio_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:00+00:00",
                "cash_by_currency": {"HKD": 1_104_060.32},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {"HKD": 171_000, "USD": 8_500},
                "cash_secured_unavailable_by_symbol": {},
                "cash_secured_total_cny": 213_400.0,
                "exchange_rates": {
                    "rates": {"USDCNY": 7.0, "HKDCNY": 0.9},
                    "timestamp": "2026-07-17T13:59:30+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    funds = brief["funds"]
    assert funds["cash_total_by_currency"] == {"HKD": 1_104_060.32}
    assert funds["option_opening_available_by_currency"]["HKD"] == pytest.approx(933_060.32)
    assert funds["cash_total_cny"] == pytest.approx(1_104_060.32 * 0.9)
    assert funds["cash_secured_total_cny"] == 213_400.0
    assert funds["option_opening_available_cny"] == pytest.approx(1_104_060.32 * 0.9 - 213_400.0)
    assert funds["available"] is True
    assert funds["reason"] == "ok"
    assert not any(item.get("scope") == "funds" for item in brief["data_gaps"])


def test_unreliable_secured_usage_keeps_cash_but_does_not_invent_opening_funds(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = account_dir / "state"
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {"USD": 3_000},
                "cash_secured_unavailable_by_symbol": {"PDD": "basis_missing"},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["funds"]["cash_total_by_currency"] == {"HKD": 480_000.0, "USD": 18_000.0}
    assert brief["funds"]["option_opening_available_by_currency"] == {}
    assert brief["funds"]["available"] is False
    assert brief["funds"]["reason"] == "option_cash_secured_unavailable"


def test_malformed_secured_reliability_flag_fails_opening_funds_closed(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    (account_dir / "state" / "option_positions_context.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:30+00:00",
                "cash_secured_total_by_ccy": {},
                "cash_secured_unavailable_by_symbol": ["malformed"],
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["funds"]["available"] is False
    assert brief["funds"]["option_opening_available_by_currency"] == {}
    assert brief["status"] == "degraded"


def test_missing_cash_context_blocks_snapshot_without_fabricating_zero(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    (account_dir / "state" / "portfolio_context.json").unlink()

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert brief["funds"]["cash_total_by_currency"] == {}
    assert brief["funds"]["option_opening_available_by_currency"] == {}
    assert "cash_total_unavailable" in brief["actions"][0]["reason"]


def test_prepared_portfolio_context_is_used_when_legacy_file_is_absent(
    tmp_path: Path,
) -> None:
    from src.application.source_receipts import sha256_bytes
    from src.application.tick_run_workspace import publish_account_run_config

    account_dir = _account_dir(tmp_path)
    state_dir = account_dir / "state"
    (state_dir / "portfolio_context.json").unlink()
    config = _config()
    config["portfolio"] = {"account": "lx", "source": "futu"}
    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        config=config,
    )
    context = {
        "as_of_utc": "2026-07-17T13:59:00+00:00",
        "source_observed_at": "2026-07-17T13:59:00+00:00",
        "filters": {"account": "lx"},
        "portfolio_source_name": "futu",
        "cash_by_currency": {"HKD": 480_000, "USD": 18_000},
    }
    payload_bytes = (
        json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    payload_digest = sha256_bytes(payload_bytes)
    payload_name = f"portfolio_context.{payload_digest}.json"
    (state_dir / payload_name).write_bytes(payload_bytes)
    manifest = {
        "schema_version": "prepared_portfolio_context.v1",
        "run_id": "run-1",
        "account": "lx",
        "status": "ready",
        "account_config_sha256": authority.account_config_sha256,
        "portfolio_context_relpath": payload_name,
        "payload_sha256": payload_digest,
        "portfolio_source_name": "futu",
        "portfolio_source_account": "lx",
    }
    (state_dir / "prepared_portfolio_context.v1.json").write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path, config=config)

    assert brief["funds"]["cash_total_by_currency"] == {
        "HKD": 480_000.0,
        "USD": 18_000.0,
    }
    assert not any(
        item.get("kind") == "portfolio_context"
        for item in brief["data_gaps"]
    )
    assert any(
        item.get("kind") == "prepared_portfolio_context"
        for item in brief["source_artifacts"]
    )


def test_candidate_index_uses_one_ranked_candidate_per_symbol_beyond_display_limit(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(symbol="NVDA", contract="NVDA_LOW", annualized=0.10),
            _put_row(symbol="NVDA", contract="NVDA_HIGH", annualized=0.30),
            _put_row(symbol="PDD", contract="PDD_1", annualized=0.25),
            _put_row(symbol="FUTU", contract="FUTU_1", annualized=0.20),
            _put_row(symbol="GOOGL", contract="GOOGL_1", annualized=0.15),
        ]
    ).to_csv(account_dir / "all_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)

    assert len(brief["candidates"]["sell_put"]) == 3
    assert len(brief["candidate_index"]) == 4
    by_symbol = {item["symbol"]: item for item in brief["candidate_index"]}
    assert by_symbol["NVDA"]["contract_count"] == 2
    assert by_symbol["NVDA"]["representative"]["contract_symbol"] == "NVDA_HIGH"
    assert set(by_symbol) == {"NVDA", "PDD", "FUTU", "GOOGL"}


def test_noop_account_result_is_not_a_successful_snapshot(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path, result=_result(ran_scan=False, reason="scheduler noop"))

    assert brief["actionability"] == "blocked"
    assert "scheduler noop" in brief["actions"][0]["reason"]
    assert brief["candidate_index"] == []


def test_distant_candidate_event_is_retained_without_attention_filter(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = _put_row()
    row.update(
        _earnings_evidence(
            event_date="2026-08-05",
            event_extra={"fiscal_year": "2026", "financial_type": "Q2"},
        )
    )
    pd.DataFrame([row]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path)
    candidate = brief["candidates"]["sell_put"][0]
    action = next(item for item in brief["actions"] if item["action_type"] == "open_candidate")
    risk = candidate["event_risk"]

    assert action["event_risk"] == risk
    assert risk["user_state"] == "confirmed_event"
    assert risk["reason_code"] == "confirmed_distant_earnings_event"
    assert risk["days_to_event"] == 19
    assert risk["expiration_relations"]["contract"] == {
        "expiration": "2026-08-21",
        "relation": "before_expiration",
        "days_before_expiration": 16,
    }
    assert risk["in_attention_window"] is False
    assert brief["events"] == [
        {
            **risk["events"][0],
            "symbol": "NVDA",
            "candidate_action_id": action["action_id"],
            "strategy_family": "sell_put",
            "contract_symbol": "NVDA260821P00100000",
            "strategy_group_id": "",
        }
    ]


def test_candidate_event_presence_mismatch_fails_closed(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = _put_row()
    row.update(_earnings_evidence(event_date="2026-08-05"))
    row["earnings_has_event"] = False
    pd.DataFrame([row]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )

    brief = _assemble(tmp_path)
    risk = brief["candidates"]["sell_put"][0]["event_risk"]

    assert risk["user_state"] == "unknown"
    assert risk["reason_code"] == "earnings_event_evidence_inconsistent"
    assert risk["reliable"] is False


def test_candidate_event_projection_confirms_complete_primary_absence(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv", index=False
    )
    brief = _assemble(tmp_path)

    assert brief["candidates"]["sell_put"][0]["event_risk"]["user_state"] == "confirmed_none"
    assert brief["events"] == []


def test_candidate_event_projection_never_falls_back_to_candidate_csv_fields(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = _put_row()
    row.update(
        {
            "earnings_evidence_status": "data_unavailable",
            "earnings_reason_code": "earnings_evidence_unavailable",
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-08-05",
            "event_source_status": "ok",
        }
    )
    pd.DataFrame([row]).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)

    assert brief["candidates"]["sell_put"][0]["event_risk"]["user_state"] == "unknown"
    assert brief["candidates"]["sell_put"][0]["event_risk"]["reason_code"] == "earnings_evidence_unavailable"
    assert brief["events"] == []


def test_event_projection_does_not_change_action_identity_or_candidate_order(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(contract="NVDA_LOW", annualized=0.10),
            _put_row(contract="NVDA_HIGH", annualized=0.25),
        ]
    ).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    without_snapshot = _assemble(tmp_path)
    before = {
        item["contract_symbol"]: item["action_id"]
        for item in without_snapshot["actions"]
        if item["action_type"] == "open_candidate"
    }
    with_events = []
    for row in (
        _put_row(contract="NVDA_LOW", annualized=0.10),
        _put_row(contract="NVDA_HIGH", annualized=0.25),
    ):
        row.update(_earnings_evidence(event_date="2026-08-05"))
        with_events.append(row)
    pd.DataFrame(with_events).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    with_snapshot = _assemble(tmp_path)
    after = {
        item["contract_symbol"]: item["action_id"]
        for item in with_snapshot["actions"]
        if item["action_type"] == "open_candidate"
    }

    assert before == after
    assert [
        item["contract_symbol"]
        for item in with_snapshot["candidates"]["sell_put"]
    ] == ["NVDA_HIGH", "NVDA_LOW"]


def test_candidate_priority_does_not_change_sealed_candidate_order(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            _put_row(contract="NVDA_DEFAULT", annualized=0.30),
            _put_row(contract="NVDA_STRONG", annualized=0.20, priority="strong"),
        ]
    ).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)
    priorities = {item["contract_symbol"]: item["priority"] for item in brief["actions"] if item["action_type"] == "open_candidate"}

    assert [
        item["contract_symbol"]
        for item in brief["candidates"]["sell_put"]
    ] == ["NVDA_DEFAULT", "NVDA_STRONG"]
    assert priorities["NVDA_DEFAULT"] == "P1"
    assert priorities["NVDA_STRONG"] == "P0"


def test_close_advice_preserves_lot_group_and_leg_identity(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)
    _write_close_report(
        account_dir,
        [
            {
                "account": "lx",
                "position_lot_id": "lot-put",
                "strategy_group_id": "group-1",
                "leg_role": "funding_put",
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 100,
                "reason": "收益已锁定",
                "recommendation_state": "close",
                "policy_version": "strict_profit_capture.v1",
                "decision_basis": "strict_profit_capture_all_gates_passed",
                "decision_evidence_status": "complete",
                "evaluation_status": "priced",
                "quote_status": "priced",
                "position_side": "short",
                "ask": 0.54,
                "net_capture_ratio": 0.94,
                "all_in_close_cost": 52.0,
                "close_cost_ratio": 0.00052,
                "remaining_term_ratio": 0.60,
                "estimated_pnl_if_close_net": 474.5,
            }
        ],
    )

    brief = _assemble(tmp_path)
    action = next(item for item in brief["actions"] if item["action_type"] == "close_position")

    assert action["priority"] == "P2"
    assert action["position_lot_id"] == "lot-put"
    assert action["strategy_group_id"] == "group-1"
    assert action["leg_role"] == "funding_put"
    assert action["recommendation_state"] == "close"
    assert brief["positions"][0]["position_lot_id"] == "lot-put"
    assert brief["positions"][0]["metrics"] == {
        "ask": 0.54,
        "remaining_term_ratio": 0.60,
        "net_capture_ratio": 0.94,
        "all_in_close_cost": 52.0,
        "close_cost_ratio": 0.00052,
        "estimated_pnl_if_close_net": 474.5,
    }


def test_close_advice_daily_brief_selects_only_close_state(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    close_rows = [
            {
                "account": "lx",
                "position_lot_id": f"lot-{state}-{symbol.lower()}",
                "symbol": symbol,
                "strategy_family": "sell_put",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": strike,
                "reason": "test",
                "recommendation_state": state,
                "policy_version": "strict_profit_capture.v1",
                "decision_basis": (
                    "strict_profit_capture_all_gates_passed"
                    if state == "close"
                    else "test_gate"
                ),
                "decision_evidence_status": (
                    "not_evaluable" if state == "not_evaluable" else "complete"
                ),
                "evaluation_status": "priced",
                "quote_status": "priced",
            }
            for state, symbol, strike in (
                ("close", "FIRST", 100),
                ("close", "SECOND", 101),
                ("hold", "HOLD", 102),
                ("not_evaluable", "GAP", 103),
            )
        ]
    _write_close_report(account_dir, close_rows)
    config = _config()
    config["close_advice"] = {
        "enabled": True,
    }

    brief = _assemble(tmp_path, config=config)
    close_actions = [
        item
        for item in brief["actions"]
        if item["action_type"] == "close_position"
    ]
    eligibility = {
        item["symbol"]: item["notification_eligible"]
        for item in brief["positions"]
    }
    message = render_full_brief(
        brief,
        limits={"max_actions_per_priority": 10},
    )

    assert {item["symbol"] for item in close_actions} == {"FIRST", "SECOND"}
    assert eligibility == {
        "FIRST": True,
        "SECOND": True,
        "HOLD": False,
        "GAP": False,
    }
    assert "FIRST｜Sell Put｜08-21 $100 Put｜建议平仓" in message
    assert "SECOND｜Sell Put｜08-21 $101 Put｜建议平仓" in message
    assert "HOLD" not in message
    assert "GAP" not in message
    assert "汇总｜共 4 条，需处理 2 条。" in message


def test_close_advice_without_valid_manifest_cannot_enter_daily_brief(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "position_lot_id": "lot-unbound",
                "symbol": "NVDA",
                "strategy_family": "sell_put",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": 100,
                "recommendation_state": "close",
                "policy_version": "strict_profit_capture.v1",
                "decision_basis": "strict_profit_capture_all_gates_passed",
                "decision_evidence_status": "complete",
                "evaluation_status": "priced",
                "quote_status": "priced",
            }
        ]
    ).to_csv(account_dir / "close_advice.csv", index=False)

    brief = _assemble(tmp_path)

    assert not any(
        action["action_type"] == "close_position"
        for action in brief["actions"]
    )
    assert any(
        gap.get("reason") == "close_advice_manifest_missing"
        for gap in brief["data_gaps"]
    )


def test_close_advice_mixed_account_report_cannot_enter_daily_brief(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    common = {
        "position_lot_id": "lot-close",
        "symbol": "NVDA",
        "strategy_family": "sell_put",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 100,
        "recommendation_state": "close",
        "policy_version": "strict_profit_capture.v1",
        "decision_basis": "strict_profit_capture_all_gates_passed",
        "decision_evidence_status": "complete",
        "evaluation_status": "priced",
        "quote_status": "priced",
    }
    _write_close_report(
        account_dir,
        [
            {**common, "account": "lx"},
            {**common, "account": "sy", "position_lot_id": "lot-other"},
        ],
    )

    brief = _assemble(tmp_path)

    assert not any(
        action["action_type"] == "close_position"
        for action in brief["actions"]
    )
    assert any(
        gap.get("reason")
        == "close_advice_report_account_row_mismatch"
        for gap in brief["data_gaps"]
    )


def test_close_advice_loader_consumes_the_validated_csv_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import daily_decision_brief_service as service

    account_dir = _account_dir(tmp_path)
    csv_path = account_dir / "close_advice.csv"
    original_row = {
        "account": "lx",
        "symbol": "NVDA",
        "recommendation_state": "close",
        "policy_version": "strict_profit_capture.v1",
        "decision_basis": "strict_profit_capture_all_gates_passed",
        "decision_evidence_status": "complete",
        "evaluation_status": "priced",
    }
    _write_close_report(account_dir, [original_row])
    original_reader = service.read_close_advice_report_snapshot

    def _read_then_replace(**kwargs):
        snapshot = original_reader(**kwargs)
        pd.DataFrame([{**original_row, "symbol": "TSLA"}]).to_csv(
            csv_path,
            index=False,
        )
        return snapshot

    monkeypatch.setattr(
        service,
        "read_close_advice_report_snapshot",
        _read_then_replace,
    )
    source_artifacts: list[dict[str, Any]] = []
    data_gaps: list[dict[str, Any]] = []

    rows, available = service._load_close_advice(
        path=csv_path,
        run_account_dir=account_dir,
        market="US",
        account="lx",
        run_id="run-1",
        source_artifacts=source_artifacts,
        data_gaps=data_gaps,
    )

    assert available is True
    assert [row["symbol"] for row in rows] == ["NVDA"]
    assert data_gaps == []


def test_close_advice_daily_brief_honors_ranked_account_limit(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    close_rows = [
            {
                "account": "lx",
                "position_lot_id": f"lot-{symbol.lower()}",
                "symbol": symbol,
                "strategy_family": "sell_put",
                "option_type": "put",
                "expiration": "2026-08-21",
                "strike": strike,
                "reason": "test",
                "recommendation_state": "close",
                "policy_version": "strict_profit_capture.v1",
                "decision_basis": "strict_profit_capture_all_gates_passed",
                "decision_evidence_status": "complete",
                "evaluation_status": "priced",
                "quote_status": "priced",
                "net_capture_ratio": capture_ratio,
                "all_in_close_cost": remaining_premium,
            }
            for symbol, strike, capture_ratio, remaining_premium in (
                ("SECOND", 101, 0.80, 10),
                ("FIRST", 100, 0.95, 5),
                ("THIRD", 102, 0.70, 20),
            )
        ]
    _write_close_report(account_dir, close_rows)
    config = _config()
    config["close_advice"] = {
        "enabled": True,
        "max_items_per_account": 1,
    }

    brief = _assemble(tmp_path, config=config)
    close_actions = [
        item
        for item in brief["actions"]
        if item["action_type"] == "close_position"
    ]
    eligibility = {
        item["symbol"]: item["notification_eligible"]
        for item in brief["positions"]
    }
    message = render_full_brief(
        brief,
        limits={"max_actions_per_priority": 10},
    )

    assert [item["symbol"] for item in close_actions] == ["FIRST"]
    assert eligibility == {
        "SECOND": False,
        "FIRST": True,
        "THIRD": False,
    }
    assert "FIRST｜Sell Put｜08-21 $100 Put｜建议平仓" in message
    assert "SECOND" not in message
    assert "THIRD" not in message
    assert "汇总｜共 3 条，需处理 1 条。" in message


def test_combo_yield_selects_one_pair_per_symbol_and_ranks_before_truncation(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                    "candidate_pair_id": "combo_yield:NVDA:NVDA_P95:NVDA_C130",
                "put_contract_symbol": "NVDA_P95",
                "call_contract_symbol": "NVDA_C130",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-08-21",
                "put_strike": 95,
                "call_strike": 130,
                "structure_mode": "same_expiry_pair",
                "funding_accepted": True,
                "put_only_annualized_net_return": 0.08,
                "call_delta": 0.20,
                "net_credit_retention": 0.70,
            },
            {
                "symbol": "NVDA",
                    "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C125",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C125",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-08-21",
                "put_strike": 100,
                "call_strike": 125,
                "structure_mode": "same_expiry_pair",
                "funding_accepted": True,
                "put_only_annualized_net_return": 0.20,
                "call_delta": 0.15,
                "net_credit_retention": 0.75,
            },
            {
                "symbol": "AAPL",
                    "candidate_pair_id": "combo_yield:AAPL:AAPL_P180:AAPL_C220",
                "put_contract_symbol": "AAPL_P180",
                "call_contract_symbol": "AAPL_C220",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-08-21",
                "put_strike": 180,
                "call_strike": 220,
                "bid": 4.25,
                "linked_call_ask": 0.55,
                "cash_required_usd": 18_000,
                "cash_free_usd": 36_000,
                "structure_mode": "same_expiry_pair",
                "funding_accepted": True,
                "put_only_annualized_net_return": 0.30,
                "call_delta": 0.10,
                "net_credit_retention": 0.80,
            },
        ]
    ).to_csv(account_dir / "nvda_combo_yield_candidates.csv", index=False)

    brief = _assemble(tmp_path)
    combos = brief["candidates"]["combo_yield"]

    assert [item["strategy_group_id"] for item in combos] == [
        "combo_yield:AAPL:AAPL_P180:AAPL_C220",
        "combo_yield:NVDA:NVDA_P100:NVDA_C125",
    ]
    assert combos[0]["put_leg_role"] == "funding_put"
    assert combos[0]["call_leg_role"] == "participation_call"
    assert combos[0]["put_sell_reference"] == 4.25
    assert combos[0]["call_buy_reference"] == 0.55
    combo_index = {
        item["symbol"]: item["representative"]
        for item in brief["candidate_index"]
        if item["strategy_family"] == "combo_yield"
    }
    assert combo_index["AAPL"]["put_sell_reference"] == 4.25
    assert combo_index["AAPL"]["call_buy_reference"] == 0.55
    combo_actions = [item for item in brief["actions"] if item["strategy_family"] == "combo_yield"]
    assert [item["strategy_group_id"] for item in combo_actions] == [
        "combo_yield:AAPL:AAPL_P180:AAPL_C220",
        "combo_yield:NVDA:NVDA_P100:NVDA_C125",
    ]


def test_combo_snapshot_partial_status_warns_without_csv_authority(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    _seal_combo_status_snapshot(tmp_path, opening_status="partial_data")

    brief = _assemble(tmp_path)

    combo_partial_gaps = [
        item
        for item in brief["data_gaps"]
        if item.get("strategy_family") == "combo_yield"
        and item.get("reason") == "opening_candidate_strategy_partial_data"
    ]
    assert len(combo_partial_gaps) == 1
    assert brief["candidates"]["combo_yield"] == []
    assert "NVDA 组合增强｜本轮部分行情证据不可用，候选结果不完整" in (
        render_full_brief(brief)
    )


def test_combo_snapshot_data_unavailable_is_not_clean_no_candidate(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    _seal_combo_status_snapshot(tmp_path, opening_status="data_unavailable")

    brief = _assemble(tmp_path)

    assert any(
        item.get("strategy_family") == "combo_yield"
        and item.get("reason") == "data_unavailable"
        and item.get("symbol") == "NVDA"
        for item in brief["data_gaps"]
    )
    combo_source = next(
        item
        for item in brief["source_artifacts"]
        if item.get("kind") == "combo_yield_snapshot"
    )
    assert combo_source["opening_status"] == "data_unavailable"


def test_combo_yield_event_projection_relates_to_shared_expiration(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                    "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C125",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C125",
                "put_expiration": "2026-08-21",
                "call_expiration": "2026-08-21",
                "put_strike": 100,
                "call_strike": 125,
                "annualized_net_credit_yield": 0.20,
                **_earnings_evidence(event_date="2026-08-14"),
                "earnings_snapshot_hash": "e" * 64,
            }
        ]
    ).to_csv(account_dir / "nvda_combo_yield_candidates.csv", index=False)

    brief = _assemble(tmp_path)
    candidate = brief["candidates"]["combo_yield"][0]
    action = next(item for item in brief["actions"] if item["action_type"] == "open_combo_yield")

    assert action["event_risk"] == candidate["event_risk"]
    assert candidate["event_risk"]["user_state"] == "confirmed_event"
    assert candidate["event_risk"]["reason_code"] == "confirmed_distant_earnings_event"
    assert candidate["event_risk"]["reliable"] is True
    assert candidate["event_risk"]["evidence_chain_id"] == "e" * 64
    assert candidate["event_risk"]["expiration_relations"] == {
        "put": {
            "expiration": "2026-08-21",
            "relation": "before_expiration",
            "days_before_expiration": 7,
        },
        "call": {
            "expiration": "2026-08-21",
            "relation": "before_expiration",
            "days_before_expiration": 7,
        },
    }
    assert candidate["event_risk"]["in_attention_window"] is False


def test_partial_symbol_csv_failure_becomes_gap_without_blocking_other_actions(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "aaa_sell_put_candidates_labeled.csv").write_text('symbol,contract_symbol\n"broken', encoding="utf-8")
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert any(item["contract_symbol"] == "PDD_VALID" for item in brief["actions"])
    assert any(
        item.get("symbol") == "AAA"
        and item.get("reason") == "candidate_fixture_parse_failed"
        for item in brief["data_gaps"]
    )


def test_partial_frozen_scope_warns_without_erasing_valid_candidate(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    _account_dir(tmp_path)
    put_row = _put_row()
    call_row = _call_row(symbol="AAPL", contract="AAPL_CALL_UNAVAILABLE")
    seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="US",
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "US",
            "source": "opend",
        },
        account_config_sha256="f" * 64,
        strategy_policy_sha256="1" * 64,
        dependencies=_fixture_dependencies(),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
            },
            {
                "symbol": "AAPL",
                "strategy_mode": "call",
                "status": "unavailable",
                "reason": "partial_data",
            },
        ],
        final_candidates={"put": [put_row], "call": []},
        candidate_evaluations={
            "put": [
                {
                    "normalized_input": put_row,
                    "opening_decision": build_candidate_decision(
                        mode="put",
                        symbol="NVDA",
                        contract_symbol=str(put_row["contract_symbol"]),
                        accepted=True,
                        normalized_input=put_row,
                    ),
                }
            ],
            "call": [
                {
                    "normalized_input": call_row,
                    "opening_decision": build_candidate_decision(
                        mode="call",
                        symbol="AAPL",
                        contract_symbol=str(call_row["contract_symbol"]),
                        accepted=False,
                        rejects=[
                            {
                                "stage": STAGE_INPUT_NORMALIZATION,
                                "reason": "input_invalid",
                                "message": "term-matched realized volatility is unavailable",
                                "metric_value": {
                                    "reason_code": "term_matched_rv_unavailable",
                                },
                                "threshold": "ready",
                            }
                        ],
                        normalized_input=call_row,
                    ),
                }
            ],
        },
        sealed_at="2026-07-17T13:59:59Z",
    )

    brief = _assemble(tmp_path)

    assert [
        item["contract_symbol"]
        for item in brief["candidates"]["sell_put"]
    ] == ["NVDA260821P00100000"]
    assert any(
        item.get("symbol") == "AAPL"
        and item.get("strategy_family") == "covered_call"
        and item.get("reason") == "opening_candidate_strategy_partial_data"
        and item.get("reason_code") == "term_matched_rv_unavailable"
        for item in brief["data_gaps"]
    )
    assert "AAPL Covered Call｜期限匹配的已实现波动率（RV）证据不可用，候选结果不完整" in (
        render_full_brief(brief)
    )


def test_header_only_and_empty_csv_are_readable_empty_decisions(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=_put_row().keys()).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)
    _write_close_report(account_dir, [])

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert not any(item["action_type"] == "resolve_data_blocker" for item in brief["actions"])


def test_all_structured_sources_unavailable_blocks_account(tmp_path: Path) -> None:
    _account_dir(tmp_path)

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    blocker = brief["actions"][0]
    assert blocker["priority"] == "P0"
    assert blocker["state"] == "blocked"
    assert "all_structured_decision_sources_unavailable" in blocker["reason"]


def test_pipeline_failure_blocks_even_when_ran_scan_and_candidate_artifact_exist(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(
        tmp_path,
        result=_result(ran_scan=True, reason="pipeline failed"),
        pipeline_succeeded=False,
    )

    assert brief["actionability"] == "blocked"
    assert "pipeline failed" in brief["actions"][0]["reason"]


def test_missing_capacity_suppresses_only_affected_candidate_and_blocks_when_all_requirements_missing(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    row = _put_row()
    row.pop("cash_required_cny")
    row.pop("cash_free_cny")
    pd.DataFrame([row]).to_csv(account_dir / "nvda_sell_put_candidates_labeled.csv", index=False)

    brief = _assemble(tmp_path)

    assert brief["candidates"]["sell_put"][0]["contract_symbol"] == row["contract_symbol"]
    assert not any(item.get("contract_symbol") == row["contract_symbol"] for item in brief["actions"])
    assert brief["actionability"] == "blocked"
    assert any(item["reason"] == "cash_capacity_unavailable" for item in brief["data_gaps"])


def test_market_partition_excludes_other_market_rows_and_uses_market_date(tmp_path: Path) -> None:
    roots = {market: tmp_path / market.lower() for market in ("US", "HK")}
    for root in roots.values():
        account_dir = _account_dir(root)
        pd.DataFrame(
            [
                _put_row(symbol="NVDA", contract="US_NVDA"),
                _put_row(symbol="0700.HK", contract="HK_0700"),
            ]
        ).to_csv(account_dir / "mixed_sell_put_candidates_labeled.csv", index=False)

    us = _assemble(roots["US"], market="US")
    hk = _assemble(roots["HK"], market="HK")

    assert [item["contract_symbol"] for item in us["candidates"]["sell_put"]] == ["US_NVDA"]
    assert [item["contract_symbol"] for item in hk["candidates"]["sell_put"]] == ["HK_0700"]
    assert us["market_trading_date"] == "2026-07-17"
    assert hk["market_trading_date"] == "2026-07-17"
    assert us["valid_until_utc"] != hk["valid_until_utc"]


def test_prefetch_symbol_failure_is_a_local_gap_not_account_blocker(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv", index=False
    )
    state_dir = account_dir / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "required_data_prefetch_summary.json").write_text(
        json.dumps(
            {
                "as_of_utc": "2026-07-17T13:59:00+00:00",
                "summary": {"errors": 1},
                "symbols": {"NVDA": {"status": "error", "reason": "quote unavailable"}},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert any(item.get("symbol") == "NVDA" for item in brief["data_gaps"])
    assert any(item["contract_symbol"] == "PDD_VALID" for item in brief["actions"])


def test_canonical_prefetch_shape_projects_one_symbol_gap_without_duplicate_aggregate(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv",
        index=False,
    )
    state_dir = account_dir / "state"
    (state_dir / "required_data_prefetch_summary.json").write_text(
        json.dumps(
            {
                "errors": 1,
                "symbols": [
                    {"symbol": "NVDA", "status": "error"},
                    {"symbol": "PDD", "status": "ok"},
                ],
                "results": {"NVDA": "empty_chain", "PDD": "ok"},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)
    matching = [
        item
        for item in brief["data_gaps"]
        if item.get("symbol") == "NVDA"
        and item.get("reason") == "empty_chain"
    ]

    assert len(matching) == 1
    assert not any(
        item.get("reason") == "required_data_prefetch_errors"
        for item in brief["data_gaps"]
    )


def test_successfully_fetched_prefetch_symbols_do_not_create_data_gaps(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(symbol="PDD", contract="PDD_VALID")]).to_csv(
        account_dir / "pdd_sell_put_candidates_labeled.csv",
        index=False,
    )
    state_dir = account_dir / "state"
    (state_dir / "required_data_prefetch_summary.json").write_text(
        json.dumps(
            {
                "errors": 0,
                "symbols": [
                    {"symbol": "GOOGL", "status": "fetched"},
                    {"symbol": "NVDA", "status": "fetched"},
                ],
                "results": {"GOOGL": "ok", "NVDA": "ok"},
            }
        ),
        encoding="utf-8",
    )

    brief = _assemble(tmp_path)

    assert not any(
        item.get("source") == "required_data_prefetch_summary"
        and item.get("symbol") in {"GOOGL", "NVDA"}
        for item in brief["data_gaps"]
    )


@pytest.mark.parametrize(
    "symbol_cfg",
    [
        {"symbol": "NVDA", "combo_yield": {"enabled": True, "variant": "sp_lc"}},
        {"symbol": "NVDA", "combo_yield": {"enabled": False, "variant": "cc_lp"}},
        {"symbol": "0700.HK", "combo_yield": {"enabled": True, "variant": "cc_lp"}},
    ],
)
def test_cc_lp_snapshot_is_not_required_without_enabled_current_market_cc_lp(
    tmp_path: Path,
    symbol_cfg: dict,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    config = _config()
    config["symbols"] = [symbol_cfg]

    brief = _assemble(tmp_path, config=config)

    assert not any(
        item.get("reason")
        in {"cc_lp_snapshot_unavailable", "cc_lp_snapshot_market_mismatch"}
        for item in brief["data_gaps"]
    )


@pytest.mark.parametrize(
    "config_overlay",
    [
        {
            "symbols": [
                {
                    "symbol": "NVDA",
                    "combo_yield": {"enabled": True, "variant": "cc_lp"},
                }
            ]
        },
        {
            "templates": {
                "cc_lp_base": {
                    "combo_yield": {"enabled": True, "variant": "cc_lp"},
                }
            },
            "symbols": [{"symbol": "NVDA", "use": "cc_lp_base"}],
        },
    ],
)
def test_cc_lp_applicability_comes_from_terminal_manifest_not_live_config(
    tmp_path: Path,
    config_overlay: dict,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    config = _config()
    config.update(config_overlay)

    brief = _assemble(tmp_path, config=config)

    assert not any(
        item.get("variant") == "cc_lp"
        for item in brief["data_gaps"]
    )


def test_symbol_override_can_disable_template_cc_lp_snapshot_requirement(
    tmp_path: Path,
) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row()]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    config = _config()
    config["templates"] = {
        "cc_lp_base": {
            "combo_yield": {"enabled": True, "variant": "cc_lp"},
        }
    }
    config["symbols"] = [
        {
            "symbol": "NVDA",
            "use": "cc_lp_base",
            "combo_yield": {"enabled": False},
        }
    ]

    brief = _assemble(tmp_path, config=config)

    assert not any(
        item.get("reason")
        in {"cc_lp_snapshot_unavailable", "cc_lp_snapshot_market_mismatch"}
        for item in brief["data_gaps"]
    )


def test_status_index_treats_completed_zero_as_available_with_partial_failure(
    tmp_path: Path,
) -> None:
    from src.application.strategy_scan_status import publish_strategy_scan_status

    account_dir = _account_dir(tmp_path)
    pd.DataFrame(columns=["symbol", "contract_symbol"]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    (account_dir / "nvda_sell_call_candidates.csv").write_bytes(b"\n")
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="sell_put",
        status="completed",
        candidate_count=0,
    )
    publish_strategy_scan_status(
        report_dir=account_dir,
        run_id="run-1",
        account="lx",
        market="US",
        symbol="NVDA",
        strategy_family="covered_call",
        status="unavailable",
        reason="empty_chain",
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["status"] == "degraded"
    assert not any(
        "candidate_strategy_execution_failed" in str(item.get("reason"))
        for item in brief["actions"]
    )
    assert any(
        item.get("strategy_family") == "covered_call"
        and item.get("reason") == "empty_chain"
        for item in brief["data_gaps"]
    )


def test_sell_put_conflict_uses_only_labeled_candidates(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    labeled_rows = [
        _put_row(symbol="0700.HK", contract="0700_P430", annualized=0.18),
        _put_row(symbol="0700.HK", contract="0700_P440", annualized=0.20),
    ]
    raw_rows = [
        *labeled_rows,
        _put_row(symbol="0700.HK", contract="0700_P450_RAW_ONLY", annualized=0.99),
    ]
    pd.DataFrame(labeled_rows).to_csv(account_dir / "0700_sell_put_candidates_labeled.csv", index=False)
    pd.DataFrame(raw_rows).to_csv(account_dir / "0700_sell_put_candidates.csv", index=False)

    brief = _assemble(tmp_path, market="HK")

    assert [
        item["contract_symbol"]
        for item in brief["candidates"]["sell_put"]
    ] == ["0700_P430", "0700_P440"]
    assert {item["contract_symbol"] for item in brief["actions"] if item.get("contract_symbol")} == {
        "0700_P430",
        "0700_P440",
    }
    assert "0700_P450_RAW_ONLY" not in json.dumps(brief, sort_keys=True)
    from src.application.daily_decision_brief_renderer import render_full_brief

    assert "0700_P450_RAW_ONLY" not in render_full_brief(brief)
    assert "有效行动 2 条" in brief["strategy_summary"]
    assert "候选证据：Sell Put 2，Covered Call 0，Combo Yield 0" in brief["strategy_summary"]
    assert not any(item["path"].endswith("_sell_put_candidates.csv") for item in brief["source_artifacts"])


def test_sell_put_controlled_newline_is_authoritative_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"\n")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert not any(
        item["strategy_family"] == "sell_put" and item["reason"] == "csv_unavailable"
        for item in brief["data_gaps"]
    )


def test_sell_put_controlled_crlf_is_authoritative_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"\r\n")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert not any(
        item["strategy_family"] == "sell_put" and item["reason"] == "csv_unavailable"
        for item in brief["data_gaps"]
    )


def test_legacy_header_shape_does_not_define_snapshot_availability(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text("symbol,annualized\n", encoding="utf-8")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["candidates"]["sell_put"] == []
    assert any(
        item["kind"] == "opening_candidate_snapshot"
        for item in brief["source_artifacts"]
    )


def test_sell_put_zero_byte_is_malformed_not_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "opening_candidate_strategy_data_unavailable"
        for item in brief["data_gaps"]
    )


def test_sell_put_unrecognized_whitespace_is_malformed_not_empty(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"  ")

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "opening_candidate_strategy_data_unavailable"
        for item in brief["data_gaps"]
    )


def test_sell_put_raw_only_artifact_reports_canonical_missing_without_fallback(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(contract="RAW_ONLY")]).to_csv(
        account_dir / "nvda_sell_put_candidates.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert brief["candidates"]["sell_put"] == []
    assert "RAW_ONLY" not in json.dumps(brief, sort_keys=True)
    assert any(
        item.get("kind") == "candidate_snapshot_manifest"
        for item in brief["source_artifacts"]
    )
    assert not any(
        item.get("kind") == "opening_candidate_snapshot"
        for item in brief["source_artifacts"]
    )


def test_sell_put_failure_preserves_covered_call_action_and_degrades_status(tmp_path: Path) -> None:
    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        'symbol,contract_symbol\n"broken', encoding="utf-8"
    )
    pd.DataFrame([_call_row(contract="NVDA_CALL_VALID")]).to_csv(
        account_dir / "nvda_sell_call_candidates.csv", index=False
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "live_actionable"
    assert brief["status"] == "degraded"
    assert brief["candidates"]["sell_put"] == []
    assert any(item.get("contract_symbol") == "NVDA_CALL_VALID" for item in brief["actions"])
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "opening_candidate_strategy_data_unavailable"
        for item in brief["data_gaps"]
    )


def test_strategy_step_failure_trace_blocks_false_normal_empty_result(tmp_path: Path) -> None:
    from src.application.strategy_scan_failures import append_strategy_scan_failure

    account_dir = _account_dir(tmp_path)
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_bytes(b"\n")
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="NVDA",
        strategy_family="sell_put",
        error=RuntimeError("scanner crashed"),
    )

    brief = _assemble(tmp_path)

    assert brief["actionability"] == "blocked"
    assert "candidate_strategy_execution_failed" in brief["actions"][0]["reason"]
    assert any(
        item["strategy_family"] == "sell_put"
        and item["reason"] == "strategy_step_failed"
        and item["error_type"] == "RuntimeError"
        for item in brief["data_gaps"]
    )


def test_strategy_step_failure_preserves_other_candidates_and_warns_user(tmp_path: Path) -> None:
    from src.application.strategy_scan_failures import append_strategy_scan_failure
    from src.application.daily_decision_brief_renderer import render_fixed_report

    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_put_row(contract="STALE_PUT")]).to_csv(
        account_dir / "nvda_sell_put_candidates_labeled.csv",
        index=False,
    )
    pd.DataFrame([_call_row(contract="NVDA_CALL_VALID")]).to_csv(
        account_dir / "nvda_sell_call_candidates.csv",
        index=False,
    )
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="NVDA",
        strategy_family="sell_put",
        error=RuntimeError("scanner crashed"),
    )

    brief = _assemble(tmp_path)
    message = render_fixed_report(brief)

    assert brief["actionability"] == "live_actionable"
    assert brief["status"] == "degraded"
    assert any(item.get("contract_symbol") == "NVDA_CALL_VALID" for item in brief["actions"])
    assert not any(item.get("contract_symbol") == "STALE_PUT" for item in brief["actions"])
    assert brief["candidates"]["sell_put"] == []
    assert "NVDA Sell Put 扫描失败，本轮无结果" in message
    assert "补充｜NVDA Sell Put 扫描失败，未纳入本轮候选" in message
    assert "本轮结果不完整" not in message
    assert "NVDA_CALL_VALID" not in message
    assert "Covered Call" in message


def test_strategy_step_failure_reminders_list_each_failed_symbol(tmp_path: Path) -> None:
    from src.application.strategy_scan_failures import append_strategy_scan_failure
    from src.application.daily_decision_brief_renderer import render_fixed_report

    account_dir = _account_dir(tmp_path)
    pd.DataFrame([_call_row(contract="NVDA_CALL_VALID")]).to_csv(
        account_dir / "nvda_sell_call_candidates.csv", index=False
    )
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="SPCX",
        strategy_family="sell_put",
        error=RuntimeError("rv missing"),
    )
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="XYZ",
        strategy_family="sell_put",
        error=RuntimeError("scanner crashed"),
    )
    # Duplicate rows for the same symbol+family collapse into one reminder.
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="SPCX",
        strategy_family="sell_put",
        error=RuntimeError("rv missing again"),
    )

    brief = _assemble(tmp_path)
    message = render_fixed_report(brief)

    assert message.count("SPCX Sell Put 扫描失败，本轮无结果") == 1
    assert "XYZ Sell Put 扫描失败，本轮无结果" in message
    assert "补充｜SPCX、XYZ Sell Put 扫描失败，未纳入本轮候选" in message
    assert "本轮结果不完整" not in message


def test_strategy_step_failure_empty_summary_points_to_reminders(tmp_path: Path) -> None:
    from src.application.strategy_scan_failures import append_strategy_scan_failure
    from src.application.daily_decision_brief_renderer import (
        build_daily_brief_user_view,
    )

    account_dir = _account_dir(tmp_path)
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="SPCX",
        strategy_family="sell_put",
        error=RuntimeError("rv missing"),
    )
    append_strategy_scan_failure(
        report_dir=account_dir,
        symbol="XYZ",
        strategy_family="sell_put",
        error=RuntimeError("scanner crashed"),
    )

    brief = _assemble(tmp_path)
    view = build_daily_brief_user_view(brief, delivery_kind="fixed_report")

    assert (
        view["candidate_empty_summary"]
        == "本轮暂无符合条件的候选；SPCX、XYZ Sell Put 扫描失败。"
    )
    assert "SPCX Sell Put 扫描失败，本轮无结果" in view["reminders"]
    assert "XYZ Sell Put 扫描失败，本轮无结果" in view["reminders"]
    assert not any("本轮结果不完整" in item for item in view["reminders"])
