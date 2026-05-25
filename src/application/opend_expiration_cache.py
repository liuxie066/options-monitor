from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.application.expiration_normalization import normalize_expiration_ymd
from src.infrastructure.io_utils import atomic_write_json


def option_expiration_cache_root(base_dir: Path) -> Path:
    return Path(base_dir) / "cache" / "opend_option_expirations"


def option_expiration_cache_path(base_dir: Path, underlier_code: str, asof_date: str) -> Path:
    safe_underlier = str(underlier_code or "").replace(".", "_")
    safe_date = _safe_asof_date(asof_date)
    return option_expiration_cache_root(base_dir) / safe_underlier / f"{safe_date}.json"


def load_option_expiration_cache(path: Path, *, asof_date: str) -> list[str] | None:
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if str(obj.get("asof_date") or "") != str(asof_date):
        return None
    if str(obj.get("status") or "").lower() != "ok":
        return None
    expirations = obj.get("expirations")
    if not isinstance(expirations, list):
        return None
    return _normalize_expirations(expirations)


def save_option_expiration_cache(
    path: Path,
    *,
    asof_date: str,
    underlier_code: str,
    expirations: list[str],
) -> None:
    normalized = _normalize_expirations(expirations)
    if not normalized:
        return
    atomic_write_json(
        Path(path),
        {
            "asof_date": str(asof_date),
            "underlier_code": str(underlier_code or ""),
            "status": "ok",
            "expirations": normalized,
        },
        default=str,
    )


def _normalize_expirations(values: list[Any]) -> list[str]:
    return sorted(
        {
            exp
            for exp in (normalize_expiration_ymd(value) for value in values)
            if exp
        }
    )


def _safe_asof_date(value: str) -> str:
    raw = str(value or "").strip()
    return raw[:10] if raw else "unknown"
