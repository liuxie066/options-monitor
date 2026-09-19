from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from domain.domain.ledger import (
    ProjectionResult,
    ProjectionTransition,
    ResumableProjectionResult,
    ResumableProjectionState,
    TradeEvent,
    project_resumable_trade_events,
)
from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.ledger.lots import PositionLot, lot_is_stock
from domain.domain.ledger.position_fields import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    SELL_TO_CLOSE,
    apply_strategy_metadata_patch,
    build_position_lot_fields,
    parse_exp_to_ms,
    strategy_metadata_fields_from_payload,
)
from domain.domain.money import canonical_decimal_text, quantize_money
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import normalize_trade_side
from src.application.ledger.event_codec import (
    effective_import_diagnostics,
    iter_import_stored_trade_events,
    trade_event_payload_dict,
)
from src.application.ledger.position_records import PositionLotRecord


PROJECTION_CONTRACT_VERSION = "position_lot_projection.v2"
_AUTO_CLOSE_FIELD_KEYS = (
    "auto_close_exp_src",
    "auto_close_grace_days",
)


@dataclass(frozen=True)
class PublishedPositionLotProjection:
    lots: list[PositionLotRecord]
    diagnostics: list[LedgerDiagnostic]
    ledger_projection: ProjectionResult
    resumable_state: ResumableProjectionState | None = None
    resumable_publication_state: ResumablePublicationState | None = None

    @property
    def has_errors(self) -> bool:
        return any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lots": [lot.to_dict() for lot in self.lots],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "has_errors": self.has_errors,
            "ledger_projection": self.ledger_projection.to_dict(),
        }


