"""Account-scoped, bounded reads of formally sealed candidate run evidence."""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Callable

from src.application.agent_tools.project_reader import ProjectReaderError, list_names, read_bytes
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    validate_candidate_snapshot_bundle_bytes,
    validate_candidate_snapshot_manifest,
)
from src.application.runtime_paths import RuntimeRootResolution

_MAX_BYTES = 8 * 1024 * 1024
_MANIFESTS = (CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE, CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE)
_DIAGNOSTIC_REASONS = {
    "account_config_hash_mismatch": "该次账户配置内容与权威哈希不一致。",
    "prepared_option_context_integrity_failed": "该次预备期权上下文未通过完整性校验。",
}


def _check(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise ProjectReaderError("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise ProjectReaderError("time_deadline")


def _scope(root: RuntimeRootResolution, accounts: list[str] | tuple[str, ...],
           account: str | None, market: str) -> list[str]:
    if root.source not in {"argument", "env:OM_RUNTIME_ROOT"}:
        raise ProjectReaderError("runtime_root_unavailable")
    allowed = sorted(set(accounts))
    if not allowed or any(not re.fullmatch(r"[a-z0-9_-]+", item) for item in allowed):
        raise ProjectReaderError("scope_unavailable")
    if market.upper() not in {"US", "HK"}:
        raise ProjectReaderError("scope_unavailable")
    if account is not None and account not in allowed:
        raise ProjectReaderError("permission_denied")
    return [account] if account is not None else allowed


def load_run_bundle(
    *, runtime_root: RuntimeRootResolution, run_id: str, account: str, market: str,
    authorized_accounts: list[str] | tuple[str, ...], deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None, max_bytes: int = _MAX_BYTES,
    _budget: dict[str, int] | None = None, _reader_root: int | None = None,
) -> dict[str, Any]:
    """Return validated original bytes only for index/owner resources, never dependencies."""
    _scope(runtime_root, authorized_accounts, account, market)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id):
        raise ProjectReaderError("invalid_run_id")
    budget = _budget if _budget is not None else {"bytes": min(max_bytes, _MAX_BYTES), "metadata": 40000}
    check = lambda: _check(deadline_monotonic, cancelled)
    options = {"deadline_monotonic": deadline_monotonic, "cancelled": cancelled}
    reader_root = runtime_root.runtime_root if _reader_root is None else _reader_root
    prefix = f"output_runs/{run_id}/accounts/{account}"
    read_cache: dict[str, bytes] = {}
    used_before = budget["bytes"]

    def read(name: str) -> bytes:
        check()
        if name not in read_cache:
            if budget["bytes"] <= 0:
                raise ProjectReaderError("bundle_too_large")
            raw = read_bytes(reader_root, name,
                             max_bytes=min(1024 * 1024, budget["bytes"]), **options)
            budget["bytes"] -= len(raw)
            read_cache[name] = raw
        return read_cache[name]

    def names(name: str) -> tuple[list[str], dict[str, Any]]:
        check()
        if budget["metadata"] <= 0:
            raise ProjectReaderError("scope_too_broad")
        result, identity = list_names(reader_root, name,
                                      max_entries=min(10000, budget["metadata"]), **options)
        budget["metadata"] -= len(result)
        return result, identity

    try:
        account_names, _ = names(prefix)
        state_names, _ = names(prefix + "/state")
        present = [name for name in _MANIFESTS if name in state_names]
        if len(present) != 1:
            raise ProjectReaderError("bundle_unavailable" if not present else "artifact_version_mismatch")
        manifest_name = present[0]
        files = {"state/" + manifest_name: read(prefix + "/state/" + manifest_name)}
        manifest = json.loads(files["state/" + manifest_name])
        validate_candidate_snapshot_manifest(manifest, expected_run_id=run_id, expected_account=account)
        if not manifest["markets"]:
            raise ProjectReaderError("market_unverifiable")
        if set(manifest["markets"]) - {market.upper()}:
            raise ProjectReaderError("permission_denied")
        index_name = manifest["status_index"]["relpath"]
        resource_names = [index_name] + [row["relpath"] for row in manifest["owner_snapshots"]]
        for name in resource_names:
            files[name] = read(prefix + "/" + name)
        index = json.loads(files[index_name])
        for row in index.get("items", []):
            name = row.get("source_status_path")
            if name:
                # Index validators enforce canonical status filenames; path trust remains with FD reader.
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ProjectReaderError("permission_denied")
                files[name] = read(prefix + "/" + name)
        dependencies = {}
        for row in manifest["owner_snapshots"]:
            snapshot = json.loads(files[row["relpath"]])
            for dependency in snapshot.get("dependencies", []):
                name = dependency.get("relpath")
                if not name:
                    continue
                # Dependency contents are hash inputs only, never model-visible resources.
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts or not (
                    name.startswith(prefix + "/") or name.startswith(f"output_runs/{run_id}/required_data/")
                )):
                    raise ProjectReaderError("permission_denied")
                dependencies[name] = read(name)
        bundle = validate_candidate_snapshot_bundle_bytes(
            manifest_name=manifest_name, files=files, account_names=account_names,
            state_names=state_names, run_id=run_id, account=account,
            dependencies=dependencies, check=check,
        )
        check()
    except ProjectReaderError:
        raise
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ProjectReaderError("bundle_invalid") from exc
    manifest = bundle["manifest"]
    identity = {"account": account, "run_id": run_id, "market": market.upper(),
                "as_of": manifest["sealed_at_utc"], "revision": manifest["content_sha256"]}
    return {
        "manifest": {**identity, "terminal": True, "scanned": bool(manifest["expected_scopes"]),
                     "resource_categories": ["candidate_snapshot", "status_index"]},
        "resources": {name: files[name] for name in resource_names},
        "scope": identity, "bytes_read": used_before - budget["bytes"],
    }


