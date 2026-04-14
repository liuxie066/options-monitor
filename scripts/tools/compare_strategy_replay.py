#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain.engine import (
    build_strategy_config,
    rank_scored_candidates,
    sort_columns,
)


def _legacy_rank(df: pd.DataFrame, mode: str, top_n: int) -> pd.DataFrame:
    cols = [c for c in sort_columns(mode) if c in df.columns]
    if not cols:
        ranked = df.copy()
    else:
        ranked = df.sort_values(cols, ascending=[False] * len(cols))
    return ranked.head(top_n)


def _new_rank(df: pd.DataFrame, mode: str, top_n: int) -> pd.DataFrame:
    cfg = build_strategy_config(mode)
    return rank_scored_candidates(df, cfg, layered=False, top=top_n)


def _top_signature(df: pd.DataFrame) -> list[str]:
    out: list[str] = []
    for _, row in df.iterrows():
        out.append(
            "|".join(
                [
                    str(row.get("symbol") or ""),
                    str(row.get("expiration") or ""),
                    str(row.get("strike") or ""),
                    str(row.get("contract_symbol") or ""),
                ]
            )
        )
    return out


def _risk_dist(df: pd.DataFrame) -> dict[str, int]:
    if df.empty or "risk_label" not in df.columns:
        return {}
    vc = df["risk_label"].astype(str).value_counts(dropna=False).to_dict()
    return {str(k): int(v) for k, v in vc.items()}


def _detect_mode(path: Path) -> str:
    name = path.name.lower()
    if "sell_call_candidates" in name:
        return "call"
    return "put"


def _collect_samples(run_root: Path, sample_size: int) -> list[Path]:
    files = list(run_root.glob("*/accounts/*/*_sell_put_candidates_labeled.csv"))
    files.extend(list(run_root.glob("*/accounts/*/*_sell_call_candidates.csv")))
    files = [p for p in files if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:sample_size]


def _parse_run_account(path: Path) -> tuple[str, str]:
    parts = path.parts
    run_id = ""
    account = ""
    for i, p in enumerate(parts):
        if p == "output_runs" and i + 1 < len(parts):
            run_id = parts[i + 1]
        if p == "accounts" and i + 1 < len(parts):
            account = parts[i + 1]
    return run_id, account


def _build_markdown(summary: dict, rows: list[dict]) -> str:
    lines = []
    lines.append("# Strategy Replay Compare Report")
    lines.append("")
    lines.append(f"- generated_at_utc: {summary['generated_at_utc']}")
    lines.append(f"- sample_size: {summary['sample_size']}")
    lines.append(f"- top_n: {summary['top_n']}")
    lines.append(f"- topn_changed_samples: {summary['topn_changed_samples']}")
    lines.append("")
    lines.append("| sample | run_id | account | mode | candidates | topN_changed | legacy_risk | new_risk |")
    lines.append("|---|---|---|---:|---:|---:|---|---|")
    for r in rows:
        lines.append(
            "| {sample} | {run_id} | {account} | {mode} | {candidate_count} | {topn_changed} | {legacy_risk} | {new_risk} |".format(
                sample=r["sample"],
                run_id=r["run_id"],
                account=r["account"],
                mode=r["mode"],
                candidate_count=r["candidate_count"],
                topn_changed=("Y" if r["topn_changed"] else "N"),
                legacy_risk=json.dumps(r["legacy_topn_risk"], ensure_ascii=False),
                new_risk=json.dumps(r["new_topn_risk"], ensure_ascii=False),
            )
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare legacy vs new strategy ranking on reproducible run snapshots")
    parser.add_argument("--run-root", default="output_runs", help="run root dir (default: output_runs)")
    parser.add_argument("--sample-size", type=int, default=8, help="sample file count (5~10 recommended)")
    parser.add_argument("--top-n", type=int, default=5, help="top N for comparison")
    parser.add_argument("--output-json", default=None, help="output json path")
    parser.add_argument("--output-md", default=None, help="output markdown path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base = Path(__file__).resolve().parents[2]
    run_root = Path(args.run_root)
    if not run_root.is_absolute():
        run_root = (base / run_root).resolve()
    if not run_root.exists():
        raise SystemExit(f"run root not found: {run_root}")

    sample_files = _collect_samples(run_root, max(1, int(args.sample_size)))
    rows: list[dict] = []
    topn_changed_samples = 0
    for path in sample_files:
        mode = _detect_mode(path)
        run_id, account = _parse_run_account(path)
        try:
            df = pd.read_csv(path)
        except Exception:
            df = pd.DataFrame()

        legacy_top = _legacy_rank(df, mode, int(args.top_n))
        new_top = _new_rank(df, mode, int(args.top_n))

        legacy_sig = _top_signature(legacy_top)
        new_sig = _top_signature(new_top)
        changed = legacy_sig != new_sig
        if changed:
            topn_changed_samples += 1

        rows.append(
            {
                "sample": path.name,
                "path": str(path),
                "run_id": run_id,
                "account": account,
                "mode": mode,
                "candidate_count": int(len(df)),
                "legacy_topn": legacy_sig,
                "new_topn": new_sig,
                "topn_changed": changed,
                "legacy_topn_risk": _risk_dist(legacy_top),
                "new_topn_risk": _risk_dist(new_top),
            }
        )

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(rows),
        "top_n": int(args.top_n),
        "topn_changed_samples": int(topn_changed_samples),
    }
    payload = {"summary": summary, "rows": rows}

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_dir = (base / "output" / "reports").resolve()
    default_dir.mkdir(parents=True, exist_ok=True)
    output_json = Path(args.output_json).resolve() if args.output_json else (default_dir / f"strategy_replay_compare_{ts}.json")
    output_md = Path(args.output_md).resolve() if args.output_md else (default_dir / f"strategy_replay_compare_{ts}.md")

    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    output_md.write_text(_build_markdown(summary, rows), encoding="utf-8")

    print(f"[DONE] json -> {output_json}")
    print(f"[DONE] md   -> {output_md}")
    print(f"[DONE] samples={len(rows)} topn_changed={topn_changed_samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
