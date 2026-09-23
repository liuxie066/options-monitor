"""Read-only inventory of the 2026-09-23 incident cleanup candidates."""
from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SNAPSHOTS = (
    "last-three-terminal-repair-20260914-v1", "intake-evidence-repair-20260914-v2",
    "identity-evidence-repair-20260914-v1", "cnooc-repair-20260914-v2",
    "last-stock-production-repair-20260914-v1", "intake-evidence-repair-20260914-v1",
    "cnooc-repair-20260914", "last-stock-repair-20260914-v1",
    "last-stock-repair-20260914-v2", "wheel-fee-repair-20260914-v3",
    "wheel-fee-repair-20260914-v2", "sy-wheel-recovery-357-20260914",
)
TMP_NAMES = (
    "om-legacy-association-rehearsal", "om-hk-timing-proof", "om-hk-data-rehearsal",
    "om-v362-control.zocW8R", "om-pdd-order-repair", "om-v361-control.gTCvAT",
    "om-wheel-assignment-recovery-20260914", "om-readonly-20260919T065001.sqlite3",
)
BACKUP_PATTERNS = (
    "option_positions.sqlite3.before-cash-rekey-*.bak",
    "option_positions.sqlite3.before-realized-pnl-repair-*.bak",
    "option_positions.before-0700-terminal-time-correction-*.sqlite3",
    "option_positions.before-sy-0700-450p-*.sqlite3",
    "option_positions.sqlite3.bak.manual-reconcile-*",
    "inbound_control.sqlite3.pre-bot.sqlite3",
)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _size(path: Path) -> tuple[int, int, bool]:
    logical = allocated = 0
    unsafe = False
    paths = [path]
    if path.is_dir() and not path.is_symlink():
        for parent, dirs, files in os.walk(path, followlinks=False):
            paths.extend(Path(parent) / name for name in dirs + files)
    for item in paths:
        try:
            info = item.lstat()
        except OSError:
            unsafe = True
            continue
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            unsafe = True
        if stat.S_ISREG(info.st_mode):
            logical += info.st_size
            allocated += info.st_blocks * 512
    return logical, allocated, unsafe


def _open_references(paths: list[Path], proc_root: Path) -> dict[Path, list[str] | None]:
    out: dict[Path, list[str] | None] = {path: [] for path in paths}
    if not proc_root.is_dir():
        return {path: None for path in paths}
    try:
        for process in proc_root.iterdir():
            if not process.name.isdigit():
                continue
            fd_dir = process / "fd"
            try:
                fds = list(fd_dir.iterdir())
            except (PermissionError, FileNotFoundError):
                return {path: None for path in paths}
            for fd in fds:
                try:
                    raw = os.readlink(fd).removesuffix(" (deleted)")
                except (PermissionError, FileNotFoundError, OSError):
                    continue
                opened = Path(raw)
                for path in paths:
                    if _inside(opened, path):
                        out[path].append(f"{process.name}/{fd.name}")
    except OSError:
        return {path: None for path in paths}
    return out


def _text_references(paths: list[Path], roots: tuple[Path, ...]) -> dict[Path, list[str] | None]:
    out: dict[Path, list[str] | None] = {path: [] for path in paths}
    existing = [root for root in roots if root.is_dir()]
    if len(existing) != len(roots):
        return {path: None for path in paths}
    patterns = [str(path) for path in paths] + [path.name for path in paths]
    args = ["rg", "-l", "-F"]
    for pattern in patterns:
        args.extend(("-e", pattern))
    for glob in ("*.service", "*.timer", "*.sh", "*.py", "*.json", "*.yaml", "*.yml", "*.toml"):
        args.extend(("--glob", glob))
    try:
        result = subprocess.run([*args, *map(str, roots)], capture_output=True, text=True,
                                timeout=30, check=False)
        if result.returncode not in (0, 1):
            return {path: None for path in paths}
        for name in result.stdout.splitlines():
            source = Path(name)
            try:
                if source.stat().st_size > 2_000_000:
                    return {path: None for path in paths}
                body = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                return {path: None for path in paths}
            for path in paths:
                if str(path) in body or path.name in body:
                    out[path].append(str(source))
    except (OSError, subprocess.TimeoutExpired):
        return {path: None for path in paths}
    return out