def load_run_metrics(
    *, runtime_root: RuntimeRootResolution, run_id: str, account: str, market: str,
    authorized_accounts: list[str] | tuple[str, ...], deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None, _budget: dict[str, int] | None = None,
    _reader_root: int | None = None,
) -> dict[str, Any]:
    """Read only account metrics whose payload proves the historical identity."""
    _scope(runtime_root, authorized_accounts, account, market)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id):
        raise ProjectReaderError("invalid_run_id")
    budget = _budget if _budget is not None else {"bytes": _MAX_BYTES}
    if budget["bytes"] <= 0:
        raise ProjectReaderError("bundle_too_large")
    raw = read_bytes(runtime_root.runtime_root if _reader_root is None else _reader_root,
                     f"output_runs/{run_id}/accounts/{account}/state/account_metrics.json",
                     max_bytes=budget["bytes"], deadline_monotonic=deadline_monotonic, cancelled=cancelled)
    budget["bytes"] -= len(raw)
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("metrics object required")
        if value.get("run_id") != run_id or value.get("account") != account:
            raise ProjectReaderError("scope_conflict")
        markets = value.get("markets_to_run")
        if not isinstance(markets, list) or not markets or any(not isinstance(item, str) for item in markets):
            raise ProjectReaderError("market_unverifiable")
        if {item.upper() for item in markets} != {market.upper()}:
            raise ProjectReaderError("market_unverifiable")
        as_of = value.get("as_of_utc")
        parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if isinstance(as_of, str) else None
        if parsed is None or parsed.tzinfo is None:
            raise ValueError("timestamp with timezone required")
        if value.get("reason") is not None and not isinstance(value["reason"], str):
            raise ValueError("reason must be text when present")
        if any(type(value.get(field)) is not bool for field in ("ran_scan", "ran_pipeline")):
            raise ValueError("boolean run flags required")
    except ProjectReaderError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise ProjectReaderError("metrics_invalid") from None
    reason = value.get("reason")
    known_reason = reason if isinstance(reason, str) and reason in _DIAGNOSTIC_REASONS else None
    return {
        "run_id": run_id, "account": account, "market": market.upper(), "as_of": as_of,
        "revision": hashlib.sha256(raw).hexdigest(), "terminal": None,
        "scanned": value["ran_scan"], "ran_pipeline": value["ran_pipeline"],
        "outcomes": {
            "usable_scan_result": value["ran_scan"],
            "pipeline_completed_successfully": value["ran_pipeline"],
        },
        "resource_categories": ["account_diagnostics"],
        "reason": known_reason,
        "reason_description": _DIAGNOSTIC_REASONS.get(known_reason),
        "cause_available": known_reason is not None,
    }


