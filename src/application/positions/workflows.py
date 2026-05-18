from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.ledger.position_fields import (
    OpenPositionCommand,
    effective_expiration_ymd,
)
from src.application.ledger.api import (
    LotCloseResolutionError,
    preview_manual_position_adjust,
    preview_manual_position_close,
    preview_manual_position_open,
    record_manual_position_adjust,
    record_manual_position_close,
    record_manual_position_open,
    resolve_manual_position_close_target,
)
from src.application.positions.feishu_sync import sync_single_option_position_record
from src.application.positions.sync_config import effective_option_positions_sync_to_feishu_enabled


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
    del native_event, verification_snapshot_id, verification_note
    idempotent_duplicate = result.get("created") is False
    v2_result = {
        "mode": "retired",
        "reason": "post_write_v2_projection_disabled",
    }
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


@dataclass(frozen=True)
class ManualCloseResolvedMatch:
    record_id: str
    rule: str
    selector: dict[str, Any]
    candidate: dict[str, Any]
    close_target_resolution: dict[str, Any]

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
    selector_payload = {
        "broker": broker,
        "account": account,
        "symbol": symbol,
        "option_type": option_type,
        "side": position_side,
        "strike": strike,
        "expiration_ymd": expiration_ymd,
        "contracts_to_close": contracts_to_close,
    }
    missing = [
        key
        for key in ("broker", "account", "symbol", "option_type", "side", "strike", "expiration_ymd")
        if selector_payload.get(key) in (None, "")
    ]
    if missing:
        raise ManualCloseMatchError(
            "missing_selectors",
            "manual close auto matching requires " + ",".join(missing),
            selector=selector_payload,
        )
    if int(contracts_to_close) <= 0:
        raise ManualCloseMatchError(
            "invalid_quantity",
            "contracts_to_close must be > 0",
            selector=selector_payload,
        )

    try:
        resolution = resolve_manual_position_close_target(
            repo,
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd=expiration_ymd,
            contracts_to_close=contracts_to_close,
        )
    except LotCloseResolutionError as exc:
        selector_payload = exc.selector.to_dict()
        candidates = [item.to_dict() for item in exc.candidates]
        messages = {
            "not_found": "no open lot matches the manual close selector",
            "insufficient_contracts": "matching lots do not have enough open contracts",
            "multiple_matches": "multiple open lots match the manual close selector; specify record_id",
        }
        raise ManualCloseMatchError(
            exc.code,
            messages.get(exc.code, str(exc)),
            selector=selector_payload,
            candidates=candidates,
        ) from exc

    match = resolution.single_match
    return ManualCloseResolvedMatch(
        record_id=match.record_id,
        rule=match.matched_by,
        selector=resolution.selector,
        candidate=match.candidate.to_dict() if match.candidate is not None else {},
        close_target_resolution=resolution.to_dict(),
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
    if dry_run:
        return {"mode": "dry_run", **preview_manual_position_open(repo, command).to_payload()}
    if repo is None:
        raise ValueError("repo is required when dry_run is false")
    payload = record_manual_position_open(repo, command).to_payload()
    result = payload["result"]
    fields = payload["fields"]
    payload_command = payload.get("command")
    command = payload_command if isinstance(payload_command, OpenPositionCommand) else command
    synced_record_id = _manual_open_record_id(result)
    return _apply_with_optional_sync(
        repo,
        record_id=synced_record_id,
        result=result,
        payload=payload,
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
            "expiration_ymd": effective_expiration_ymd(fields) or expiration_ymd,
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

    if dry_run:
        return {
            "mode": "dry_run",
            "match": match_info,
            **preview_manual_position_close(
                repo,
                record_id=resolved_record_id,
                contracts_to_close=int(contracts_to_close),
                close_price=close_price,
                close_reason=close_reason,
            ).to_payload(),
        }
    close_payload = record_manual_position_close(
        repo,
        record_id=resolved_record_id,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
    ).to_payload()
    result = close_payload["result"]
    if "close_target_resolution" in close_payload and "close_target_resolution" not in match_info:
        match_info["close_target_resolution"] = close_payload["close_target_resolution"]
    payload = close_payload | {"match": match_info}
    ledger_preflight = close_payload["ledger_preflight"]
    is_duplicate = result.get("created") is False
    return _apply_with_optional_sync(
        repo,
        record_id=resolved_record_id,
        result=result,
        payload=payload,
        native_event=None if is_duplicate else {
            "event_id": result.get("event_id"),
            "event_kind": "close_trade",
            "event_at_utc": _ms_to_iso(int(ledger_preflight["event_time_ms"])),
            "source_name": "cli_manual_close",
            "source_type": "manual_trade_event",
            "broker": close_payload["fields"].get("broker"),
            "account": close_payload["fields"].get("account"),
            "symbol": close_payload["fields"].get("symbol"),
            "option_type": close_payload["fields"].get("option_type"),
            "side": close_payload["fields"].get("side"),
            "strike": close_payload["fields"].get("strike"),
            "expiration_ymd": effective_expiration_ymd(close_payload["fields"]),
            "currency": close_payload["fields"].get("currency"),
            "multiplier": close_payload["fields"].get("multiplier"),
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
    if dry_run:
        return {
            "mode": "dry_run",
            **preview_manual_position_adjust(
                repo,
                record_id=record_id,
                contracts=contracts,
                strike=strike,
                expiration_ymd=expiration_ymd,
                premium_per_share=premium_per_share,
                multiplier=multiplier,
                opened_at_ms=opened_at_ms,
            ).to_payload(),
        }
    adjust_payload = record_manual_position_adjust(
        repo,
        record_id=record_id,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
    ).to_payload()
    result = adjust_payload["result"]
    fields = adjust_payload["fields"]
    patch = adjust_payload["patch"]
    raw_target_contracts = patch.get("contracts_open")
    if raw_target_contracts is None:
        raw_target_contracts = fields.get("contracts_open") or fields.get("contracts") or 0
    return _apply_with_optional_sync(
        repo,
        record_id=record_id,
        result=result,
        payload=adjust_payload,
        native_event={
            "event_id": result.get("event_id"),
            "event_kind": "manual_adjustment",
            "event_at_utc": _ms_to_iso(int(adjust_payload["ledger_preflight"]["event_time_ms"])),
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
