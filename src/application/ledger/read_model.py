from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
    expiration_timestamp_to_date,
    expiration_timestamp_to_ymd,
)
from domain.domain.ledger.position_fields import (
    effective_contracts,
    effective_contracts_closed,
    effective_contracts_open,
    effective_multiplier,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_option_type,
    normalize_side,
    normalize_status,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import canonical_symbol
from domain.domain.wheel import (
    attach_lot_strategy_metadata,
    lot_strategy_metadata_from_trade_events,
)
from src.application.config_loader import resolve_data_config_path
from src.application.settings import build_effective_env
from src.application.ledger.bootstrap import load_option_positions_repo
from src.application.ledger.repository import require_option_positions_read_repo
from src.infrastructure.feishu_bitable import safe_float


def _resolve_data_config_for_config_path(
    *,
    base: Path,
    data_config: str | Path | None,
    config_path: str | Path | None = None,
) -> Path:
    if config_path is None or not str(config_path).strip():
        return resolve_data_config_path(base=base, data_config=data_config)
    resolved_config = Path(config_path).expanduser()
    if not resolved_config.is_absolute():
        resolved_config = resolved_config.resolve()
    if data_config is not None and str(data_config).strip():
        path = Path(data_config).expanduser()
        if not path.is_absolute():
            path = (resolved_config.parent / path).resolve()
        return path
    env_ref = str(build_effective_env().get("OM_DATA_CONFIG") or "").strip()
    if env_ref:
        return Path(env_ref).expanduser().resolve()
    return (resolved_config.parent / "portfolio.runtime.json").resolve()


def resolve_position_repo(
    *,
    base: Path,
    data_config: str | Path | None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> tuple[Path, Any]:
    resolved_data_config = _resolve_data_config_for_config_path(
        base=base,
        data_config=data_config,
        config_path=config_path,
    )
    return resolved_data_config, load_option_positions_repo(
        resolved_data_config,
        config_path=config_path,
        runtime_root=runtime_root,
    )


def resolve_position_repo_from_config(
    *,
    base: Path,
    cfg: dict[str, Any] | None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> tuple[Path, Any]:
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) and isinstance(cfg.get("portfolio"), dict) else {}
    data_config_ref = data_config
    if data_config_ref is None or not str(data_config_ref).strip():
        data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    return resolve_position_repo(
        base=base,
        data_config=data_config_ref,
        config_path=config_path,
        runtime_root=runtime_root,
    )


def resolve_position_data_config_path(
    *,
    base: Path,
    cfg: dict[str, Any] | None = None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) and isinstance(cfg.get("portfolio"), dict) else {}
    data_config_ref = data_config
    if data_config_ref is None or not str(data_config_ref).strip():
        data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    return _resolve_data_config_for_config_path(
        base=base,
        data_config=data_config_ref,
        config_path=config_path,
    )


def open_performance_evidence_repository(repo: Any) -> Any:
    """Open the performance-evidence repository that shares the ledger SQLite file."""
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    db_path = getattr(repo, "db_path", None)
    if db_path in (None, ""):
        ledger_store = getattr(repo, "ledger_store", None)
        db_path = getattr(ledger_store, "sqlite_path", None)
    if db_path in (None, ""):
        raise ValueError("position ledger does not expose its SQLite path")
    return PerformanceEvidenceSQLiteRepository(Path(db_path))


def canonicalize_position_lot_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Publish a lot payload in the read model's own (flat) vocabulary.

    This is the single place the read model's vocabulary meets the stored
    payload's. ``position_lots.fields_json`` is now exactly
    ``PositionLot.to_dict()`` (``write-side-definition.md`` §1) -- the contract
    under one nested ``contract_key``, ``position_side``, ``contracts_opened``,
    ``premium_open``, ``opened_at_ms``, ``open_event_id`` -- while the read model
    and its consumers keep the flat names ``broker`` / ``symbol`` / ``side`` /
    ``contracts`` / ``premium`` / ``opened_at`` / ``source_event_id``. Each value
    is read from its converged home first and from the retired flat sibling
    after, so a row written before the shape switch reads the same as it always
    did. The raw payload keys stay in the result, so a consumer that wants the
    nested shape still finds it.
    """
    raw = dict(fields or {})
    contract_key = raw.get("contract_key")
    contract_key = contract_key if isinstance(contract_key, dict) else {}

    def _nested(key: str, *flat_keys: str) -> Any:
        value = contract_key.get(key)
        if value not in (None, ""):
            return value
        for flat_key in flat_keys:
            value = raw.get(flat_key)
            if value not in (None, ""):
                return value
        return None

    def _renamed(key: str, *flat_keys: str) -> Any:
        value = raw.get(key)
        if value not in (None, ""):
            return value
        for flat_key in flat_keys:
            value = raw.get(flat_key)
            if value not in (None, ""):
                return value
        return None

    #: The contract identity in the read model's flat spelling. Used below for
    #: the read model's own fields *and* as the source for the ``effective_*``
    #: helpers, which read those flat names.
    contract_scalars = {
        "broker": _nested("broker", "broker"),
        "account": _nested("account", "account"),
        "symbol": _nested("underlying_symbol", "symbol", "underlying_symbol"),
        "option_type": _nested("option_type", "option_type"),
        "strike": _nested("strike", "strike"),
        "expiration_ymd": _nested("expiration_ymd", "expiration_ymd"),
        "side": _renamed("position_side", "side"),
        "contracts": _renamed("contracts_opened", "contracts"),
        "premium": _renamed("premium_open", "premium"),
        "opened_at": _renamed("opened_at_ms", "opened_at"),
        "source_event_id": _renamed("open_event_id", "source_event_id"),
    }
    effective_source = {
        **raw,
        **{key: value for key, value in contract_scalars.items() if value not in (None, "")},
    }
    expiration = raw.get("expiration")
    expiration_ymd = (
        str(contract_scalars["expiration_ymd"] or "").strip()
        or str(raw.get("exp") or "").strip()
        or expiration_timestamp_to_ymd(expiration)
        or ""
    ).strip() or None
    locked_shares = safe_float(raw.get("underlying_share_locked"))
    if locked_shares is None:
        locked_shares = safe_float(raw.get("underlying_shares_locked"))
    close_event_ids = raw.get("close_event_ids")
    last_close_event_id = None
    if isinstance(close_event_ids, (list, tuple)):
        closed_ids = [str(item or "").strip() for item in close_event_ids if str(item or "").strip()]
        if closed_ids:
            last_close_event_id = closed_ids[-1]
    if last_close_event_id is None:
        last_close_event_id = str(raw.get("last_close_event_id") or "").strip() or None

    normalized = dict(raw)
    normalized.update(
        {
            "broker": normalize_broker(contract_scalars["broker"]) or None,
            "account": normalize_account(contract_scalars["account"]) or contract_scalars["account"],
            "symbol": (str(contract_scalars["symbol"] or "").strip().upper() or None),
            # ``note`` is not a payload key (``write-side-definition.md`` §2/§7,
            # 2026-09-20 ruling): ``PositionLot.to_dict()`` has no ``note``, so a
            # ``parse_note_kv`` fallback here could only ever read a key the write
            # side never writes. The retired flat spellings stay the only
            # fallbacks, and the ``note`` key itself is passed through untouched
            # for the display layer.
            "option_type": normalize_option_type(contract_scalars["option_type"]) or None,
            "side": normalize_side(contract_scalars["side"]) or None,
            "status": normalize_status(raw.get("status")) or None,
            "currency": normalize_currency(raw.get("currency")) or raw.get("currency") or None,
            "contracts": effective_contracts(effective_source),
            "contracts_open": effective_contracts_open(effective_source),
            "contracts_closed": effective_contracts_closed(effective_source),
            "multiplier": effective_multiplier(effective_source),
            "premium": contract_scalars["premium"],
            "opened_at": contract_scalars["opened_at"],
            "underlying_share_locked": locked_shares,
            "cash_secured_amount": safe_float(raw.get("cash_secured_amount")),
            "close_type": normalize_close_type(raw.get("close_type")) if raw.get("close_type") else None,
            # §7.1: ``position_id`` is retired; ``position_key`` is the single
            # derived display/aggregation key.
            "position_key": (str(raw.get("position_key") or "").strip() or None),
            "source_event_id": (str(contract_scalars["source_event_id"] or "").strip() or None),
            "last_close_event_id": last_close_event_id,
            "expiration_ymd": expiration_ymd,
        }
    )
    strike = safe_float(contract_scalars["strike"])
    if strike is not None:
        normalized["strike"] = strike
    # §7.2: ``expiration_ymd`` is the single expiry field of the read model. The
    # stored ms ``expiration`` still arrives from persisted fields_json (it backs
    # the position_lots.expiration column) and is read through it, but it is no
    # longer re-derived or published as a read-model field here.
    return normalized


def canonicalize_position_lot_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "lot_id": item.get("lot_id") or item.get("record_id"),
        "fields": canonicalize_position_lot_fields(item.get("fields") or {}),
    }


def attach_event_strategy_metadata(
    records: Sequence[Mapping[str, Any]],
    trade_events: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Fold the event-derived strategy family into projected lot records.

    The family (``wheel.STRATEGY_METADATA_KEYS``) is RECONSTRUCTIBLE
    (``write-side-definition.md`` §2): its home is the open/adjust event's
    ``raw_payload``/``fields``, so ``position_lots.fields_json`` no longer
    carries it. Every read-model consumer that reads the family off a record's
    flat ``fields`` -- ``views.as_open_position_min``,
    ``context_builder``'s combo inventory, ``close_advice_runner``'s
    relationship fields -- therefore has to get it back before the record is
    handed on.

    This is that one place, and it reuses the event-side reconstruction
    (``wheel.lot_strategy_metadata_from_trade_events``) rather than deriving the
    family a second way. Only keys the payload does not carry at all are filled
    (``wheel.merge_lot_strategy_metadata``), so an explicit clear stays a clear
    and a payload that still holds the family is left alone.
    """
    items = [dict(record) for record in records if isinstance(record, Mapping)]
    if not trade_events:
        return items
    strategy_by_lot_id = lot_strategy_metadata_from_trade_events(trade_events)
    if not strategy_by_lot_id:
        return items
    return [
        {**record, "fields": attach_lot_strategy_metadata(record, strategy_by_lot_id)}
        for record in items
    ]


def load_position_lot_records(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    _ = base
    primary_repo = require_option_positions_read_repo(repo)
    projected = primary_repo.list_position_lots()
    if not isinstance(projected, list):
        raise TypeError("position lot repository returned a non-list payload")
    # ``write-side-definition.md`` §2: the strategy family's home is the event
    # layer, so the read model's own loader is where it goes back onto the
    # records every consumer below reads. A repo that cannot serve events (a
    # projection-only double, a read model over a store without the table) is
    # left as-is rather than failing the read.
    event_reader = getattr(primary_repo, "list_trade_events", None)
    if not callable(event_reader):
        return projected
    return attach_event_strategy_metadata(projected, event_reader())


def load_canonical_position_lot_records(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    return [canonicalize_position_lot_record(item) for item in load_position_lot_records(repo, base=base)]


def resolve_position_lot_records(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any, list[dict[str, Any]]]:
    resolved_data_config, repo = resolve_position_repo(base=base, data_config=data_config)
    return resolved_data_config, repo, load_position_lot_records(repo, base=base)


def build_position_lot_view(
    item: dict[str, Any],
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    record = canonicalize_position_lot_record(item)
    fields = record.get("fields") or {}
    # §7.2: derive from expiration_ymd first; the stored ms form is only a
    # fallback for payloads written before expiration_ymd was authoritative.
    expiration_date = _parse_filter_date(fields.get("expiration_ymd")) or expiration_timestamp_to_date(
        fields.get("expiration")
    )
    resolved_as_of_date = as_of_date or datetime.now(EXPIRATION_DATE_TZ).date()
    days_to_expiration = (expiration_date - resolved_as_of_date).days if expiration_date is not None else None
    status = str(fields.get("status") or "").strip().lower()
    expiration_state = "unknown" if days_to_expiration is None else ("expired" if days_to_expiration < 0 else "active")
    state_warning = "expired_position_marked_open" if expiration_state == "expired" and status == "open" else None
    return {
        "lot_id": record.get("lot_id"),
        "fields": fields,
        "position_key": fields.get("position_key"),
        "broker": fields.get("broker"),
        "account": fields.get("account"),
        "symbol": fields.get("symbol"),
        "option_type": fields.get("option_type"),
        "side": fields.get("side"),
        "status": fields.get("status"),
        "strike": fields.get("strike"),
        "multiplier": fields.get("multiplier"),
        "expiration_ymd": fields.get("expiration_ymd"),
        "expiration_date": expiration_date,
        "days_to_expiration": days_to_expiration,
        "expiration_state": expiration_state,
        "state_warning": state_warning,
        "contracts": fields.get("contracts"),
        "contracts_open": fields.get("contracts_open"),
        "contracts_closed": fields.get("contracts_closed"),
        "currency": fields.get("currency"),
        "cash_secured_amount": fields.get("cash_secured_amount"),
        "cash_secured_amount_role": "assignment_collateral" if fields.get("cash_secured_amount") not in (None, "") else None,
        "underlying_share_locked": fields.get("underlying_share_locked"),
        "premium": fields.get("premium"),
        "opened_at": fields.get("opened_at"),
        "closed_at": fields.get("closed_at"),
        "last_action_at": fields.get("last_action_at"),
        "close_type": fields.get("close_type"),
        "close_reason": fields.get("close_reason"),
        "note": fields.get("note"),
    }


def _position_row_from_view(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "lot_id": view.get("lot_id"),
        "broker": view.get("broker"),
        "account": view.get("account"),
        "symbol": view.get("symbol"),
        "option_type": view.get("option_type"),
        "side": view.get("side"),
        "strike": view.get("strike"),
        "multiplier": view.get("multiplier"),
        "expiration_ymd": view.get("expiration_ymd"),
        "days_to_expiration": view.get("days_to_expiration"),
        "expiration_state": view.get("expiration_state"),
        "state_warning": view.get("state_warning"),
        "contracts": view.get("contracts"),
        "contracts_open": view.get("contracts_open"),
        "contracts_closed": view.get("contracts_closed"),
        "currency": view.get("currency"),
        "cash_secured_amount": view.get("cash_secured_amount"),
        "cash_secured_amount_role": view.get("cash_secured_amount_role"),
        "underlying_share_locked": view.get("underlying_share_locked"),
        "close_type": view.get("close_type"),
        "close_reason": view.get("close_reason"),
        "status": view.get("status"),
        "note": view.get("note"),
    }


def list_open_short_assignment_rows(
    repo: Any,
    *,
    accounts: list[str],
) -> list[dict[str, Any]]:
    """Strictly read every open short put/call needed by the stress scenario."""

    primary_repo = require_option_positions_read_repo(repo)
    projected = primary_repo.list_position_lots()
    if not isinstance(projected, list):
        raise TypeError("position lot repository must return a list")
    normalized_accounts = {
        normalize_account(account)
        for account in accounts
        if normalize_account(account)
    }
    rows: list[dict[str, Any]] = []
    for item in projected:
        view = build_position_lot_view(item)
        if normalize_account(view.get("account")) not in normalized_accounts:
            continue
        if view.get("status") != "open" or view.get("side") != "short":
            continue
        if view.get("option_type") not in {"put", "call"}:
            continue
        rows.append(_position_row_from_view(view))
    rows.sort(key=_position_row_sort_key)
    return rows


def list_position_rows(
    repo: Any,
    *,
    broker: str,
    account: str | None = None,
    status: str = "open",
    limit: int = 50,
    expiration_within_days: int | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    side: str | None = None,
    strike: float | None = None,
    expiration_exact: str | None = None,
    expiration_month: str | None = None,
    expiration_before: str | None = None,
    expiration_after: str | None = None,
    as_of_ms: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized_broker = normalize_broker(broker)
    normalized_account = normalize_account(account) if account else None
    normalized_symbol = canonical_symbol(symbol) if symbol else None
    normalized_option_type = normalize_option_type(option_type) if option_type else None
    normalized_side = normalize_side(side) if side else None
    normalized_strike = float(strike) if strike is not None else None
    exact_expiration = _parse_filter_date(expiration_exact)
    before_expiration = _parse_filter_date(expiration_before)
    after_expiration = _parse_filter_date(expiration_after)
    resolved_as_of_date = (
        datetime.fromtimestamp(int(as_of_ms) / 1000, tz=EXPIRATION_DATE_TZ).date()
        if as_of_ms is not None
        else datetime.now(EXPIRATION_DATE_TZ).date()
    )
    for item in load_canonical_position_lot_records(repo):
        view = build_position_lot_view(item, as_of_date=resolved_as_of_date)
        if normalized_broker and view.get("broker") != normalized_broker:
            continue
        if normalized_account and view.get("account") != normalized_account:
            continue
        if normalized_symbol and canonical_symbol(view.get("symbol")) != normalized_symbol:
            continue
        if normalized_option_type and view.get("option_type") != normalized_option_type:
            continue
        if normalized_side and view.get("side") != normalized_side:
            continue
        if normalized_strike is not None:
            raw_strike = view.get("strike")
            if raw_strike is None:
                continue
            try:
                if float(raw_strike) != normalized_strike:
                    continue
            except Exception:
                continue
        normalized_status = view.get("status")
        if status != "all" and normalized_status != status:
            continue
        expiration_ymd = _parse_filter_date(view.get("expiration_ymd"))
        if exact_expiration is not None and expiration_ymd != exact_expiration:
            continue
        if expiration_month and not str(view.get("expiration_ymd") or "").startswith(expiration_month):
            continue
        if before_expiration is not None and (expiration_ymd is None or expiration_ymd > before_expiration):
            continue
        if after_expiration is not None and (expiration_ymd is None or expiration_ymd < after_expiration):
            continue
        days_to_expiration = view.get("days_to_expiration")
        if expiration_within_days is not None:
            if days_to_expiration is None or days_to_expiration < 0 or days_to_expiration > int(expiration_within_days):
                continue
        rows.append(_position_row_from_view(view))
    rows.sort(key=_position_row_sort_key)
    return rows[: max(limit, 1)]


def _parse_filter_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _position_row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    expiration_date = _parse_filter_date(row.get("expiration_ymd"))
    strike = safe_float(row.get("strike"))
    return (
        expiration_date is None,
        expiration_date or date.max,
        str(row.get("account") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        str(row.get("option_type") or ""),
        strike is None,
        strike if strike is not None else float("inf"),
        str(row.get("lot_id") or ""),
    )


def format_position_money(value: float | int | None, currency: str) -> str:
    if value is None:
        return "-"
    amount = float(value)
    normalized_currency = str(currency or "").upper()
    if normalized_currency == "USD":
        return f"${amount:,.2f}"
    if normalized_currency == "HKD":
        return f"HKD {amount:,.2f}"
    if normalized_currency == "CNY":
        return f"¥{amount:,.2f}"
    return f"{amount:,.2f} {normalized_currency}"


def format_cash_secured_amount(value: Any, currency: str) -> str:
    amount = safe_float(value)
    return format_position_money(amount, currency) if amount is not None else "-"
