from __future__ import annotations

import json
from pathlib import Path

from scripts.opend_utils import normalize_underlier, resolve_underlier_alias


def test_resolve_underlier_alias_uses_builtin_fallbacks() -> None:
    assert resolve_underlier_alias("POP") == "9992.HK"
    out = normalize_underlier("POP")
    assert out.market == "HK"
    assert out.code == "HK.09992"
    assert out.currency == "HKD"


def test_resolve_underlier_alias_prefers_runtime_config(tmp_path: Path) -> None:
    (tmp_path / "config.us.json").write_text(
        json.dumps({"intake": {"symbol_aliases": {"MELIHK": "3690.HK"}}}),
        encoding="utf-8",
    )

    assert resolve_underlier_alias("MELIHK", base_dir=tmp_path) == "3690.HK"
