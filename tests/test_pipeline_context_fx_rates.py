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


def test_load_fx_rates_fetches_latest_when_cache_missing(monkeypatch, tmp_path: Path) -> None:
    from scripts import pipeline_context as ctx

    base = Path(__file__).resolve().parents[1]
    account_state = tmp_path / "account_state"
    account_state.mkdir()

    class _FxMod:
        @staticmethod
        def get_rates_or_fetch_latest(*, cache_path, shared_cache_path=None, max_age_hours=None, log=None):
            return {"rates": {"USDCNY": 7.25, "HKDCNY": 0.93}}

    def _fake_spec_from_file_location(_name, _path):
        class _Loader:
            @staticmethod
            def exec_module(mod):
                mod.get_rates_or_fetch_latest = _FxMod.get_rates_or_fetch_latest

        class _Spec:
            loader = _Loader()

        return _Spec()

    monkeypatch.setattr("importlib.util.spec_from_file_location", _fake_spec_from_file_location)
    monkeypatch.setattr("importlib.util.module_from_spec", lambda _spec: type("FxModule", (), {})())

    fx_usd_per_cny, hkdcny = ctx.load_fx_rates(
        base=base,
        state_dir=account_state,
        log=lambda _msg: None,
    )

    assert round(fx_usd_per_cny or 0.0, 8) == round(1.0 / 7.25, 8)
    assert hkdcny == 0.93
