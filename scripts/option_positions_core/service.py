from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.feishu_bitable import (
    bitable_create_record,
    bitable_list_records,
    bitable_update_record,
    get_tenant_access_token,
)
from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_expire_auto_close_patch,
    build_open_fields,
    effective_expiration,
    exp_ms_to_datetime,
)


@dataclass(frozen=True)
class OptionPositionsTableRef:
    app_id: str
    app_secret: str
    app_token: str
    table_id: str


def load_table_ref(pm_config: Path) -> OptionPositionsTableRef:
    cfg = json.loads(pm_config.read_text(encoding="utf-8"))
    feishu_cfg = cfg.get("feishu", {}) or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")
    ref = (feishu_cfg.get("tables", {}) or {}).get("option_positions")
    if not (app_id and app_secret and ref and "/" in ref):
        raise SystemExit("pm config missing feishu app_id/app_secret/option_positions")
    app_token, table_id = ref.split("/", 1)
    return OptionPositionsTableRef(str(app_id), str(app_secret), str(app_token), str(table_id))


class OptionPositionsRepository:
    def __init__(self, table_ref: OptionPositionsTableRef):
        self.table_ref = table_ref
        self._token: str | None = None

    @property
    def token(self) -> str:
        if not self._token:
            self._token = get_tenant_access_token(self.table_ref.app_id, self.table_ref.app_secret)
        return self._token

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return bitable_list_records(self.token, self.table_ref.app_token, self.table_ref.table_id, page_size=page_size)

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        return bitable_create_record(self.token, self.table_ref.app_token, self.table_ref.table_id, fields)

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return bitable_update_record(self.token, self.table_ref.app_token, self.table_ref.table_id, record_id, fields)

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        for item in self.list_records(page_size=500):
            if str(item.get("record_id") or item.get("id") or "") == str(record_id):
                fields = item.get("fields") or {}
                if isinstance(fields, dict):
                    return fields
        raise ValueError(f"record not found: {record_id}")


def open_position(repo: OptionPositionsRepository, command: OpenPositionCommand) -> dict[str, Any]:
    fields = build_open_fields(command)
    return repo.create_record(fields)


def buy_to_close_position(
    repo: OptionPositionsRepository,
    *,
    record_id: str,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_buy_to_close",
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    fields = repo.get_record_fields(record_id)
    patch = build_buy_to_close_patch(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    )
    return repo.update_record(record_id, patch)


def build_expired_close_decisions(
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    as_of_dt = exp_ms_to_datetime(as_of_ms)
    if as_of_dt is None:
        raise ValueError("invalid as_of_ms")
    cutoff_ms = int((as_of_dt.timestamp() - int(grace_days) * 86400) * 1000)

    for item in positions:
        fields = dict(item)
        record_id = str(fields.get("record_id") or "").strip()
        position_id = str(fields.get("position_id") or "").strip() or "(no position_id)"
        if not record_id:
            decisions.append(
                {
                    "record_id": "",
                    "position_id": position_id,
                    "expiration_ms": None,
                    "effective_exp_source": "none",
                    "should_close": False,
                    "reason": "missing record_id",
                    "patch": None,
                }
            )
            continue

        exp_ms, exp_source = effective_expiration(fields)
        if exp_ms is None:
            decisions.append(
                {
                    "record_id": record_id,
                    "position_id": position_id,
                    "expiration_ms": None,
                    "effective_exp_source": "none",
                    "should_close": False,
                    "reason": "missing expiration (field and note)",
                    "patch": None,
                }
            )
            continue

        exp_dt = exp_ms_to_datetime(exp_ms)
        should_close = int(exp_ms) <= cutoff_ms
        reason = (
            f"expired: exp={exp_dt.date().isoformat() if exp_dt else exp_ms} "
            f"grace_days={grace_days} as_of={as_of_dt.date().isoformat()}"
        )
        patch = (
            build_expire_auto_close_patch(
                fields,
                as_of_ms=as_of_ms,
                close_reason="expired",
                exp_source=exp_source,
                grace_days=grace_days,
            )
            if should_close
            else None
        )
        decisions.append(
            {
                "record_id": record_id,
                "position_id": position_id,
                "expiration_ms": int(exp_ms),
                "effective_exp_source": exp_source,
                "should_close": should_close,
                "reason": reason,
                "patch": patch,
            }
        )
    return decisions


def auto_close_expired_positions(
    repo: OptionPositionsRepository,
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
    max_close: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    decisions = build_expired_close_decisions(positions, as_of_ms=as_of_ms, grace_days=grace_days)
    to_close = [d for d in decisions if bool(d.get("should_close")) and d.get("record_id")]
    errors: list[str] = []
    applied: list[dict[str, Any]] = []
    if len(to_close) > int(max_close):
        return decisions, applied, [f"too many to close: {len(to_close)} > max_close={max_close}; abort"]
    for decision in to_close:
        patch = decision.get("patch")
        if not isinstance(patch, dict):
            continue
        try:
            repo.update_record(str(decision["record_id"]), patch)
            applied.append(decision)
        except Exception as exc:
            errors.append(f"{decision.get('record_id')} {decision.get('position_id')}: {exc}")
    return decisions, applied, errors
