"""Frozen strike-rejection evidence and the weekly report's review rules.

The same source can run on the data host via Python stdin with --export-root.
Collection calls the installed snapshot owner; it never re-evaluates a strategy.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from domain.domain.symbol_identity import canonical_symbol

EVIDENCE_SCHEMA = "config_review_candidate_evidence.v1"
DIRECTIONS = {
    "put_strike_below_min": "put 下界",
    "put_strike_above_max": "put 上界",
    "call_strike_below_min": "call 下界",
    "call_strike_above_max": "call 上界",
}
RULES = {
    "min_observations": 30, "min_exposure_dates": 3,
    "recommend_pressure_pct": 15, "recommend_strike_only_count": 20,
    "recommend_strike_only_rate_pct": 30, "recommend_unique_contracts": 5,
    "recommend_rejection_dates": 3,
    "unchanged_pressure_pct": 5, "unchanged_rejection_rate_pct": 10,
    "max_price_age_hours": 96,
    "distance_denominator": "boundary",
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_POLICY_KEYS = (
    "policy_min_strike", "policy_max_strike",
    "policy_call_min_strike", "policy_call_max_strike",
    "policy_put_min_strike", "policy_put_max_strike",
)


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def previous_week(now: datetime) -> dict[str, str]:
    if now.tzinfo is None:
        raise ValueError("report time must include timezone")
    local = now.astimezone(_SHANGHAI)
    end = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    return {"timezone": "Asia/Shanghai", "start_utc": _iso(start), "end_utc": _iso(end),
            "label": f"{start:%Y-%m-%d %H:%M} ～ {end:%Y-%m-%d %H:%M}（右端不含）"}


def unavailable_evidence(now: datetime, reason: str) -> dict[str, Any]:
    return {"schema_version": EVIDENCE_SCHEMA, "period": previous_week(now),
            "exported_at_utc": _iso(now), "source": {}, "snapshots": [],
            "errors": [{"reason": reason}], "excluded_runs": 0}


def collect_candidate_evidence(runtime_root: str | Path, now: datetime) -> dict[str, Any]:
    """Validate originals, including dependencies, and project report-only fields."""
    from src.application.opening_candidate_snapshot import (
        candidate_universe_summary, load_opening_candidate_snapshot,
    )

    root = Path(runtime_root).expanduser().resolve()
    evidence = unavailable_evidence(now, "no_candidate_snapshots")
    evidence["errors"] = []
    evidence["source"] = {"runtime_root": str(root)}
    start, end = (_timestamp(evidence["period"][key]) for key in ("start_utc", "end_utc"))
    for path in sorted((root / "output_runs").glob("*/accounts/*/state/opening_candidate_snapshot.json")):
        relative = path.relative_to(root).as_posix()
        try:
            # The envelope selects the window; only the canonical loader can admit it.
            envelope = json.loads(path.read_text(encoding="utf-8"))
            sealed = _timestamp(envelope.get("sealed_at_utc"))
            if sealed > now.astimezone(timezone.utc):
                raise ValueError("snapshot seal is in the future")
            if not start <= sealed < end:
                continue
            if envelope.get("scan_mode") == "experience":
                evidence["excluded_runs"] += 1
                continue
            if envelope.get("scan_mode") != "standard":
                raise ValueError("snapshot scan mode is missing or unsupported")
            run_id, account = path.parts[-5], path.parts[-3]
            snapshot = load_opening_candidate_snapshot(
                base=root, run_id=run_id, account=account, require_current_contract=False,
            )
            if snapshot.get("scan_mode") != "standard" or snapshot.get("sealed_at_utc") != envelope.get("sealed_at_utc"):
                raise ValueError("snapshot envelope changed during collection")
            rows = []
            for row in snapshot.get("candidate_decisions") or []:
                src = row.get("normalized_input") or {}
                opening = row.get("opening_decision") or {}
                rows.append({
                    "candidate_id": row.get("candidate_id"), "symbol": src.get("symbol"),
                    "strategy_mode": row.get("strategy_mode"), "contract_symbol": src.get("contract_symbol"),
                    "normalized_input": {key: src[key] for key in _POLICY_KEYS if key in src},
                    "opening_decision": {
                        "accepted": opening.get("accepted"),
                        "rejects": [{key: reject.get(key) for key in ("reason", "stage", "threshold")}
                                    for reject in opening.get("rejects") or []],
                    },
                })
            projected = {
                "run_id": snapshot["run_id"], "account": snapshot["account"], "market": snapshot["market"],
                "sealed_at_utc": snapshot["sealed_at_utc"], "scan_mode": "standard",
                "content_sha256": snapshot["content_sha256"], "source_path": relative,
                "strike_boundary_reason_codes_version": snapshot.get("strike_boundary_reason_codes_version"),
                "universe": candidate_universe_summary(snapshot),
                "scope_keys": [[row.get("symbol"), row.get("strategy_mode")]
                               for row in snapshot.get("scope_results") or [] if row.get("scope") == "strategy"],
                "candidate_decisions": rows,
            }
            admitted = _validate_evidence({**evidence, "snapshots": [projected]}, now)
            evidence["snapshots"].extend(admitted["snapshots"])
        except Exception as exc:
            evidence["errors"].append({"path": relative, "reason": type(exc).__name__, "detail": str(exc)[:240]})
    return _validate_evidence(evidence, now)


def load_candidate_evidence(path: str | Path, now: datetime) -> dict[str, Any]:
    try:
        evidence = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        return _validate_evidence(evidence, now)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return unavailable_evidence(now, f"candidate_evidence_unavailable:{type(exc).__name__}:{exc}")


def _validate_evidence(evidence: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(evidence, dict) or evidence.get("schema_version") != EVIDENCE_SCHEMA:
        raise ValueError("unsupported candidate evidence schema")
    period = previous_week(now)
    if not isinstance(evidence.get("period"), dict) or any(evidence["period"].get(key) != period[key] for key in ("start_utc", "end_utc", "timezone")):
        raise ValueError("candidate evidence period mismatch")
    exported = _timestamp(evidence.get("exported_at_utc"))
    if exported > now.astimezone(timezone.utc):
        raise ValueError("candidate evidence export is in the future")
    if not isinstance(evidence.get("snapshots"), list) or not isinstance(evidence.get("errors"), list):
        raise ValueError("candidate evidence collections must be lists")
    start, end = _timestamp(period["start_utc"]), _timestamp(period["end_utc"])
    snapshots: dict[tuple[str, str, str], dict[str, Any]] = {}
    for snapshot in evidence["snapshots"]:
        if not isinstance(snapshot, dict) or snapshot.get("scan_mode") != "standard":
            raise ValueError("candidate evidence must use standard snapshots")
        if snapshot.get("market") not in {"HK", "US"} or not start <= _timestamp(snapshot.get("sealed_at_utc")) < end:
            raise ValueError("candidate snapshot outside report scope")
        if any(not isinstance(snapshot.get(key), str) or not snapshot[key] for key in ("run_id", "account", "content_sha256")):
            raise ValueError("candidate snapshot identity is missing")
        version = snapshot.get("strike_boundary_reason_codes_version")
        if version is not None and (type(version) is not int or version != 1):
            raise ValueError("unsupported strike reason capability")
        universe, scopes = snapshot.get("universe"), snapshot.get("scope_keys")
        if (not isinstance(universe, dict) or universe.get("status") not in {"complete", "partial"}
                or not isinstance(universe.get("affected_scopes"), list) or not isinstance(scopes, list)
                or any(not isinstance(scope, list) or len(scope) != 2 or any(not isinstance(x, str) for x in scope) for scope in scopes)
                or any(not isinstance(scope, dict) or any(not isinstance(scope.get(key), str) or not scope[key]
                                                         for key in ("symbol", "strategy_mode"))
                       for scope in universe["affected_scopes"])):
            raise ValueError("candidate snapshot completion facts are missing")
        if (universe["status"] == "complete") != (not universe["affected_scopes"]):
            raise ValueError("candidate snapshot completion facts conflict")
        universe = {**universe, "affected_scopes": [{**scope, "symbol": canonical_symbol(scope["symbol"])}
                                                   for scope in universe["affected_scopes"]]}
        scope_keys = [[canonical_symbol(scope[0]), scope[1]] for scope in scopes]
        if any(not scope[0] for scope in scope_keys) or any(not scope["symbol"] for scope in universe["affected_scopes"]):
            raise ValueError("candidate scope symbol is invalid")
        snapshot = {**snapshot, "universe": universe, "scope_keys": scope_keys}
        if not isinstance(snapshot.get("candidate_decisions"), list):
            raise ValueError("candidate decisions must be a list")
        decisions = {}
        for row in snapshot["candidate_decisions"]:
            if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key]
                                                for key in ("candidate_id", "symbol", "strategy_mode", "contract_symbol")):
                raise ValueError("candidate identity is missing")
            opening = row.get("opening_decision")
            if not isinstance(opening, dict) or type(opening.get("accepted")) is not bool or not isinstance(opening.get("rejects"), list):
                raise ValueError("candidate decision is incomplete")
            if opening["accepted"] and opening["rejects"]:
                raise ValueError("accepted candidate cannot have rejects")
            if not opening["accepted"] and not opening["rejects"]:
                raise ValueError("rejected candidate must have reasons")
            if not isinstance(row.get("normalized_input"), dict):
                raise ValueError("candidate policy input is missing")
            if any(value is not None and _number(value) is None for key, value in row["normalized_input"].items() if key in _POLICY_KEYS):
                raise ValueError("candidate strike policy value is invalid")
            row = {**row, "symbol": canonical_symbol(row["symbol"])}
            if not row["symbol"]:
                raise ValueError("candidate symbol is invalid")
            if any(not isinstance(reject, dict) or not isinstance(reject.get("reason"), str) or not reject["reason"].strip() or reject["reason"] != reject["reason"].strip()
                   for reject in opening["rejects"]):
                raise ValueError("candidate rejection is incomplete")
            if any(reject["reason"] in DIRECTIONS and _number(reject.get("threshold")) is None for reject in opening["rejects"]):
                raise ValueError("directional strike threshold is missing or invalid")
            prior = decisions.setdefault(row["candidate_id"], row)
            if prior != row:
                raise ValueError("conflicting candidate identity")
        snapshot = {**snapshot, "candidate_decisions": list(decisions.values())}
        key = (snapshot["run_id"], snapshot["account"], snapshot["market"])
        prior = snapshots.setdefault(key, snapshot)
        if {k: v for k, v in prior.items() if k != "source_path"} != {k: v for k, v in snapshot.items() if k != "source_path"}:
            raise ValueError("conflicting snapshot identity")
    return {**evidence, "period": period, "snapshots": list(snapshots.values())}


def _observations(evidence: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for snapshot in evidence["snapshots"]:
        for row in snapshot["candidate_decisions"]:
            yield snapshot, row


def _reasons(row: dict[str, Any]) -> set[str]:
    return {item["reason"] for item in row["opening_decision"]["rejects"]}


def _coverage(evidence: dict[str, Any], snapshots: list[dict[str, Any]], symbol: str | None = None, mode: str | None = None) -> str:
    if not snapshots:
        return "unavailable"
    complete = not evidence["errors"]
    for snapshot in snapshots:
        universe = snapshot["universe"]
        affected = universe.get("affected_scopes") or []
        scope_failed = universe["status"] != "complete"
        if symbol is not None and affected:
            scope_failed = any(item.get("symbol") == symbol and item.get("strategy_mode") == mode for item in affected)
        unknown_direction = any("hard_strike" in _reasons(row) for row in snapshot["candidate_decisions"]
                                if symbol is None or (row["symbol"] == symbol and row["strategy_mode"] == mode))
        complete = complete and snapshot.get("strike_boundary_reason_codes_version") == 1 and not scope_failed and not unknown_direction
    if complete:
        return "complete"
    has_direction = any(_reasons(row) & DIRECTIONS.keys() for snap in snapshots for row in snap["candidate_decisions"]
                        if symbol is None or (row["symbol"] == symbol and row["strategy_mode"] == mode))
    return "partial" if has_direction or any(s.get("strike_boundary_reason_codes_version") == 1 for s in snapshots) else "unavailable"


def summarize_rejections(evidence: dict[str, Any]) -> dict[str, Any]:
    observed = Counter({key: 0 for key in DIRECTIONS})
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    legacy = rejected = strike_only = decisions = 0
    dates, contracts = set(), set()
    for snapshot, row in _observations(evidence):
        decisions += 1
        dates.add(_timestamp(snapshot["sealed_at_utc"]).astimezone(_SHANGHAI).date().isoformat())
        reasons = _reasons(row)
        legacy += int("hard_strike" in reasons)
        directions = reasons & DIRECTIONS.keys()
        if not directions:
            continue
        rejected += 1
        pure = not (reasons - DIRECTIONS.keys())
        strike_only += int(pure)
        contract = (snapshot["market"], row["symbol"], row["strategy_mode"], row["contract_symbol"])
        contracts.add(contract)
        for reason in directions:
            observed[reason] += 1
            key = (snapshot["market"], row["symbol"], reason)
            group = grouped.setdefault(key, {"market": key[0], "symbol": key[1], "reason": reason,
                                             "observed_count": 0, "strike_only_count": 0, "contracts": set()})
            group["observed_count"] += 1
            group["strike_only_count"] += int(pure)
            group["contracts"].add(contract)
    coverage = _coverage(evidence, evidence["snapshots"])
    groups = [{**{k: v for k, v in group.items() if k != "contracts"}, "unique_contracts": len(group["contracts"])}
              for _, group in sorted(grouped.items())]
    markets = []
    for market in sorted({s["market"] for s in evidence["snapshots"]}):
        selected = [s for s in evidence["snapshots"] if s["market"] == market]
        status = _coverage(evidence, selected)
        rows = [row for s in selected for row in s["candidate_decisions"]]
        count = sum(bool(_reasons(row) & DIRECTIONS.keys()) for row in rows)
        markets.append({"market": market, "coverage": status, "snapshot_count": len(selected),
                        "observed_count": count, "count": count if status == "complete" else None})
    return {"coverage": coverage, "snapshot_count": len(evidence["snapshots"]), "decision_count": decisions,
            "count": rejected if coverage == "complete" else None, "observed_count": rejected,
            "direction_counts": dict(observed) if coverage == "complete" else {key: None for key in DIRECTIONS},
            "observed_direction_counts": dict(observed), "legacy_hard_strike_count": legacy,
            "strike_only_count": strike_only, "unique_contracts": len(contracts), "dates": sorted(dates),
            "markets": markets, "groups": groups, "errors": evidence["errors"],
            "sealed_at_min": min((s["sealed_at_utc"] for s in evidence["snapshots"]), default=None),
            "sealed_at_max": max((s["sealed_at_utc"] for s in evidence["snapshots"]), default=None),
            "source": evidence.get("source", {})}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _same(left: Any, right: Any) -> bool:
    a, b = _number(left), _number(right)
    return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8)


def assess_boundary(*, market: str, symbol: str, path: str, value: float,
                    enabled: bool, price: dict[str, Any] | None, evidence: dict[str, Any], now: datetime) -> dict[str, Any]:
    side, field = path.rsplit(".", 1)
    mode = "put" if side == "sell_put" else "call" if side == "sell_call" else "combo_yield"
    leg = "put" if side == "sell_put" or side.endswith(".put") else "call"
    direction = "below_min" if field == "min_strike" else "above_max"
    reason = f"{leg}_strike_{direction}"
    policy_key = f"policy_{field}" if mode != "combo_yield" else f"policy_{leg}_{field}"
    snapshots = [s for s in evidence["snapshots"] if s["market"] == market and
                 ([symbol, mode] in s["scope_keys"] or any(r["symbol"] == symbol and r["strategy_mode"] == mode for r in s["candidate_decisions"]))]
    coverage = _coverage(evidence, snapshots, symbol, mode)
    n = r = c = 0
    exposure_dates, rejection_dates, contracts = set(), set(), set()
    for snapshot in snapshots:
        day = _timestamp(snapshot["sealed_at_utc"]).astimezone(_SHANGHAI).date().isoformat()
        for row in snapshot["candidate_decisions"]:
            if row["symbol"] != symbol or row["strategy_mode"] != mode or not _same(row["normalized_input"].get(policy_key), value):
                continue
            n += 1
            exposure_dates.add(day)
            matching = any(item["reason"] == reason and _same(item.get("threshold"), value) for item in row["opening_decision"]["rejects"])
            if matching:
                r += 1
                if not (_reasons(row) - DIRECTIONS.keys()):
                    c += 1
                    rejection_dates.add(day)
                    contracts.add(row["contract_symbol"])
    spot = _number(price.get("value")) if price else None
    distance = (spot - value) / value * 100 if spot is not None and spot > 0 and value > 0 else None
    pressure = distance if field == "max_strike" or distance is None else -distance
    price_valid = False
    try:
        age = now.astimezone(timezone.utc) - _timestamp(price.get("observed_at_utc") if price else None)
        price_valid = distance is not None and timedelta(0) <= age <= timedelta(hours=RULES["max_price_age_hours"])
    except (ValueError, TypeError):
        pass
    conclusion, why = "观察", "尚未形成一致的调整或无需调整信号"
    if not enabled:
        why = "策略未启用，不纳入整体判定"
    elif not price_valid:
        why = "价格缺失、时点不明、未来时点或超过96小时"
    elif coverage != "complete":
        why = "该期方向化拒绝证据不足，不能把缺失当成0"
    elif n < RULES["min_observations"] or len(exposure_dates) < RULES["min_exposure_dates"]:
        why = "可比样本不足30次或不足3个日期"
    elif (pressure >= RULES["recommend_pressure_pct"] and c >= RULES["recommend_strike_only_count"]
          and c / n * 100 >= RULES["recommend_strike_only_rate_pct"]
          and len(contracts) >= RULES["recommend_unique_contracts"]
          and len(rejection_dates) >= RULES["recommend_rejection_dates"]):
        conclusion, why = "建议调整", "建议人工复核该边界：明显价格压力且多日、多合约持续仅因strike被拒；不自动修改配置"
    elif pressure <= RULES["unchanged_pressure_pct"] and r / n * 100 < RULES["unchanged_rejection_rate_pct"]:
        conclusion, why = "无需调整", "价格压力不高于5%，同值边界拒绝率低于10%"
    return {"market": market, "symbol": symbol, "path": path, "value": value, "enabled": enabled,
            "price": price, "distance_pct": round(distance, 4) if distance is not None else None,
            "pressure_pct": round(pressure, 4) if pressure is not None else None,
            "coverage": coverage, "N": n, "R": r, "C": c, "unique_contracts": len(contracts),
            "exposure_dates": len(exposure_dates), "rejection_dates": len(rejection_dates),
            "rejection_rate_pct": round(r / n * 100, 4) if n else None,
            "strike_only_rate_pct": round(c / n * 100, 4) if n else None,
            "conclusion": conclusion, "reason": why}


def report_conclusion(assessments: list[dict[str, Any]], data_gaps: list[str]) -> dict[str, Any]:
    active = [row for row in assessments if row["enabled"]]
    recommendations = [row for row in active if row["conclusion"] == "建议调整"]
    if recommendations:
        return {"state": "建议调整", "reasons": [f"{r['market']} {r['symbol']} {r['path']}：{r['reason']}" for r in recommendations]}
    if active and not data_gaps and all(row["conclusion"] == "无需调整" for row in active):
        return {"state": "无需调整", "reasons": ["全部启用边界的数据和样本合格，未形成调整信号"]}
    reasons = list(dict.fromkeys(row["reason"] for row in active if row["conclusion"] == "观察"))
    return {"state": "观察", "reasons": (reasons + data_gaps)[:3] or ["没有可判定的启用边界"]}


def _export_main() -> None:
    import argparse
    import contextlib
    import socket
    import sys

    parser = argparse.ArgumentParser(description="Read-only weekly candidate evidence projection")
    parser.add_argument("--export-root", required=True)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    now = _timestamp(args.as_of)
    with contextlib.redirect_stdout(sys.stderr):
        evidence = collect_candidate_evidence(args.export_root, now)
    evidence["source"]["hostname"] = socket.gethostname()
    evidence["source"]["release_root"] = str(Path.cwd().resolve())
    version = Path("VERSION")
    if version.is_file():
        evidence["source"]["version"] = version.read_text().strip()
    print(json.dumps(evidence, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    _export_main()
