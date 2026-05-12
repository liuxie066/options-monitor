from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.option_position_lots import (
    OpenPositionCommand,
    build_close_patch,
    build_open_adjustment_patch,
    build_open_fields,
    effective_contracts,
    effective_contracts_open,
    effective_expiration_ymd,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
    normalize_status,
    parse_exp_to_ms,
)
from domain.domain.symbol_identity import canonical_symbol
from src.application.option_positions_facade import build_option_position_view
from src.application.option_positions_service import (
    existing_manual_close_event_result,
    persist_manual_adjust_event,
    persist_manual_close_event,
    persist_manual_open_event,
    require_option_positions_read_repo,
)
from src.application.option_positions_feishu_sync import sync_single_option_position_record
from src.application.trade_event_normalizer import NormalizedTradeDeal
from src.application.option_positions_sync_config import effective_option_positions_sync_to_feishu_enabled
from src.application.option_positions_v2_service import (
    append_option_positions_v2_event,
    refresh_option_positions_v2_state,
    snapshot_current_positions_as_verification,
)


def _auto_sync_record_if_possible(repo: Any, *, record_id: str) -> dict[str, Any] | None:
    try:
        data_config = getattr(repo, "data_config_path", None)
        if data_config is None:
            return None
        resolved_data_config = Path(str(data_config))
        if not effective_option_positions_sync_to_feishu_enabled(data_config=resolved_data_config, repo=repo):
            return None
        return sync_single_option_position_record(repo=repo, data_config=resolved_data_config, record_id=record_id, apply_mode=True)
    except Exception as sync_error:
        print(
            f"[WARN] option_positions post-write Feishu sync skipped for {record_id} ({type(sync_error).__name__}): {sync_error}",
            file=sys.stderr,
        )
        return None


