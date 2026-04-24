from __future__ import annotations

import json
from pathlib import Path


def test_load_exchange_rates_fetches_latest_when_cache_missing(monkeypatch, tmp_path: Path) -> None:
    from scripts import pipeline_context as ctx

    base = Path(__file__).resolve().parents[1]
    account_state = tmp_path / "account_state"
    account_state.mkdir()

    class _FxMod:
        @staticmethod
        def get_exchange_rates_or_fetch_latest(*, cache_path, max_age_hours=None, log=None):
            return {"rates": {"USDCNY": 7.25, "HKDCNY": 0.93}}

    def _fake_spec_from_file_location(_name, _path):
        class _Loader:
            @staticmethod
            def exec_module(mod):
                mod.get_exchange_rates_or_fetch_latest = _FxMod.get_exchange_rates_or_fetch_latest

        class _Spec:
            loader = _Loader()

        return _Spec()

    monkeypatch.setattr("importlib.util.spec_from_file_location", _fake_spec_from_file_location)
    monkeypatch.setattr("importlib.util.module_from_spec", lambda _spec: type("FxModule", (), {})())

    usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = ctx.load_exchange_rates(
        base=base,
        state_dir=account_state,
        log=lambda _msg: None,
    )

    assert round(usd_per_cny_exchange_rate or 0.0, 8) == round(1.0 / 7.25, 8)
    assert cny_per_hkd_exchange_rate == 0.93
