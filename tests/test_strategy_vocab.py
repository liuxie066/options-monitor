from __future__ import annotations


def test_strategy_vocab_decouples_covered_call_display_from_internal_key() -> None:
    from domain.domain.strategy_vocab import (
        STRATEGY_COVERED_CALL,
        canonical_strategy_id,
        strategy_action_label,
        strategy_display_name,
        strategy_key_help,
        strategy_section_label,
    )

    assert STRATEGY_COVERED_CALL == "sell_call"
    assert canonical_strategy_id("sell_call") == STRATEGY_COVERED_CALL
    assert canonical_strategy_id("covered_call") == STRATEGY_COVERED_CALL
    assert canonical_strategy_id("Covered Call") == STRATEGY_COVERED_CALL
    assert canonical_strategy_id("CC") == STRATEGY_COVERED_CALL
    assert canonical_strategy_id("call") == STRATEGY_COVERED_CALL
    assert strategy_display_name(STRATEGY_COVERED_CALL) == "Covered Call (CC)"
    assert strategy_section_label(STRATEGY_COVERED_CALL) == "CC"
    assert strategy_action_label(STRATEGY_COVERED_CALL) == "CC"
    assert strategy_key_help(("sell_put", "sell_call")) == (
        "sell_put (Cash-Secured Put (CSP) internal key)|"
        "sell_call (Covered Call (CC) internal key)"
    )


def test_strategy_vocab_uses_csp_display_and_preserves_legacy_aliases() -> None:
    from domain.domain.strategy_vocab import (
        STRATEGY_SELL_PUT,
        canonical_strategy_id,
        strategy_action_label,
        strategy_display_name,
        strategy_section_label,
    )

    assert canonical_strategy_id("CSP") == STRATEGY_SELL_PUT
    assert canonical_strategy_id("Cash-Secured Put") == STRATEGY_SELL_PUT
    assert canonical_strategy_id("sell put") == STRATEGY_SELL_PUT
    assert strategy_display_name(STRATEGY_SELL_PUT) == "Cash-Secured Put (CSP)"
    assert strategy_section_label(STRATEGY_SELL_PUT) == "CSP"
    assert strategy_action_label(STRATEGY_SELL_PUT) == "CSP"
