from __future__ import annotations

"""Canonical Combo-owned Funding Put preparation inside an isolated dataset."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_PUT_WINDOW,
    resolve_candidate_liquidity,
    resolve_candidate_window,
)
from src.application.combo_yield_steps import (
    enrich_and_filter_combo_funding_cash,
    run_combo_yield_scan_and_summarize,
)
from src.application.exchange_rate_loader import build_converter
from src.application.shadow_replay.common import (
    dataset_dir_from_arg,
    dataset_write_lock,
    refresh_dataset_manifest,
    safety_payload,
    text,
    validate_dataset_integrity,
    write_json,
)
from src.application.yield_enhancement_config import derive_yield_enhancement_policy


def prepare_combo_funding_puts(
    *,
    dataset: str | Path,
    portfolio_context_path: str | Path,
    usd_per_cny_exchange_rate: float | None = None,
    cny_per_hkd_exchange_rate: float | None = None,
    write: bool = False,
) -> dict[str, Any]:
    """Run canonical scan/event/cash/underwriting against captured required-data."""

    dataset_dir = dataset_dir_from_arg(dataset)
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    context_path = Path(portfolio_context_path).expanduser().resolve()
    if not context_path.is_file():
        raise ValueError(f"portfolio context does not exist: {context_path}")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    if not isinstance(context, dict):
        raise ValueError("portfolio context must be a JSON object")
    output_path = dataset_dir / "combo_owned_underwritten_puts.csv"
    receipt_path = dataset_dir / "combo_owned_underwritten_puts.source.json"
    preview = {
        "schema_version": "shadow_combo_funding_put_preparation.v1",
        "dataset_dir": str(dataset_dir),
        "symbols": list(manifest.get("symbols") or []),
        "portfolio_context_path": str(context_path),
        "output_path": str(output_path),
        "receipt_path": str(receipt_path),
        "written": bool(write),
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if not write:
        return preview
    with dataset_write_lock(dataset_dir):
        validate_dataset_integrity(dataset_dir)
        report_root = dataset_dir / "combo_funding_pipeline"
        report_root.mkdir(parents=True, exist_ok=True)
        converter = build_converter(
            usd_per_cny_exchange_rate=usd_per_cny_exchange_rate,
            cny_per_hkd_exchange_rate=cny_per_hkd_exchange_rate,
        )
        frames: list[pd.DataFrame] = []
        for raw_symbol in manifest.get("symbols") or []:
            symbol = text(raw_symbol).upper()
            combo_cfg = dict(
                (manifest.get("normalized_effective_combo_policy") or {}).get(symbol)
                or {}
            )
            combo_cfg["enabled"] = True
            sell_put_cfg = dict(
                (manifest.get("normalized_sell_put_policy") or {}).get(symbol)
                or {}
            )
            policy = derive_yield_enhancement_policy(combo_cfg)
            funding_put_cfg = dict(sell_put_cfg)
            funding_put_cfg["strategy"] = policy.derived_from_sell_put_strategy
            symbol_report = report_root / symbol.lower()
            symbol_report.mkdir(parents=True, exist_ok=True)
            run_combo_yield_scan_and_summarize(
                base=dataset_dir,
                sym=symbol,
                symbol=symbol,
                symbol_lower=symbol.lower(),
                symbol_cfg={
                    "symbol": symbol,
                    "sell_put": sell_put_cfg,
                    "combo_yield": combo_cfg,
                    "_global_yield_enhancement_liquidity": (
                        (manifest.get("normalized_global_combo_liquidity") or {}).get(symbol)
                        or {}
                    ),
                },
                yield_enhancement_cfg=combo_cfg,
                yield_sp=funding_put_cfg,
                yield_enhancement_policy=policy,
                df_sell_put_labeled=pd.DataFrame(),
                sell_put_labeled_path=symbol_report / f"{symbol.lower()}_sell_put_candidates_labeled.csv",
                required_data_dir=dataset_dir / "required_data",
                report_dir=symbol_report,
                yield_window=resolve_candidate_window(
                    sell_put_cfg,
                    defaults=DEFAULT_SELL_PUT_WINDOW,
                ),
                liquidity=resolve_candidate_liquidity(
                    (manifest.get("normalized_global_sell_put_liquidity") or {}).get(symbol)
                    or {}
                ),
                exchange_rate_converter=converter,
                portfolio_ctx=context,
                top_n=1000,
                is_scheduled=False,
                cash_filter_put_candidates_fn=enrich_and_filter_combo_funding_cash,
            )
            underwritten = (
                symbol_report
                / f"{symbol.lower()}_combo_yield_put_universe_underwritten.csv"
            )
            if underwritten.is_file() and underwritten.stat().st_size > 0:
                frames.append(pd.read_csv(underwritten))
        combined = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=["symbol", "contract_symbol"])
        )
        combined.to_csv(output_path, index=False)
        receipt = {
            "schema_version": "shadow_combo_funding_put_source.v1",
            "dataset_id": manifest.get("dataset_id"),
            "required_data_file_sha256": manifest.get("required_data_file_sha256"),
            "portfolio_context_sha256": _file_sha256(context_path),
            "underwritten_put_sha256": _file_sha256(output_path),
            "row_count": len(combined),
            "stages": [
                "combo_owned_put_scan",
                "event_gate",
                "cash_gate",
                "insurance_underwriting",
            ],
        }
        write_json(receipt_path, receipt)
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["combo_funding_put_facet"] = {
            "output_path": str(output_path),
            "receipt_path": str(receipt_path),
            **receipt,
        }
        write_json(dataset_dir / "manifest.json", manifest)
        preview.update(
            {
                "row_count": len(combined),
                "underwritten_put_sha256": receipt["underwritten_put_sha256"],
                "dataset_integrity": refresh_dataset_manifest(dataset_dir)["integrity"],
            }
        )
    return preview


def validate_combo_funding_put_source(
    *,
    dataset: str | Path,
    underwritten_put_path: str | Path,
) -> dict[str, Any]:
    dataset_dir = dataset_dir_from_arg(dataset)
    source = Path(underwritten_put_path).expanduser().resolve()
    receipt_path = dataset_dir / "combo_owned_underwritten_puts.source.json"
    if source != (dataset_dir / "combo_owned_underwritten_puts.csv").resolve():
        raise ValueError("underwritten Put artifact must be the dataset-owned canonical output")
    if not receipt_path.is_file():
        raise ValueError(f"Combo Funding Put source receipt is missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    if receipt.get("dataset_id") != manifest.get("dataset_id"):
        raise ValueError("Combo Funding Put receipt dataset_id mismatch")
    if receipt.get("required_data_file_sha256") != manifest.get("required_data_file_sha256"):
        raise ValueError("Combo Funding Put receipt required-data scope mismatch")
    if receipt.get("underwritten_put_sha256") != _file_sha256(source):
        raise ValueError("Combo Funding Put artifact hash mismatch")
    return receipt


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "prepare_combo_funding_puts",
    "validate_combo_funding_put_source",
]