def _symlink_references(paths: list[Path], roots: tuple[Path, ...]) -> dict[Path, list[str] | None]:
    out: dict[Path, list[str] | None] = {path: [] for path in paths}
    if any(not root.is_dir() for root in roots):
        return {path: None for path in paths}
    seen = 0
    try:
        for root in roots:
            for parent, dirs, files in os.walk(root, followlinks=False):
                for name in dirs + files:
                    seen += 1
                    if seen > 100_000:
                        return {path: None for path in paths}
                    link = Path(parent) / name
                    if not link.is_symlink():
                        continue
                    target = link.resolve()
                    for path in paths:
                        if _inside(target, path):
                            out[path].append(str(link))
    except OSError:
        return {path: None for path in paths}
    return out


def preview_incident_cleanup(*, runtime_root: Path, tmp_root: Path = Path("/tmp"),
                             apps_root: Path = Path.home() / "apps",
                             unit_root: Path = Path("/etc/systemd/system"),
                             opend_log_root: Path = Path.home() / ".com.futunn.FutuOpenD" / "Log",
                             proc_root: Path = Path("/proc"),
                             now: datetime | None = None) -> dict[str, Any]:
    """No deletion path exists. Unknown reference checks always protect a candidate."""
    runtime = Path(runtime_root).absolute()
    state = runtime / "output_shared" / "state"
    candidates: list[tuple[str, Path]] = [("repair_snapshot", state / name) for name in SNAPSHOTS]
    candidates += [("tmp", tmp_root / name) for name in TMP_NAMES]
    candidates.append(("migration_backups", state / "backups"))
    for pattern in BACKUP_PATTERNS[:2]:
        candidates.extend(("ledger_backup", path) for path in runtime.glob(pattern))
    for pattern in BACKUP_PATTERNS[2:]:
        candidates.extend(("state_backup", path) for path in state.glob(pattern))
    clock = now or datetime.now(timezone.utc)
    if opend_log_root.is_dir() and not opend_log_root.is_symlink():
        for path in opend_log_root.iterdir():
            if path.suffix in {".ftlog", ".logs"} and path.is_file() and not path.is_symlink():
                candidates.append(("opend_log", path))
    paths = [path.absolute() for _, path in candidates]
    opened = _open_references(paths, proc_root)
    references = _text_references(paths, (apps_root, unit_root))
    symlinks = _symlink_references(paths, (apps_root, unit_root, runtime, tmp_root))
    latest_backup = max((path for path in (state / "backups").glob("*") if path.is_file()),
                        key=lambda path: path.stat().st_mtime, default=None) if (state / "backups").is_dir() else None
    active_ledgers = (state / "option_positions.sqlite3", runtime / "option_positions.sqlite3")
    items = []
    for kind, source in candidates:
        path = source.absolute()
        exists = path.exists() or path.is_symlink()
        info = path.lstat() if exists else None
        logical, allocated, unsafe = _size(path) if exists else (0, 0, False)
        reasons = []
        if not exists:
            reasons.append("missing")
        if unsafe or path.is_symlink():
            reasons.append("symlink_or_hardlink")
        if latest_backup and _inside(latest_backup, path):
            reasons.append("latest_migration_backup")
        if any(_inside(ledger, path) for ledger in active_ledgers):
            reasons.append("active_ledger")
        if kind == "opend_log" and info and datetime.fromtimestamp(info.st_mtime, timezone.utc) > clock - timedelta(days=7):
            reasons.append("within_7_days")
        if opened[path] is None:
            reasons.append("open_file_check_unavailable")
        elif opened[path]:
            reasons.append("open_file_reference")
        if references[path] is None:
            reasons.append("manifest_check_unavailable")
        elif references[path]:
            reasons.append("manifest_reference")
        if symlinks[path] is None:
            reasons.append("symlink_check_unavailable")
        elif symlinks[path]:
            reasons.append("symlink_reference")
        items.append({"kind": kind, "path": str(path), "realpath": str(path.resolve()) if exists else None,
            "exists": exists, "type": "directory" if path.is_dir() and not path.is_symlink() else "file" if path.is_file() else "missing",
            "logical_size": logical, "allocated_bytes": allocated,
            "mtime_utc": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat() if info else None,
            "reference_checks": {"open_files": opened[path], "manifests": references[path], "symlinks": symlinks[path]},
            "protected": bool(reasons), "reason": reasons})
    return {"schema_version": "incident_cleanup_preview.v1", "mode": "preview_only",
            "target_host": os.uname().nodename, "items": items,
            "estimated_releasable_bytes": sum(item["allocated_bytes"] for item in items if not item["protected"])}
