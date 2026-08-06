from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from conftest import phase2_opening_row


def _add_repo_to_syspath() -> Path:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    return base


def test_validate_config_rejects_symbol_level_strategy_filter_keys() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
            'call_base': {'sell_call': {'min_open_interest': 50, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                    'min_iv': 0.2,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'AAPL.sell_put' in msg
        assert 'min_iv' in msg

    cfg['symbols'][0]['sell_put'].pop('min_iv')
    cfg['symbols'][0]['sell_call'] = {
        'enabled': True,
        'min_dte': 7,
        'max_dte': 45,
        'min_strike': 120,
        'max_delta': 0.35,
    }
    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'AAPL.sell_call' in msg
        assert 'max_delta' in msg


def test_validate_config_rejects_removed_global_strategy_filter_keys() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'strategy': 'return_first',
                    'min_open_interest': 60,
                    'min_volume': 10,
                    'max_spread_ratio': 0.3,
                    'min_iv': 0.2,
                }
            }
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'templates.put_base.sell_put' in msg
        assert 'only min_open_interest, min_volume, max_spread_ratio are allowed' in msg
        assert 'min_iv' in msg


def test_validate_config_rejects_candidate_score_weights() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'min_open_interest': 60,
                    'min_volume': 10,
                    'max_spread_ratio': 0.3,
                    'score_weights': {
                        'annualized_return': 1.0,
                        'net_income': 0.000001,
                        'liquidity': 0.02,
                        'risk_distance': 0.03,
                        'vol_edge': 0.5,
                        'delta_target': 0.2,
                        'concentration': 0.2,
                        'path_risk': 0.2,
                    },
                }
            },
            'call_base': {
                'sell_call': {
                    'strategy': 'return_first',
                    'min_open_interest': 50,
                    'min_volume': 10,
                    'max_spread_ratio': 0.3,
                    'score_weights': {'liquidity': 0.02, 'risk_distance': 0.015},
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base', 'call_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                    'strategy': 'return_first',
                    'score_weights': {'liquidity': 0.01},
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'score_weights has been removed from opening config' in str(e)


def test_validate_config_accepts_sell_put_insurance_underwriting_strategy_config() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'strategy': 'insurance_underwriting',
                    'min_iv_rv_ratio': 1.10,
                    'min_iv_minus_rv': 0.05,
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                    'strategy': 'insurance_underwriting',
                    'min_iv_rv_ratio': 1.10,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    validate_config(cfg)

def test_validate_config_rejects_opening_short_vol_strategy_value() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {'sell_put': {'strategy': 'short_vol'}},
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'templates.put_base.sell_put.strategy=short_vol is no longer supported' in str(e)


def test_validate_config_rejects_underwriting_score_weights() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'strategy': 'insurance_underwriting',
                    'score_weights': {'liquidity': 0.02},
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'templates.put_base.sell_put.score_weights has been removed from opening config' in str(e)


def test_validate_config_rejects_opening_concentration_config() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'strategy': 'insurance_underwriting',
                    'concentration': {'max_single_trade_nav_pct': 0.08},
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'templates.put_base.sell_put.concentration has been removed from opening config' in str(e)


def test_validate_config_rejects_sell_put_short_vol_opening_config() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'strategy': 'insurance_underwriting',
                    'short_vol': {'min_abs_delta': 0.35, 'max_abs_delta': 0.30},
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'templates.put_base.sell_put.short_vol has been removed from opening config' in str(e)


def test_validate_config_rejects_sell_call_short_vol_opening_config() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'call_base': {
                'sell_call': {
                    'strategy': 'insurance_underwriting',
                    'short_vol': {'max_call_gap_up_opportunity_cost_nav_pct': 1.2},
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['call_base'],
                'sell_put': {'enabled': False},
                'sell_call': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'templates.call_base.sell_call.short_vol has been removed from opening config' in str(e)


def test_validate_config_rejects_sell_call_short_vol_opening_config_even_when_legacy_budget_key() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'call_base': {
                'sell_call': {
                    'strategy': 'insurance_underwriting',
                    'short_vol': {'max_call_gap_up_opportunity_cost_to_premium': -0.1},
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['call_base'],
                'sell_put': {'enabled': False},
                'sell_call': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'templates.call_base.sell_call.short_vol has been removed from opening config' in str(e)


def test_validate_config_rejects_return_first_opening_strategy() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'strategy': 'return_first',
                    'min_open_interest': 60,
                    'min_volume': 10,
                    'max_spread_ratio': 0.3,
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert 'templates.put_base.sell_put.strategy=return_first is no longer supported' in msg


def test_validate_config_rejects_removed_sell_put_min_otm_pct() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'put_base': {
                'sell_put': {
                    'min_otm_pct': 0.05,
                }
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert 'templates.put_base.sell_put has removed OTM fields: min_otm_pct' in msg

    del cfg['templates']['put_base']['sell_put']['min_otm_pct']
    cfg['symbols'][0]['sell_put']['min_otm_pct'] = 0.05
    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert 'AAPL.sell_put has removed OTM fields: min_otm_pct' in msg


def test_validate_config_rejects_removed_legacy_sell_call_fetch_fields_in_templates() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'templates': {
            'call_base': {
                'sell_call': {
                    'min_open_interest': 50,
                    'min_volume': 10,
                    'max_spread_ratio': 0.3,
                    'target_otm_pct_min': 0.05,
                }
            }
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['call_base'],
                'sell_put': {'enabled': False},
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert 'templates.call_base.sell_call' in msg
        assert 'removed legacy fetch planning keys' in msg


def test_validate_config_rejects_fees_config() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'fees': {'US': {'model': 'futu_us_simplified'}},
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'fees is no longer supported' in msg


def test_validate_config_rejects_invalid_close_advice_config() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'close_advice': {
            'enabled': True,
            'quote_source': 'required-data',
            'notify_levels': ['strong'],
        },
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'fetch': {'source': 'futu'},
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'close_advice.quote_source' in msg


def test_validate_config_rejects_close_advice_strategy_mode() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'close_advice': {'enabled': True, 'strategy': 'short_vol'},
        'symbols': [{'symbol': 'AAPL', 'sell_put': {'enabled': False}, 'sell_call': {'enabled': False}}],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'close_advice.strategy is not supported' in msg


def test_validate_config_rejects_market_local_position_advice_authority() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'close_advice': {
            'enabled': True,
            'position_advice_authority': 'v2',
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'sell_put': {'enabled': False},
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'close_advice.position_advice_authority is not supported' in msg


def test_validate_config_rejects_duplicate_normalized_account_labels() -> None:
    import pytest

    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        "symbols": [
            {
                "symbol": "AAPL",
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ]
    }
    cfg["accounts"] = ["lx", " LX "]
    with pytest.raises(SystemExit) as exc:
        validate_config(cfg)
    assert "duplicate labels after trim + lowercase" in str(exc.value)

    cfg = {
        "symbols": [
            {
                "symbol": "AAPL",
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ]
    }
    cfg["accounts"] = ["lx"]
    cfg["account_settings"] = {
        "lx": {"type": "futu", "futu": {"account_id": "123"}},
        " LX ": {"type": "futu", "futu": {"account_id": "123"}},
    }
    with pytest.raises(SystemExit) as exc:
        validate_config(cfg)
    assert "account_settings contains duplicate labels" in str(exc.value)


def test_validate_config_rejects_yield_enhancement_strategy_mode() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'symbols': [
            {
                'symbol': 'AAPL',
                'sell_put': {'enabled': True},
                'combo_yield': {'enabled': True, 'strategy': 'short_vol'},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'combo_yield is isolated from sell_put.strategy' in msg


def test_validate_config_rejects_decimal_close_advice_max_items_per_account() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'close_advice': {
            'enabled': True,
            'max_items_per_account': 1.5,
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'sell_put': {'enabled': False},
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'close_advice.max_items_per_account must be an integer' in msg


def test_validate_config_rejects_unknown_opend_rate_limit_endpoint() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'runtime': {
            'opend_rate_limits': {
                'market_snapshots': {'max_calls': 10},
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'sell_put': {'enabled': False},
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        msg = str(e)
        assert '[CONFIG_ERROR]' in msg
        assert 'runtime.opend_rate_limits.market_snapshots is not supported' in msg
        assert 'market_snapshot' in msg
        assert 'option_chain' in msg
        assert 'option_expiration' in msg


def test_validate_config_accepts_option_chain_opend_rate_limit_endpoint() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'runtime': {
            'opend_rate_limits': {
                'option_chain': {'max_calls': 10, 'window_sec': 30, 'max_wait_sec': 90},
            },
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'sell_put': {'enabled': False},
                'sell_call': {'enabled': False},
            }
        ],
    }

    validate_config(cfg)


def test_validate_config_accepts_external_holdings_account_settings() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'accounts': ['user1', 'ext1'],
        'account_settings': {
            'user1': {'type': 'futu', 'futu': {'account_id': 'REAL_1'}},
            'ext1': {'type': 'external_holdings', 'holdings_account': 'Feishu EXT'},
        },
        'portfolio': {
            'source': 'futu',
            'source_by_account': {'ext1': 'holdings'},
        },
        'trade_intake': {
            'account_mapping': {
                'futu': {'REAL_1': 'user1'},
            }
        },
        'templates': {
            'put_base': {'sell_put': {'min_open_interest': 60, 'min_volume': 10, 'max_spread_ratio': 0.3}},
        },
        'symbols': [
            {
                'symbol': 'AAPL',
                'use': ['put_base'],
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 10,
                    'max_strike': 200,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    validate_config(cfg)


def test_validate_config_rejects_zero_strike_sentinels_and_removed_legacy_sell_call_fields() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'symbols': [
            {
                'symbol': '0700.HK',
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 0,
                    'max_strike': 420,
                },
                'sell_call': {'enabled': False},
            }
        ],
    }

    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'min_strike must be > 0' in str(e)

    cfg['symbols'][0]['sell_put']['min_strike'] = 360
    cfg['symbols'][0]['sell_call'] = {
        'enabled': True,
        'min_dte': 7,
        'max_dte': 45,
        'target_otm_pct_min': 0.05,
    }
    try:
        validate_config(cfg)
        raise AssertionError('expected config validation failure')
    except SystemExit as e:
        assert 'removed legacy fetch planning keys' in str(e)


def test_validate_config_allows_single_near_bound_modes() -> None:
    _add_repo_to_syspath()
    from src.application.config_validator import validate_config

    cfg = {
        'symbols': [
            {
                'symbol': 'AAPL',
                'sell_put': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'max_strike': 200,
                },
                'sell_call': {
                    'enabled': True,
                    'min_dte': 7,
                    'max_dte': 45,
                    'min_strike': 220,
                },
            }
        ],
    }

    validate_config(cfg)
