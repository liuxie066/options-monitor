"""Best-effort last-run observation for reports that are registered but not migrated."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.application.reports.registry import LegacyProbe, ReportDefinition


_TAIL_BYTES = 256 * 1024


def observe_legacy_last_run(
    definition: ReportDefinition,
    *,
    runtime_root: str | Path,
    home: str | Path | None = None,
) -> dict[str, Any] | None:
    probe = definition.legacy_probe
    if probe is None:
        return None
    home_path = Path(home).expanduser() if home is not None else Path.home()
    if probe.kind == "json_object":
        return _json_object_probe(probe, home=home_path)
    if probe.kind == "jsonl_tail":
        return _jsonl_tail_probe(probe, home=home_path)
    if probe.kind == "daily_brief":
        return _daily_brief_probe(Path(runtime_root))
    if probe.kind == "close_advice":
        return _close_advice_probe(Path(runtime_root))
    return None


def _json_object_probe(probe: LegacyProbe, *, home: Path) -> dict[str, Any] | None:
    path = _expand(probe.path, home)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return _observation(
            at_utc=None,
            status="unreadable",
            source=str(path),
            detail=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict):
        return _observation(at_utc=None, status="unreadable", source=str(path), detail="manifest is not an object")
    return _observation(
        at_utc=_first_text(payload, probe.stamp_fields),
        status=_text(payload.get(probe.status_field)),
        source=str(path),
        detail=_text(payload.get("mode")) or _text(payload.get("target")),
    )


def _jsonl_tail_probe(probe: LegacyProbe, *, home: Path) -> dict[str, Any] | None:
    path = _expand(probe.path, home)
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _TAIL_BYTES))
            chunk = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _observation(at_utc=None, status="unreadable", source=str(path), detail=f"{type(exc).__name__}: {exc}")
    for raw_line in reversed(chunk.splitlines()):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        detail_parts = [value for value in (_text(payload.get("mode")), _text(payload.get("main_sha"))) if value]
        return _observation(
            at_utc=_first_text(payload, probe.stamp_fields),
            status=_text(payload.get(probe.status_field)),
            source=str(path),
            detail="; ".join(detail_parts) or None,
        )
    return None


def _daily_brief_probe(runtime_root: Path) -> dict[str, Any] | None:
    best: tuple[datetime, dict[str, Any], Path] | None = None
    for path in runtime_root.glob("output_accounts/*/state/daily_decision_brief.*.current.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        generated = _parse_iso(payload.get("generated_at_utc")) or _mtime(path)
        if generated is None:
            continue
        if best is None or generated > best[0]:
            best = (generated, payload, path)
    if best is None:
        return None
    generated, payload, path = best
    detail = " ".join(
        value
        for value in (
            f"market={payload.get('market')}",
            f"account={payload.get('account')}",
            f"status={payload.get('status')}" if payload.get("status") else "",
        )
        if value
    )
    return _observation(
        at_utc=_iso(generated),
        status=_text(payload.get("status")),
        source=str(path),
        detail=detail or None,
    )


def _close_advice_probe(runtime_root: Path) -> dict[str, Any] | None:
    best: tuple[datetime, dict[str, Any], Path] | None = None
    for path in runtime_root.glob("output_accounts/*/reports/close_advice.manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        generated = _parse_iso(payload.get("generated_at_utc")) or _mtime(path)
        if generated is None:
            continue
        if best is None or generated > best[0]:
            best = (generated, payload, path)
    if best is None:
        return None
    generated, payload, path = best
    detail = " ".join(
        value
        for value in (
            f"rows={payload.get('row_count')}" if payload.get("row_count") is not None else "",
            f"accounts={','.join(payload.get('accounts') or [])}" if payload.get("accounts") else "",
        )
        if value
    )
    return _observation(
        at_utc=_iso(generated),
        status=_text(payload.get("status")),
        source=str(path),
        detail=detail or None,
    )


def _observation(
    *,
    at_utc: str | None,
    status: str | None,
    source: str,
    detail: str | None,
) -> dict[str, Any]:
    return {
        "at_utc": at_utc,
        "status": status,
        "source": source,
        "detail": detail,
    }


def _expand(path: str | None, home: Path) -> Path | None:
    if not path:
        return None
    if path.startswith("~/"):
        return home / path[2:]
    return Path(path).expanduser()


def _first_text(payload: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = _text(payload.get(field))
        if value:
            return value
    return None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_iso(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["observe_legacy_last_run"]