def _ms_to_iso(value: int | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def _append_native_event(repo: Any, *, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None
    try:
        return append_option_positions_v2_event(repo=repo, payload=payload)
    except Exception as exc:
        print(f"[WARN] option_positions v2 event append skipped ({type(exc).__name__}): {exc}", file=sys.stderr)
        return None


def _write_verification_snapshot(repo: Any, *, snapshot_id: str | None, event_id: str | None, note: str | None) -> dict[str, Any] | None:
    resolved_snapshot_id = str(snapshot_id or "").strip()
    if not resolved_snapshot_id and event_id:
        resolved_snapshot_id = f"verify-{str(event_id).strip()}"
    if not resolved_snapshot_id:
        return None
    try:
        return snapshot_current_positions_as_verification(
            repo=repo,
            snapshot_id=resolved_snapshot_id,
            source_name="cli_verification_checkpoint",
            source_type="manual_verification",
            note=note,
        )
    except Exception as exc:
        print(
            f"[WARN] option_positions verification snapshot skipped ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return None


def _apply_with_optional_sync(
    repo: Any,
    *,
    record_id: str,
    result: dict[str, Any],
    payload: dict[str, Any],
    native_event: dict[str, Any] | None = None,
    verification_snapshot_id: str | None = None,
    verification_note: str | None = None,
) -> dict[str, Any]:
    idempotent_duplicate = result.get("created") is False
    native_event_result = None if idempotent_duplicate else _append_native_event(repo, payload=native_event)
    verification_snapshot = _write_verification_snapshot(
        repo,
        snapshot_id=verification_snapshot_id,
        event_id=result.get("event_id"),
        note=verification_note,
    )
    v2_result = None
    try:
        v2_state = refresh_option_positions_v2_state(repo=repo)
        v2_result = {
            "baseline_snapshot_id": v2_state.baseline_snapshot.get("snapshot_id"),
            "processed_event_count": v2_state.projection.get("processed_event_count"),
            "open_position_count": v2_state.projection.get("open_position_count"),
            "diagnostic_count": len(v2_state.projection.get("diagnostics") or []),
            "native_event_id": (native_event_result or {}).get("event_id"),
            "verification_snapshot_id": (verification_snapshot or {}).get("snapshot_id"),
        }
    except Exception as exc:
        print(f"[WARN] option_positions v2 refresh skipped ({type(exc).__name__}): {exc}", file=sys.stderr)
    sync_result = _auto_sync_record_if_possible(repo, record_id=record_id) if record_id else None
    return payload | {
        "mode": "applied",
        "result": result,
        "v2_result": v2_result,
        "sync_result": sync_result,
        "idempotent_duplicate": bool(idempotent_duplicate),
    }


def _manual_open_record_id(result: dict[str, Any]) -> str:
    record_id = str(result.get("record_id") or "").strip()
    if record_id:
        return record_id
    event_id = str(result.get("event_id") or "").strip()
    if not event_id:
        return ""
    return f"lot_{event_id}"


def _build_trade_open_command(deal: NormalizedTradeDeal) -> OpenPositionCommand:
    side = str(deal.side or "").strip().lower()
    return OpenPositionCommand(
        broker="富途",
        account=str(deal.internal_account or ""),
        symbol=str(deal.symbol or ""),
        option_type=str(deal.option_type or ""),
        side="short" if side == "sell" else "long",
        contracts=int(deal.contracts or 0),
        currency=str(deal.currency or ""),
        strike=(float(deal.strike) if deal.strike is not None else None),
        multiplier=float(deal.multiplier) if deal.multiplier is not None else None,
        expiration_ymd=(str(deal.expiration_ymd or "").strip() or None),
        premium_per_share=float(deal.price or 0.0),
        note=(
            f"source=opend_push "
            f"deal_id={deal.deal_id} "
            f"order_id={deal.order_id or ''} "
            f"multiplier_source={deal.multiplier_source or ''} "
            f"trade_time_ms={deal.trade_time_ms or ''}"
        ).strip(),
        opened_at_ms=deal.trade_time_ms,
    )


def preview_trade_open(deal: NormalizedTradeDeal) -> dict[str, Any]:
    command = _build_trade_open_command(deal)
    return {"command": command, "fields": build_open_fields(command)}


def apply_trade_open_with(repo: Any, deal: NormalizedTradeDeal, *, persist_trade_event_fn: Any) -> dict[str, Any]:
    persist_trade_event_fn(repo, deal)
    return {"action": "open", "event_id": deal.deal_id}


def preview_trade_close(repo: Any, *, matches: list[Any], deal: NormalizedTradeDeal) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    close_action = "buy_close" if str(deal.side or "").strip().lower() == "buy" else "sell_close"
    close_reason = "auto_trade_buy_to_close" if close_action == "buy_close" else "auto_trade_sell_to_close"
    for match in matches:
        fields = repo.get_record_fields(match.record_id)
        operations.append(
            {
                "action": close_action,
                "record_id": match.record_id,
                "contracts_to_close": match.contracts_to_close,
                "patch": build_close_patch(
                    fields,
                    contracts_to_close=match.contracts_to_close,
                    close_price=(float(deal.price) if deal.price is not None else None),
                    close_reason=close_reason,
                    as_of_ms=deal.trade_time_ms,
                ),
                "matched_by": match.matched_by,
            }
        )
    return operations


def apply_trade_close_with(
    repo: Any,
    *,
    matches: list[Any],
    deal: NormalizedTradeDeal,
    persist_trade_event_fn: Any,
) -> list[dict[str, Any]]:
    persist_trade_event_fn(repo, deal)
    close_action = "buy_close" if str(deal.side or "").strip().lower() == "buy" else "sell_close"
    return [
        {
            "action": close_action,
            "record_id": match.record_id,
            "contracts_to_close": match.contracts_to_close,
        }
        for match in matches
    ]


@dataclass(frozen=True)
class ManualCloseResolvedMatch:
    record_id: str
    rule: str
    selector: dict[str, Any]
    candidate: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManualCloseMatchError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        selector: dict[str, Any],
        candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.selector = dict(selector)
        self.candidates = list(candidates or [])


def _canonical_selector_symbol(value: Any) -> str:
    return canonical_symbol(value) or str(value or "").strip().upper()


def _normalize_selector_expiration(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    exp_ms = parse_exp_to_ms(raw)
    if exp_ms is None:
        return raw
    return exp_ms_to_ymd(exp_ms)


def _same_optional_float(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return False


def _manual_close_candidate_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    record_id = str(item.get("record_id") or "").strip()
    if not record_id:
        return None
    view = build_option_position_view(item)
    fields = view.get("fields") if isinstance(view.get("fields"), dict) else {}
    return {
        "record_id": record_id,
        "broker": view.get("broker"),
        "account": view.get("account"),
        "symbol": _canonical_selector_symbol(view.get("symbol")),
        "option_type": view.get("option_type"),
        "side": view.get("side"),
        "status": view.get("status"),
        "strike": effective_strike(fields),
        "expiration_ymd": view.get("expiration_ymd"),
        "contracts": effective_contracts(fields),
        "contracts_open": effective_contracts_open(fields),
        "contracts_closed": view.get("contracts_closed"),
        "opened_at": view.get("opened_at"),
        "premium": view.get("premium"),
        "currency": view.get("currency"),
        "source_event_id": fields.get("source_event_id"),
    }


def _selector_summary(
    *,
    broker: str,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
) -> dict[str, Any]:
    return {
        "broker": normalize_broker(broker),
        "account": normalize_account(account),
        "symbol": _canonical_selector_symbol(symbol),
        "option_type": normalize_option_type(option_type),
        "side": normalize_side(position_side),
        "strike": float(strike) if strike is not None else None,
        "expiration_ymd": _normalize_selector_expiration(expiration_ymd),
        "contracts_to_close": int(contracts_to_close),
    }


def _manual_close_candidate_rows(repo: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        raw_records = require_option_positions_read_repo(repo).list_position_lots()
    except Exception:
        list_records = getattr(repo, "list_records", None)
        raw_records = list_records(page_size=500) if callable(list_records) else []
    for item in raw_records if isinstance(raw_records, list) else []:
        candidate = _manual_close_candidate_summary(item)
        if candidate is None:
            continue
        rows.append(candidate)
    rows.sort(key=lambda row: (int(row.get("opened_at") or 0), str(row.get("record_id") or "")))
    return rows


def resolve_manual_close_record_id(
    repo: Any,
    *,
    broker: str = "富途",
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    position_side: str | None,
    strike: float | None,
    expiration_ymd: str | None,
    contracts_to_close: int,
) -> ManualCloseResolvedMatch:
    selector = _selector_summary(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        position_side=position_side,
        strike=strike,
        expiration_ymd=expiration_ymd,
        contracts_to_close=contracts_to_close,
    )
    missing = [
        key for key in ("broker", "account", "symbol", "option_type", "side", "strike", "expiration_ymd")
        if selector.get(key) in (None, "")
    ]
    if missing:
        raise ManualCloseMatchError(
            "missing_selectors",
            "manual close auto matching requires " + ",".join(missing),
            selector=selector,
        )
    if int(contracts_to_close) <= 0:
        raise ManualCloseMatchError(
            "invalid_quantity",
            "contracts_to_close must be > 0",
            selector=selector,
        )

    rows = _manual_close_candidate_rows(repo)
    semantic_candidates = [
        row for row in rows
        if row.get("broker") == selector["broker"]
        and row.get("account") == selector["account"]
        and row.get("symbol") == selector["symbol"]
        and row.get("option_type") == selector["option_type"]
        and row.get("side") == selector["side"]
        and normalize_status(row.get("status")) == "open"
        and int(row.get("contracts_open") or 0) > 0
    ]
    exact_candidates = [
        row for row in semantic_candidates
        if _same_optional_float(row.get("strike"), selector["strike"])
        and row.get("expiration_ymd") == selector["expiration_ymd"]
    ]
    eligible_candidates = [
        row for row in exact_candidates
        if int(row.get("contracts_open") or 0) >= int(contracts_to_close)
    ]

    if not exact_candidates:
        raise ManualCloseMatchError(
            "not_found",
            "no open lot matches the manual close selector",
            selector=selector,
            candidates=semantic_candidates,
        )
    if not eligible_candidates:
        raise ManualCloseMatchError(
            "insufficient_contracts",
            "matching lots do not have enough open contracts",
            selector=selector,
            candidates=exact_candidates,
        )
    if len(eligible_candidates) > 1:
        raise ManualCloseMatchError(
            "multiple_matches",
            "multiple open lots match the manual close selector; specify record_id",
            selector=selector,
            candidates=eligible_candidates,
        )

    candidate = eligible_candidates[0]
    return ManualCloseResolvedMatch(
        record_id=str(candidate["record_id"]),
        rule="strict_contract_unique",
        selector=selector,
        candidate=candidate,
    )


def format_manual_close_match_error(error: ManualCloseMatchError) -> str:
    selector = error.selector
    selector_text = (
        f"broker={selector.get('broker') or '-'} account={selector.get('account') or '-'} "
        f"symbol={selector.get('symbol') or '-'} side={selector.get('side') or '-'} "
        f"option_type={selector.get('option_type') or '-'} exp={selector.get('expiration_ymd') or '-'} "
        f"strike={selector.get('strike') if selector.get('strike') is not None else '-'} "
        f"qty={selector.get('contracts_to_close') or '-'}"
    )
    lines = [f"[MATCH_FAIL] {error.code}: {error}", f"selector: {selector_text}"]
    if error.candidates:
        lines.append("candidates:")
        for row in error.candidates[:10]:
            lines.append(
                f"- {row.get('record_id')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('side')} {row.get('option_type')} | exp {row.get('expiration_ymd') or '-'} | "
                f"strike {row.get('strike') if row.get('strike') is not None else '-'} | "
                f"remaining {row.get('contracts_open')} | opened_at {row.get('opened_at') or '-'}"
            )
        if len(error.candidates) > 10:
            lines.append(f"... {len(error.candidates) - 10} more candidates")
    lines.append("hint: specify --record-id, or narrow account/symbol/exp/strike/side.")
    return "\n".join(lines)


def execute_manual_open(
    repo: Any | None,
    *,
    broker: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts: int,
    currency: str | None,
    strike: float | None,
    multiplier: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    underlying_share_locked: int | None,
    note: str | None,
    dry_run: bool,
    opened_at_ms: int | None = None,
) -> dict[str, Any]:
    command = OpenPositionCommand(
        broker=broker,
        account=account,
        symbol=symbol,
        option_type=option_type,
        side=side,
        contracts=int(contracts),
        currency=currency,
        strike=strike,
        multiplier=multiplier,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        underlying_share_locked=underlying_share_locked,
        note=note,
        opened_at_ms=opened_at_ms,
    )
    fields = build_open_fields(command)
    if dry_run:
        return {"mode": "dry_run", "fields": fields, "command": command}
    if repo is None:
        raise ValueError("repo is required when dry_run is false")
    result = persist_manual_open_event(repo, command)
    synced_record_id = _manual_open_record_id(result)
    return _apply_with_optional_sync(
        repo,
        record_id=synced_record_id,
        result=result,
        payload={"fields": fields, "command": command},
        native_event={
            "event_id": result.get("event_id"),
            "event_kind": "open_trade",
            "event_at_utc": _ms_to_iso(command.opened_at_ms),
            "source_name": "cli_manual_open",
            "source_type": "manual_trade_event",
            "broker": broker,
            "account": account,
            "symbol": symbol,
            "option_type": option_type,
            "side": side,
            "strike": fields.get("strike"),
            "expiration_ymd": expiration_ymd,
            "currency": fields.get("currency"),
            "multiplier": fields.get("multiplier"),
            "contracts": int(contracts),
            "snapshot_lot_id": synced_record_id or None,
        },
    )


def execute_manual_close(
    repo: Any,
    *,
    record_id: str | None = None,
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    dry_run: bool,
    broker: str = "富途",
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    position_side: str | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
) -> dict[str, Any]:
    resolved_record_id = str(record_id or "").strip()
    match_info: dict[str, Any] = {"rule": "explicit_record_id", "record_id": resolved_record_id}
    if not resolved_record_id:
        resolved_match = resolve_manual_close_record_id(
            repo,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=int(contracts_to_close),
        )
        resolved_record_id = resolved_match.record_id
        match_info = resolved_match.to_dict()

    fields = repo.get_record_fields(resolved_record_id)
    if not dry_run:
        duplicate_result = existing_manual_close_event_result(
            repo,
            record_id=resolved_record_id,
            fields=fields,
            contracts_to_close=int(contracts_to_close),
            close_price=close_price,
            close_reason=close_reason,
        )
        if duplicate_result is not None:
            return _apply_with_optional_sync(
                repo,
                record_id=resolved_record_id,
                result=duplicate_result,
                payload={
                    "fields": fields,
                    "patch": {},
                    "duplicate_checked_before_patch": True,
                    "match": match_info,
                },
                native_event=None,
            )
    patch = build_close_patch(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    )
    if dry_run:
        return {"mode": "dry_run", "fields": fields, "patch": patch, "match": match_info}
    result = persist_manual_close_event(
        repo,
        record_id=resolved_record_id,
        fields=fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    )
    return _apply_with_optional_sync(
        repo,
        record_id=resolved_record_id,
        result=result,
        payload={"fields": fields, "patch": patch, "match": match_info},
        native_event={
            "event_id": result.get("event_id"),
            "event_kind": "close_trade",
            "event_at_utc": _ms_to_iso(None),
            "source_name": "cli_manual_close",
            "source_type": "manual_trade_event",
            "broker": fields.get("broker"),
            "account": fields.get("account"),
            "symbol": fields.get("symbol"),
            "option_type": fields.get("option_type"),
            "side": fields.get("side"),
            "strike": fields.get("strike"),
            "expiration_ymd": effective_expiration_ymd(fields),
            "currency": fields.get("currency"),
            "multiplier": fields.get("multiplier"),
            "contracts": int(contracts_to_close),
            "snapshot_lot_id": resolved_record_id,
        },
    )


def execute_manual_adjust(
    repo: Any,
    *,
    record_id: str,
    contracts: int | None,
    strike: float | None,
    expiration_ymd: str | None,
    premium_per_share: float | None,
    multiplier: float | None,
    opened_at_ms: int | None,
    dry_run: bool,
) -> dict[str, Any]:
    fields = repo.get_record_fields(record_id)
    patch = build_open_adjustment_patch(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
    )
    if dry_run:
        return {"mode": "dry_run", "fields": fields, "patch": patch}
    result = persist_manual_adjust_event(
        repo,
        record_id=record_id,
        fields=fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
    )
    raw_target_contracts = patch.get("contracts_open")
    if raw_target_contracts is None:
        raw_target_contracts = fields.get("contracts_open") or fields.get("contracts") or 0
    return _apply_with_optional_sync(
        repo,
        record_id=record_id,
        result=result,
        payload={"fields": fields, "patch": patch},
        native_event={
            "event_id": result.get("event_id"),
            "event_kind": "manual_adjustment",
            "event_at_utc": _ms_to_iso(None),
            "source_name": "cli_manual_adjust",
            "source_type": "manual_trade_event",
            "broker": fields.get("broker"),
            "account": fields.get("account"),
            "symbol": fields.get("symbol"),
            "option_type": fields.get("option_type"),
            "side": fields.get("side"),
            "strike": patch.get("strike", fields.get("strike")),
            "expiration_ymd": expiration_ymd or effective_expiration_ymd(fields),
            "currency": fields.get("currency"),
            "multiplier": patch.get("multiplier", fields.get("multiplier")),
            "target_contracts": int(raw_target_contracts or 0),
            "snapshot_lot_id": record_id,
        },
        verification_snapshot_id=f"verify-{result.get('event_id')}",
        verification_note="manual_adjust checkpoint",
    )
