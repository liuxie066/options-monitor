from __future__ import annotations

import json
from pathlib import Path


def test_load_fx_rates_falls_back_to_shared_state_dir_for_hkdcny(tmp_path: Path) -> None:
    from scripts.pipeline_context import load_fx_rates

    base = Path(__file__).resolve().parents[1]
    account_state = tmp_path / "output_runs" / "run1" / "accounts" / "lx" / "state"
    shared_state = tmp_path / "output_runs" / "run1" / "state"
    account_state.mkdir(parents=True)
    shared_state.mkdir(parents=True)
    (shared_state / "rate_cache.json").write_text(
        json.dumps({"rates": {"HKDCNY": 0.92}}, ensure_ascii=False),
        encoding="utf-8",
    )

    logs: list[str] = []
    _usd_per_cny, hkdcny = load_fx_rates(
        base=base,
        state_dir=account_state,
        shared_state_dir=shared_state,
        log=logs.append,
    )

    assert hkdcny == 0.92


def test_load_fx_rates_falls_back_to_legacy_shared_cache_for_hkdcny(tmp_path: Path) -> None:
    from scripts import pipeline_context as ctx

    base = Path(__file__).resolve().parents[1]
    account_state = tmp_path / "account_state"
    account_state.mkdir()
    shared_cache = base / "output_shared" / "state" / "rate_cache.json"
    original = shared_cache.read_text(encoding="utf-8") if shared_cache.exists() else None
    shared_cache.parent.mkdir(parents=True, exist_ok=True)

    try:
        shared_cache.write_text(
            json.dumps({"rates": {"HKDCNY": 0.91}}, ensure_ascii=False),
            encoding="utf-8",
        )
        _usd_per_cny, hkdcny = ctx.load_fx_rates(
            base=base,
            state_dir=account_state,
            log=lambda _msg: None,
        )
    finally:
        if original is None:
            shared_cache.unlink(missing_ok=True)
        else:
            shared_cache.write_text(original, encoding="utf-8")

    assert hkdcny == 0.91
