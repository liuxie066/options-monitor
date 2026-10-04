from __future__ import annotations

import pytest

from src.application.parse_option_message import parse_option_message_text


def test_parse_futu_tencent_fill_uses_resolved_multiplier_when_account_present(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93，此笔订单委托已全部成交，2026/04/09 13:10:25 (香港)。【富途证券(香港)】 lx"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "0700.HK"
    assert out["parsed"]["multiplier"] == 100
    assert out["parsed"]["account"] == "lx"
    assert out["parsed"]["currency"] == "HKD"


def test_parse_futu_tencent_call_fill_infers_hkd_from_symbol(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出2张$腾讯 260629 510.00 购$，成交价格：9.48，此笔订单委托已全部成交，2026/04/29 13:15:24 (香港)。【富途证券(香港)】 lx"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "0700.HK"
    assert out["parsed"]["option_type"] == "call"
    assert out["parsed"]["side"] == "short"
    assert out["parsed"]["strike"] == 510.0
    assert out["parsed"]["exp"] == "2026-06-29"
    assert out["parsed"]["premium_per_share"] == 9.48
    assert out["parsed"]["currency"] == "HKD"


def test_parse_futu_us_fill_infers_usd_when_currency_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出1张$PLTR 260515 30.00 沽$，成交价格：1.25，此笔订单委托已全部成交，2026/04/26 15:30:00 (美国)。【富途证券】 lx"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "PLTR"
    assert out["parsed"]["currency"] == "USD"


def test_parse_futu_us_fill_uses_symbol_currency_with_hong_kong_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.application.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "【成交提醒】成功卖出1张$PDD 260626 78.00P$，成交价格：1.43，2026/06/12 01:06:23 (香港)。【富途证券(香港)】 sy"

    out = parse_option_message_text(msg, accounts=["lx", "sy"])

    assert out["ok"] is True
    assert out["parsed"]["symbol"] == "PDD"
    assert out["parsed"]["currency"] == "USD"


@pytest.mark.parametrize("raw", ["100.00000000000000001", "100.5", "0", "-1", "nan", "inf"])
def test_manual_message_original_multiplier_is_not_truncated(tmp_path, monkeypatch, raw):
    from src.application.multiplier_cache import resolve_multiplier_with_source, save_cache

    save_cache(tmp_path / "output_shared/state/multiplier_cache.json", {"NVDA": {"multiplier": 500, "source": "cache"}})
    monkeypatch.setattr("src.application.parse_option_message.resolve_multiplier_with_source",
                        lambda **kw: resolve_multiplier_with_source(repo_base=tmp_path, symbol="NVDA",
                                                                    multiplier=kw["multiplier"], allow_opend_refresh=False))
    result = parse_option_message_text(f"NVDA put short strike 100 exp 2026-09-18 premium 1 1张 lx multiplier {raw}")
    assert result["ok"] is False
    assert result["parsed"]["multiplier"] is None


@pytest.mark.parametrize("suffix, expected", [
    ("乘数", None), ("乘数，", None), ("乘数：", None),
    ("multiplier", None), ("multiplier=", None),
    ("", 500), ("乘数500", 500), ("multiplier=1000", 1000),
])
def test_manual_message_empty_multiplier_is_not_cache_fallback(tmp_path, monkeypatch, suffix, expected):
    from src.application.multiplier_cache import resolve_multiplier_with_source, save_cache

    save_cache(tmp_path / "output_shared/state/multiplier_cache.json",
               {"0700.HK": {"multiplier": 500, "source": "cache"}})
    monkeypatch.setattr(
        "src.application.parse_option_message.resolve_multiplier_with_source",
        lambda **kw: resolve_multiplier_with_source(
            repo_base=tmp_path, symbol=kw["symbol"], multiplier=kw["multiplier"],
            allow_opend_refresh=False,
        ),
    )
    result = parse_option_message_text(
        "期权：腾讯20260330 put，strike500，成本5.425每股，short 10张，sy，HKD，" + suffix,
        accounts=["lx", "sy"],
    )
    assert result["ok"] is (expected is not None)
    assert result["parsed"]["multiplier"] == expected
    assert result["parsed"]["multiplier_source"] == (
        None if expected is None else "cache" if not suffix else "payload"
    )
