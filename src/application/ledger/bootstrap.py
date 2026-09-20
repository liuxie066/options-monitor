from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    now_ms,
    strategy_metadata_fields_from_payload,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.trade_contract_identity import canonical_contract_symbol, derive_trade_side
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.current_decision_runtime import (
    capture_trade_event_decision_projection_fence,
    defer_current_decision_projection,
)
from src.application.ledger.position_projection_runtime import (
    projection_refresh_result_from_runtime,
    run_position_projection_in_transaction,
)
from src.application.ledger.repository import (
    SQLiteOptionPositionsRepository,
    _load_data_config,
    with_sqlite_repo_transaction,
)
from src.application.ledger.results import ProjectionRefreshResult
from src.application.ledger.store_resolution import resolve_ledger_store
from src.infrastructure.feishu_bitable import safe_float


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _is_incomplete_option_bootstrap_fields(fields: dict[str, Any]) -> bool:
    option_type = str(fields.get("option_type") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return False
    expiration = fields.get("expiration_ymd") or fields.get("expiration")
    strike = safe_float(fields.get("strike"))
    return expiration in (None, "") or strike is None


def _normalize_bootstrap_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    skipped = 0
    for item in records:
        lot_id = str(item.get("record_id") or item.get("id") or "").strip()
        fields = item.get("fields") or {}
        if not lot_id or not isinstance(fields, dict):
            skipped += 1
            continue
        broker = normalize_broker(fields.get("broker"))
        if not broker:
            broker = normalize_broker(fields.get("market"))
        if not broker:
            skipped += 1
            continue
        if _is_incomplete_option_bootstrap_fields(fields):
            skipped += 1
            print(
                (
                    f"[WARN] option_positions bootstrap skipped incomplete option row "
                    f"record_id={lot_id or '(missing)'} symbol={fields.get('symbol') or ''} "
                    f"option_type={fields.get('option_type') or ''} expiration={fields.get('expiration_ymd') or fields.get('expiration') or ''} "
                    f"strike={fields.get('strike') or ''}"
                ),
                file=sys.stderr,
            )
            continue
        normalized_fields = dict(fields)
        normalized_fields["broker"] = broker
        normalized.append({"record_id": lot_id, "fields": normalized_fields})
    if skipped:
        print(f"[WARN] option_positions bootstrap skipped {skipped} rows without broker/market", file=sys.stderr)
    return normalized


def _stable_bootstrap_event_id(source_name: str, lot_id: str, fields: dict[str, Any]) -> str:
    seed = json.dumps({"record_id": lot_id, "fields": fields}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"bootstrap:{source_name}:{lot_id}:{digest}"


def _safe_bootstrap_trade_time_ms(lot_id: str, fields: dict[str, Any]) -> int | None:
    saw_nonempty = False
    for key in ("opened_at", "last_action_at"):
        raw = fields.get(key)
        if raw in (None, ""):
            continue
        saw_nonempty = True
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    if not saw_nonempty:
        return now_ms()
    print(
        (
            f"[WARN] option_positions bootstrap skipped row with invalid timestamps "
            f"record_id={lot_id or '(missing)'} opened_at={fields.get('opened_at') or ''} "
            f"last_action_at={fields.get('last_action_at') or ''}"
        ),
        file=sys.stderr,
    )
    return None


def _bootstrap_trade_event(item: dict[str, Any], *, source_name: str) -> Any | None:
    lot_id = str(item.get("record_id") or "").strip()
    fields = item.get("fields") or {}
    if not lot_id or not isinstance(fields, dict):
        return None
    broker = normalize_broker(fields.get("broker") or fields.get("market"))
    if not broker:
        return None
    trade_time_ms = _safe_bootstrap_trade_time_ms(lot_id, fields)
    if trade_time_ms is None:
        return None
    raw_fields = dict(fields)
    raw_fields["broker"] = broker
    raw_multiplier = safe_float(fields.get("multiplier"))
    expiration_ymd = str(fields.get("expiration_ymd") or exp_ms_to_ymd(fields.get("expiration")) or "").strip() or None
    event_id = _stable_bootstrap_event_id(source_name, lot_id, raw_fields)
    multiplier_evidence = None
    if (
        raw_multiplier is not None
        and raw_multiplier > 0
        and raw_multiplier == int(raw_multiplier)
    ):
        source_receipt_sha256 = canonical_sha256(
            {
                "source": source_name,
                "lot_record_id": lot_id,
                "fields": raw_fields,
            }
        )
        multiplier_evidence = {
            "schema_version": "contract_multiplier_evidence.v1",
            "source": "bootstrap_snapshot",
            "canonical_symbol": _canonical_trade_symbol(fields.get("symbol")),
            "multiplier": int(raw_multiplier),
            "source_receipt_id": event_id,
            "source_receipt_sha256": source_receipt_sha256,
        }
    raw_payload = {
        "source_type": "bootstrap_snapshot",
        "lot_record_id": lot_id,
        # The legacy ``fields`` snapshot is deliberately NOT seeded. The payload
        # is a pure function of the projected lot (I-1), and a verbatim copy of a
        # historical row is an open set: any legacy spelling it happens to carry
        # (``exp``, ``underlying_shares_locked``) would ride into a new payload.
        # The values the event needs are on the event itself (``contract_key``,
        # ``contracts``, ``price``, ``multiplier``, ``side``, ``currency``).
        "source": source_name,
        "multiplier_source": "bootstrap_snapshot" if raw_multiplier is not None else None,
        "multiplier_evidence": multiplier_evidence,
        "multiplier_evidence_hash": (
            canonical_sha256(multiplier_evidence)
            if multiplier_evidence is not None
            else None
        ),
        "side": derive_trade_side("open", fields.get("side")),
    }
    # The strategy family is the one fact whose home this batch declares to be
    # the event layer (``write-side-definition.md`` §2), and the read side reads
    # it off the payload's top level
    # (``wheel.lot_strategy_metadata_from_trade_events``). Seeding the whole
    # snapshot is what the note above rules out; seeding the family is not the
    # same act -- these are declared patch keys, not an open set of spellings --
    # and without it an imported lot has no carrier left for its family once the
    # payload keys are dropped.
    raw_payload.update(
        strategy_metadata_fields_from_payload(raw_fields, include_legacy=True)
    )
    try:
        contract_key = ContractKey.from_values(
            broker=broker,
            account=normalize_account(fields.get("account")),
            underlying_symbol=_canonical_trade_symbol(fields.get("symbol")),
            option_type=str(fields.get("option_type") or ""),
            strike=safe_float(fields.get("strike")),
            expiration_ymd=expiration_ymd,
        )
    except Exception as exc:
        print(
            (
                f"[WARN] option_positions bootstrap skipped row that cannot be converted "
                f"record_id={lot_id or '(missing)'} source={source_name} error={exc}"
            ),
            file=sys.stderr,
        )
        return None
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=trade_time_ms,
        contract_key=contract_key,
        contracts=max(0, int(safe_float(fields.get("contracts")) or safe_float(fields.get("contracts_open")) or 0)),
        price=float(safe_float(fields.get("premium")) or 0.0),
        currency=normalize_currency(fields.get("currency")),
        source=source_name,
        multiplier=(float(raw_multiplier) if raw_multiplier is not None else 100.0),
        lot_id=lot_id,
        raw_payload=raw_payload,
    )


def _bootstrap_trade_events(records: list[dict[str, Any]], *, source_name: str) -> list[Any]:
    events: list[Any] = []
    for item in records:
        event = _bootstrap_trade_event(item, source_name=source_name)
        if event is not None:
            events.append(event)
    return events


def _has_retired_feishu_bootstrap_opt_in(cfg: dict[str, Any]) -> bool:
    option_positions_cfg = cfg.get("option_positions")
    if not isinstance(option_positions_cfg, dict):
        return False
    bootstrap_cfg = option_positions_cfg.get("bootstrap_from_feishu")
    if not isinstance(bootstrap_cfg, dict):
        return False
    return bool(bootstrap_cfg.get("enabled") is True)


def _raise_if_local_bootstrap_projection_failed(events: list[Any], projection: Any) -> None:
    """Re-raise a failed local bootstrap import with the ledger's error codes.

    The per-field pass that used to sit here read the open event's legacy
    ``fields`` snapshot (``_bootstrap_event_raw_fields``) to name a missing
    ``expiration``/``strike``. That snapshot is no longer seeded -- the event
    carries its own contract -- so there is nothing left to name a field off, and
    the diagnostic codes below are the whole message.
    """
    position_lot_sources = {"sqlite_position_lots", "legacy_position_lots"}
    if not any(_bootstrap_event_source(event) in position_lot_sources for event in events):
        return
    if not bool(getattr(projection, "has_errors", False)):
        return
    diagnostics = getattr(projection, "diagnostics", [])
    codes = ", ".join(str(getattr(item, "code", "") or "") for item in diagnostics if getattr(item, "severity", "") == "error")
    raise ValueError(f"local position_lots bootstrap projection invalid: {codes or 'unknown'}")


def _bootstrap_event_source(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("source_name") or (event.get("raw_payload") or {}).get("source") or "").strip()
    return str(getattr(event, "source", "") or "").strip()


def materialize_bootstrap_events(repo: SQLiteOptionPositionsRepository, events: list[Any]) -> int:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> int:
        if conn is None:
            raise TypeError("ledger bootstrap requires SQLite transaction authority")
        _raise_if_local_bootstrap_projection_failed(
            events,
            project_stored_trade_events_to_position_lots(events),
        )
        run_position_projection_in_transaction(
            sqlite_repo,
            events,
            conn=conn,
            mode="forced_full",
            seed_checkpoint=True,
        )
        return len(events)

    return int(
        with_sqlite_repo_transaction(
            repo,
            _run,
            require_projection_publication=True,
        )
    )


def apply_bootstrap_snapshot(
    repo: Any,
    *,
    records: list[dict[str, Any]],
    source_name: str,
    success_status: str,
    success_message: str,
    failure_status: str,
    failure_message: str,
    failure_log_prefix: str,
) -> bool:
    try:
        count = materialize_bootstrap_events(repo, _bootstrap_trade_events(records, source_name=source_name))
        repo.bootstrap_status = success_status
        repo.bootstrap_message = success_message.format(count=count)
        return True
    except Exception as exc:
        repo.bootstrap_status = failure_status
        repo.bootstrap_message = failure_message.format(error=exc)
        print(
            f"[WARN] {failure_log_prefix} for {repo.db_path}: {exc}",
            file=sys.stderr,
        )
        return False


def load_option_positions_repo(
    data_config: Path,
    *,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> SQLiteOptionPositionsRepository:
    store = resolve_ledger_store(data_config, config_path=config_path, runtime_root=runtime_root)
    repo = SQLiteOptionPositionsRepository(store.sqlite_path)
    repo.data_config_path = store.data_config_path
    setattr(repo, "ledger_store", store)
    setattr(repo, "bootstrap_projection_refresh", None)
    data_cfg = _load_data_config(data_config)
    if repo.count_trade_events() > 0:
        repo.bootstrap_status = "skipped_existing_trade_events"
        repo.bootstrap_message = "trade_events already present"
        if repo.count_position_lots() == 0:
            def _recover(
                sqlite_repo: Any,
                conn: sqlite3.Connection | None,
            ) -> ProjectionRefreshResult:
                if conn is None:
                    raise TypeError("ledger startup recovery requires SQLite transaction authority")
                event_count = int(
                    conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0]
                )
                decision_fence = capture_trade_event_decision_projection_fence(
                    sqlite_repo,
                    conn=conn,
                )
                runtime = run_position_projection_in_transaction(
                    sqlite_repo,
                    conn=conn,
                    mode="forced_full",
                )
                return projection_refresh_result_from_runtime(
                    runtime,
                    trade_event_count=event_count,
                    decision_projection=defer_current_decision_projection(
                        decision_fence
                    ),
                )

            setattr(
                repo,
                "bootstrap_projection_refresh",
                with_sqlite_repo_transaction(
                    repo,
                    _recover,
                    require_projection_publication=True,
                ),
            )
        return repo

    if repo.count_position_lots() > 0:
        repo.bootstrap_status = "sqlite_only_position_lots_without_trade_events"
        repo.bootstrap_message = (
            "position_lots exist without trade_events; rebuild from canonical trade_events "
            "or repair the active ledger before writes"
        )
        return repo

    if _has_retired_feishu_bootstrap_opt_in(data_cfg):
        repo.bootstrap_status = "sqlite_only_feishu_bootstrap_retired"
        repo.bootstrap_message = "feishu option_positions bootstrap is retired; local trade_events remain source of truth"
    else:
        repo.bootstrap_status = "sqlite_only_no_feishu_bootstrap"
        repo.bootstrap_message = "feishu option_positions bootstrap is not used; local trade_events remain source of truth"

    return repo
