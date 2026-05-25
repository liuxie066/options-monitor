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
    assert canonical_strategy_id("call") == STRATEGY_COVERED_CALL
    assert strategy_display_name(STRATEGY_COVERED_CALL) == "Covered Call"
    assert strategy_section_label(STRATEGY_COVERED_CALL) == "Covered Call"
    assert strategy_action_label(STRATEGY_COVERED_CALL) == "Covered Call"
    assert strategy_key_help(("sell_put", "sell_call")) == "sell_put|sell_call (Covered Call internal key)"


def test_strategy_vocab_preserves_existing_sell_put_notification_label() -> None:
    from domain.domain.strategy_vocab import STRATEGY_SELL_PUT, strategy_action_label, strategy_section_label

    assert strategy_section_label(STRATEGY_SELL_PUT) == "Put"
    assert strategy_action_label(STRATEGY_SELL_PUT) == "卖Put"
