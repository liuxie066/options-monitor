from __future__ import annotations


def test_symbol_resolve_tool_maps_name_alias_to_canonical_symbol() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("symbol_resolve", {"symbol": "泡泡玛特"})

    assert out["ok"] is True
    assert out["data"]["resolved"] is True
    assert out["data"]["raw_input"] == "泡泡玛特"
    assert out["data"]["canonical_symbol"] == "9992.HK"
    assert out["data"]["market"] == "HK"
    assert out["data"]["currency"] == "HKD"
    assert out["data"]["futu_code"] == "HK.09992"
