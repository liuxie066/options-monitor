from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.projection_verify import load_projection_verify_state
from src.application.ledger.bootstrap import load_option_positions_repo
from src.application.ledger.event_codec import valid_void_target_event_id
from src.application.ledger.lifecycle_overlay import (
    resolve_lifecycle_account_rows,
)
from src.application.ledger.repository import (
    require_option_positions_event_read_repo,
)
from src.application.ledger.risk_context import summarize_ledger_shadow_status
from src.application.ledger.views import PositionLotSnapshot, RiskPositionView


@dataclass(frozen=True)
class AssignedStockEventLog:
    events: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [dict(item) for item in self.events],
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


def open_position_ledger(data_config: Any) -> Any:
    return load_option_positions_repo(data_config)


def open_position_ledger_from_data_config(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any]:
    from src.application.ledger.read_model import resolve_position_repo as _impl

    return _impl(base=base, data_config=data_config)


def resolve_position_data_config_path(
    *,
    base: Path,
    cfg: dict[str, Any] | None = None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    from src.application.ledger.read_model import resolve_position_data_config_path as _impl

    return _impl(base=base, cfg=cfg, data_config=data_config, config_path=config_path)


def resolve_position_ledger_sqlite_path(
    *,
    base: Path,
    cfg: dict[str, Any] | None = None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    """Resolve the canonical SQLite owner behind a runtime config."""

    from src.application.ledger.store_resolution import resolve_ledger_store

    resolved_data_config = resolve_position_data_config_path(
        base=base,
        cfg=cfg,
        data_config=data_config,
        config_path=config_path,
    )
    return resolve_ledger_store(resolved_data_config).sqlite_path.resolve()


def open_position_ledger_from_runtime_config(
    *,
    base: Path,
    cfg: dict[str, Any] | None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> tuple[Path, Any]:
    from src.application.ledger.read_model import resolve_position_repo_from_config as _impl

    resolved_data_config, repo = _impl(
        base=base,
        cfg=cfg,
        data_config=data_config,
        config_path=config_path,
        runtime_root=runtime_root,
    )
    apply_position_ledger_runtime_config(repo, cfg)
    return resolved_data_config, repo


def open_performance_evidence_repository(repo: Any) -> Any:
    from src.application.ledger.read_model import open_performance_evidence_repository as _impl

    return _impl(repo)


def normalize_position_lot_fields(fields: dict[str, Any]) -> dict[str, Any]:
    from src.application.ledger.read_model import canonicalize_position_lot_fields as _impl

    return _impl(fields)


def normalize_position_lot_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    from src.application.ledger.read_model import canonicalize_position_lot_record as _impl

    return _impl(item)


def position_lot_snapshot(item: dict[str, Any]) -> PositionLotSnapshot:
    return PositionLotSnapshot.from_record(normalize_position_lot_snapshot(item))


def list_position_lot_snapshots(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import load_position_lot_records as _impl

    return _impl(repo, base=base)


def list_position_lot_sync_snapshots(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import load_canonical_position_lot_records as _impl

    return _impl(repo, base=base)


def list_canonical_position_lot_snapshots(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import load_canonical_position_lot_records as _impl

    return _impl(repo, base=base)


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
    from src.application.ledger.read_model import list_position_rows as _impl

    return _impl(
        repo,
        broker=broker,
        account=account,
        status=status,
        limit=limit,
        expiration_within_days=expiration_within_days,
        symbol=symbol,
        option_type=option_type,
        side=side,
        strike=strike,
        expiration_exact=expiration_exact,
        expiration_month=expiration_month,
        expiration_before=expiration_before,
        expiration_after=expiration_after,
        as_of_ms=as_of_ms,
    )


def list_open_short_assignment_rows(
    repo: Any,
    *,
    accounts: list[str],
) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import list_open_short_assignment_rows as _impl

    return _impl(repo, accounts=accounts)


def resolve_position_lot_snapshots(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any, list[dict[str, Any]]]:
    from src.application.ledger.read_model import resolve_position_lot_records as _impl

    return _impl(base=base, data_config=data_config)


def position_lot_context_view(
    item: dict[str, Any],
    *,
    as_of_date: Any = None,
) -> dict[str, Any]:
    from src.application.ledger.read_model import build_position_lot_view as _impl

    return _impl(item, as_of_date=as_of_date)


def position_lot_risk_view(
    item: dict[str, Any],
    *,
    as_of_date: Any = None,
) -> RiskPositionView:
    return RiskPositionView.from_view(position_lot_context_view(item, as_of_date=as_of_date))


def format_position_money(value: float | int | None, currency: str) -> str:
    from src.application.ledger.read_model import format_position_money as _impl

    return _impl(value, currency)


def format_position_cash_secured(value: Any, currency: str) -> str:
    from src.application.ledger.read_model import format_cash_secured_amount as _impl

    return _impl(value, currency)


def summarize_position_lot_shadow_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_ledger_shadow_status(records)


def apply_position_ledger_runtime_config(repo: Any, cfg: dict[str, Any] | None) -> Any:
    _ = cfg
    return repo


def assigned_stock_event_log(repo: Any) -> AssignedStockEventLog:
    candidate = getattr(repo, "primary_repo", repo)
    list_events = getattr(candidate, "list_assigned_stock_events", None)
    if not callable(list_events):
        return AssignedStockEventLog(
            events=(),
            diagnostics=(
                {
                    "context": "assigned_stock",
                    "code": "assigned_stock_event_log_unavailable",
                    "message": "ledger repository does not expose assigned-stock events",
                },
            ),
        )
    try:
        raw_events = list_events()
    except Exception as exc:
        return AssignedStockEventLog(
            events=(),
            diagnostics=(
                {
                    "context": "assigned_stock",
                    "code": "assigned_stock_event_log_read_failed",
                    "message": str(exc),
                },
            ),
        )
    if not isinstance(raw_events, list):
        return AssignedStockEventLog(
            events=(),
            diagnostics=(
                {
                    "context": "assigned_stock",
                    "code": "assigned_stock_event_log_invalid_payload",
                    "message": "assigned-stock repository returned a non-list payload",
                },
            ),
        )
    events: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, item in enumerate(raw_events):
        if isinstance(item, dict):
            events.append(dict(item))
            continue
        diagnostics.append(
            {
                "context": "assigned_stock",
                "code": "assigned_stock_event_invalid_row",
                "message": "assigned-stock event row is not an object",
                "row_index": index,
            }
        )
    return AssignedStockEventLog(events=tuple(events), diagnostics=tuple(diagnostics))


def trade_event_log(repo: Any) -> list[dict[str, Any]]:
    sqlite_repo = require_option_positions_event_read_repo(repo)
    events = sqlite_repo.list_trade_events()
    return events if isinstance(events, list) else []


def list_trade_lifecycle_cases(
    repo: Any,
    *,
    status: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_fn = getattr(candidate, "list_trade_lifecycle_cases", None)
    if not callable(list_fn):
        return []
    rows = list_fn(status=status) if status else list_fn()
    out: list[dict[str, Any]] = []
    account_filter = str(account or "").strip().lower()
    symbol_filter = str(symbol or "").strip().upper()
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        if account_filter and str(row.get("account") or "").strip().lower() != account_filter:
            continue
        if symbol_filter and str(row.get("symbol") or "").strip().upper() != symbol_filter:
            continue
        out.append(dict(row))
    return out


def list_trade_lifecycle_evidence(
    repo: Any,
    *,
    case_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_fn = getattr(candidate, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return []
    rows = list_fn(case_id=case_id, account=account, symbol=symbol)
    return [dict(row) for row in list(rows or []) if isinstance(row, dict)]


def list_trade_lifecycle_due_candidates(
    repo: Any,
    *,
    account: str,
) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_fn = getattr(
        candidate,
        "list_trade_lifecycle_due_candidates",
        None,
    )
    if not callable(list_fn):
        raise TypeError("compact lifecycle due reader is unavailable")
    rows = list_fn(account=str(account or "").strip().lower())
    return [
        dict(row)
        for row in rows or ()
        if isinstance(row, dict)
    ]


def latest_trade_lifecycle_settlement_evidence(
    repo: Any,
    *,
    case_id: str,
) -> dict[str, Any] | None:
    candidate = getattr(repo, "primary_repo", repo)
    get_fn = getattr(
        candidate,
        "get_latest_trade_lifecycle_settlement_evidence",
        None,
    )
    if not callable(get_fn):
        raise TypeError("latest settlement evidence reader is unavailable")
    row = get_fn(case_id=str(case_id or "").strip())
    return dict(row) if isinstance(row, dict) else None


def lifecycle_reconciliation_facts(
    repo: Any,
    *,
    case_id: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Return the semantic ledger facts needed for lifecycle reconciliation."""

    candidate = getattr(repo, "primary_repo", repo)
    requested_case_id = str(case_id or "").strip()
    cases = list_trade_lifecycle_cases(repo)
    if requested_case_id:
        cases = [
            item
            for item in cases
            if str(item.get("case_id") or "").strip() == requested_case_id
        ]
    list_allocations = getattr(
        candidate,
        "list_trade_lifecycle_allocations",
        None,
    )
    allocations = (
        list(list_allocations(case_id=requested_case_id or None) or [])
        if callable(list_allocations)
        else []
    )
    evidence = list_trade_lifecycle_evidence(
        repo,
        case_id=requested_case_id or None,
    )
    list_events = getattr(candidate, "list_trade_events", None)
    trade_events = list(list_events() or []) if callable(list_events) else []
    effective_void_event_ids = sorted(
        {
            target
            for item in trade_events
            for target in [valid_void_target_event_id(item)]
            if target
        }
    )
    requested_evidence = None
    requested_evidence_id = str(evidence_id or "").strip()
    if requested_evidence_id:
        get_evidence = getattr(
            candidate,
            "get_trade_lifecycle_evidence",
            None,
        )
        if callable(get_evidence):
            raw_evidence = get_evidence(requested_evidence_id)
            if isinstance(raw_evidence, dict):
                requested_evidence = dict(raw_evidence)
    lot_ids = sorted(
        {
            str(lot_id or "").strip()
            for lifecycle_case in cases
            for lot_id in dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            )
            if str(lot_id or "").strip()
        }
    )
    get_lot_fields = getattr(candidate, "get_position_lot_fields", None)
    lot_fields_by_id: dict[str, dict[str, Any]] = {}
    if callable(get_lot_fields):
        for lot_id in lot_ids:
            fields = get_lot_fields(lot_id)
            if isinstance(fields, dict):
                lot_fields_by_id[lot_id] = dict(fields)
    return {
        "schema_version": "lifecycle_reconciliation_facts.v3",
        "cases": [dict(item) for item in cases if isinstance(item, dict)],
        "evidence": [
            dict(item) for item in evidence if isinstance(item, dict)
        ],
        "allocations": [
            dict(item) for item in allocations if isinstance(item, dict)
        ],
        "requested_evidence": requested_evidence,
        "position_lot_fields_by_id": lot_fields_by_id,
        "effective_void_event_ids": effective_void_event_ids,
    }


def lifecycle_option_close_anchor_facts(
    repo: Any,
    *,
    case_id: str,
) -> dict[str, Any]:
    """Expose validated anchors without re-reading or changing source ownership."""

    facts = lifecycle_case_coherent_facts(repo, case_id=case_id)
    resolution = dict(facts["case_resolution"])
    return {
        "schema_version": "lifecycle_option_close_anchor_facts.v1",
        "status": str(resolution.get("status") or "conflict"),
        "reason_codes": list(resolution.get("reason_codes") or []),
        "anchors": [dict(item) for item in facts["validated_anchors"]],
        "source_claims": [dict(item) for item in facts["anchor_source_claims"]],
        "bridge_evidence_ids": sorted(
            str(item.get("bridge_evidence_id") or "")
            for item in resolution.get("anchor_facts") or []
            if str(item.get("bridge_evidence_id") or "")
        ),
        "generation_token": dict(facts["generation_token"]),
    }


def lifecycle_account_coherent_facts(
    repo: Any,
    *,
    account: str,
) -> dict[str, Any]:
    """Read and resolve one account's lifecycle closure in one transaction."""

    candidate = getattr(repo, "primary_repo", repo)
    reader = getattr(candidate, "read_lifecycle_account_rows", None)
    if not callable(reader):
        raise TypeError("coherent lifecycle account reader is unavailable")
    rows = reader(account=str(account or "").strip().lower())
    if not isinstance(rows, dict):
        raise TypeError("coherent lifecycle account reader returned invalid rows")
    resolution = resolve_lifecycle_account_rows(rows)
    return {
        **rows,
        "account_lifecycle_resolution": resolution,
        "effective_void_event_ids": _effective_void_event_ids_from_rows(rows),
    }


def lifecycle_case_coherent_facts(
    repo: Any,
    *,
    case_id: str,
) -> dict[str, Any]:
    """Return one case view from the account-coherent lifecycle generation."""

    case_value = str(case_id or "").strip()
    candidate = getattr(repo, "primary_repo", repo)
    reader = getattr(candidate, "read_lifecycle_case_rows", None)
    if not case_value or not callable(reader):
        raise TypeError("coherent lifecycle case reader is unavailable")
    rows = reader(case_id=case_value)
    if not isinstance(rows, dict):
        raise TypeError("coherent lifecycle case reader returned invalid rows")
    return lifecycle_case_coherent_facts_from_account_snapshot(
        rows,
        case_id=case_value,
    )


def lifecycle_case_coherent_facts_from_account_snapshot(
    account_facts: Mapping[str, Any],
    *,
    case_id: str,
) -> dict[str, Any]:
    """Materialize one case without rereading an account-coherent snapshot."""

    case_value = str(case_id or "").strip()
    if not case_value or not isinstance(account_facts, Mapping):
        raise TypeError("coherent lifecycle account snapshot is unavailable")
    return lifecycle_case_coherent_facts_many_from_account_snapshot(
        account_facts,
        case_ids=(case_value,),
    )[case_value]


@dataclass(frozen=True)
class _LifecycleAccountSnapshotIndex:
    rows: dict[str, Any]
    account_resolution: dict[str, Any]
    lifecycle_cases_by_id: dict[str, dict[str, Any]]
    case_resolutions_by_id: dict[str, dict[str, Any]]
    generation_tokens_by_id: dict[str, dict[str, Any]]
    case_evidence_by_id: dict[str, list[dict[str, Any]]]
    case_allocations_by_id: dict[str, list[dict[str, Any]]]
    timing_policies_by_id: dict[str, dict[str, Any]]
    position_lot_fields_by_id: dict[str, dict[str, Any]]
    evidence_by_id: dict[str, dict[str, Any]]
    claim_by_binding: dict[tuple[str, str], dict[str, Any]]
    effective_void_event_ids: tuple[str, ...]


def lifecycle_case_coherent_facts_many_from_account_snapshot(
    account_facts: Mapping[str, Any],
    *,
    case_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Materialize selected cases after indexing one account snapshot once."""

    if not isinstance(account_facts, Mapping):
        raise TypeError("coherent lifecycle account snapshot is unavailable")
    requested: list[str] = []
    seen: set[str] = set()
    for raw_case_id in case_ids:
        case_value = str(raw_case_id or "").strip()
        if case_value and case_value not in seen:
            requested.append(case_value)
            seen.add(case_value)
    if not requested:
        return {}
    snapshot = _index_lifecycle_account_snapshot(account_facts)
    return {
        case_value: _materialize_lifecycle_case_from_account_snapshot_index(
            snapshot,
            case_id=case_value,
        )
        for case_value in requested
    }


def _index_lifecycle_account_snapshot(
    account_facts: Mapping[str, Any],
) -> _LifecycleAccountSnapshotIndex:
    rows = dict(account_facts)
    raw_resolution = rows.get("account_lifecycle_resolution")
    account_resolution = (
        dict(raw_resolution)
        if isinstance(raw_resolution, Mapping)
        else resolve_lifecycle_account_rows(rows)
    )

    lifecycle_cases_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in rows.get("account_lifecycle_cases") or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_id = str(item.get("case_id") or "").strip()
        if item_id:
            lifecycle_cases_by_id.setdefault(item_id, item)

    case_resolutions_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in account_resolution.get("case_resolutions") or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_id = str(item.get("case_id") or "").strip()
        if item_id:
            case_resolutions_by_id.setdefault(item_id, item)

    generation_tokens_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in account_resolution.get("generation_tokens") or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_id = str(item.get("case_id") or "").strip()
        if item_id:
            generation_tokens_by_id.setdefault(item_id, item)

    case_evidence_by_id: dict[str, list[dict[str, Any]]] = {}
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in rows.get("account_lifecycle_evidence") or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        case_evidence_by_id.setdefault(
            str(item.get("case_id") or "").strip(),
            [],
        ).append(item)
        evidence_id = str(item.get("evidence_id") or "").strip()
        if evidence_id:
            evidence_by_id[evidence_id] = item

    case_allocations_by_id: dict[str, list[dict[str, Any]]] = {}
    for raw_item in rows.get("account_lifecycle_allocations") or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        case_allocations_by_id.setdefault(
            str(item.get("case_id") or "").strip(),
            [],
        ).append(item)

    timing_policies_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in rows.get("account_lifecycle_timing_policies") or ():
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_id = str(item.get("case_id") or "").strip()
        if item_id:
            timing_policies_by_id.setdefault(item_id, item)

    position_lot_fields_by_id = {
        str(item.get("record_id") or "").strip(): dict(
            item.get("fields") or {}
        )
        for item in rows.get("account_position_lots") or ()
        if isinstance(item, Mapping)
        and str(item.get("record_id") or "").strip()
    }
    claim_by_binding = {
        (
            str(item.get("source_key") or "").strip(),
            str(item.get("owner_evidence_id") or "").strip(),
        ): dict(item)
        for item in rows.get("account_lifecycle_source_consumptions") or ()
        if isinstance(item, Mapping)
    }
    effective_void_event_ids = tuple(
        rows.get("effective_void_event_ids")
        or _effective_void_event_ids_from_rows(rows)
    )
    return _LifecycleAccountSnapshotIndex(
        rows=rows,
        account_resolution=account_resolution,
        lifecycle_cases_by_id=lifecycle_cases_by_id,
        case_resolutions_by_id=case_resolutions_by_id,
        generation_tokens_by_id=generation_tokens_by_id,
        case_evidence_by_id=case_evidence_by_id,
        case_allocations_by_id=case_allocations_by_id,
        timing_policies_by_id=timing_policies_by_id,
        position_lot_fields_by_id=position_lot_fields_by_id,
        evidence_by_id=evidence_by_id,
        claim_by_binding=claim_by_binding,
        effective_void_event_ids=effective_void_event_ids,
    )


def _materialize_lifecycle_case_from_account_snapshot_index(
    snapshot: _LifecycleAccountSnapshotIndex,
    *,
    case_id: str,
) -> dict[str, Any]:
    case_resolution = snapshot.case_resolutions_by_id.get(case_id)
    generation_token = snapshot.generation_tokens_by_id.get(case_id)
    if case_resolution is None or generation_token is None:
        raise ValueError(f"active lifecycle case not found: {case_id}")
    lifecycle_case = snapshot.lifecycle_cases_by_id.get(case_id)
    if lifecycle_case is None:
        raise ValueError(f"lifecycle case not found: {case_id}")
    validated_anchors, anchor_claims = _materialize_validated_anchors(
        lifecycle_case=lifecycle_case,
        case_resolution=case_resolution,
        evidence_by_id=snapshot.evidence_by_id,
        claim_by_binding=snapshot.claim_by_binding,
    )
    return {
        "schema_version": "lifecycle_case_coherent_facts.v1",
        **snapshot.rows,
        "lifecycle_case": dict(lifecycle_case),
        "case_evidence": list(
            snapshot.case_evidence_by_id.get(case_id, ())
        ),
        "case_allocations": list(
            snapshot.case_allocations_by_id.get(case_id, ())
        ),
        "timing_policy": (
            dict(snapshot.timing_policies_by_id[case_id])
            if case_id in snapshot.timing_policies_by_id
            else None
        ),
        "position_lot_fields_by_id": dict(
            snapshot.position_lot_fields_by_id
        ),
        "effective_void_event_ids": list(
            snapshot.effective_void_event_ids
        ),
        "account_lifecycle_resolution": snapshot.account_resolution,
        "case_resolution": dict(case_resolution),
        "generation_token": dict(generation_token),
        "validated_anchors": validated_anchors,
        "anchor_source_claims": anchor_claims,
    }


def _effective_void_event_ids_from_rows(rows: dict[str, Any]) -> list[str]:
    return sorted(
        {
            target
            for item in rows.get("trade_events") or []
            if isinstance(item, dict)
            for target in [valid_void_target_event_id(item)]
            if target
        }
    )


def _materialize_validated_anchors(
    *,
    lifecycle_case: dict[str, Any],
    case_resolution: dict[str, Any],
    evidence_by_id: Mapping[str, dict[str, Any]],
    claim_by_binding: Mapping[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    anchors: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    for raw_fact in case_resolution.get("anchor_facts") or []:
        if not isinstance(raw_fact, dict):
            continue
        fact = dict(raw_fact)
        owner_evidence_id = str(
            fact.get("source_owner_evidence_id") or ""
        ).strip()
        source_key = str(fact.get("source_key") or "").strip()
        original = dict(evidence_by_id.get(owner_evidence_id) or {})
        claim = dict(
            claim_by_binding.get((source_key, owner_evidence_id)) or {}
        )
        payload = (
            dict(claim.get("source_payload") or {})
            if isinstance(claim.get("source_payload"), dict)
            else {}
        )
        manifest = dict(fact.get("target_contracts_by_lot") or {})
        anchor = {
            **original,
            "evidence_id": owner_evidence_id,
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "source_event_id": source_key,
            "evidence_type": "option_zero_price_close",
            "account": payload.get("account"),
            "futu_account_id": payload.get("futu_account_id"),
            "symbol": payload.get("symbol"),
            "option_type": payload.get("option_type"),
            "position_side": payload.get("position_side"),
            "strike": payload.get("strike"),
            "expiration_ymd": payload.get("expiration_ymd"),
            "contracts": fact.get("quantity"),
            "price": payload.get("price"),
            "event_time_ms": fact.get("execution_time_ms"),
            "received_at_ms": fact.get("received_at_ms"),
            "order_id": payload.get("order_id"),
            "clearing_date": payload.get("clearing_date"),
            "target_contracts_by_lot": manifest,
            "target_lot_id": (
                next(iter(manifest)) if len(manifest) == 1 else None
            ),
            "bridge_evidence_id": fact.get("bridge_evidence_id"),
            "source_owner_case_id": fact.get("source_owner_case_id"),
            "source_owner_evidence_id": owner_evidence_id,
            "anchor_fact_id": fact.get("anchor_fact_id"),
        }
        anchors.append(anchor)
        if claim:
            claims.append(claim)
    anchors.sort(key=lambda item: str(item.get("anchor_fact_id") or ""))
    claims.sort(
        key=lambda item: (
            str(item.get("source_key") or ""),
            str(item.get("owner_evidence_id") or ""),
        )
    )
    return anchors, claims


def project_trade_event_log(events: list[dict[str, Any]]) -> Any:
    return project_stored_trade_events_to_position_lots(events)



def trade_event_economic_allocations(repo: Any) -> list[Any]:
    projection = project_trade_event_log(trade_event_log(repo))
    return list(projection.ledger_projection.allocations)

def trade_event_projection_preview(events: list[dict[str, Any]]) -> dict[str, Any]:
    projection = project_trade_event_log(events)
    return {
        "trade_event_count": int(len(events)),
        "position_lot_count": int(len(projection.lots)),
        "projection_diagnostic_count": int(len(projection.diagnostics)),
        "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
    }


def position_projection_verify_state(base: Path) -> dict[str, Any]:
    return load_projection_verify_state(base=base)


__all__ = [
    "AssignedStockEventLog",
    "assigned_stock_event_log",
    "PositionLotSnapshot",
    "RiskPositionView",
    "apply_position_ledger_runtime_config",
    "format_position_cash_secured",
    "format_position_money",
    "list_canonical_position_lot_snapshots",
    "list_position_lot_snapshots",
    "list_position_lot_sync_snapshots",
    "list_position_rows",
    "list_trade_lifecycle_cases",
    "list_trade_lifecycle_due_candidates",
    "list_trade_lifecycle_evidence",
    "latest_trade_lifecycle_settlement_evidence",
    "lifecycle_account_coherent_facts",
    "lifecycle_case_coherent_facts",
    "lifecycle_case_coherent_facts_from_account_snapshot",
    "lifecycle_case_coherent_facts_many_from_account_snapshot",
    "lifecycle_option_close_anchor_facts",
    "lifecycle_reconciliation_facts",
    "normalize_position_lot_fields",
    "normalize_position_lot_snapshot",
    "open_position_ledger",
    "open_position_ledger_from_data_config",
    "open_position_ledger_from_runtime_config",
    "position_lot_context_view",
    "position_lot_risk_view",
    "position_lot_snapshot",
    "position_projection_verify_state",
    "project_trade_event_log",
    "resolve_position_data_config_path",
    "resolve_position_lot_snapshots",
    "summarize_position_lot_shadow_status",
    "trade_event_economic_allocations",
    "trade_event_log",
    "trade_event_projection_preview",
]
