from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import symbol_market
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    publish_source_receipt,
    safe_existing_relative_path,
    sha256_bytes,
    source_snapshot_id,
    validate_source_receipt,
)
from src.infrastructure.io_utils import atomic_write_text


REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA = "required_data_quote_snapshot.v1"


REQUIRED_DATA_COLUMNS = [
    "symbol",
    "option_type",
    "expiration",
    "dte",
    "contract_symbol",
    "strike",
    "spot",
    "bid",
    "ask",
    "last_price",
    "mid",
    "volume",
    "open_interest",
    "implied_volatility",
    "realized_volatility_20",
    "realized_volatility_60",
    "realized_volatility_120",
    "realized_volatility_estimate",
    "in_the_money",
    "currency",
    "otm_pct",
    "delta",
    "multiplier",
]


def append_metrics_json(metrics_path: Path, payload: dict[str, Any], max_entries: int = 400) -> None:
    """Append payload into a bounded JSON list file. Keeps last max_entries records."""
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        arr = []
        if metrics_path.exists() and metrics_path.stat().st_size > 0:
            try:
                obj = json.loads(metrics_path.read_text(encoding="utf-8"))
                if isinstance(obj, list):
                    arr = obj
            except Exception:
                arr = []
        arr.append(payload)
        if len(arr) > int(max_entries):
            arr = arr[-int(max_entries) :]
        metrics_path.write_text(json.dumps(arr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def save_outputs(base: Path, symbol: str, payload: dict[str, Any], *, output_root: Path | None = None) -> tuple[Path, Path]:
    root = output_root.resolve() if output_root is not None else (base / "output_shared" / "required_data").resolve()
    raw_dir = root / "raw"
    parsed_dir = root / "parsed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{symbol}_required_data.json"
    csv_path = parsed_dir / f"{symbol}_required_data.csv"

    try:
        from src.application.required_data_validation import validate_required_rows

        rows0 = payload.get("rows") or []
        rows1, st = validate_required_rows(rows0)
        payload["rows"] = rows1
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {"meta": str(meta)}
        meta["validation"] = {
            "total_rows": int(st.total_rows),
            "kept_rows": int(st.kept_rows),
            "dropped_rows": int(st.dropped_rows),
            "missing_strike": int(st.missing_strike),
            "missing_expiration": int(st.missing_expiration),
            "missing_dte": int(st.missing_dte),
            "missing_option_type": int(st.missing_option_type),
        }
        payload["meta"] = meta
    except Exception:
        pass

    atomic_write_text(raw_path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    is_error_payload = str((meta or {}).get("status") or "").lower() in {"error", "fail", "failed"}
    if is_error_payload and csv_path.exists() and csv_path.stat().st_size > 0:
        return raw_path, csv_path

    df = pd.DataFrame(payload.get("rows") or [])
    if is_error_payload:
        df = pd.DataFrame()

    if df.empty:
        df_out = pd.DataFrame(columns=REQUIRED_DATA_COLUMNS)
    else:
        for column in REQUIRED_DATA_COLUMNS:
            if column not in df.columns:
                df[column] = pd.NA
        df_out = df[REQUIRED_DATA_COLUMNS]

    buf = io.StringIO()
    df_out.to_csv(buf, index=False)
    atomic_write_text(csv_path, buf.getvalue(), encoding="utf-8")
    return raw_path, csv_path


def publish_required_data_quote_snapshot(
    *,
    producer_root: Path,
    producer_run_id: str,
    symbol: str,
    raw_path: Path,
    csv_path: Path,
    fetch_plan: dict[str, Any],
    fetch_policy: dict[str, Any],
    source_observed_at: datetime | str,
    completed_at: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Bind the exact required-data JSON/CSV bytes and fetch policy immutably."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        raise PositionAdviceSourceError("quote producer root is invalid")
    root = root_input.resolve()
    raw_input = Path(raw_path).absolute()
    csv_input = Path(csv_path).absolute()
    try:
        raw_relpath = raw_input.relative_to(root).as_posix()
        csv_relpath = csv_input.relative_to(root).as_posix()
    except ValueError as exc:
        raise PositionAdviceSourceError(
            "required-data quote files escape producer root"
        ) from exc
    raw = safe_existing_relative_path(root, raw_relpath)
    csv = safe_existing_relative_path(root, csv_relpath)
    try:
        raw_payload = json.loads(raw.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PositionAdviceSourceError("required-data JSON is unreadable") from exc
    meta = raw_payload.get("meta") if isinstance(raw_payload, dict) else None
    status = str((meta or {}).get("status") or "").strip().lower()
    if status != "ok":
        raise PositionAdviceSourceError(
            "incomplete required-data payload cannot produce a quote receipt"
        )

    symbol_norm = str(symbol or "").strip().upper()
    run_id = str(producer_run_id or "").strip()
    market = str(symbol_market(symbol_norm) or "").strip().upper()
    if not symbol_norm or not run_id or market not in {"US", "HK"}:
        raise PositionAdviceSourceError(
            "quote producer run, symbol, or market is unavailable"
        )
    policy_payload = {
        "schema": "required_data_fetch_policy.v1",
        "fetch_plan": dict(fetch_plan or {}),
        "fetch_policy": dict(fetch_policy or {}),
    }
    policy_hash = canonical_sha256(policy_payload)
    bundle = {
        "schema_version": REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA,
        "symbol": symbol_norm,
        "market": market,
        "fetch_plan": policy_payload["fetch_plan"],
        "fetch_policy": policy_payload["fetch_policy"],
        "fetch_policy_hash": policy_hash,
        "raw_json_relpath": raw_relpath,
        "required_data_csv_relpath": csv_relpath,
        "raw_json_base64": base64.b64encode(raw.read_bytes()).decode("ascii"),
        "required_data_csv_base64": base64.b64encode(csv.read_bytes()).decode(
            "ascii"
        ),
    }
    bundle_bytes = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    bundle_hash = canonical_sha256(bundle)
    run_key = canonical_sha256({"producer_run_id": run_id})
    symbol_key = canonical_sha256({"symbol": symbol_norm})
    source_native_id = f"opend-required-data:{symbol_norm}:{bundle_hash}"
    snapshot_key = source_snapshot_id(
        source_kind="quotes",
        source_native_id=source_native_id,
        source_observed_at=source_observed_at,
        payload_sha256=sha256_bytes(bundle_bytes),
        producer_policy_hash=policy_hash,
    )
    prefix = (
        f"position_advice_sources/quotes/{run_key}/{symbol_key}/{snapshot_key}"
    )
    receipt = publish_source_receipt(
        producer_root=root,
        receipt_relpath=f"{prefix}/receipt.json",
        payload_relpath=f"{prefix}/payload.json",
        payload_bytes=bundle_bytes,
        source_kind="quotes",
        producer_schema_version=REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA,
        producer_run_id=run_id,
        producer_scope="market",
        producer_account_run_id=None,
        broker="futu",
        account=None,
        portfolio_account_identity_hash=None,
        included_markets=[market],
        source_native_id=source_native_id,
        source_observed_at=source_observed_at,
        completed_at=completed_at or datetime.now(timezone.utc),
        producer_policy_hash=policy_hash,
    )
    return root / f"{prefix}/receipt.json", receipt


def find_fresh_required_data_quote_receipts(
    *,
    producer_root: Path,
    symbols: list[str],
    now: datetime | str | None = None,
) -> dict[str, str]:
    """Discover the newest valid immutable receipt per symbol without refreshing it."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        return {}
    root = root_input.resolve()
    receipt_root = root / "position_advice_sources" / "quotes"
    if not receipt_root.exists():
        return {}
    expected = {str(symbol or "").strip().upper() for symbol in symbols}
    expected.discard("")
    found: dict[str, tuple[datetime, str]] = {}
    now_value = now or datetime.now(timezone.utc)
    for receipt_path in receipt_root.glob("*/*/*/receipt.json"):
        try:
            receipt_relpath = receipt_path.relative_to(root).as_posix()
            validated_receipt_path = safe_existing_relative_path(
                root,
                receipt_relpath,
            )
            receipt = json.loads(
                validated_receipt_path.read_text(encoding="utf-8")
            )
            validated = validate_source_receipt(
                receipt,
                producer_root=root,
                now=now_value,
                expected_source_kind="quotes",
            )
            native_id = str(receipt.get("source_native_id") or "")
            if not native_id.startswith("opend-required-data:"):
                continue
            symbol = native_id.split(":", 2)[1].strip().upper()
            if symbol not in expected:
                continue
            observed = datetime.fromisoformat(
                str(validated["source_observed_at"]).replace("Z", "+00:00")
            )
            current = found.get(symbol)
            if current is None or observed > current[0]:
                found[symbol] = (
                    observed,
                    receipt_relpath,
                )
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            PositionAdviceSourceError,
        ):
            continue
    return {symbol: item[1] for symbol, item in sorted(found.items())}


def resolve_exact_fresh_required_data_quote_receipt(
    *,
    producer_root: Path,
    symbol: str,
    now: datetime | str | None = None,
    expected_producer_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a fresh receipt only when it binds the exact current scan bytes."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        return None
    root = root_input.resolve()
    symbol_norm = str(symbol or "").strip().upper()
    if not symbol_norm:
        return None
    receipt_root = root / "position_advice_sources" / "quotes"
    raw_path = root / "raw" / f"{symbol_norm}_required_data.json"
    csv_path = root / "parsed" / f"{symbol_norm}_required_data.csv"
    if (
        not receipt_root.exists()
        or not raw_path.is_file()
        or raw_path.is_symlink()
        or not csv_path.is_file()
        or csv_path.is_symlink()
    ):
        return None
    try:
        raw_bytes = raw_path.read_bytes()
        csv_bytes = csv_path.read_bytes()
    except OSError:
        return None

    now_value = now or datetime.now(timezone.utc)
    matches: list[tuple[datetime, dict[str, Any]]] = []
    for receipt_path in receipt_root.glob("*/*/*/receipt.json"):
        try:
            receipt_relpath = receipt_path.relative_to(root).as_posix()
            validated_receipt_path = safe_existing_relative_path(
                root,
                receipt_relpath,
            )
            receipt_bytes = validated_receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes.decode("utf-8"))
            validated = validate_source_receipt(
                receipt,
                producer_root=root,
                now=now_value,
                expected_source_kind="quotes",
            )
            if (
                expected_producer_run_id is not None
                and str(validated.get("producer_run_id") or "").strip()
                != str(expected_producer_run_id).strip()
            ):
                continue
            payload = json.loads(
                validated["payload_path"].read_text(encoding="utf-8")
            )
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version")
                != REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA
                or str(payload.get("symbol") or "").strip().upper()
                != symbol_norm
            ):
                continue
            captured_raw = base64.b64decode(
                str(payload.get("raw_json_base64") or ""),
                validate=True,
            )
            captured_csv = base64.b64decode(
                str(payload.get("required_data_csv_base64") or ""),
                validate=True,
            )
            if captured_raw != raw_bytes or captured_csv != csv_bytes:
                continue
            observed = datetime.fromisoformat(
                str(validated["source_observed_at"]).replace("Z", "+00:00")
            )
            matches.append(
                (
                    observed,
                    {
                        "receipt_relpath": receipt_relpath,
                        "receipt_hash": sha256_bytes(receipt_bytes),
                        "snapshot_id": validated["snapshot_id"],
                        "payload_sha256": validated["payload_sha256"],
                        "source_observed_at": validated["source_observed_at"],
                        "expires_at": validated["expires_at"],
                    },
                )
            )
        except (
            OSError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
            PositionAdviceSourceError,
        ):
            continue
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]["snapshot_id"]))
    return matches[-1][1]


__all__ = [
    "REQUIRED_DATA_COLUMNS",
    "REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA",
    "append_metrics_json",
    "find_fresh_required_data_quote_receipts",
    "publish_required_data_quote_snapshot",
    "resolve_exact_fresh_required_data_quote_receipt",
    "save_outputs",
]
