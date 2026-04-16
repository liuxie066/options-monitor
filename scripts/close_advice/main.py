from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runner import run_from_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build close-advice report from open option positions")
    parser.add_argument("--config", required=True)
    parser.add_argument("--context", required=True, help="Path to option_positions_context.json")
    parser.add_argument("--required-data-root", required=True, help="Root containing parsed/*_required_data.csv")
    parser.add_argument("--output-dir", required=True, help="Directory for close_advice.csv/txt")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    base = Path(__file__).resolve().parents[2]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (base / config_path).resolve()
    context_path = Path(args.context)
    if not context_path.is_absolute():
        context_path = (base / context_path).resolve()
    required_data_root = Path(args.required_data_root)
    if not required_data_root.is_absolute():
        required_data_root = (base / required_data_root).resolve()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = (base / output_dir).resolve()

    result = run_from_paths(
        config_path=config_path,
        context_path=context_path,
        required_data_root=required_data_root,
        output_dir=output_dir,
        base_dir=base,
    )
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
