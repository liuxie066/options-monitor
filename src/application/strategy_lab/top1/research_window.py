from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import estimate_futu_option_sell_fee
from domain.domain.short_vol_assessment import (
    calculate_option_market_concentration_after,
)
from domain.domain.symbol_identity import resolve_symbol_identity
from src.application.prepared_option_positions_context import (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2,
    PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2,
    PREPARED_OPTION_POSITIONS_PAYLOAD_NAME,
    PreparedOptionPositionsContextError,
    validate_strategy_lab_option_market_evidence,
)
from src.application.strategy_lab.evidence import load_strategy_lab_dataset
from src.application.strategy_lab.top1.contracts import (
    HISTORICAL_RESEARCH_WINDOW_SCHEMA,
    RESEARCH_REQUIRED_DAYS,
)
from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    read_bound_market_calendar_snapshot,
)
from src.application.strategy_lab.top1.economics import (
    build_fx_rate_binding_from_projection,
)
from src.infrastructure.private_storage import private_path


_RUN_ID = re.compile(r"(?P<stamp>\d{8}T\d{6}Z)-[A-Za-z0-9._-]+\Z")
_DATASET_STAMP = re.compile(r"(?P<stamp>\d{8}[Tt]\d{6}[Zz])")
_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_CANDIDATE_UNIVERSE = "same_point_accepted_sell_put_candidates"
_POINT_COMPLETENESS = "all_verified_observed_dataset_points"
_WINDOW_KEYS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "cutoff_at_utc",
        "required_days",
        "market_calendar_version",
        "market_calendar_ref",
        "market_calendar_content_sha256",
        "market_calendar_file_sha256",
        "latest_mature_trading_date",
        "selected_trading_dates",
        "candidate_universe",
        "point_completeness",
        "days",
        "content_sha256",
    }
)
_DAY_KEYS = frozenset({"trading_date", "points"})
_POINT_KEYS = frozenset(
    {
        "recommendation_point_id",
        "run_id",
        "observed_at_utc",
        "source_kind",
        "dataset_ref",
        "source_files",
        "candidate_facts_sha256",
    }
)


class ResearchWindowError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _select_complete_days(
    trading_dates: Sequence[object],
    latest_mature_trading_date: object,
    required_days: object,
) -> list[str]:
    if isinstance(required_days, bool) or not isinstance(required_days, int) or required_days != RESEARCH_REQUIRED_DAYS:
        raise ResearchWindowError(
            "research_window_invalid",
            f"required_days must equal {RESEARCH_REQUIRED_DAYS}",
        )
    dates: list[str] = []
    try:
        for raw in trading_dates:
            value = str(raw)
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
            dates.append(value)
        latest = str(latest_mature_trading_date)
        if date.fromisoformat(latest).isoformat() != latest:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ResearchWindowError("research_window_invalid", "trading dates must be canonical ISO dates") from exc
    if not dates or dates != sorted(set(dates)):
        raise ResearchWindowError(
            "research_window_invalid",
            "calendar dates must be ordered and unique",
        )
    if latest not in dates:
        raise ResearchWindowError(
            "research_window_coverage_missing",
            "market calendar does not cover the mature trading date",
        )
    end = dates.index(latest) + 1
    if end < required_days:
        raise ResearchWindowError(
            "research_window_coverage_missing",
            f"fewer than {required_days} mature trading days are available",
        )
    return dates[end - required_days : end]


