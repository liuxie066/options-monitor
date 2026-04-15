from __future__ import annotations

import sys
import types


class _FakeFastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def mount(self, *args, **kwargs):
        return None

    def get(self, *args, **kwargs):
        return lambda fn: fn

    def post(self, *args, **kwargs):
        return lambda fn: fn


def _install_fastapi_stubs() -> None:
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.FastAPI = _FakeFastAPI
    fastapi_mod.Request = object

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi_mod.HTTPException = _HTTPException

    responses_mod = types.ModuleType("fastapi.responses")
    responses_mod.FileResponse = object
    responses_mod.HTMLResponse = object

    staticfiles_mod = types.ModuleType("fastapi.staticfiles")

    class _StaticFiles:
        def __init__(self, *args, **kwargs):
            pass

    staticfiles_mod.StaticFiles = _StaticFiles

    sys.modules.setdefault("fastapi", fastapi_mod)
    sys.modules.setdefault("fastapi.responses", responses_mod)
    sys.modules.setdefault("fastapi.staticfiles", staticfiles_mod)


_install_fastapi_stubs()

from scripts.webui.server import SYMBOL_LEVEL_D3_FORBIDDEN_FIELDS, _clean_symbol_level_d3_fields, _patch_entry


def test_patch_entry_removes_forbidden_symbol_level_d3_fields() -> None:
    entry = {
        "symbol": "PDD",
        "sell_put": {
            "enabled": True,
            "min_dte": 7,
            "max_dte": 45,
            "min_strike": 0,
            "max_strike": 120,
            "require_bid_ask": True,
            "min_iv": 0.2,
            "max_iv": 1.2,
            "min_abs_delta": 0.1,
            "max_abs_delta": 0.3,
            "min_open_interest": 50,
            "min_volume": 10,
            "max_spread_ratio": 0.3,
            "d3_event": {"enabled": True},
        },
        "sell_call": {
            "enabled": False,
            "require_bid_ask": True,
            "min_iv": 0.2,
        },
    }

    _patch_entry(
        entry,
        {
            "sell_put_min_dte": 10,
            "sell_put_max_dte": 50,
            "sell_put_min_strike": 1,
            "sell_put_max_strike": 110,
        },
    )

    for field in SYMBOL_LEVEL_D3_FORBIDDEN_FIELDS:
        assert field not in entry["sell_put"]
        assert field not in entry["sell_call"]
    assert entry["sell_put"]["min_dte"] == 10
    assert entry["sell_put"]["max_dte"] == 50
    assert entry["sell_put"]["min_strike"] == 1.0
    assert entry["sell_put"]["max_strike"] == 110.0


def test_clean_symbol_level_d3_fields_removes_stale_keys_from_all_symbols() -> None:
    cfg = {
        "symbols": [
            {
                "symbol": "PDD",
                "sell_put": {
                    "enabled": True,
                    "require_bid_ask": True,
                    "min_iv": 0.2,
                },
            },
            {
                "symbol": "NVDA",
                "sell_call": {
                    "enabled": True,
                    "max_spread_ratio": 0.3,
                    "d3_event": {"enabled": True},
                },
            },
        ]
    }

    _clean_symbol_level_d3_fields(cfg)

    for item in cfg["symbols"]:
        for side in ("sell_put", "sell_call"):
            side_cfg = item.get(side) or {}
            for field in SYMBOL_LEVEL_D3_FORBIDDEN_FIELDS:
                assert field not in side_cfg
