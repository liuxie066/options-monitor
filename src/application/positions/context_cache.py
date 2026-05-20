from __future__ import annotations

from pathlib import Path
from typing import Any


def invalidate_option_positions_context_cache(*, runtime_root: str | Path, account: str | None = None) -> dict[str, Any]:
    root = Path(runtime_root).expanduser().resolve()
    targets = [
        root / "output" / "state" / "option_positions_context.json",
        root / "output_shared" / "state" / "option_positions_context.shared.json",
    ]
    account_name = str(account or "").strip().lower()
    if account_name:
        targets.append(root / "output_accounts" / account_name / "state" / "option_positions_context.json")
    else:
        accounts_root = root / "output_accounts"
        if accounts_root.exists():
            targets.extend(accounts_root.glob("*/state/option_positions_context.json"))

    invalidated: list[str] = []
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in targets:
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if not resolved.exists():
            missing.append(key)
            continue
        try:
            resolved.unlink()
            invalidated.append(key)
        except Exception as exc:
            errors.append({"path": key, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "runtime_root": str(root),
        "account": account_name or None,
        "invalidated_paths": invalidated,
        "missing_paths": missing,
        "errors": errors,
        "ok": not errors,
    }
