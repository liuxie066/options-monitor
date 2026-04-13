from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


DELIVERY_PIPELINE_FILES = [
    BASE / "scripts" / "run_pipeline.py",
    BASE / "scripts" / "pipeline_symbol.py",
    BASE / "scripts" / "pipeline_watchlist.py",
    BASE / "scripts" / "pipeline_context.py",
    BASE / "scripts" / "pipeline_alert_steps.py",
    BASE / "scripts" / "pipeline_postprocess.py",
    BASE / "scripts" / "required_data_steps.py",
    BASE / "scripts" / "pipeline_fetch_models.py",
]


RAW_FETCH_PATTERNS = [
    re.compile(r"_required_data\.json"),
    re.compile(r"required_data_dir\s*/\s*['\"]raw['\"]"),
]


def test_delivery_pipeline_does_not_directly_access_raw_fetch_files() -> None:
    offenders: list[str] = []
    for path in DELIVERY_PIPELINE_FILES:
        text = path.read_text(encoding="utf-8")
        for pat in RAW_FETCH_PATTERNS:
            if pat.search(text):
                offenders.append(f"{path.relative_to(BASE)} :: {pat.pattern}")
    assert offenders == [], "raw-fetch direct access found:\n" + "\n".join(offenders)


def main() -> None:
    test_delivery_pipeline_does_not_directly_access_raw_fetch_files()
    print("OK (pipeline-delivery-raw-fetch-guard)")


if __name__ == "__main__":
    main()
