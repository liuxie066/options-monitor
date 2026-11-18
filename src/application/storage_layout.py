"""Canonical, read-only path planning for OM storage volumes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import re
from typing import Any


STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
_PACKAGE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{6}Z)_([A-Za-z0-9._-]{1,80})_(full|pre-upgrade)\.tar\.gz\.cms")


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise ValueError("storage name must use 1–80 ASCII letters, digits, dots, underscores or hyphens")
    return value


def _stamp(value: str) -> str:
    parsed = datetime.strptime(value, STAMP_FORMAT)
    if parsed.strftime(STAMP_FORMAT) != value:
        raise ValueError("storage timestamp must use YYYY-MM-DDTHHMMSSZ")
    return value


def parse_backup_filename(filename: str) -> tuple[str, str, str]:
    match = _PACKAGE.fullmatch(filename)
    if match is None:
        raise ValueError("backup filename must use <UTC time>_<series>_<purpose>.tar.gz.cms")
    timestamp, series, purpose = match.groups()
    return _stamp(timestamp), _component(series), purpose


_VOLUME_LAYOUT_ROOT = Path("OM")


def plan_backup_layout(
    *, series: str, purpose: str, timestamp: str | None = None,
    restored_at: str | None = None, retired_at: str | None = None,
    plan_sha256: str | None = None,
) -> dict[str, Any]:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
    filename = f"{timestamp}_{series}_{purpose}.tar.gz.cms"
    parse_backup_filename(filename)
    base = _VOLUME_LAYOUT_ROOT / "backups" / "prod" / series
    stem = filename.removesuffix(".tar.gz.cms")
    packages, receipts, retirements = (base / name for name in ("packages", "receipts", "retirements"))
    restored = None if restored_at is None else receipts / f"{stem}.restored.{_stamp(restored_at)}.json"
    retired = None
    if retired_at is not None or plan_sha256 is not None:
        if retired_at is None or plan_sha256 is None or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
            raise ValueError("retirement naming requires a UTC time and full SHA-256 plan digest")
        retired = retirements / f"{_stamp(retired_at)}_{plan_sha256[:12]}.retire.jsonl"
    publication = receipts / f"{stem}.published.json"
    entry = {"publication": str(publication.relative_to(base))}
    if restored is not None:
        entry["verification"] = str(restored.relative_to(base))
    return {
        "series": series, "filename": filename, "package": str(packages / filename),
        "target": str(packages), "publication_receipt": str(publication),
        "verification_receipt": None if restored is None else str(restored),
        "retirement_receipt": None if retired is None else str(retired),
        "retirements": str(retirements), "rotation_inventory": str(base / "rotation.json"),
        "rotation_entry": entry,
    }


def plan_archive_layout(
    *, kind: str, when: str, batch_id: str,
    market: str | None = None, dataset: str | None = None,
) -> dict[str, str]:
    root = _VOLUME_LAYOUT_ROOT
    batch_id = _component(batch_id)
    if kind == "research":
        if market not in {"us", "hk"} or dataset is None:
            raise ValueError("research archive requires market us or hk and a dataset name")
        if date.fromisoformat(when).isoformat() != when:
            raise ValueError("research archive requires a YYYY-MM-DD market trading date")
        target = root / "archives" / kind / market / when[:4] / f"{when}_{market}_{_component(dataset)}_{batch_id}"
    elif kind in {"incidents", "maintenance"}:
        if market is not None or dataset is not None:
            raise ValueError("market and dataset apply only to research archives")
        target = root / "archives" / kind / when[:4] / f"{_stamp(when)}_{batch_id}"
    else:
        raise ValueError("unknown storage archive kind")
    return {"archive_root": str(target), "legacy_inbox": str(root / "inbox" / "legacy")}