def _normalize_point(
    artifact_root: str | Path,
    dataset_ref: object,
    *,
    market: str,
    account: str,
    cutoff_at_utc: str,
    runs_root_ref: str | None = None,
    expected_source_files: object = None,
) -> dict[str, Any]:
    def fail(reason_code: str, message: str) -> NoReturn:
        raise ResearchWindowError(reason_code, message)

    research_root = private_path(artifact_root).parent.resolve()

    def safe_ref(raw: object, label: str) -> tuple[str, Path]:
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            fail("research_window_invalid", f"{label} must be canonical text")
        parts = raw.split("/")
        if raw.startswith("/") or "\\" in raw or any(part in {"", ".", ".."} for part in parts):
            fail("research_window_invalid", f"{label} must be a safe relative ref")
        path = research_root.joinpath(*parts).resolve()
        if not path.is_relative_to(research_root):
            fail("research_window_invalid", f"{label} escapes the research root")
        return raw, path

    def source_file(kind: str, ref: str, path: Path) -> dict[str, str]:
        try:
            if not path.is_file():
                raise OSError
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError as exc:
            raise ResearchWindowError("research_window_coverage_missing", f"{kind} source file is unavailable") from exc
        return {"kind": kind, "ref": ref, "sha256": digest}

    def number(raw: object, label: str, *, positive: bool = False) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            fail("research_window_coverage_missing", f"{label} is missing")
        value = float(raw)
        if not math.isfinite(value) or (positive and value <= 0):
            fail("research_window_coverage_missing", f"{label} is invalid")
        return value

    ref, dataset_dir = safe_ref(dataset_ref, "dataset_ref")
    if not dataset_dir.is_dir() or dataset_dir.is_symlink():
        fail("research_window_coverage_missing", "Shadow Replay dataset is unavailable")
    manifest_ref = f"{ref}/manifest.json"
    candidates_ref = f"{ref}/candidate_snapshots.jsonl"
    files = [
        source_file("manifest", manifest_ref, dataset_dir / "manifest.json"),
        source_file(
            "candidate_snapshots",
            candidates_ref,
            dataset_dir / "candidate_snapshots.jsonl",
        ),
    ]
    try:
        evidence = load_strategy_lab_dataset(dataset_dir)
    except (OSError, TypeError, ValueError) as exc:
        raise ResearchWindowError("research_window_conflict", "Shadow Replay dataset cannot be verified") from exc
    integrity = evidence.get("integrity") or {}
    if integrity.get("status") == "verified":
        source_kind = "shadow_replay_verified_point"
    elif integrity == {
        "status": "legacy_unverified",
        "reason": "integrity_receipt_missing",
    }:
        source_kind = "shadow_replay_legacy_hash_bound_point"
    else:
        fail("research_window_coverage_missing", "dataset integrity is not usable")
    manifest = evidence.get("manifest")
    if not isinstance(manifest, Mapping):
        fail("research_window_conflict", "dataset manifest is invalid")
    source = manifest.get("source")
    source = source if isinstance(source, Mapping) else {}
    coverage = source.get("candidate_evidence_coverage")
    matching_coverage: list[Mapping[str, Any]] = []
    modern_empty_scope = False
    if isinstance(coverage, Mapping):
        raw_accounts = coverage.get("accounts")
        if not isinstance(raw_accounts, list):
            fail("research_window_conflict", "dataset candidate evidence coverage is invalid")
        for raw_account in raw_accounts:
            if not isinstance(raw_account, Mapping):
                fail("research_window_conflict", "dataset candidate evidence account is invalid")
            raw_markets = raw_account.get("markets") or []
            owners = raw_account.get("owner_snapshots") or []
            if not isinstance(raw_markets, list) or not isinstance(owners, list):
                fail("research_window_conflict", "dataset candidate evidence scope is invalid")
            markets = {str(value).lower() for value in raw_markets if isinstance(value, str) and value}
            if str(raw_account.get("account") or "").lower() == account and (not markets or market.lower() in markets):
                matching_coverage.append(raw_account)
        if matching_coverage and not all(
            item.get("status") == "supported"
            and item.get("strict_replay_authority") is True
            and item.get("contributes_snapshot_facts") is True
            for item in matching_coverage
        ):
            fail("research_window_coverage_missing", "dataset candidate evidence is not usable")
        modern_empty_scope = any("opening" in item["owner_snapshots"] for item in matching_coverage)
    dataset_id = manifest.get("dataset_id")
    if dataset_id != dataset_dir.name:
        fail("research_window_conflict", "dataset ref and manifest identity differ")
    run_id = source.get("run_id") or dataset_id
    run_match = _RUN_ID.fullmatch(run_id) if isinstance(run_id, str) else None
    if run_match is None:
        fail("research_window_conflict", "dataset run identity is invalid")
    dataset_stamp = _DATASET_STAMP.search(dataset_dir.name)
    if dataset_stamp is None or dataset_stamp.group("stamp").upper() != run_match.group("stamp"):
        fail("research_window_conflict", "dataset ref and run timestamp differ")
    if integrity.get("status") == "verified":
        integrity_files = integrity.get("files")
        candidate_receipt = (
            integrity_files.get("candidate_snapshots.jsonl") if isinstance(integrity_files, Mapping) else None
        )
        if not isinstance(candidate_receipt, Mapping) or candidate_receipt.get("sha256") != files[1]["sha256"]:
            fail("research_window_conflict", "candidate file receipt changed")

    observed = datetime.strptime(run_match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    try:
        if not cutoff_at_utc.endswith("Z"):
            raise ValueError
        cutoff = datetime.fromisoformat(f"{cutoff_at_utc[:-1]}+00:00")
    except (AttributeError, ValueError) as exc:
        raise ResearchWindowError("research_window_invalid", "cutoff_at_utc is invalid") from exc
    if cutoff.utcoffset() != timezone.utc.utcoffset(cutoff) or observed > cutoff:
        fail("research_window_coverage_missing", "dataset was observed after cutoff")

    raw_rows = evidence.get("candidate_snapshots")
    if not isinstance(raw_rows, list):
        fail("research_window_conflict", "candidate snapshots are invalid")
    scoped: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            fail("research_window_conflict", "candidate row is invalid")
        row = dict(raw)
        identity = resolve_symbol_identity(row.get("symbol"))
        row_mode = str(row.get("mode") or row.get("option_type") or "").lower()
        family = str(row.get("strategy_family") or row.get("strategy") or "").lower()
        source_path = str(row.get("source_path") or "").lower()
        if (
            str(row.get("account") or "").lower() == account
            and identity is not None
            and identity.market == market
            and row_mode == "put"
            and family == "sell_put"
            and not source_path.endswith("_labeled.csv")
        ):
            scoped.append(row)
    if any(str(row.get("status") or "").lower() in {"partial_data", "data_unavailable"} for row in scoped):
        fail("research_window_coverage_missing", "candidate point is incomplete")
    if scoped and isinstance(coverage, Mapping) and not matching_coverage:
        fail("research_window_conflict", "candidate rows are outside the manifest account scope")
    if not scoped:
        account_path = f"/accounts/{account}/"
        candidate_paths = source.get("candidate_paths")
        legacy_scope = isinstance(candidate_paths, list) and any(
            isinstance(path, str) and account_path in path and "_sell_put_candidates" in path and ".hk_" in path.lower()
            for path in candidate_paths
        )
        if not modern_empty_scope and not legacy_scope:
            fail("research_window_not_applicable", "dataset is outside the requested candidate universe")
    accepted = [row for row in scoped if str(row.get("status") or "").lower() == "accepted"]

    expected_by_kind = {
        str(item.get("kind")): item
        for item in expected_source_files or []
        if isinstance(item, Mapping)
    }
    if expected_by_kind:
        context_refs = {
            kind: str(expected_by_kind.get(kind, {}).get("ref") or "")
            for kind in ("prepared_option_manifest", "prepared_option_payload")
        }
    else:
        if runs_root_ref is None:
            fail(
                "research_window_coverage_missing",
                "prepared option-market evidence is missing",
            )
        runs_ref, _runs_path = safe_ref(runs_root_ref, "runs_root_ref")
        state_ref = f"{runs_ref}/{run_id}/accounts/{account}/state"
        context_refs = {
            "prepared_option_manifest": (
                f"{state_ref}/{PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2}"
            ),
            "prepared_option_payload": (
                f"{state_ref}/{PREPARED_OPTION_POSITIONS_PAYLOAD_NAME}"
            ),
        }
    contexts: dict[str, dict[str, Any]] = {}
    for kind, context_ref in context_refs.items():
        context_ref, context_path = safe_ref(context_ref, f"{kind}.ref")
        entry = source_file(kind, context_ref, context_path)
        files.append(entry)
        try:
            payload = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResearchWindowError(
                "research_window_coverage_missing",
                f"{kind} cannot be read",
            ) from exc
        if not isinstance(payload, dict):
            fail("research_window_coverage_missing", f"{kind} is invalid")
        contexts[kind] = payload
    prepared_manifest = contexts["prepared_option_manifest"]
    option_payload = contexts["prepared_option_payload"]
    payload_file = next(
        item for item in files if item["kind"] == "prepared_option_payload"
    )
    if (
        prepared_manifest.get("schema_version")
        != PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2
        or prepared_manifest.get("status") != "ready"
        or prepared_manifest.get("run_id") != run_id
        or prepared_manifest.get("account") != account
        or prepared_manifest.get("payload_relpath")
        != PREPARED_OPTION_POSITIONS_PAYLOAD_NAME
        or prepared_manifest.get("payload_sha256") != payload_file["sha256"]
    ):
        fail(
            "research_window_coverage_missing",
            "prepared option-market evidence is not ready",
        )
    try:
        option_market_evidence = validate_strategy_lab_option_market_evidence(
            option_payload.get("strategy_lab_option_market_evidence"),
            manifest=prepared_manifest,
        )
        evidence_at = datetime.fromisoformat(
            str(option_market_evidence["evidence_at_utc"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError, PreparedOptionPositionsContextError) as exc:
        raise ResearchWindowError(
            "research_window_coverage_missing",
            "prepared option-market evidence is invalid",
        ) from exc
    if (
        option_market_evidence.get("status") != "ready"
        or evidence_at.utcoffset() != timezone.utc.utcoffset(evidence_at)
        or evidence_at > cutoff
    ):
        fail(
            "research_window_coverage_missing",
            "prepared option-market evidence is not usable",
        )
    evidence_at_ms = int(evidence_at.timestamp() * 1000)
    opening_fx_by_currency = {
        str(rate.get("base_currency")): build_fx_rate_binding_from_projection(
            rate,
            selected_at_ms=evidence_at_ms,
        )
        for rate in option_market_evidence["fx_rate_facts"]
    }

    normalized: dict[str, dict[str, Any]] = {}
    cutoff_date = cutoff.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    for row in accepted:
        identity = resolve_symbol_identity(row.get("symbol"))
        if identity is None:
            fail("research_window_coverage_missing", "candidate symbol is invalid")
        contract = str(row.get("contract_symbol") or "").strip().upper()
        if not contract:
            fail("research_window_coverage_missing", "contract symbol is missing")
        try:
            expiration = date.fromisoformat(str(row.get("expiration") or ""))
        except ValueError as exc:
            raise ResearchWindowError("research_window_coverage_missing", "candidate expiration is invalid") from exc
        if expiration > cutoff_date:
            fail("research_window_coverage_missing", "candidate outcome is not mature")
        strike = number(row.get("strike"), "strike", positive=True)
        multiplier_value = number(row.get("multiplier"), "multiplier", positive=True)
        if not multiplier_value.is_integer():
            fail("research_window_coverage_missing", "multiplier is invalid")
        multiplier = int(multiplier_value)
        spot = number(row.get("spot"), "spot", positive=True)
        net_premium = number(
            row.get("net_premium") if row.get("net_premium") is not None else row.get("net_income"),
            "net_premium",
            positive=True,
        )
        net_cash_basis = number(
            row.get("net_cash_basis") if row.get("net_cash_basis") is not None else strike * multiplier - net_premium,
            "net_cash_basis",
            positive=True,
        )
        raw_sell_limit = row.get("sell_limit")
        if raw_sell_limit is None:
            mid = number(row.get("mid"), "mid", positive=True)
            try:
                fee_estimate = estimate_futu_option_sell_fee(
                    identity.currency,
                    mid,
                    contracts=1,
                    multiplier=int(multiplier),
                )
                sell_limit = (net_premium + fee_estimate.amount) / multiplier
                fee_estimate = estimate_futu_option_sell_fee(
                    identity.currency,
                    sell_limit,
                    contracts=1,
                    multiplier=int(multiplier),
                )
                sell_limit = (net_premium + fee_estimate.amount) / multiplier
            except ValueError as exc:
                raise ResearchWindowError(
                    "research_window_coverage_missing",
                    "historical sell limit cannot be reconstructed",
                ) from exc
        else:
            sell_limit = number(raw_sell_limit, "sell_limit", positive=True)
            try:
                fee_estimate = estimate_futu_option_sell_fee(
                    identity.currency,
                    sell_limit,
                    contracts=1,
                    multiplier=int(multiplier),
                )
            except ValueError as exc:
                raise ResearchWindowError(
                    "research_window_coverage_missing",
                    "historical fee contract is unsupported",
                ) from exc
        try:
            concentration = calculate_option_market_concentration_after(
                candidate={
                    "symbol": identity.canonical,
                    "sell_limit": sell_limit,
                    "multiplier": multiplier,
                    "currency": identity.currency,
                },
                open_option_positions=list(
                    option_market_evidence["open_option_positions"]
                ),
                valuation_mark_facts=list(
                    option_market_evidence["valuation_mark_facts"]
                ),
                fx_rate_facts=list(option_market_evidence["fx_rate_facts"]),
            )
        except (TypeError, ValueError) as exc:
            raise ResearchWindowError(
                "research_window_coverage_missing",
                "option-market concentration cannot be evaluated",
            ) from exc
        opening_fx_binding = opening_fx_by_currency.get(identity.currency)
        if identity.currency != "CNY" and opening_fx_binding is None:
            fail("research_window_coverage_missing", "opening FX binding is missing")
        opening_rate = (
            1.0
            if opening_fx_binding is None
            else float(opening_fx_binding["rate"])
        )
        period_return = net_premium / net_cash_basis
        discount = (spot - (strike - net_premium / multiplier)) / spot
        net_income_cny = net_premium * opening_rate
        dte = number(row.get("dte"), "dte", positive=True)
        candidate = {
            "candidate_id": contract,
            "symbol": identity.canonical,
            "contract_symbol": contract,
            "expiration": expiration.isoformat(),
            "option_type": "put",
            "stock_owner": identity.futu_code,
            "strike": strike,
            "spot": spot,
            "dte": dte,
            "sell_limit": sell_limit,
            "multiplier": multiplier,
            "currency": identity.currency,
            "open_interest": row.get("open_interest"),
            "volume": row.get("volume"),
            "spread_ratio": row.get("spread_ratio"),
            "period_net_return_on_cash_basis": period_return,
            "annualized_net_return_on_cash_basis": (period_return * 365.0 / dte),
            "net_assignment_discount_pct": discount,
            "option_market_concentration_after": concentration[
                "option_market_concentration_after"
            ],
            "option_market_value_cny": concentration["option_market_value_cny"],
            "option_market_concentration_metric_version": concentration[
                "metric_version"
            ],
            "option_market_evidence_refs": concentration["evidence_refs"],
            "opening_fx_binding": opening_fx_binding,
            "net_income": net_premium,
            "net_premium": net_premium,
            "net_cash_basis": net_cash_basis,
            "net_income_cny": net_income_cny,
            "fee_schedule_version": fee_estimate.fee_schedule_version,
            "fee_basis": fee_estimate.fee_basis,
            "fee_schedule_url": fee_estimate.fee_schedule_url,
        }
        previous = normalized.get(contract)
        if previous is not None:
            fail("research_window_conflict", "candidate is duplicated")
        normalized[contract] = candidate

    files = sorted(files, key=lambda item: (item["kind"], item["ref"]))
    if expected_source_files is not None:
        if not isinstance(expected_source_files, list) or files != expected_source_files:
            fail("research_window_conflict", "historical source file binding changed")
    candidates = [normalized[key] for key in sorted(normalized)]
    point_id = canonical_sha256(
        {
            "market": market,
            "account": account,
            "run_id": run_id,
            "candidate_file_sha256": files[
                next(index for index, item in enumerate(files) if item["kind"] == "candidate_snapshots")
            ]["sha256"],
        }
    )
    return {
        "recommendation_point_id": point_id,
        "run_id": run_id,
        "observed_at_utc": observed.isoformat().replace("+00:00", "Z"),
        "source_kind": source_kind,
        "dataset_ref": ref,
        "source_files": files,
        "candidate_facts_sha256": canonical_sha256(candidates),
        "trading_date": observed.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat(),
        "candidates": candidates,
    }


def build_research_window(
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    cutoff_at_utc: str,
    latest_mature_trading_date: str,
    market_calendar: Mapping[str, Any],
    datasets_root_ref: str,
    runs_root_ref: str,
    required_days: int = RESEARCH_REQUIRED_DAYS,
) -> dict[str, Any]:
    """Build, but do not publish, one deterministic historical research window."""

    if market != "HK" or not isinstance(account, str) or _ACCOUNT.fullmatch(account) is None:
        raise ResearchWindowError("research_window_invalid", "historical research requires HK/lowercase account")
    if not isinstance(market_calendar, Mapping):
        raise ResearchWindowError("research_window_invalid", "market_calendar must be a verified binding")
    try:
        calendar = read_bound_market_calendar_snapshot(
            artifact_root,
            market=market,
            snapshot_ref=market_calendar["snapshot_ref"],
            snapshot_content_sha256=market_calendar["snapshot_content_sha256"],
            snapshot_file_sha256=market_calendar["snapshot_file_sha256"],
        )
    except (CorpusError, KeyError) as exc:
        raise ResearchWindowError("research_window_invalid", "market calendar binding is invalid") from exc
    if calendar["market_calendar_version"] != market_calendar.get("market_calendar_version"):
        raise ResearchWindowError("research_window_conflict", "market calendar version changed")
    selected_dates = _select_complete_days(calendar["trading_dates"], latest_mature_trading_date, required_days)
    try:
        parsed_cutoff = datetime.fromisoformat(cutoff_at_utc.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ResearchWindowError("research_window_invalid", "cutoff_at_utc must be an ISO timestamp") from exc
    if not cutoff_at_utc.endswith("Z") or parsed_cutoff.utcoffset() != timezone.utc.utcoffset(parsed_cutoff):
        raise ResearchWindowError("research_window_invalid", "cutoff_at_utc must be UTC")

    research_root = private_path(artifact_root).parent.resolve()
    parts = datasets_root_ref.split("/") if isinstance(datasets_root_ref, str) else []
    if not parts or "\\" in datasets_root_ref or any(part in {"", ".", ".."} for part in parts):
        raise ResearchWindowError("research_window_invalid", "datasets_root_ref is invalid")
    datasets_root = research_root.joinpath(*parts).resolve()
    if not datasets_root.is_relative_to(research_root) or not datasets_root.is_dir():
        raise ResearchWindowError("research_window_coverage_missing", "datasets root is unavailable")
    selected_set = set(selected_dates)
    points_by_day: dict[str, list[dict[str, Any]]] = {trading_date: [] for trading_date in selected_dates}
    seen_runs: set[str] = set()
    for dataset_dir in sorted(datasets_root.iterdir(), key=lambda path: path.name):
        matched = _DATASET_STAMP.search(dataset_dir.name)
        if matched is None or not dataset_dir.is_dir():
            continue
        observed = datetime.strptime(matched.group("stamp").upper(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        trading_date = observed.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        if trading_date not in selected_set:
            continue
        dataset_ref = dataset_dir.relative_to(research_root).as_posix()
        try:
            point = _normalize_point(
                artifact_root,
                dataset_ref,
                market=market,
                account=account,
                cutoff_at_utc=cutoff_at_utc,
                runs_root_ref=runs_root_ref,
            )
        except ResearchWindowError as exc:
            if exc.reason_code == "research_window_not_applicable":
                continue
            raise
        point_date = point["trading_date"]
        if point_date not in selected_set:
            continue
        if point["run_id"] in seen_runs:
            raise ResearchWindowError("research_window_conflict", "historical run is duplicated")
        seen_runs.add(point["run_id"])
        points_by_day[point_date].append(point)

    days: list[dict[str, Any]] = []
    for trading_date in selected_dates:
        points = sorted(
            points_by_day[trading_date],
            key=lambda item: (item["observed_at_utc"], item["recommendation_point_id"]),
        )
        if not points:
            raise ResearchWindowError(
                "research_window_coverage_missing",
                f"no verified observed point for {trading_date}",
            )
        days.append(
            {
                "trading_date": trading_date,
                "points": [{key: point[key] for key in _POINT_KEYS} for point in points],
            }
        )

    payload: dict[str, Any] = {
        "schema_version": HISTORICAL_RESEARCH_WINDOW_SCHEMA,
        "market": market,
        "account": account,
        "cutoff_at_utc": cutoff_at_utc,
        "required_days": required_days,
        "market_calendar_version": calendar["market_calendar_version"],
        "market_calendar_ref": calendar["snapshot_ref"],
        "market_calendar_content_sha256": calendar["snapshot_content_sha256"],
        "market_calendar_file_sha256": calendar["snapshot_file_sha256"],
        "latest_mature_trading_date": latest_mature_trading_date,
        "selected_trading_dates": selected_dates,
        "candidate_universe": _CANDIDATE_UNIVERSE,
        "point_completeness": _POINT_COMPLETENESS,
        "days": days,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload


def load_research_window(
    artifact_root: str | Path,
    window: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Verify source hashes and materialize candidates in memory for evaluation."""

    if not isinstance(window, Mapping) or set(window) != _WINDOW_KEYS:
        raise ResearchWindowError("research_window_conflict", "historical research window keys are invalid")
    item = dict(window)
    if item["schema_version"] != HISTORICAL_RESEARCH_WINDOW_SCHEMA:
        raise ResearchWindowError("research_window_conflict", "historical research window schema changed")
    content_hash = item.get("content_sha256")
    if (
        not isinstance(content_hash, str)
        or _HASH_64.fullmatch(content_hash) is None
        or canonical_sha256({key: value for key, value in item.items() if key != "content_sha256"}) != content_hash
    ):
        raise ResearchWindowError("research_window_conflict", "historical research window hash changed")
    try:
        calendar = read_bound_market_calendar_snapshot(
            artifact_root,
            market=item["market"],
            snapshot_ref=item["market_calendar_ref"],
            snapshot_content_sha256=item["market_calendar_content_sha256"],
            snapshot_file_sha256=item["market_calendar_file_sha256"],
        )
    except (CorpusError, KeyError) as exc:
        raise ResearchWindowError("research_window_conflict", "bound market calendar changed") from exc
    selected_dates = _select_complete_days(
        calendar["trading_dates"],
        item["latest_mature_trading_date"],
        item["required_days"],
    )
    if (
        item["market"] != "HK"
        or not isinstance(item["account"], str)
        or item["account"] != item["account"].lower()
        or calendar["market_calendar_version"] != item["market_calendar_version"]
        or item["selected_trading_dates"] != selected_dates
        or item["candidate_universe"] != _CANDIDATE_UNIVERSE
        or item["point_completeness"] != _POINT_COMPLETENESS
    ):
        raise ResearchWindowError("research_window_conflict", "historical research window binding changed")
    raw_days = item["days"]
    if not isinstance(raw_days, list) or len(raw_days) != RESEARCH_REQUIRED_DAYS:
        raise ResearchWindowError("research_window_coverage_missing", "historical day coverage is incomplete")
    materialized: list[dict[str, Any]] = []
    seen_points: set[str] = set()
    for index, raw_day in enumerate(raw_days):
        if not isinstance(raw_day, Mapping) or set(raw_day) != _DAY_KEYS:
            raise ResearchWindowError("research_window_conflict", "historical day is invalid")
        trading_date = raw_day["trading_date"]
        points = raw_day["points"]
        if trading_date != selected_dates[index] or not isinstance(points, list) or not points:
            raise ResearchWindowError("research_window_coverage_missing", "historical point coverage is incomplete")
        for raw_point in points:
            if not isinstance(raw_point, Mapping) or set(raw_point) != _POINT_KEYS:
                raise ResearchWindowError("research_window_conflict", "historical point is invalid")
            point = dict(raw_point)
            normalized = _normalize_point(
                artifact_root,
                point["dataset_ref"],
                market=item["market"],
                account=item["account"],
                cutoff_at_utc=item["cutoff_at_utc"],
                expected_source_files=point["source_files"],
            )
            binding = {key: normalized[key] for key in _POINT_KEYS}
            if binding != point or normalized["trading_date"] != trading_date:
                raise ResearchWindowError("research_window_conflict", "historical point binding changed")
            point_id = str(point["recommendation_point_id"])
            if point_id in seen_points:
                raise ResearchWindowError("research_window_conflict", "historical point is duplicated")
            seen_points.add(point_id)
            materialized.append(
                {
                    "trading_date": trading_date,
                    "recommendation_point_id": point_id,
                    "candidates": normalized["candidates"],
                }
            )
    return materialized


__all__ = [
    "ResearchWindowError",
    "build_research_window",
    "load_research_window",
]
