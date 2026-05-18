from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class _Unset:
    pass


_UNSET = _Unset()
_PatchValue = int | str | None | _Unset

POSITION_LOT_SYNC_METADATA_FIELDS = (
    "feishu_record_id",
    "feishu_sync_hash",
    "feishu_last_synced_at_ms",
)


@dataclass(frozen=True)
class PositionLotSyncMetadataPatch:
    feishu_record_id: _PatchValue = _UNSET
    feishu_sync_hash: _PatchValue = _UNSET
    feishu_last_synced_at_ms: _PatchValue = _UNSET

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in POSITION_LOT_SYNC_METADATA_FIELDS:
            value = getattr(self, key)
            if value is _UNSET:
                continue
            payload[key] = value
        return payload

    def has(self, key: str) -> bool:
        if key not in POSITION_LOT_SYNC_METADATA_FIELDS:
            raise KeyError(f"unsupported position lot sync metadata field: {key}")
        return getattr(self, key) is not _UNSET

    def value(self, key: str) -> int | str | None:
        if key not in POSITION_LOT_SYNC_METADATA_FIELDS:
            raise KeyError(f"unsupported position lot sync metadata field: {key}")
        value = getattr(self, key)
        if value is _UNSET:
            raise KeyError(f"position lot sync metadata field is unset: {key}")
        return value


def build_position_lot_sync_metadata_patch(
    *,
    feishu_record_id: str,
    sync_hash: str,
    synced_at_ms: int,
) -> PositionLotSyncMetadataPatch:
    clean_record_id = str(feishu_record_id or "").strip()
    clean_hash = str(sync_hash or "").strip()
    if not clean_record_id:
        raise ValueError("feishu_record_id is required")
    if not clean_hash:
        raise ValueError("feishu_sync_hash is required")
    return PositionLotSyncMetadataPatch(
        feishu_record_id=clean_record_id,
        feishu_sync_hash=clean_hash,
        feishu_last_synced_at_ms=int(synced_at_ms),
    )


def decode_position_lot_sync_metadata_patch(payload: Any) -> PositionLotSyncMetadataPatch:
    if isinstance(payload, PositionLotSyncMetadataPatch):
        return payload
    if not isinstance(payload, dict):
        raise TypeError("position lot sync metadata patch must be a dict or PositionLotSyncMetadataPatch")
    unsupported = sorted(str(key) for key in payload if str(key) not in POSITION_LOT_SYNC_METADATA_FIELDS)
    if unsupported:
        raise ValueError(f"position lot sync metadata patch contains unsupported fields: {', '.join(unsupported)}")
    return PositionLotSyncMetadataPatch(
        feishu_record_id=(
            _normalize_optional_str(payload.get("feishu_record_id")) if "feishu_record_id" in payload else _UNSET
        ),
        feishu_sync_hash=(
            _normalize_optional_str(payload.get("feishu_sync_hash")) if "feishu_sync_hash" in payload else _UNSET
        ),
        feishu_last_synced_at_ms=(
            _normalize_optional_int(payload.get("feishu_last_synced_at_ms"))
            if "feishu_last_synced_at_ms" in payload
            else _UNSET
        ),
    )


def apply_position_lot_sync_metadata_patch(
    fields: dict[str, Any],
    patch: PositionLotSyncMetadataPatch,
) -> dict[str, Any]:
    if not isinstance(patch, PositionLotSyncMetadataPatch):
        raise TypeError("position lot sync metadata write requires PositionLotSyncMetadataPatch")
    patched = dict(fields)
    for key in POSITION_LOT_SYNC_METADATA_FIELDS:
        if not patch.has(key):
            continue
        value = patch.value(key)
        if value in (None, ""):
            patched.pop(key, None)
        else:
            patched[key] = value
    return patched


def _normalize_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _normalize_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid feishu_last_synced_at_ms: {value!r}") from exc


__all__ = [
    "POSITION_LOT_SYNC_METADATA_FIELDS",
    "PositionLotSyncMetadataPatch",
    "apply_position_lot_sync_metadata_patch",
    "build_position_lot_sync_metadata_patch",
    "decode_position_lot_sync_metadata_patch",
]
