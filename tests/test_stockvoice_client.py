from __future__ import annotations

from src.infrastructure.stockvoice_client import extract_stockvoice_signals, normalize_stockvoice_symbol


def test_extract_stockvoice_signals_keeps_strong_bullish_us_symbols() -> None:
    html = """
    <main>
      <section>
        <h2>SPCX SPCX</h2>
        <p>$140.00 -0.91%</p>
        <p>看多 13 位 中性 7 位 看空 5 位</p>
      </section>
      <section>
        <h2>2330 台积电</h2>
        <p>看多 20 位 中性 2 位 看空 1 位</p>
      </section>
      <section>
        <h2>NVDA NVIDIA</h2>
        <p>看多 5 位 中性 2 位 看空 1 位</p>
      </section>
      <section>
        <h2>META Meta</h2>
        <p>$600.00 +1.2%</p>
        <p>看多 11 位 中性 4 位 看空 3 位</p>
      </section>
    </main>
    """

    signals = extract_stockvoice_signals(html, min_bullish_count=8, min_bull_bear_ratio=2.0, min_net_bullish=3)

    assert [signal.symbol for signal in signals] == ["META", "SPCX"]
    assert signals[1].bullish_count == 13
    assert signals[1].bearish_count == 5
    assert signals[1].neutral_count == 7
    assert signals[1].bullish_ratio == 0.52
    assert signals[1].bull_bear_ratio == 2.6
    assert signals[1].price == 140.0


def test_normalize_stockvoice_symbol_removes_non_symbol_text() -> None:
    assert normalize_stockvoice_symbol(" spcx ") == "SPCX"
    assert normalize_stockvoice_symbol("BRK.B") == "BRK.B"
    assert normalize_stockvoice_symbol("SPCX／SpaceX") == "SPCX"