def load_run_record(**kwargs: Any) -> dict[str, Any]:
    """Discovery accepts metrics; candidate content still requires a sealed bundle."""
    try:
        record = dict(load_run_bundle(**kwargs)["manifest"])
    except ProjectReaderError as exc:
        if exc.code != "bundle_unavailable":
            raise
        return load_run_metrics(**kwargs)
    try:
        metrics = load_run_metrics(**kwargs)
    except ProjectReaderError as exc:
        if exc.code == "not_found":
            return record
        raise
    record["resource_categories"] += metrics["resource_categories"]
    return record



def load_run_diagnostics(
    *, runtime_root: RuntimeRootResolution, scope: dict[str, Any], account: str, run_id: str,
    limit: int = 20, cursor: str | None = None,
    deadline_monotonic: float | None = None, cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Project fixed, authenticated failure fields; never expose raw audit text."""
    from src.application.agent_tools.project_reader import (
        _directory, reader_query_context, digest, decode_cursor, set_continuation,
    )

    market = str(scope.get("market") or "")
    accounts = scope.get("accounts") or []
    _scope(runtime_root, accounts, account, market)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", run_id):
        raise ProjectReaderError("invalid_run_id")
    if type(limit) is not int or not 1 <= limit <= 40:
        raise ProjectReaderError("invalid_limit")
    options = {"deadline_monotonic": deadline_monotonic, "cancelled": cancelled}
    budget = {"bytes": _MAX_BYTES, "metadata": 40000}
    rows, missing, statuses = [], [], []
    fingerprints = {}
    authorized_rows = skipped = 0
    with reader_query_context():
        descriptor = _directory(runtime_root.runtime_root, "", deadline_monotonic, cancelled)
        try:
            root_stat = os.fstat(descriptor)
            binding = digest({"tool": "runtime_logs", "action": "scoped", "scope": scope,
                "account": account, "run_id": run_id, "limit": limit,
                "root": [root_stat.st_dev, root_stat.st_ino]})
            state = decode_cursor(cursor, binding)
            owner_options = {"runtime_root": runtime_root, "run_id": run_id, "account": account,
                "market": market, "authorized_accounts": accounts, "_budget": budget,
                "_reader_root": descriptor, **options}
            try:
                metrics = load_run_metrics(**owner_options)
            except ProjectReaderError as exc:
                if exc.code != "not_found":
                    raise
                metrics = None
            if metrics is not None:
                as_of = metrics["as_of"]
                fingerprints["account_metrics"] = metrics["revision"]
                rows.append({"source_kind": "account_metrics", "record_scope": "account_metrics",
                    "causal_link_to_other_records": "not_established",
                    "run_id": run_id, "account": account, "event_at_utc": as_of,
                    "outcomes": metrics["outcomes"],
                    "reason": metrics["reason"], "reason_description": metrics["reason_description"],
                    "cause_available": metrics["cause_available"]})
                authorized_rows += 1
                statuses.append({"source_kind": "account_metrics", "status": "ok"})
                names, _ = list_names(descriptor, f"output_runs/{run_id}/accounts/{account}/state",
                    max_entries=min(10000, budget["metadata"]), **options)
                budget["metadata"] -= len(names)
                if any(name in names for name in _MANIFESTS):
                    # A present formal bundle must not contradict the metrics authority.
                    bundle = load_run_bundle(**owner_options)
                    fingerprints["candidate_manifest"] = bundle["scope"]["revision"]
            else:
                bundle = load_run_bundle(**owner_options)
                as_of = bundle["scope"]["as_of"]
                fingerprints["candidate_manifest"] = bundle["scope"]["revision"]
                missing.append("account_metrics_missing")
                statuses.append({"source_kind": "account_metrics", "status": "missing"})
            if budget["bytes"] <= 0:
                raise ProjectReaderError("bundle_too_large")
            try:
                audit = read_bytes(descriptor, f"output_runs/{run_id}/state/audit_events.jsonl",
                    max_bytes=budget["bytes"], **options)
                budget["bytes"] -= len(audit)
                fingerprints["run_audit"] = hashlib.sha256(audit).hexdigest()
                lines = audit.decode("utf-8").splitlines()
            except UnicodeError:
                lines = []
                missing.append("audit_invalid_encoding")
                statuses.append({"source_kind": "run_audit", "status": "unreadable"})
            except ProjectReaderError as exc:
                if exc.code in {"cancelled", "time_deadline", "source_changed"}:
                    raise
                lines = []
                missing.append("audit_" + exc.code)
                statuses.append({"source_kind": "run_audit", "status": "missing" if exc.code == "not_found" else "unreadable"})
            else:
                statuses.append({"source_kind": "run_audit", "status": "ok"})
            for line in lines:
                _check(deadline_monotonic, cancelled)
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    if "audit_unattributed_rows" not in missing:
                        missing.append("audit_unattributed_rows")
                    continue
                if not isinstance(value, dict) or not value.get("account"):
                    if "audit_unattributed_rows" not in missing:
                        missing.append("audit_unattributed_rows")
                    continue
                if value["account"] != account:
                    continue
                authorized_rows += 1
                extra = value.get("extra") if isinstance(value.get("extra"), dict) else {}
                if (value.get("run_id") != run_id or any(extra.get(key) not in (None, expected)
                    for key, expected in (("account", account), ("run_id", run_id)))
                    or any(str(value.get(key)).upper() != market.upper() for key in ("market",) if value.get(key))
                    or (extra.get("market") and str(extra["market"]).upper() != market.upper())):
                    skipped += 1
                    continue
                if value.get("schema_kind") != "audit_event" or value.get("schema_version") != "1.0":
                    skipped += 1
                    continue
                timestamp = value.get("event_at_utc")
                try:
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else None
                    if parsed is None or parsed.tzinfo is None:
                        raise ValueError("timestamp required")
                except ValueError:
                    skipped += 1
                    continue
                # Only this writer call site has a closed phase/adapter projection here.
                if value.get("status") not in ("error", "failed"):
                    continue
                if (value.get("event_type"), value.get("action")) == ("tool_call", "run_pipeline"):
                    # The subprocess invocation is instrumentation, not the failure-result diagnostic.
                    continue
                if (value.get("event_type"), value.get("action")) != ("tool_call", "run_pipeline_result"):
                    skipped += 1
                    continue
                if (extra.get("failure_kind") not in ("io_error", "decision_error")
                    or extra.get("failure_stage") != "run_pipeline"
                    or extra.get("failure_adapter") not in (None, "pipeline")):
                    skipped += 1
                    continue
                error_code = value.get("error_code")
                reason = "account_config_hash_mismatch" if error_code == "ACCOUNT_CONFIG_HASH_MISMATCH" else None
                rows.append({"source_kind": "run_audit", "record_scope": "run_audit",
                    "causal_link_to_other_records": "not_established",
                    "run_id": run_id, "account": account,
                    "event_at_utc": timestamp, "event_type": "tool_call", "action": "run_pipeline_result",
                    "status": value["status"], "failure_kind": extra["failure_kind"],
                    "failure_stage": "run_pipeline", "failure_adapter": extra.get("failure_adapter"),
                    "error_code": error_code if reason else None,
                    "reason": reason, "reason_description": _DIAGNOSTIC_REASONS.get(reason),
                    "cause_available": reason is not None})
        finally:
            os.close(descriptor)
    if skipped:
        missing.append("audit_authorized_rows_unsupported")
    source_hash = digest({"sources": fingerprints, "statuses": statuses})
    if state and state.get("source_hash") != source_hash:
        raise ProjectReaderError("source_changed")
    offset = state.get("offset", 0) if state else 0
    if type(offset) is not int or not 0 <= offset <= len(rows):
        raise ProjectReaderError("cursor_invalidated")
    page = []
    for row in rows[offset:offset + limit]:
        _check(deadline_monotonic, cancelled)
        if len(json.dumps([*page, row], ensure_ascii=False).encode()) > 5500:
            break
        page.append(row)
    if rows and not page:
        raise ProjectReaderError("scope_too_broad")
    has_more = offset + len(page) < len(rows)
    bound_scope = {"account": account, "run_id": run_id, "market": market.upper(),
        "source_hash": source_hash, "page_range": {"start": offset, "end": offset + len(page)}}
    complete = not missing
    result = {"action": "scoped", "diagnostics": page,
        "record_relationship": "independent_records_no_causal_or_sequence_link",
        "scope": bound_scope,
        "source": {"kind": "account_run_diagnostics", "revision": source_hash}, "source_hash": source_hash,
        "freshness": {"status": "historical", "as_of": as_of},
        "read_status": "ok" if complete else "partial" if page else "unavailable",
        "read_statuses": statuses, "missing_data": missing,
        "skipped_authorized_count": skipped,
        "pagination": {"total_count": authorized_rows if complete else None,
            "matched_count": len(rows) if complete else None, "returned_count": len(page),
            "scanned_count": authorized_rows, "has_more": has_more},
        "coverage": {"status": "complete" if complete else "partial" if page else "unknown",
            "complete_for": "requested_page", "included_count": len(page),
            "total_count": len(rows) if complete else None,
            "omitted_count": len(rows) - len(page) if complete else None, "has_more": has_more},
        "limitations": [
            "每条诊断记录相互独立；同一运行、排列顺序或时间接近都不能证明因果或先后链路。",
            "false 结果字段仅表示没有可用扫描结果或流水线未成功完成，不能证明相应流程从未被调用。",
            "失败阶段只定位记录发生的位置，不证明具体根因、成交或通知完成。",
        ]}
    set_continuation(result, {"binding": binding, "source_hash": source_hash,
        "offset": offset + len(page)} if has_more else None)
    _check(deadline_monotonic, cancelled)
    return result

def discover_runs(
    *, runtime_root: RuntimeRootResolution, market: str,
    authorized_accounts: list[str] | tuple[str, ...], account: str | None = None,
    cursor: dict[str, Any] | None = None, limit: int = 40,
    deadline_monotonic: float | None = None, cancelled: Callable[[], bool] | None = None,
    _reader_root: int | None = None,
) -> dict[str, Any]:
    """Bounded run discovery; facade must authenticate the returned continuation state."""
    accounts = _scope(runtime_root, authorized_accounts, account, market)
    _check(deadline_monotonic, cancelled)
    reader_root = runtime_root.runtime_root if _reader_root is None else _reader_root
    names, identity = list_names(reader_root, "output_runs", max_entries=10000,
                                 deadline_monotonic=deadline_monotonic, cancelled=cancelled)
    metadata_count = len(names)
    names = sorted((name for name in names if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", name)), reverse=True)
    binding = {"directory": identity, "accounts": accounts, "market": market.upper()}
    state = cursor or {"binding": binding, "index": 0, "account_index": 0, "unverified": {item: 0 for item in accounts}, "reasons": []}
    if state.get("binding") != binding:
        raise ProjectReaderError("source_changed")
    index, account_index = state.get("index"), state.get("account_index")
    if (type(index) is not int or not 0 <= index <= len(names) or type(account_index) is not int
            or not 0 <= account_index < len(accounts)):
        raise ProjectReaderError("cursor_invalid")
    entries, checked = [], 0
    unverified = dict(state.get("unverified", {item: 0 for item in accounts}))
    reasons = list(state.get("reasons", []))[:8]
    budget = {"bytes": _MAX_BYTES, "metadata": 40000 - metadata_count}
    while index < len(names) and checked < 200 and len(entries) < min(max(limit, 1), 40):
        _check(deadline_monotonic, cancelled)
        if budget["bytes"] <= 0 or budget["metadata"] <= 0:
            break
        selected_account = accounts[account_index]
        checked += 1
        before_candidate = dict(budget)
        try:
            record = load_run_record(runtime_root=runtime_root, run_id=names[index],
                                     account=selected_account, market=market, authorized_accounts=accounts,
                                     deadline_monotonic=deadline_monotonic, cancelled=cancelled, _budget=budget, _reader_root=_reader_root)
            entries.append({**record, "unverified_newer_count": unverified[selected_account]})
        except ProjectReaderError as exc:
            if exc.code in {"cancelled", "time_deadline"}:
                raise
            if (exc.code in {"file_too_large", "bundle_too_large", "directory_too_large", "scope_too_broad"}
                    and checked > 1
                    and (before_candidate["bytes"] < _MAX_BYTES or before_candidate["metadata"] < 20000)):
                # This query used part of the budget on newer candidates. Retry this
                # same candidate with a fresh page before declaring it unverifiable.
                break
            unverified[selected_account] += 1
            if exc.code not in reasons and len(reasons) < 8:
                reasons.append(exc.code)
        account_index += 1
        if account_index == len(accounts):
            account_index = 0
            index += 1
    has_more = index < len(names)
    next_state = ({"binding": binding, "index": index, "account_index": account_index,
                   "unverified": unverified, "reasons": reasons} if has_more else None)
    return {"entries": entries, "next_state": next_state, "unverified_newer_count": sum(unverified.values()),
            "reasons": reasons, "scanned": checked, "coverage": {
                "status": "partial" if has_more or any(unverified.values()) else "complete",
                "complete_for": "requested_page", "included_count": len(entries),
                "has_more": has_more,
            }}


def _project_run_files(
    runtime_root: RuntimeRootResolution, *, scope: dict[str, Any],
    account: str | None = None, run_id: str | None = None,
    action: str = "list", relative_name: str = "", query: str = "",
    start_line: int = 1, max_lines: int = 80, cursor: str | None = None,
    deadline_monotonic: float | None = None, cancelled: Callable[[], bool] | None = None,
    _reader_root: int,
) -> dict[str, Any]:
    """Canonical-tool adapter; cursor scope is rechecked before every underlying read."""
    from src.application.agent_tools.project_reader import (
        MAX_OUTPUT_BYTES, set_continuation, decode_cursor, digest, redacted_text, page_text,
        _directory,
    )

    if action not in {"list", "read", "search"} or not isinstance(query, str) or len(query) > 512:
        raise ProjectReaderError("INPUT_ERROR")
    if cursor and start_line != 1:
        raise ProjectReaderError("cursor_invalidated")
    market, accounts = str(scope.get("market") or ""), scope.get("accounts") or []
    selected = _scope(runtime_root, accounts, account, market)
    options = {"deadline_monotonic": deadline_monotonic, "cancelled": cancelled}
    descriptor = _directory(_reader_root, "", deadline_monotonic, cancelled)
    try:
        info = os.fstat(descriptor)
        root_identity = {"dev": info.st_dev, "ino": info.st_ino}
    finally:
        os.close(descriptor)
    trusted = {**scope, "root_identity": root_identity, "account": account}
    if not run_id:
        if action != "list" or relative_name or query:
            raise ProjectReaderError("run_id_required")
        binding = digest({"scope": trusted, "resource": "run", "action": "discover"})
        state = decode_cursor(cursor, binding)
        found = discover_runs(runtime_root=runtime_root, market=market, authorized_accounts=accounts,
                              account=account, cursor=state.get("scan_state") if state else None,
                              limit=8, _reader_root=_reader_root, **options)
        next_state = found.pop("next_state")
        result = {**found, "action": "list", "resource": "run", "scope": scope,
                  "source": {"resource": "run", "revision": digest({"root": root_identity, "page": found})},
                  "freshness": {"status": "not_applicable"}}
        set_continuation(result, {"binding": binding, "scan_state": next_state} if next_state else None)
    else:
        if len(selected) != 1:
            raise ProjectReaderError("account_required")
        bundle = load_run_bundle(runtime_root=runtime_root, run_id=run_id, account=selected[0], market=market,
                                 authorized_accounts=accounts, _reader_root=_reader_root, **options)
        resources = bundle["resources"]
        # config authority participates in pagination binding without exposing the full configuration.
        bound_scope = {**bundle["scope"], "config_revision": scope.get("config_revision"),
                       "authorized_accounts": accounts, "root_identity": root_identity}
        if action == "read":
            if relative_name not in resources:
                raise ProjectReaderError("permission_denied")
            result = page_text(resources[relative_name], relative_name=relative_name, scope=bound_scope,
                               cursor=cursor, start_line=start_line, max_lines=max_lines, resource="run", **options)
            result.update(action="read", resource="run", scope={**bundle["scope"], **result.get("scope", {})},
                          freshness={"status": "historical", "as_of": bundle["scope"]["as_of"]})
        else:
            if relative_name and relative_name not in resources:
                raise ProjectReaderError("permission_denied")
            if action == "search" and not query:
                raise ProjectReaderError("INPUT_ERROR")
            if cursor:
                raise ProjectReaderError("cursor_invalidated")
            entries = []
            candidates = [relative_name] if relative_name else sorted(resources)
            for name in candidates:
                _check(deadline_monotonic, cancelled)
                if action == "list":
                    entries.append({"relative_name": name, "size_bytes": len(resources[name])})
                else:
                    text = redacted_text(resources[name])
                    offset = text.find(query)
                    if offset >= 0:
                        entries.append({"relative_name": name, "line": text.count("\n", 0, offset) + 1,
                                        "text": text[max(0, offset - 40):offset + 160]})
            result = {"action": action, "resource": "run", "entries": entries, "scope": bundle["scope"],
                      "source": {"resource": "run", "revision": bundle["scope"]["revision"]},
                      "coverage": {"status": "complete", "complete_for": "requested_page",
                                   "included_count": len(entries), "has_more": False},
                      "freshness": {"status": "historical", "as_of": bundle["scope"]["as_of"]},
                      "next_cursor": None, "scanned": len(candidates),
                      "limitations": ["搜索每文件仅给首个匹配片段；完整内容使用分段读取。"] if action == "search" else []}
    _check(deadline_monotonic, cancelled)
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        raise ProjectReaderError("scope_too_broad")
    return result


def project_run_files(runtime_root: RuntimeRootResolution, **kwargs: Any) -> dict[str, Any]:
    """Pin the trusted root and share one transient retry across the logical query."""
    from src.application.agent_tools.project_reader import _directory, reader_query_context

    if runtime_root.source not in {"argument", "env:OM_RUNTIME_ROOT"}:
        raise ProjectReaderError("runtime_root_unavailable")
    with reader_query_context():
        descriptor = _directory(runtime_root.runtime_root, "", kwargs.get("deadline_monotonic"), kwargs.get("cancelled"))
        try:
            return _project_run_files(runtime_root, _reader_root=descriptor, **kwargs)
        finally:
            os.close(descriptor)
