"""Explicit, preview-bound import of verifiable legacy decision exports."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from datetime import date
from typing import Any

from domain.domain.daily_decision_brief import normalize_persisted_daily_decision_brief
from domain.storage import paths
from src.application.account_config import build_account_runtime_plan, normalize_account_label
from src.application.candidate_snapshot_manifest import CandidateSnapshotManifestError, load_candidate_snapshot_bundle
from src.application.tick_run_workspace import AccountRunConfigError, account_run_config_path, load_published_account_run_config, read_account_run_state_bytes_safely
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, DecisionHistoryError, content_hash, history_path


def _target_state(store: DecisionHistoryStore, conn=None) -> str:
    if conn is None:
        if not store.path.exists():
            return content_hash({"decisions": [], "imports": []})
        with store.connect() as opened:
            return _target_state(store, opened)
    rows = conn.execute("SELECT id,account,market,market_date,run_id,revision,input_hash,payload_hash,scope_hash FROM decisions ORDER BY id").fetchall()
    imports = conn.execute("SELECT preview_hash,report_hash FROM history_imports ORDER BY id").fetchall()
    return content_hash({"decisions": [list(row) for row in rows], "imports": [list(row) for row in imports]})


def _verified_source(base: Path, path: Path, account: str, market: str) -> dict[str, Any]:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("source_symlink")
    match = re.fullmatch(r"daily_decision_brief\.([A-Z]+)\.(\d{4}-\d{2}-\d{2})\.r(\d+)\.json", path.name)
    if not match:
        raise ValueError("source_filename_invalid")
    encoded = path.read_bytes()
    raw = json.loads(encoded)
    normalized = normalize_persisted_daily_decision_brief(raw)
    if (normalized["account"], normalized["market"], normalized["market_trading_date"], normalized["revision"]) != (account, market, match[2], int(match[3])):
        raise ValueError("source_identity_mismatch")
    run_id = normalized["run_id"]
    run_bytes = read_account_run_state_bytes_safely(base=base, run_id=run_id, account=account, name=f"daily_decision_brief.{market}.json")
    if content_hash(json.loads(run_bytes)) != content_hash(raw):
        raise ValueError("run_copy_mismatch")
    bundle = load_candidate_snapshot_bundle(base=base, run_id=run_id, account=account)
    manifest = bundle["manifest"]
    cfg = load_published_account_run_config(base=base, run_id=run_id, account=account,
        state_path=account_run_config_path(base=base, run_id=run_id, account=account),
        account_config_sha256=manifest["account_config_sha256"])
    from src.application.futu_quote_routing import runtime_config_market
    if runtime_config_market(cfg) != market:
        raise ValueError("source_market_mismatch")
    binding = build_account_runtime_plan(cfg, account=account)
    scope = {"futu_account_id": binding.futu_account_id, "trade_env": binding.futu_trd_env}
    if not scope["futu_account_id"] or scope["trade_env"] not in {"REAL", "SIMULATE"}:
        raise ValueError("historical_scope_unproven")
    if raw.get("decision_scope") and raw["decision_scope"] != scope:
        raise ValueError("historical_scope_conflict")
    return {"payload": raw, "scope": scope, "source_hash": hashlib.sha256(encoded).hexdigest(),
            "binding_hash": content_hash(manifest)}


def preview_history_import(*, base: Path, account: str, market: str) -> dict[str, Any]:
    report, _ = _preview(base=Path(base), account=normalize_account_label(account), market=market.upper())
    return report


def _preview(*, base: Path, account: str, market: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if market not in {"US", "HK"}:
        raise ValueError("market must be US or HK")
    store = DecisionHistoryStore(history_path(base))
    before = _target_state(store)
    rows, accepted, reserved = [], [], {}
    for path in sorted(paths.account_state_dir(base, account).glob(f"daily_decision_brief.{market}.*.r*.json")):
        item = {"source": str(path.relative_to(base)), "status": "rejected"}
        match = re.fullmatch(r"daily_decision_brief\.[A-Z]+\.(\d{4}-\d{2}-\d{2})\.r(\d+)\.json", path.name)
        if match:
            try:
                day = date.fromisoformat(match[1]).isoformat()
                reserved[day] = max(reserved.get(day, -1), int(match[2]))
            except ValueError:
                pass
        try:
            verified = _verified_source(base, path, account, market)
            raw = verified["payload"]
            item.update(source_hash=verified["source_hash"], binding_hash=verified["binding_hash"], scope=verified["scope"],
                        run_id=raw["run_id"], market_date=raw["market_trading_date"], revision=raw["revision"], payload_hash=content_hash(raw))
            existing = store.get(account=account, market=market, market_date=raw["market_trading_date"], revision=raw["revision"]) if store.path.exists() else None
            same_run = store.get(account=account, market=market, run_id=raw["run_id"]) if store.path.exists() else None
            if existing or same_run:
                if not existing or not same_run or existing["id"] != same_run["id"] or existing["payload"] != raw or existing["scope"] != verified["scope"]:
                    raise ValueError("history_import_conflict")
                item["status"] = "already_imported"
            else:
                item["status"] = "ready"
                accepted.append(verified)
        except (OSError, ValueError, KeyError, TypeError, AccountRunConfigError, CandidateSnapshotManifestError) as exc:
            item["reason"] = str(exc) if re.fullmatch(r"[a-z_]{1,80}", str(exc)) else type(exc).__name__
            # Bind rejected files too: fixing a rejected input invalidates the old preview.
            if path.is_file() and not any(part.is_symlink() for part in (path, *path.parents)):
                item["source_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(item)
    report = {"account": account, "market": market, "target_state": before, "rows": rows,
              "ready_count": len(accepted), "reserved_revisions": reserved, "coverage": "verified_retained_sources_only"}
    report["preview_hash"] = content_hash(report)
    return report, accepted


def apply_history_import(*, base: Path, account: str, market: str, preview_hash: str) -> dict[str, Any]:
    base = Path(base)
    account, market = normalize_account_label(account), market.upper()
    store = DecisionHistoryStore(history_path(base))
    if store.path.exists():
        with store.connect() as conn:
            prior = next((report for report in store.import_reports(conn, account=account, market=market)
                          if report["preview_hash"] == preview_hash), None)
        if prior is not None:
            for item in prior["rows"]:
                if item["status"] not in {"ready", "already_imported"}:
                    continue
                saved = store.get(account=account, market=market, run_id=item["run_id"])
                if not saved or saved["payload_hash"] != item["payload_hash"] or saved["scope"] != item["scope"]:
                    raise DecisionHistoryError("history_import_readback_failed")
            return {**prior, "applied_count": 0, "readback": "passed", "already_applied": True}
    report, accepted = _preview(base=base, account=normalize_account_label(account), market=market.upper())
    if report["preview_hash"] != preview_hash:
        raise DecisionHistoryError("history_import_preview_changed")
    store = DecisionHistoryStore(history_path(base))
    needs_receipt = bool(report["rows"]) and (bool(accepted) or not store.path.exists())
    if not needs_receipt and report["rows"]:
        with store.connect() as conn:
            prior = store.import_reports(conn, account=report["account"], market=report["market"])
            def disposition(value):
                return {"reserved": value["reserved_revisions"],
                        "rejected": [row for row in value["rows"] if row["status"] == "rejected"]}
            needs_receipt = not prior or disposition(prior[-1]) != disposition(report)
    if needs_receipt:
        with store.connect(write=True) as conn:
            if _target_state(store, conn) != report["target_state"]:
                raise DecisionHistoryError("history_import_target_changed")
            for row in accepted:
                raw = row["payload"]
                # Preserve exact historical source, including compatible retired fields.
                store.insert(conn, source=raw, payload=raw, scope=row["scope"],
                             successful=raw.get("status") in {"ready", "degraded"} and raw.get("actionability") != "blocked")
            from src.infrastructure.decision_history_sqlite import canonical_json
            conn.execute("INSERT INTO history_imports(account,market,preview_hash,report_json,report_hash) VALUES(?,?,?,?,?)",
                         (report["account"], report["market"], preview_hash, canonical_json(report), content_hash(report)))
        with store.connect() as conn:
            saved = store.import_reports(conn, account=report["account"], market=report["market"])
            if not saved or saved[-1] != report:
                raise DecisionHistoryError("history_import_readback_failed")
        for row in accepted:
            raw = row["payload"]
            readback = store.get(account=raw["account"], market=raw["market"], run_id=raw["run_id"])
            if not readback or readback["payload"] != raw or readback["scope"] != row["scope"]:
                raise DecisionHistoryError("history_import_readback_failed")
    return {**report, "applied_count": len(accepted), "readback": "passed" if needs_receipt else "no_changes"}
