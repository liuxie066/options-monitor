from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


DEFAULT_CONFIG_REF = "src.application.config_defaults.DEFAULT_CONFIG"

DEFAULT_CONFIG: dict[str, Any] = {
    "defaults": {
        "templates": {
            "put_base": {
                "sell_put": {
                    "min_otm_pct": 0.05,
                    "min_annualized_net_return": 0.1,
                    "min_net_income": 300.0,
                    "min_open_interest": 50,
                    "min_volume": 10,
                    "max_spread_ratio": 0.3,
                    "score_weights": {
                        "annualized_return": 1.0,
                        "net_income": 0.000001,
                        "liquidity": 0.02,
                        "risk_distance": 0.03,
                    },
                }
            },
            "call_base": {
                "sell_call": {
                    "min_annualized_net_return": 0.1,
                    "min_open_interest": 50,
                    "min_volume": 10,
                    "max_spread_ratio": 0.3,
                    "min_strike_cost_multiplier": 1.02,
                    "min_net_income": 300.0,
                    "score_weights": {
                        "annualized_return": 1.0,
                        "net_income": 0.000001,
                        "liquidity": 0.02,
                        "risk_distance": 0.015,
                    },
                }
            },
        },
        "portfolio": {
            "broker": "富途",
            "source": "futu",
            "base_currency": "CNY",
            "futu": {"host": "127.0.0.1", "port": 11111},
        },
        "trade_intake": {
            "enabled": True,
            "mode": "apply",
            "receipt": {
                "enabled": True,
                "notify_applied": True,
                "notify_unresolved": True,
                "notify_failed": True,
                "notify_duplicate": False,
                "retry_unconfirmed_duplicate": True,
            },
        },
        "option_positions": {
            "auto_close": {
                "enabled": True,
                "grace_days": 1,
                "max_close_per_run": 20,
                "receipt": {
                    "enabled": True,
                    "notify_applied": True,
                    "notify_failed": True,
                    "notify_noop": False,
                    "notify_dry_run": False,
                    "retry_unconfirmed": True,
                },
            }
        },
        "intake": {
            "symbol_aliases": {
                "中海油": "0883.HK",
                "中国海洋石油": "0883.HK",
                "腾讯": "0700.HK",
                "腾讯控股": "0700.HK",
                "泡泡玛特": "9992.HK",
                "美团": "3690.HK",
                "美团w": "3690.HK",
                "美团-w": "3690.HK",
                "美团-W": "3690.HK",
                "英伟达": "NVDA",
                "NVIDIA": "NVDA",
                "拼多多": "PDD",
                "富途": "FUTU",
                "富途控股": "FUTU",
                "谷歌": "GOOGL",
                "Google": "GOOGL",
                "携程": "TCOM",
                "携程网": "TCOM",
                "Credo": "CRDO",
                "Toast": "TOST",
            }
        },
        "outputs": {"top_n_alerts": 3},
        "runtime": {
            "symbol_timeout_sec": 120,
            "portfolio_timeout_sec": 60,
            "pipeline_timeout_sec": 600,
            "option_positions_context_ttl_sec": 900,
            "portfolio_context_ttl_sec": 900,
        },
        "notifications": {
            "opend_alert_cooldown_sec": 600,
            "opend_alert_burst_window_sec": 900,
            "opend_alert_burst_max": 3,
            "opend_alert_after_consecutive_failures": 3,
            "opend_alert_send_recovery_notice": True,
        },
        "inbound": {
            "feishu_ws": {
                "reply_enabled": True,
                "reply_in_thread": False,
                "max_reply_chars": 3500,
                "ack_reaction": "",
                "queue_size": 100,
            }
        },
        "watchdog": {
            "retry_enabled": True,
            "retry_interval_sec": 3,
            "retry_timeout_sec": 25,
            "success_threshold": 2,
        },
        "alert_policy": {
            "change_annual_threshold": 0.02,
            "sell_put": {
                "high_annual": 0.20,
                "high_spread_max": 0.20,
                "medium_annual": 0.12,
            },
            "sell_call": {
                "high_annual": 0.10,
                "high_total": 0.15,
                "medium_annual": 0.06,
            },
        },
        "close_advice": {
            "enabled": True,
            "quote_source": "auto",
            "notify_levels": ["strong", "medium"],
            "max_items_per_account": 5,
            "max_spread_ratio": 0.3,
            "strong_remaining_annualized_max": 0.045,
            "medium_remaining_annualized_max": 0.07,
        },
    },
    "markets": {
        "us": {
            "schedule": {
                "enabled": True,
                "timezone": "America/New_York",
                "cron_interval_min": 10,
                "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
                "run_points": {
                    "start_plus_min": 10,
                    "hourly_minute": 0,
                    "end_minus_min": 10,
                },
                "gates": [
                    {
                        "type": "before",
                        "timezone": "Asia/Shanghai",
                        "time": "02:00",
                        "day_offset_from_window_start": 1,
                    }
                ],
            },
            "symbol_defaults": {
                "broker": "US",
                "fetch": {
                    "source": "futu",
                    "host": "127.0.0.1",
                    "port": 11111,
                    "limit_expirations": 10,
                },
                "use": ["put_base", "call_base"],
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60},
                "yield_enhancement": {
                    "enabled": False,
                    "objective": "premium_funded_long_call",
                    "output_mode": "separate",
                    "funding_mode": "credit_or_even",
                    "min_combo_net_credit": 0.0,
                    "max_call_cost_to_put_credit": 1.0,
                    "min_upside_lift_to_call_cost": 1.5,
                    "min_upside_lift_to_put_credit": 0.5,
                    "min_put_otm_pct": 0.05,
                    "min_open_interest": 100,
                    "min_volume": 5,
                    "max_spread_ratio": 0.35,
                    "max_combo_spread_ratio": 0.5,
                    "call": {
                        "min_otm_pct": 0.03,
                        "max_otm_pct": 0.4,
                        "min_delta": 0.1,
                        "max_delta": 0.45,
                    },
                },
                "sell_call": {"enabled": False, "min_dte": 20, "max_dte": 60},
            },
        },
        "hk": {
            "schedule": {
                "enabled": True,
                "timezone": "Asia/Hong_Kong",
                "cron_interval_min": 10,
                "run_window": {
                    "start": "09:30",
                    "end": "16:00",
                    "breaks": [{"start": "12:00", "end": "13:00"}],
                },
                "run_points": {
                    "start_plus_min": 10,
                    "hourly_minute": 0,
                    "end_minus_min": 10,
                },
            },
            "templates": {
                "put_base": {"sell_put": {"min_volume": 0}},
                "call_base": {"sell_call": {"min_volume": 0}},
            },
            "symbol_defaults": {
                "broker": "HK",
                "fetch": {
                    "source": "futu",
                    "host": "127.0.0.1",
                    "port": 11111,
                    "limit_expirations": 8,
                },
                "use": ["put_base", "call_base"],
                "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 90},
                "yield_enhancement": {
                    "enabled": False,
                    "objective": "premium_funded_long_call",
                    "output_mode": "separate",
                    "funding_mode": "credit_or_even",
                    "min_combo_net_credit": 0.0,
                    "max_call_cost_to_put_credit": 1.0,
                    "min_upside_lift_to_call_cost": 1.5,
                    "min_upside_lift_to_put_credit": 0.5,
                    "min_put_otm_pct": 0.05,
                    "min_open_interest": 100,
                    "min_volume": 0,
                    "max_spread_ratio": 0.35,
                    "max_combo_spread_ratio": 0.5,
                    "call": {
                        "min_otm_pct": 0.03,
                        "max_otm_pct": 0.4,
                        "min_delta": 0.1,
                        "max_delta": 0.45,
                    },
                },
                "sell_call": {"enabled": False, "min_dte": 20, "max_dte": 90},
            },
        },
    },
}


def default_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)


def default_config_sha256() -> str:
    payload = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_CONFIG_REF",
    "default_config",
    "default_config_sha256",
]
