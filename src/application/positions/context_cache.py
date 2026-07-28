from __future__ import annotations

from pathlib import Path
from typing import Any


def invalidate_option_positions_context_cache(*, runtime_root: str | Path, account: str | None = None) -> dict[str, Any]:
    root = Path(runtime_root).expanduser().resolve()
    targets = [
        root / "output_shared" / "state" / "option_positions_context.json",
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


def invalidate_option_positions_context_cache_for_repo(
    repo: Any,
    *,
    account: str | None = None,
) -> dict[str, Any]:
    candidate = getattr(repo, "primary_repo", repo)
    ledger_store = getattr(candidate, "ledger_store", None)
    runtime_root = getattr(ledger_store, "runtime_root", None)
    db_path = getattr(candidate, "db_path", None)
    if runtime_root in (None, "") and db_path not in (None, ""):
        resolved_db = Path(db_path).expanduser().resolve()
        if (
            resolved_db.parent.name == "state"
            and resolved_db.parent.parent.name == "output_shared"
        ):
            runtime_root = resolved_db.parent.parent.parent
        else:
            runtime_root = resolved_db.parent
    if runtime_root in (None, ""):
        data_config_path = getattr(candidate, "data_config_path", None)
        if data_config_path not in (None, ""):
            runtime_root = Path(data_config_path).expanduser().resolve().parent
    if runtime_root in (None, ""):
        return {
            "runtime_root": None,
            "account": str(account or "").strip().lower() or None,
            "invalidated_paths": [],
            "missing_paths": [],
            "errors": [],
            "ok": True,
            "status": "not_applicable",
        }
    return invalidate_option_positions_context_cache(
        runtime_root=runtime_root,
        account=account,
    )