@dataclass(frozen=True)
class ResumablePublicationState:
    fields_by_lot_id: dict[str, dict[str, Any]]
    auto_close_baseline_by_lot_id: dict[str, dict[str, Any]]

    def __post_init__(self) -> None:
        if not isinstance(self.fields_by_lot_id, dict):
            raise TypeError("publication state fields_by_lot_id must be a dict")
        normalized: dict[str, dict[str, Any]] = {}
        for raw_lot_id, raw_fields in self.fields_by_lot_id.items():
            lot_id = str(raw_lot_id or "").strip()
            if not lot_id or not isinstance(raw_fields, dict):
                raise ValueError("publication state requires lot fields by id")
            normalized[lot_id] = deepcopy(raw_fields)
        raw_baselines = self.auto_close_baseline_by_lot_id
        if not isinstance(raw_baselines, dict) or set(raw_baselines) != set(
            normalized
        ):
            raise ValueError("publication state auto-close baselines must match lots")
        baselines: dict[str, dict[str, Any]] = {}
        for lot_id, raw_baseline in raw_baselines.items():
            if not isinstance(raw_baseline, dict) or not set(raw_baseline).issubset(
                _AUTO_CLOSE_FIELD_KEYS
            ):
                raise ValueError("publication state auto-close baseline is invalid")
            baselines[lot_id] = deepcopy(raw_baseline)
        object.__setattr__(
            self,
            "fields_by_lot_id",
            dict(sorted(normalized.items())),
        )
        object.__setattr__(
            self,
            "auto_close_baseline_by_lot_id",
            dict(sorted(baselines.items())),
        )

    @classmethod
    def empty(cls) -> "ResumablePublicationState":
        return cls(
            fields_by_lot_id={},
            auto_close_baseline_by_lot_id={},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "resumable_publication_state.v1",
            "active_lots": [
                {
                    "lot_id": lot_id,
                    "fields": deepcopy(fields),
                    "auto_close_baseline": deepcopy(
                        self.auto_close_baseline_by_lot_id[lot_id]
                    ),
                }
                for lot_id, fields in self.fields_by_lot_id.items()
            ],
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, decoded: Any) -> "ResumablePublicationState":
        if not isinstance(decoded, dict) or set(decoded) != {
            "schema_version",
            "active_lots",
        }:
            raise ValueError("publication state fields differ from v1 schema")
        if decoded["schema_version"] != "resumable_publication_state.v1":
            raise ValueError("publication state schema is unsupported")
        rows = decoded["active_lots"]
        if not isinstance(rows, list):
            raise ValueError("publication active_lots must be an array")
        fields_by_lot_id: dict[str, dict[str, Any]] = {}
        baselines_by_lot_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "lot_id",
                "fields",
                "auto_close_baseline",
            }:
                raise ValueError("publication lot fields differ from v1 schema")
            lot_id = str(row["lot_id"] or "").strip()
            if lot_id in fields_by_lot_id:
                raise ValueError(f"duplicate publication lot_id: {lot_id}")
            if not isinstance(row["fields"], dict):
                raise ValueError("publication lot fields must be an object")
            if not isinstance(row["auto_close_baseline"], dict):
                raise ValueError("publication auto-close baseline must be an object")
            fields_by_lot_id[lot_id] = row["fields"]
            baselines_by_lot_id[lot_id] = row["auto_close_baseline"]
        return cls(
            fields_by_lot_id=fields_by_lot_id,
            auto_close_baseline_by_lot_id=baselines_by_lot_id,
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ResumablePublicationState":
        def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in items:
                if key in out:
                    raise ValueError(f"duplicate publication state key: {key}")
                out[key] = value
            return out

        try:
            decoded = json.loads(
                bytes(payload).decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_raise_nonfinite(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("publication state is not valid UTF-8 JSON") from exc
        state = cls.from_dict(decoded)
        if state.to_json_bytes() != bytes(payload):
            raise ValueError("publication state JSON is not canonical")
        return state


@dataclass(frozen=True)
class ResumablePublishedPositionLotProjection:
    domain_state: ResumableProjectionState | None
    publication_state: ResumablePublicationState | None
    active_lots: tuple[PositionLotRecord, ...]
    touched_lots: tuple[PositionLotRecord, ...]
    diagnostics: tuple[LedgerDiagnostic, ...]
    requires_full_replay: bool = False
    full_replay_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return (
            self.domain_state is not None
            and self.publication_state is not None
            and not self.requires_full_replay
            and not self.diagnostics
        )


def _raise_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite publication state number: {value}")


def project_stored_trade_events_to_position_lots(events: list[Any]) -> PublishedPositionLotProjection:
    (
        ledger_events,
        import_diagnostics,
        legacy_by_event_id,
    ) = _collect_projection_inputs(events)
    domain_projection = project_resumable_trade_events(
        ledger_events,
        entry_mode="full",
    )
    ledger_projection = domain_projection.to_projection_result()
    effective_diagnostics = effective_import_diagnostics(
        ledger_events=ledger_events,
        import_diagnostics=import_diagnostics,
        projection_diagnostics=ledger_projection.diagnostics,
    )
    diagnostics = [*effective_diagnostics, *ledger_projection.diagnostics]
    if not diagnostics:
        current = _fold_resumable_publication(
            legacy_by_event_id=legacy_by_event_id,
            projection=domain_projection,
            publication_state=None,
        )
        if current.eligible:
            return PublishedPositionLotProjection(
                lots=list(current.touched_lots),
                diagnostics=[],
                ledger_projection=ledger_projection,
                resumable_state=current.domain_state,
                resumable_publication_state=current.publication_state,
            )
    legacy_by_event_id = {
        str(payload.get("event_id") or "").strip(): payload
        for payload in (trade_event_payload_dict(item) for item in events)
        if str(payload.get("event_id") or "").strip()
    }
    invalid_event_ids = {
        str(item.event_id or "").strip()
        for item in ledger_projection.diagnostics
        if item.severity == "error" and str(item.event_id or "").strip()
    }
    voided_event_ids = {
        str(event.target_event_id or "").strip()
        for event in ledger_events
        if (
            event.event_type == "void"
            and event.event_id not in invalid_event_ids
            and str(event.target_event_id or "").strip()
        )
    }
    applied_adjust_event_ids = {
        event.event_id
        for event in ledger_events
        if (
            event.event_type == "adjust"
            and event.event_id not in invalid_event_ids
            and event.event_id not in voided_event_ids
        )
    }
    lots = [
        _position_lot_to_legacy_record(
            lot,
            ledger_events=ledger_events,
            legacy_by_event_id=legacy_by_event_id,
            applied_adjust_event_ids=applied_adjust_event_ids,
        )
        for lot in ledger_projection.lots
    ]
    return PublishedPositionLotProjection(
        lots=lots,
        diagnostics=diagnostics,
        ledger_projection=ledger_projection,
    )


def project_stored_trade_events_to_resumable_position_lots(
    events: list[Any],
    *,
    domain_state: ResumableProjectionState | None = None,
    publication_state: ResumablePublicationState | None = None,
    entry_mode: str = "full",
) -> ResumablePublishedPositionLotProjection:
    mode = str(entry_mode or "").strip().lower()
    if mode == "tail" and (domain_state is None or publication_state is None):
        raise ValueError("tail publication requires domain and publication state")
    if mode == "full" and (domain_state is not None or publication_state is not None):
        raise ValueError("full publication cannot accept resumable state")
    if mode == "tail":
        assert domain_state is not None
        assert publication_state is not None
        domain_lot_ids = {item.lot_id for item in domain_state.active_lots}
        publication_lot_ids = set(publication_state.fields_by_lot_id)
        if domain_lot_ids != publication_lot_ids:
            return ResumablePublishedPositionLotProjection(
                domain_state=None,
                publication_state=None,
                active_lots=(),
                touched_lots=(),
                diagnostics=(),
                requires_full_replay=True,
                full_replay_reason="resumable_state_lot_ids_mismatch",
            )

    (
        ledger_events,
        import_diagnostics,
        legacy_by_event_id,
    ) = _collect_projection_inputs(events)
    projection = project_resumable_trade_events(
        ledger_events,
        initial_state=domain_state,
        entry_mode=mode,
    )
    if mode == "tail":
        effective_import = list(import_diagnostics)
    else:
        effective_import = effective_import_diagnostics(
            ledger_events=ledger_events,
            import_diagnostics=import_diagnostics,
            projection_diagnostics=list(projection.diagnostics),
        )
    diagnostics = tuple([*effective_import, *projection.diagnostics])
    if projection.requires_full_replay or diagnostics or projection.state is None:
        return ResumablePublishedPositionLotProjection(
            domain_state=None,
            publication_state=None,
            active_lots=(),
            touched_lots=(),
            diagnostics=diagnostics,
            requires_full_replay=(mode == "tail"),
            full_replay_reason=(
                projection.full_replay_reason
                or (
                    "tail_diagnostic"
                    if diagnostics and mode == "tail"
                    else "resumable_state_invalid"
                )
            ),
        )

    return _fold_resumable_publication(
        legacy_by_event_id=legacy_by_event_id,
        projection=projection,
        publication_state=publication_state,
    )


def _collect_projection_inputs(
    events: list[Any],
) -> tuple[
    list[TradeEvent],
    list[LedgerDiagnostic],
    dict[str, dict[str, Any]],
]:
    ledger_events: list[TradeEvent] = []
    diagnostics: list[LedgerDiagnostic] = []
    legacy_by_event_id: dict[str, dict[str, Any]] = {}
    public_event_types = {
        "open",
        "adjust",
        "close",
        "expire_close",
        "assignment",
        "exercise",
    }
    for payload, event, item_diagnostics in iter_import_stored_trade_events(events):
        diagnostics.extend(item_diagnostics)
        if event is not None:
            ledger_events.append(event)
        event_id = str(payload.get("event_id") or "").strip()
        event_type = str(payload.get("event_type") or "").strip().lower()
        if event_id and event_type in public_event_types:
            legacy_by_event_id[event_id] = payload
    return ledger_events, diagnostics, legacy_by_event_id


def _fold_resumable_publication(
    *,
    legacy_by_event_id: dict[str, dict[str, Any]],
    projection: ResumableProjectionResult,
    publication_state: ResumablePublicationState | None,
) -> ResumablePublishedPositionLotProjection:
    fields_by_lot_id = (
        deepcopy(publication_state.fields_by_lot_id)
        if publication_state is not None
        else {}
    )
    auto_close_baselines = (
        deepcopy(publication_state.auto_close_baseline_by_lot_id)
        if publication_state is not None
        else {}
    )
    touched_by_lot_id: dict[str, PositionLotRecord] = {}
    touched_order: list[str] = []
    for transition in projection.transitions:
        if not transition.applied or transition.lot_after is None:
            continue
        record = _fold_publication_transition(
            transition,
            existing_fields=fields_by_lot_id.get(
                transition.lot_after.lot_id
            ),
            auto_close_baseline=auto_close_baselines.get(
                transition.lot_after.lot_id,
                {},
            ),
            legacy_by_event_id=legacy_by_event_id,
        )
        if record.lot_id not in touched_by_lot_id:
            touched_order.append(record.lot_id)
        touched_by_lot_id[record.lot_id] = record
        if transition.lot_before is None:
            auto_close_baselines[record.lot_id] = {
                key: deepcopy(record.fields[key])
                for key in _AUTO_CLOSE_FIELD_KEYS
                if key in record.fields
            }
        if transition.finalized and publication_state is not None:
            fields_by_lot_id.pop(record.lot_id, None)
            auto_close_baselines.pop(record.lot_id, None)
        else:
            fields_by_lot_id[record.lot_id] = deepcopy(record.fields)

    active_lot_ids = {
        item.lot_id
        for item in (
            projection.state.active_lots if projection.state is not None else ()
        )
    }
    fields_by_lot_id = {
        lot_id: fields
        for lot_id, fields in fields_by_lot_id.items()
        if lot_id in active_lot_ids
    }
    auto_close_baselines = {
        lot_id: baseline
        for lot_id, baseline in auto_close_baselines.items()
        if lot_id in active_lot_ids
    }

    next_publication_state = ResumablePublicationState(
        fields_by_lot_id=fields_by_lot_id,
        auto_close_baseline_by_lot_id=auto_close_baselines,
    )
    active_records = tuple(
        PositionLotRecord(lot_id=lot_id, fields=deepcopy(fields))
        for lot_id, fields in next_publication_state.fields_by_lot_id.items()
    )
    return ResumablePublishedPositionLotProjection(
        domain_state=projection.state,
        publication_state=next_publication_state,
        active_lots=active_records,
        touched_lots=tuple(
            touched_by_lot_id[lot_id] for lot_id in touched_order
        ),
        diagnostics=(),
    )


def ensure_projection_publishable(
    projection: PublishedPositionLotProjection,
    *,
    operation: str,
) -> None:
    error_codes = sorted(
        {
            str(item.code or "").strip() or "unknown"
            for item in projection.diagnostics
            if item.severity == "error"
        }
    )
    if error_codes:
        raise ValueError(
            f"{str(operation or 'position projection').strip()} failed: "
            + ",".join(error_codes)
        )


def _position_lot_to_legacy_record(
    lot: PositionLot,
    *,
    ledger_events: list[TradeEvent],
    legacy_by_event_id: dict[str, dict[str, Any]],
    applied_adjust_event_ids: set[str],
) -> PositionLotRecord:
    ledger_by_event_id = {event.event_id: event for event in ledger_events}
    open_event = ledger_by_event_id.get(lot.open_event_id)
    legacy_open_event = legacy_by_event_id.get(lot.open_event_id, {})
    base_fields = _base_fields_for_lot(lot, open_event=open_event, legacy_open_event=legacy_open_event)
    fields = _apply_lot_state_fields(base_fields, lot, ledger_by_event_id=ledger_by_event_id)
    fields = _apply_adjust_strategy_patch_fields(
        fields,
        lot,
        ledger_events=ledger_events,
        applied_adjust_event_ids=applied_adjust_event_ids,
    )
    close_event = ledger_by_event_id.get(lot.close_event_ids[-1]) if lot.close_event_ids else None
    if close_event is not None:
        fields.update(_close_fields(close_event, legacy_by_event_id=legacy_by_event_id, lot=lot))
    return PositionLotRecord(lot_id=lot.lot_id, fields=fields)


def _fold_publication_transition(
    transition: ProjectionTransition,
    *,
    existing_fields: dict[str, Any] | None,
    auto_close_baseline: dict[str, Any],
    legacy_by_event_id: dict[str, dict[str, Any]],
) -> PositionLotRecord:
    lot = transition.lot_after
    if lot is None:
        raise ValueError("publication transition requires lot_after")
    event = transition.event
    if transition.lot_before is None:
        fields = _base_fields_for_lot(
            lot,
            open_event=event,
            legacy_open_event=legacy_by_event_id.get(event.event_id, event.to_dict()),
        )
    elif existing_fields is not None:
        fields = dict(existing_fields)
    else:
        raise ValueError("continuing publication transition requires existing fields")

    previous_last_close_event_id = fields.get("last_close_event_id")
    previous_close_action_at = fields.get("last_action_at")
    ledger_by_event_id = {event.event_id: event}
    fields = _apply_lot_state_fields(
        fields,
        lot,
        ledger_by_event_id=ledger_by_event_id,
    )
    if event.event_type == "adjust":
        fields = _apply_strategy_patch_fields(fields, event)
        if transition.previous_close_event_id not in (None, ""):
            fields["last_close_event_id"] = previous_last_close_event_id
            fields["last_action_at"] = previous_close_action_at
            if lot.contracts_open <= 0:
                fields["closed_at"] = previous_close_action_at
            else:
                fields.pop("closed_at", None)
    if event.event_type in {"close", "expire_close", "assignment", "exercise"}:
        for key in _AUTO_CLOSE_FIELD_KEYS:
            if key in auto_close_baseline:
                fields[key] = deepcopy(auto_close_baseline[key])
            else:
                fields.pop(key, None)
        fields.update(
            _close_fields(
                event,
                legacy_by_event_id=legacy_by_event_id,
                lot=lot,
            )
        )
    return PositionLotRecord(lot_id=lot.lot_id, fields=fields)


def _apply_strategy_patch_fields(
    fields: dict[str, Any],
    event: TradeEvent,
) -> dict[str, Any]:
    payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
    patch = payload.get("patch")
    if not isinstance(patch, dict):
        return dict(fields)
    return _apply_strategy_patch_payload(fields, patch)


def _apply_strategy_patch_payload(
    fields: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    out = apply_strategy_metadata_patch(fields, patch, include_legacy=True)
    value = patch.get("strategy_snapshot")
    if isinstance(value, dict):
        family = str(value.get("strategy_family") or value.get("family") or "").strip().lower()
        profile = str(
            value.get("strategy_profile") or value.get("profile") or value.get("strategy") or ""
        ).strip().lower()
        if family in {"", "sell_put"} and profile in {
            "legacy",
            "return",
            "return_first",
            "short_vol",
            "insurance_underwriting",
            "yield_first",
        }:
            out.pop("yield_enhancement_mode", None)
    return out


#: Published keys that only describe an option contract. A stock lot is
#: published in the §7.3 shares vocabulary and must not carry these forward from
#: a stored ``fields`` snapshot.
_OPTION_ONLY_FIELD_KEYS = (
    "strike",
    "expiration",
    "expiration_ymd",
    "premium",
    "cash_secured_amount",
    "underlying_share_locked",
)


#: Published keys that carry a §7.4 amount or price in the option vocabulary.
#: ``close_price`` is in the list although ``_close_fields`` writes it, because a
#: legacy snapshot can carry it into a row that is not being closed right now.
_OPTION_MONEY_FIELD_KEYS = (
    "strike",
    "premium",
    "close_price",
    "cash_secured_amount",
)


def _published_money_text(value: Any, *, field_name: str) -> str:
    """Render one §7.4 money/price value for the published ``fields_json``.

    §7.4 keeps the authority in ``Decimal`` and asks JSON to carry it as decimal
    text. Rounding goes through ``quantize_money`` so the stored row follows the
    project's single money rule (6 places, ROUND_HALF_UP) instead of whatever a
    binary float happened to land on.

    Quantities are a different vocabulary: ``shares_*`` stay on
    ``canonical_decimal_text`` because §7.3 makes them a Decimal share count, not
    an amount.
    """
    return canonical_decimal_text(quantize_money(value), field_name=field_name)


def _normalize_published_money_values(fields: dict[str, Any]) -> dict[str, Any]:
    """Re-render a stored snapshot's money keys as §7.4 decimal text.

    A snapshot taken before this rule existed carries money as a float, and only
    the keys this publisher re-derives are overwritten. Normalizing here is what
    keeps a legacy row from publishing a float next to a decimal string.
    """
    for key in _OPTION_MONEY_FIELD_KEYS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        fields[key] = _published_money_text(value, field_name=key)
    return fields


def _stock_lot_fields(
    lot: PositionLot,
    *,
    note: str | None = None,
    last_action_at: int | None = None,
) -> dict[str, Any]:
    """Publish a stock lot in the §7.3 quantity vocabulary.

    ``build_position_lot_fields`` treats ``strike``/``expiration_ymd``/
    ``premium_per_share`` as required, which a stock lot cannot supply: §7.3
    gives it ``shares_*`` and ``cost_basis_total`` instead. Building the option
    shape regardless is what made a stock event fail deep inside the write
    transaction with an error about ``option_type``.
    """
    opened_at = int(lot.opened_at_ms)
    return {
        "broker": lot.contract_key.broker,
        "account": lot.contract_key.account,
        "symbol": lot.contract_key.underlying_symbol,
        "asset_type": "stock",
        "quantity_unit": "share",
        "option_type": "",
        "side": lot.position_side,
        "currency": normalize_currency(lot.currency),
        "status": str(lot.status),
        "contracts": 0,
        "contracts_open": 0,
        "contracts_closed": 0,
        "shares_opened": canonical_decimal_text(
            lot.shares_opened, field_name="shares_opened"
        ),
        "shares_open": canonical_decimal_text(
            lot.shares_open, field_name="shares_open"
        ),
        "shares_closed": canonical_decimal_text(
            lot.shares_closed, field_name="shares_closed"
        ),
        "cost_basis_total": _published_money_text(
            lot.cost_basis_total, field_name="cost_basis_total"
        ),
        "multiplier": 0,
        "position_key": lot.position_key,
        "opened_at": opened_at,
        "last_action_at": int(
            opened_at if last_action_at is None else last_action_at
        ),
        "note": note or None,
    }


def _base_fields_for_lot(
    lot: PositionLot,
    *,
    open_event: TradeEvent | None,
    legacy_open_event: dict[str, Any],
) -> dict[str, Any]:
    raw_payload = _event_payload(legacy_open_event)
    snapshot_fields = raw_payload.get("fields")
    if isinstance(snapshot_fields, dict):
        fields = _normalize_published_money_values(dict(snapshot_fields))
    else:
        source_name = str(legacy_open_event.get("source_name") or (open_event.source if open_event else "")).strip()
        order_id = str(legacy_open_event.get("order_id") or "").strip()
        multiplier_source = str(legacy_open_event.get("multiplier_source") or "").strip()
        note = (
            f"source={source_name} "
            f"event_id={lot.open_event_id} "
            f"order_id={order_id} "
            f"multiplier_source={multiplier_source}"
        ).strip()
        if lot_is_stock(lot):
            fields = _stock_lot_fields(lot, note=note)
        else:
            fields = build_position_lot_fields(
                broker=lot.contract_key.broker,
                account=lot.contract_key.account,
                symbol=lot.contract_key.underlying_symbol,
                option_type=lot.contract_key.option_type,
                side=lot.position_side,
                contracts=int(lot.contracts_opened),
                currency=normalize_currency(lot.currency),
                strike=float(lot.contract_key.strike),
                multiplier=float(lot.multiplier),
                expiration_ymd=lot.contract_key.expiration_ymd,
                premium_per_share=float(lot.premium_open),
                note=note,
                opened_at_ms=int(lot.opened_at_ms),
                strategy_snapshot=_strategy_snapshot_from_payload(raw_payload),
            )
    fields.update(strategy_metadata_fields_from_payload(raw_payload, include_legacy=True))
    fields["source_event_id"] = lot.open_event_id
    fields["event_source_type"] = str(legacy_open_event.get("source_type") or "").strip()
    fields["event_source_name"] = str(legacy_open_event.get("source_name") or (open_event.source if open_event else "")).strip()
    return fields


def _apply_lot_state_fields(
    fields: dict[str, Any],
    lot: PositionLot,
    *,
    ledger_by_event_id: dict[str, TradeEvent],
) -> dict[str, Any]:
    out = dict(fields)
    # §7.1: ``position_id`` is retired. ``_base_fields_for_lot`` seeds from the
    # open event's stored ``fields`` snapshot, so a legacy row's ``position_id``
    # would otherwise be carried forward into newly published fields_json.
    out.pop("position_id", None)
    last_action_at = int(_last_action_at(lot, ledger_by_event_id=ledger_by_event_id))
    if lot_is_stock(lot):
        # §7.3: a stock lot publishes shares, not contracts. Drop any option-only
        # key a stored snapshot carried so the published row has one shape.
        for key in _OPTION_ONLY_FIELD_KEYS:
            out.pop(key, None)
        stock_fields = _stock_lot_fields(lot, last_action_at=last_action_at)
        # The option path leaves ``note`` to the base fields; do the same here so
        # a re-published stock lot does not lose the note it was opened with.
        stock_fields["note"] = out.get("note") or stock_fields["note"]
        out.update(stock_fields)
        return out
    expiration_ms = parse_exp_to_ms(lot.contract_key.expiration_ymd)
    out.update(
        {
            "broker": lot.contract_key.broker,
            "account": lot.contract_key.account,
            "symbol": lot.contract_key.underlying_symbol,
            "option_type": lot.contract_key.option_type,
            "side": lot.position_side,
            "contracts": int(lot.contracts_opened),
            "contracts_open": int(lot.contracts_open),
            "contracts_closed": int(lot.contracts_closed),
            "currency": normalize_currency(lot.currency),
            "status": lot.status,
            "strike": _published_money_text(lot.contract_key.strike, field_name="strike"),
            "expiration_ymd": lot.contract_key.expiration_ymd,
            "multiplier": _compact_number(lot.multiplier),
            "premium": _published_money_text(lot.premium_open, field_name="premium"),
            "opened_at": int(lot.opened_at_ms),
            "last_action_at": last_action_at,
            "position_key": lot.position_key,
        }
    )
    if expiration_ms is not None:
        out["expiration"] = int(expiration_ms)
    if lot.position_side == "short" and lot.contract_key.option_type == "put":
        # §7.4: compute the amount in Decimal, then store decimal text. The float
        # product this replaced could land one ulp off the exact value -- e.g.
        # ``5.001 * 100 * 3`` is ``1500.3000000000002``, and 3-decimal strikes like
        # ``5.001`` are reachable because ``PRICE_DECIMAL_PLACES`` is 3.
        out["cash_secured_amount"] = _published_money_text(
            lot.contract_key.strike * lot.multiplier * lot.contracts_opened,
            field_name="cash_secured_amount",
        )
    if lot.position_side == "short" and lot.contract_key.option_type == "call":
        out["underlying_share_locked"] = int(lot.multiplier) * int(lot.contracts_opened)
    return out


def _close_fields(
    event: TradeEvent,
    *,
    legacy_by_event_id: dict[str, dict[str, Any]],
    lot: PositionLot,
) -> dict[str, Any]:
    legacy_event = legacy_by_event_id.get(event.event_id, {})
    payload = _event_payload(legacy_event)
    close_type = str(payload.get("close_type") or "").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    trade_side = normalize_trade_side(legacy_event.get("side"))
    if close_type in {"assignment", "exercise"}:
        pass
    elif close_type != EXPIRE_AUTO_CLOSE and mode != EXPIRE_AUTO_CLOSE:
        if trade_side == "buy":
            close_type = BUY_TO_CLOSE
        elif trade_side == "sell":
            close_type = SELL_TO_CLOSE
        else:
            close_type = BUY_TO_CLOSE if event.position_side == "short" else SELL_TO_CLOSE
    else:
        close_type = EXPIRE_AUTO_CLOSE

    reason = str(payload.get("close_reason") or "").strip()
    if not reason:
        if close_type == EXPIRE_AUTO_CLOSE:
            reason = "expired"
        elif close_type == BUY_TO_CLOSE:
            reason = "broker_trade_buy_to_close"
        else:
            reason = "broker_trade_sell_to_close"

    fields: dict[str, Any] = {
        "close_type": close_type,
        "close_reason": reason,
        "close_price": _published_money_text(event.price, field_name="close_price"),
        "last_close_event_id": event.event_id,
        "last_action_at": int(event.event_time_ms),
    }
    if lot.contracts_open <= 0:
        fields["closed_at"] = int(event.event_time_ms)
    if close_type == EXPIRE_AUTO_CLOSE:
        fields["auto_close_exp_src"] = str(payload.get("auto_close_exp_src") or payload.get("effective_exp_source") or "").strip()
        raw_grace_days = payload.get("auto_close_grace_days")
        if raw_grace_days not in (None, ""):
            fields["auto_close_grace_days"] = int(raw_grace_days)
    return fields


def _last_action_at(lot: PositionLot, *, ledger_by_event_id: dict[str, TradeEvent]) -> int:
    event = ledger_by_event_id.get(lot.last_event_id)
    if event is not None:
        return int(event.event_time_ms)
    return int(lot.opened_at_ms)


def _apply_adjust_strategy_patch_fields(
    fields: dict[str, Any],
    lot: PositionLot,
    *,
    ledger_events: list[TradeEvent],
    applied_adjust_event_ids: set[str],
) -> dict[str, Any]:
    out = dict(fields)
    adjust_events = [
        event
        for event in ledger_events
        if (
            event.event_type == "adjust"
            and event.event_id in applied_adjust_event_ids
            and str(event.target_lot_id or "").strip() == lot.lot_id
        )
    ]
    adjust_events.sort(key=lambda event: (int(event.event_time_ms), str(event.event_id)))
    for event in adjust_events:
        payload = event.raw_payload if isinstance(event.raw_payload, dict) else {}
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            continue
        out = _apply_strategy_patch_payload(out, patch)
    return out


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("raw_payload") or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _strategy_snapshot_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = payload.get("strategy_snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _compact_number(value: Any) -> int | float:
    numeric = float(value or 0.0)
    return int(numeric) if numeric.is_integer() else numeric


__all__ = [
    "PROJECTION_CONTRACT_VERSION",
    "PublishedPositionLotProjection",
    "ResumablePublicationState",
    "ResumablePublishedPositionLotProjection",
    "ensure_projection_publishable",
    "project_stored_trade_events_to_resumable_position_lots",
    "project_stored_trade_events_to_position_lots",
]
