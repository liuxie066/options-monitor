from __future__ import annotations

import sys
from zoneinfo import ZoneInfo

from domain.domain import (
    FEISHU_APP_NOTIFICATION_PROVIDER,
    OPENCLAW_NOTIFICATION_PROVIDER,
    OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
    SUPPORTED_NOTIFICATION_PROVIDERS,
    WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
    normalize_notification_provider,
)
from domain.domain.fetch_source import normalize_fetch_source
from src.application.account_config import ACCOUNT_TYPES, account_settings_from_config, accounts_from_config
from src.application.config_sections import resolve_templates_config, resolve_watchlist_config, set_watchlist_config
from src.application.assistant.llm_provider_registry import supported_llm_providers
from src.application.trades.account_mapping import resolve_trade_intake_config
from src.application.positions.maintenance_receipt import resolve_auto_close_receipt_config
from src.application.opend_fetch_config import OPEND_RATE_LIMIT_ENDPOINT_KEYS
from src.application.yield_enhancement_config import (
    YIELD_ENHANCEMENT_FUNDING_MODES,
    YIELD_ENHANCEMENT_LEGACY_CALL_BOUND_FIELDS,
    YIELD_ENHANCEMENT_LEGACY_CALL_OTM_FIELDS,
    YIELD_ENHANCEMENT_LEGACY_OPTIMIZER_FIELDS,
    YIELD_ENHANCEMENT_LEGACY_PUT_OTM_FIELDS,
    YIELD_ENHANCEMENT_LEGACY_SCENARIO_FIELDS,
    YIELD_ENHANCEMENT_OBJECTIVES,
    YIELD_ENHANCEMENT_OUTPUT_MODES,
)

LIQUIDITY_ALLOWED_GLOBAL_FIELDS = (
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
)
SCORE_WEIGHT_FIELDS = (
    'annualized_return',
    'net_income',
    'liquidity',
    'risk_distance',
    'vol_edge',
    'delta_target',
    'concentration',
    'path_risk',
)
YIELD_ENHANCEMENT_LIQUIDITY_FIELDS = LIQUIDITY_ALLOWED_GLOBAL_FIELDS + (
    'max_combo_spread_ratio',
)
REMOVED_STRATEGY_FILTER_FIELDS = (
    'require_bid_ask',
    'min_iv',
    'max_iv',
    'min_abs_delta',
    'max_abs_delta',
    'min_delta',
    'max_delta',
)
SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS = LIQUIDITY_ALLOWED_GLOBAL_FIELDS + REMOVED_STRATEGY_FILTER_FIELDS + ('event_risk',)
LEGACY_SELL_CALL_FETCH_FIELDS = ('target_otm_pct_min', 'target_otm_pct_max')
LEGACY_SELL_PUT_OTM_FIELDS = ('min_otm_pct',)
YIELD_ENHANCEMENT_REMOVED_TARGET_FIELDS = (
    'target_price',
    'target_price_mode',
    'target_upside_pct',
    'default_target_upside_pct',
    'target_move_factor',
    'expected_move_factor',
    'min_target_return',
    'min_annualized_target_return',
)
REMOVED_SCHEDULE_FIELDS = (
    'market_timezone',
    'market_open',
    'market_close',
    'market_break_start',
    'market_break_end',
    'monitor_off_hours',
    'market_dense_interval_min',
    'market_sparse_interval_min',
    'market_hours_interval_min',
    'notify_cooldown_min',
    'notify_cooldown_dense_min',
    'notify_cooldown_sparse_min',
    'sparse_after_beijing',
    'interval_min',
    'first_notify_after_open_min',
    'notify_interval_min',
    'final_notify_before_close_min',
    'schedule_v2',
)
INLINE_SECRET_CONFIG_KEYS = {
    'access_token',
    'app_secret',
    'client_secret',
    'password',
    'private_key',
    'refresh_token',
    'tenant_access_token',
}
ASSISTANT_CONFIG_KEYS = {
    'active_model',
    'context_window_messages',
    'default_market_scope',
    'enabled',
    'llm',
    'models',
    'planner',
}
RETIRED_FEISHU_CALLBACK_KEYS = {
    'encrypt_key',
    'encrypt_key_env',
    'verification_token',
    'verification_token_env',
}


def die(msg: str):
    raise SystemExit(f"[CONFIG_ERROR] {msg}")


def warn(msg: str):
    print(f"[CONFIG_WARN] {msg}", file=sys.stderr)


def validate_positive_number(value, path: str):
    try:
        if float(value) <= 0:
            die(f'{path} must be > 0')
    except Exception:
        die(f'{path} must be a number')


def validate_positive_integer(value, path: str):
    try:
        parsed = int(value)
        if float(value) != float(parsed):
            die(f'{path} must be an integer')
        if parsed <= 0:
            die(f'{path} must be > 0')
    except Exception:
        die(f'{path} must be an integer')


def validate_rate_limit_object(raw: dict, path: str):
    for key in ('window_sec', 'max_wait_sec'):
        if key in raw and raw.get(key) is not None:
            validate_positive_number(raw.get(key), f'{path}.{key}')
    if 'max_calls' in raw and raw.get('max_calls') is not None:
        validate_positive_integer(raw.get('max_calls'), f'{path}.max_calls')


def validate_non_negative_integer(value, path: str):
    try:
        parsed = int(value)
        if float(value) != float(parsed):
            die(f'{path} must be an integer')
        if parsed < 0:
            die(f'{path} must be >= 0')
    except Exception:
        die(f'{path} must be an integer')


def _validate_optional_non_negative_number(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    try:
        if float(cfg.get(key)) < 0:
            die(f'{path}.{key} must be >= 0')
    except Exception:
        die(f'{path}.{key} must be a number')


def _validate_optional_positive_number(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    try:
        if float(cfg.get(key)) <= 0:
            die(f'{path}.{key} must be > 0')
    except Exception:
        die(f'{path}.{key} must be a number')


def _validate_optional_unit_interval_number(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    try:
        value = float(cfg.get(key))
        if value < 0 or value > 1:
            die(f'{path}.{key} must be between 0 and 1')
    except Exception:
        die(f'{path}.{key} must be a number')


def _validate_optional_bool(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    if not isinstance(cfg.get(key), bool):
        die(f'{path}.{key} must be a boolean')


def _validate_llm_config(llm_cfg: dict, *, path: str, enabled: bool, required_reason: str) -> None:
    for key in ('provider', 'base_url', 'model', 'api_key_env'):
        if key in llm_cfg and llm_cfg.get(key) is not None and not isinstance(llm_cfg.get(key), str):
            die(f'{path}.{key} must be a string')
    _validate_optional_unit_interval_number(llm_cfg, 'confidence_min', path)
    if str(llm_cfg.get('base_url') or '').strip():
        llm_base_url = str(llm_cfg.get('base_url') or '').strip()
        if not (llm_base_url.startswith('https://') or llm_base_url.startswith('http://')):
            die(f'{path}.base_url must start with http:// or https:// when set')
    if 'timeout_seconds' in llm_cfg and llm_cfg.get('timeout_seconds') is not None:
        validate_positive_integer(llm_cfg.get('timeout_seconds'), f'{path}.timeout_seconds')
        if int(llm_cfg.get('timeout_seconds')) > 120:
            die(f'{path}.timeout_seconds must be <= 120')
    if 'max_output_tokens' in llm_cfg and llm_cfg.get('max_output_tokens') is not None:
        validate_positive_integer(llm_cfg.get('max_output_tokens'), f'{path}.max_output_tokens')
        if int(llm_cfg.get('max_output_tokens')) < 64:
            die(f'{path}.max_output_tokens must be >= 64')
        if int(llm_cfg.get('max_output_tokens')) > 4096:
            die(f'{path}.max_output_tokens must be <= 4096')
    llm_provider = str(llm_cfg.get('provider') or '').strip()
    supported_providers = supported_llm_providers()
    if llm_provider and llm_provider not in supported_providers:
        die(f"{path}.provider must be one of: {', '.join(supported_providers)}")
    if enabled:
        if not llm_provider:
            die(f'{path}.provider is required when {required_reason}')
        if not str(llm_cfg.get('model') or '').strip():
            die(f'{path}.model is required when {required_reason}')
        if not str(llm_cfg.get('api_key_env') or '').strip():
            die(f'{path}.api_key_env is required when {required_reason}')


def _validate_inbound_config(cfg: dict) -> None:
    inbound = cfg.get('inbound') or {}
    if inbound and not isinstance(inbound, dict):
        die('inbound must be an object')
    if isinstance(inbound, dict):
        feishu_ws = inbound.get('feishu_ws') or {}
        if feishu_ws and not isinstance(feishu_ws, dict):
            die('inbound.feishu_ws must be an object')
        if isinstance(feishu_ws, dict):
            for key in ('reply_enabled', 'reply_in_thread'):
                if key in feishu_ws and feishu_ws.get(key) is not None and not isinstance(feishu_ws.get(key), bool):
                    die(f'inbound.feishu_ws.{key} must be a boolean')
            for key in ('max_reply_chars', 'queue_size'):
                if key in feishu_ws and feishu_ws.get(key) is not None:
                    validate_positive_integer(feishu_ws.get(key), f'inbound.feishu_ws.{key}')
            if 'ack_reaction' in feishu_ws and feishu_ws.get('ack_reaction') is not None and not isinstance(feishu_ws.get('ack_reaction'), str):
                die('inbound.feishu_ws.ack_reaction must be a string')
        wechat_clawbot = inbound.get('wechat_clawbot') or {}
        if wechat_clawbot and not isinstance(wechat_clawbot, dict):
            die('inbound.wechat_clawbot must be an object')
        if isinstance(wechat_clawbot, dict):
            for key in ('label', 'state_dir', 'allowed_senders'):
                if key in wechat_clawbot and wechat_clawbot.get(key) is not None and not isinstance(wechat_clawbot.get(key), str):
                    die(f'inbound.wechat_clawbot.{key} must be a string')
            if 'reply_enabled' in wechat_clawbot and wechat_clawbot.get('reply_enabled') is not None and not isinstance(wechat_clawbot.get('reply_enabled'), bool):
                die('inbound.wechat_clawbot.reply_enabled must be a boolean')
            for key in ('max_reply_chars', 'timeout_sec'):
                if key in wechat_clawbot and wechat_clawbot.get(key) is not None:
                    validate_positive_integer(wechat_clawbot.get(key), f'inbound.wechat_clawbot.{key}')
            _validate_optional_non_negative_number(wechat_clawbot, 'poll_interval_sec', 'inbound.wechat_clawbot')


def _validate_assistant_config(cfg: dict) -> None:
    assistant = cfg.get('assistant')
    if assistant is None:
        assistant = {}
    if not isinstance(assistant, dict):
        die('assistant must be an object')
    unsupported_assistant_keys = sorted(str(key) for key in assistant.keys() if str(key) not in ASSISTANT_CONFIG_KEYS)
    if unsupported_assistant_keys:
        die('assistant has unsupported keys: ' + ', '.join(unsupported_assistant_keys))
    if 'enabled' in assistant and assistant.get('enabled') is not None and not isinstance(assistant.get('enabled'), bool):
        die('assistant.enabled must be a boolean')
    planner = assistant.get('planner') or {}
    if planner and not isinstance(planner, dict):
        die('assistant.planner must be an object')
    if isinstance(planner, dict) and 'enabled' in planner and planner.get('enabled') is not None and not isinstance(planner.get('enabled'), bool):
        die('assistant.planner.enabled must be a boolean')
    if 'context_window_messages' in assistant and assistant.get('context_window_messages') is not None:
        validate_non_negative_integer(assistant.get('context_window_messages'), 'assistant.context_window_messages')
        if int(assistant.get('context_window_messages')) > 20:
            die('assistant.context_window_messages must be <= 20')
    if 'default_market_scope' in assistant and assistant.get('default_market_scope') is not None:
        if str(assistant.get('default_market_scope') or '').strip().lower() not in {'us', 'hk', 'all'}:
            die('assistant.default_market_scope must be one of: us, hk, all')
    if 'models' in assistant or 'active_model' in assistant:
        die('assistant.models and assistant.active_model belong in config.yaml; build config.assistant.json first')
    llm = assistant.get('llm') or {}
    if not isinstance(llm, dict):
        die('assistant.llm must be an object')
    if 'enabled' in llm:
        die('assistant.llm.enabled is retired; use assistant.planner.enabled')
    _validate_llm_config(
        llm,
        path='assistant.llm',
        enabled=False,
        required_reason='assistant planner uses LLM',
    )

    if 'agent' in cfg:
        die('agent.* config is retired; use assistant.*')


def validate_assistant_config(cfg: dict) -> None:
    allowed = {'assistant', 'inbound', '_generated', '_resolved'}
    unsupported = sorted(str(key) for key in cfg.keys() if str(key) not in allowed)
    if unsupported:
        die(
            'assistant config has unsupported top-level keys: '
            + ', '.join(unsupported)
            + '; use config.assistant.json, not config.<market>.json'
        )
    _validate_inbound_config(cfg)
    _validate_assistant_config(cfg)


def _validate_score_weights(cfg: dict, path: str) -> None:
    if 'score_weights' not in cfg or cfg.get('score_weights') is None:
        return
    raw = cfg.get('score_weights')
    if not isinstance(raw, dict):
        die(f'{path}.score_weights must be an object')
    unsupported = [str(k) for k in raw.keys() if k not in SCORE_WEIGHT_FIELDS]
    if unsupported:
        die(
            f"{path}.score_weights has unsupported keys: {', '.join(unsupported)}; "
            f"allowed keys: {', '.join(SCORE_WEIGHT_FIELDS)}"
        )
    for key in SCORE_WEIGHT_FIELDS:
        _validate_optional_non_negative_number(raw, key, f'{path}.score_weights')


def _validate_opening_strategy_config(cfg: dict, path: str) -> None:
    strategy = None
    if 'strategy' in cfg and cfg.get('strategy') is not None:
        strategy = str(cfg.get('strategy') or '').strip().lower()
        if strategy == 'short_vol':
            die(f'{path}.strategy=short_vol is no longer supported for opening config; use insurance_underwriting')
        if strategy not in {'return_first', 'insurance_underwriting'}:
            die(f'{path}.strategy must be one of: return_first, insurance_underwriting')
    if strategy == 'insurance_underwriting' and cfg.get('score_weights') is not None:
        die(f'{path}.score_weights is not used by insurance_underwriting; remove it or use return_first')
    event_risk = cfg.get('event_risk')
    if event_risk is not None:
        if not isinstance(event_risk, dict):
            die(f'{path}.event_risk must be an object')
        _validate_optional_bool(event_risk, 'enabled', f'{path}.event_risk')
    short_vol = cfg.get('short_vol')
    if short_vol is not None:
        die(
            f'{path}.short_vol has been removed from opening config; '
            'put min_iv_rv_ratio, min_iv_minus_rv, reject_event_risk, and event_source_fail_closed on the opening config'
        )
    for key in ('min_iv_rv_ratio', 'min_iv_minus_rv'):
        _validate_optional_non_negative_number(cfg, key, path)
    for key in ('reject_event_risk', 'event_source_fail_closed'):
        _validate_optional_bool(cfg, key, path)
    if strategy == 'insurance_underwriting' and isinstance(event_risk, dict) and event_risk.get('enabled') is False:
        fail_closed = True
        if cfg.get('event_source_fail_closed') is not None:
            fail_closed = bool(cfg.get('event_source_fail_closed'))
        if fail_closed:
            die(
                f'{path}.event_risk.enabled=false conflicts with '
                f'{path}.event_source_fail_closed=true'
            )
    concentration = cfg.get('concentration')
    if concentration is not None:
        die(f'{path}.concentration has been removed from opening config; manage assignment exposure outside candidate scan')


def _use_list(item: dict) -> list[str]:
    raw = item.get('use')
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        return [str(value).strip() for value in raw if isinstance(value, str) and str(value).strip()]
    return []


def _template_side_has_strategy(templates: dict, template_name: str, side: str) -> bool:
    template = templates.get(template_name)
    if not isinstance(template, dict):
        return False
    side_cfg = template.get(side)
    return isinstance(side_cfg, dict) and str(side_cfg.get('strategy') or '').strip() != ''


def _used_template_has_side_strategy(templates: dict, use_list: list[str], side: str) -> bool:
    return any(_template_side_has_strategy(templates, name, side) for name in use_list)


def _validate_enabled_side_template_strategy(
    *,
    sym: str,
    side: str,
    side_cfg: dict,
    item: dict,
    templates: dict,
) -> None:
    if not side_cfg.get('enabled'):
        return
    if str(side_cfg.get('strategy') or '').strip():
        return
    expected_template = 'put_base' if side == 'sell_put' else 'call_base'
    if not _template_side_has_strategy(templates, expected_template, side):
        return
    use_list = _use_list(item)
    if _used_template_has_side_strategy(templates, use_list, side):
        return
    recommendation = 'use: ["put_base", "call_base"]'
    die(
        f"{sym}.{side} enabled but no {side}.strategy is inherited. "
        f"Add {expected_template} to {sym}.use, for example {recommendation}, "
        f"or set {sym}.{side}.strategy explicitly."
    )


def _validate_optional_non_negative_number_list(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    values = cfg.get(key)
    if not isinstance(values, list) or not values:
        die(f'{path}.{key} must be a non-empty array')
    for index, value in enumerate(values):
        try:
            if float(value) < 0:
                die(f'{path}.{key}[{index}] must be >= 0')
        except Exception:
            die(f'{path}.{key}[{index}] must be a number')


def _validate_optional_strike_bounds(cfg: dict, path: str):
    min_strike = cfg.get('min_strike')
    max_strike = cfg.get('max_strike')
    if min_strike is not None:
        _validate_optional_positive_number(cfg, 'min_strike', path)
    if max_strike is not None:
        _validate_optional_positive_number(cfg, 'max_strike', path)
    if (min_strike is not None) and (max_strike is not None):
        try:
            if float(min_strike) > float(max_strike):
                die(f'{path}.min_strike > {path}.max_strike')
        except Exception:
            die(f'{path}.min_strike/max_strike must be numbers')


def _validate_optional_dte_window(cfg: dict, path: str):
    min_dte = cfg.get('min_dte')
    max_dte = cfg.get('max_dte')
    if min_dte is not None:
        validate_non_negative_integer(min_dte, f'{path}.min_dte')
    if max_dte is not None:
        validate_non_negative_integer(max_dte, f'{path}.max_dte')
    if (min_dte is not None) and (max_dte is not None):
        try:
            if int(min_dte) > int(max_dte):
                die(f'{path}.min_dte > {path}.max_dte')
        except Exception:
            die(f'{path}.min_dte/max_dte must be integers')


def _validate_yield_enhancement_cfg(cfg: dict, path: str):
    if not isinstance(cfg, dict):
        die(f'{path} must be an object')
    if 'strategy' in cfg or 'strategy_profile' in cfg:
        die(f'{path}.strategy is not supported; combo_yield is isolated from sell_put.strategy')
    bad_keys = [k for k in REMOVED_STRATEGY_FILTER_FIELDS if k in cfg]
    if bad_keys:
        die(f"{path} has unsupported strategy filter keys: {', '.join(bad_keys)}")
    removed_target_keys = [k for k in YIELD_ENHANCEMENT_REMOVED_TARGET_FIELDS if k in cfg]
    if removed_target_keys:
        die(
            f"{path} has removed target-price fields: {', '.join(removed_target_keys)}; "
            "combo_yield uses price bounds and funding economics"
        )
    legacy_scenario_keys = [k for k in YIELD_ENHANCEMENT_LEGACY_SCENARIO_FIELDS if k in cfg]
    if legacy_scenario_keys:
        die(
            f"{path} has removed scenario fields: {', '.join(legacy_scenario_keys)}; "
            "use funding, call cost, strike bounds, and delta controls instead"
        )
    legacy_optimizer_keys = [k for k in YIELD_ENHANCEMENT_LEGACY_OPTIMIZER_FIELDS if k in cfg]
    if legacy_optimizer_keys:
        die(
            f"{path} has removed optimizer fields: {', '.join(legacy_optimizer_keys)}; "
            "use funding, call cost, strike bounds, and delta controls instead"
        )
    legacy_call_bound_keys = [k for k in YIELD_ENHANCEMENT_LEGACY_CALL_BOUND_FIELDS if k in cfg]
    if legacy_call_bound_keys:
        die(
            f"{path} has removed call OTM fields: {', '.join(legacy_call_bound_keys)}; "
            "use combo_yield.call.min_strike/max_strike instead"
        )
    legacy_put_otm_keys = [k for k in YIELD_ENHANCEMENT_LEGACY_PUT_OTM_FIELDS if k in cfg]
    if legacy_put_otm_keys:
        die(
            f"{path} has removed put OTM fields: {', '.join(legacy_put_otm_keys)}; "
            "use sell_put.min_strike/max_strike assignment bounds instead"
        )
    if 'objective' in cfg and cfg.get('objective') is not None:
        objective = str(cfg.get('objective') or '').strip().lower()
        if objective not in YIELD_ENHANCEMENT_OBJECTIVES:
            die(f"{path}.objective must be one of: {', '.join(sorted(YIELD_ENHANCEMENT_OBJECTIVES))}")
    if 'output_mode' in cfg and cfg.get('output_mode') is not None:
        output_mode = str(cfg.get('output_mode') or '').strip().lower()
        if output_mode not in YIELD_ENHANCEMENT_OUTPUT_MODES:
            die(f"{path}.output_mode must be one of: {', '.join(sorted(YIELD_ENHANCEMENT_OUTPUT_MODES))}")
    for key in YIELD_ENHANCEMENT_LIQUIDITY_FIELDS:
        _validate_optional_non_negative_number(cfg, key, path)
    for key in (
        'min_combo_net_credit',
        'min_net_credit_annualized',
        'max_call_cost_to_put_credit',
    ):
        _validate_optional_non_negative_number(cfg, key, path)
    if 'funding_mode' in cfg and cfg.get('funding_mode') is not None:
        mode = str(cfg.get('funding_mode') or '').strip().lower()
        if mode not in YIELD_ENHANCEMENT_FUNDING_MODES:
            die(f"{path}.funding_mode must be one of: {', '.join(sorted(YIELD_ENHANCEMENT_FUNDING_MODES))}")
    if str(cfg.get('funding_mode') or '').strip().lower() == 'max_debit':
        if (cfg.get('max_debit') is None) and (cfg.get('max_debit_native') is None):
            die(f"{path}.funding_mode=max_debit requires max_debit or max_debit_native")
    _validate_optional_positive_number(cfg, 'max_debit', path)
    _validate_optional_positive_number(cfg, 'max_debit_native', path)
    _validate_optional_dte_window(cfg, path)

    call_leg = cfg.get('call')
    if call_leg is not None and not isinstance(call_leg, dict):
        die(f'{path}.call must be an object')
    if isinstance(call_leg, dict):
        _validate_optional_dte_window(call_leg, f'{path}.call')
        _validate_optional_strike_bounds(call_leg, f'{path}.call')
        legacy_call_otm_keys = [k for k in YIELD_ENHANCEMENT_LEGACY_CALL_OTM_FIELDS if k in call_leg]
        if legacy_call_otm_keys:
            die(
                f"{path}.call has removed OTM fields: {', '.join(legacy_call_otm_keys)}; "
                "use call.min_strike/max_strike and min_delta/max_delta instead"
            )
        for key in ('min_delta', 'max_delta'):
            _validate_optional_unit_interval_number(call_leg, key, f'{path}.call')
        min_delta = call_leg.get('min_delta')
        max_delta = call_leg.get('max_delta')
        if (min_delta is not None) and (max_delta is not None):
            try:
                if float(min_delta) > float(max_delta):
                    die(f'{path}.call.min_delta > {path}.call.max_delta')
            except Exception:
                die(f'{path}.call.min_delta/max_delta must be numbers')


def _validate_hhmm(value, path: str) -> None:
    try:
        text = str(value or '')
        hour, minute = text.split(':', 1)
        hour_i = int(hour)
        minute_i = int(minute)
        if hour_i < 0 or hour_i > 23 or minute_i < 0 or minute_i > 59:
            raise ValueError(text)
    except Exception:
        die(f'{path} must be HH:MM')


def _validate_timezone(value, path: str) -> None:
    try:
        ZoneInfo(str(value or ''))
    except Exception:
        die(f'{path} must be a valid IANA timezone')


def _validate_schedule_cfg(raw, path: str) -> None:
    if raw is None:
        return
    if not isinstance(raw, dict):
        die(f'{path} must be an object')

    removed = [key for key in REMOVED_SCHEDULE_FIELDS if key in raw]
    if removed:
        die(
            f"{path} has removed schedule fields: {', '.join(removed)}; "
            "use timezone, cron_interval_min, run_window, run_points, and gates"
        )

    if 'timezone' in raw:
        _validate_timezone(raw.get('timezone'), f'{path}.timezone')
    if 'beijing_timezone' in raw:
        _validate_timezone(raw.get('beijing_timezone'), f'{path}.beijing_timezone')
    if 'cron_interval_min' in raw and raw.get('cron_interval_min') is not None:
        validate_positive_integer(raw.get('cron_interval_min'), f'{path}.cron_interval_min')

    run_window = raw.get('run_window')
    if run_window is not None:
        if not isinstance(run_window, dict):
            die(f'{path}.run_window must be an object')
        _validate_hhmm(run_window.get('start'), f'{path}.run_window.start')
        _validate_hhmm(run_window.get('end'), f'{path}.run_window.end')
        breaks = run_window.get('breaks', [])
        if breaks is None:
            breaks = []
        if not isinstance(breaks, list):
            die(f'{path}.run_window.breaks must be an array')
        for index, item in enumerate(breaks):
            if not isinstance(item, dict):
                die(f'{path}.run_window.breaks[{index}] must be an object')
            _validate_hhmm(item.get('start'), f'{path}.run_window.breaks[{index}].start')
            _validate_hhmm(item.get('end'), f'{path}.run_window.breaks[{index}].end')

    run_points = raw.get('run_points')
    if run_points is not None:
        if not isinstance(run_points, dict):
            die(f'{path}.run_points must be an object')
        for key in ('start_plus_min', 'end_minus_min'):
            if key in run_points and run_points.get(key) is not None:
                validate_non_negative_integer(run_points.get(key), f'{path}.run_points.{key}')
        if 'hourly_minute' in run_points and run_points.get('hourly_minute') is not None:
            validate_non_negative_integer(run_points.get('hourly_minute'), f'{path}.run_points.hourly_minute')
            if int(run_points.get('hourly_minute')) > 59:
                die(f'{path}.run_points.hourly_minute must be <= 59')

    gates = raw.get('gates')
    if gates is not None:
        if not isinstance(gates, list):
            die(f'{path}.gates must be an array')
        for index, gate in enumerate(gates):
            if not isinstance(gate, dict):
                die(f'{path}.gates[{index}] must be an object')
            gate_type = str(gate.get('type') or '').strip().lower()
            if gate_type != 'before':
                die(f'{path}.gates[{index}].type must be before')
            _validate_timezone(gate.get('timezone') or 'Asia/Shanghai', f'{path}.gates[{index}].timezone')
            _validate_hhmm(gate.get('time'), f'{path}.gates[{index}].time')
            if 'day_offset_from_window_start' in gate and gate.get('day_offset_from_window_start') is not None:
                try:
                    int(gate.get('day_offset_from_window_start'))
                except Exception:
                    die(f'{path}.gates[{index}].day_offset_from_window_start must be an integer')


def _validate_no_inline_secrets_or_retired_callback_cfg(value, path: str = '') -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key or '').strip()
            child_path = f'{path}.{key_text}' if path else key_text
            key_lower = key_text.lower()
            if key_lower in RETIRED_FEISHU_CALLBACK_KEYS and item not in (None, '', {}, []):
                die(f'{child_path} is no longer supported; Feishu inbound uses long-connection Bot env settings')
            if key_lower in INLINE_SECRET_CONFIG_KEYS and isinstance(item, str) and item.strip():
                die(f'{child_path} must not contain inline secret material; store it in the env file instead')
            _validate_no_inline_secrets_or_retired_callback_cfg(item, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_no_inline_secrets_or_retired_callback_cfg(item, f'{path}[{index}]')


def validate_config(cfg: dict):
    if 'watchlist' in cfg:
        die('watchlist is no longer supported; use symbols')
    if 'profiles' in cfg:
        die('profiles is no longer supported; use templates')
    if 'fees' in cfg:
        die('fees is no longer supported; fee rules are built in')

    _validate_no_inline_secrets_or_retired_callback_cfg(cfg)

    _validate_schedule_cfg(cfg.get('schedule'), 'schedule')
    _validate_schedule_cfg(cfg.get('schedule_hk'), 'schedule_hk')

    # intake config (optional)
    intake = cfg.get('intake') or {}
    if intake and not isinstance(intake, dict):
        die('intake must be an object')
    if isinstance(intake, dict):
        sa = intake.get('symbol_aliases') or {}
        if sa and not isinstance(sa, dict):
            die('intake.symbol_aliases must be an object')
        retired_multiplier_keys = ('multiplier_by_symbol', 'default_multiplier_us', 'default_multiplier_hk')
        for key in retired_multiplier_keys:
            if key in intake and intake[key] is not None:
                die(
                    f'intake.{key} is retired; multiplier metadata must come from '
                    'payload, output_shared/state/multiplier_cache.json, or OpenD refresh'
                )

    syms = resolve_watchlist_config(cfg)
    if not syms:
        die('symbols[] is required and cannot be empty')

    set_watchlist_config(cfg, syms)

    runtime = cfg.get('runtime') or {}
    if runtime and not isinstance(runtime, dict):
        die('runtime must be an object')
    if isinstance(runtime, dict):
        st = runtime.get('symbol_timeout_sec', 120)
        pt = runtime.get('portfolio_timeout_sec', 60)
        try:
            if st is not None and int(st) <= 0:
                die('runtime.symbol_timeout_sec must be > 0')
        except Exception:
            die('runtime.symbol_timeout_sec must be an integer')
        try:
            if pt is not None and int(pt) <= 0:
                die('runtime.portfolio_timeout_sec must be > 0')
        except Exception:
            die('runtime.portfolio_timeout_sec must be an integer')
        chain_fetch = runtime.get('option_chain_fetch')
        if chain_fetch is not None:
            if not isinstance(chain_fetch, dict):
                die('runtime.option_chain_fetch must be an object')
            validate_rate_limit_object(chain_fetch, 'runtime.option_chain_fetch')
        opend_rate_limits = runtime.get('opend_rate_limits')
        if opend_rate_limits is not None:
            if not isinstance(opend_rate_limits, dict):
                die('runtime.opend_rate_limits must be an object')
            for endpoint, raw in opend_rate_limits.items():
                if str(endpoint) not in OPEND_RATE_LIMIT_ENDPOINT_KEYS:
                    allowed = ', '.join(sorted(OPEND_RATE_LIMIT_ENDPOINT_KEYS))
                    die(f'runtime.opend_rate_limits.{endpoint} is not supported; use one of: {allowed}')
                if not isinstance(raw, dict):
                    die(f'runtime.opend_rate_limits.{endpoint} must be an object')
                validate_rate_limit_object(raw, f'runtime.opend_rate_limits.{endpoint}')

    _validate_inbound_config(cfg)

    _validate_assistant_config(cfg)

    notifications = cfg.get('notifications') or {}
    if notifications and not isinstance(notifications, dict):
        die('notifications must be an object')
    if isinstance(notifications, dict) and notifications:
        has_routing = any(notifications.get(k) for k in ('provider', 'channel', 'transport_channel', 'target'))
        if has_routing:
            raw_provider = str(notifications.get('provider') or '').strip().lower()
            raw_channel = str(notifications.get('channel') or '').strip().lower()
            raw_transport_channel = str(notifications.get('transport_channel') or '').strip().lower()
            removed_openclaw_values = {OPENCLAW_NOTIFICATION_PROVIDER, OPENCLAW_WEIXIN_TRANSPORT_CHANNEL}
            if (
                raw_provider in removed_openclaw_values
                or raw_channel in removed_openclaw_values
                or raw_transport_channel in removed_openclaw_values
            ):
                die('OpenClaw notification routing has been removed; use provider=wechat_clawbot, channel=wechat_clawbot')

            provider = normalize_notification_provider(notifications.get('provider') or notifications.get('channel'))
            if provider not in SUPPORTED_NOTIFICATION_PROVIDERS:
                allowed = ', '.join(SUPPORTED_NOTIFICATION_PROVIDERS)
                die(f'notifications.provider must be one of: {allowed}')

            target = notifications.get('target')
            if provider == FEISHU_APP_NOTIFICATION_PROVIDER and isinstance(target, str) and str(target).strip():
                die('notifications.target is not used for feishu_app; set OM_FEISHU_BOT_USER_OPEN_ID')
            if target is not None and not isinstance(target, str):
                die('notifications.target must be a string when configured')
            if not isinstance(target, str) or not str(target).strip():
                if provider == WECHAT_CLAWBOT_NOTIFICATION_PROVIDER:
                    die('notifications.target must be a non-empty wechat_clawbot binding string')
                if provider != FEISHU_APP_NOTIFICATION_PROVIDER:
                    die('notifications.target must be a non-empty open_id string')

            for key in ('wechat_clawbot_label', 'wechat_clawbot_state_dir'):
                if key in notifications and notifications.get(key) is not None and not isinstance(notifications.get(key), str):
                    die(f'notifications.{key} must be a string when configured')

    close_advice = cfg.get('close_advice') or {}
    if close_advice and not isinstance(close_advice, dict):
        die('close_advice must be an object')
    if isinstance(close_advice, dict):
        if 'strategy' in close_advice or 'strategy_profile' in close_advice:
            die('close_advice.strategy is not supported; close_advice uses sell_put/sell_call strategy')
        quote_source = str(close_advice.get('quote_source') or '').strip().lower()
        if quote_source and quote_source not in {'auto', 'required_data'}:
            die('close_advice.quote_source must be auto or required_data')
        notify_levels = close_advice.get('notify_levels')
        if notify_levels is not None:
            if not isinstance(notify_levels, list):
                die('close_advice.notify_levels must be a list')
            bad_levels = [
                str(item).strip().lower()
                for item in notify_levels
                if str(item).strip().lower() not in {'strong', 'medium', 'optional', 'weak'}
            ]
            if bad_levels:
                die(f"close_advice.notify_levels has unsupported levels: {', '.join(bad_levels)}")
        if 'max_items_per_account' in close_advice and close_advice.get('max_items_per_account') is not None:
            validate_non_negative_integer(
                close_advice.get('max_items_per_account'),
                'close_advice.max_items_per_account',
            )
        for key in ('max_spread_ratio', 'strong_remaining_annualized_max', 'medium_remaining_annualized_max'):
            if key not in close_advice or close_advice.get(key) is None:
                continue
            try:
                if float(close_advice.get(key)) < 0:
                    die(f'close_advice.{key} must be >= 0')
            except Exception:
                die(f'close_advice.{key} must be a number')

    alert_policy = cfg.get('alert_policy')
    if alert_policy is not None and not isinstance(alert_policy, (dict, str)):
        die('alert_policy must be an object or a path string')
    if isinstance(alert_policy, dict):
        if 'change_annual_threshold' in alert_policy and alert_policy.get('change_annual_threshold') is not None:
            try:
                if float(alert_policy.get('change_annual_threshold')) < 0:
                    die('alert_policy.change_annual_threshold must be >= 0')
            except Exception:
                die('alert_policy.change_annual_threshold must be a number')
        for sub_key, allowed_keys in (
            ('sell_put', {'high_annual', 'high_spread_max', 'medium_annual'}),
            ('sell_call', {'high_annual', 'high_total', 'medium_annual'}),
        ):
            sub = alert_policy.get(sub_key)
            if sub is None:
                continue
            if not isinstance(sub, dict):
                die(f'alert_policy.{sub_key} must be an object')
            for k, v in sub.items():
                if k not in allowed_keys:
                    die(f'alert_policy.{sub_key}.{k} is not a supported key; use one of: {", ".join(sorted(allowed_keys))}')
                if v is None:
                    continue
                try:
                    if float(v) < 0:
                        die(f'alert_policy.{sub_key}.{k} must be >= 0')
                except Exception:
                    die(f'alert_policy.{sub_key}.{k} must be a number')

    account_settings = cfg.get('account_settings') or {}
    if account_settings and not isinstance(account_settings, dict):
        die('account_settings must be an object')
    if isinstance(account_settings, dict):
        known_accounts = set(accounts_from_config(cfg))
        for raw_key, raw_value in account_settings.items():
            account = str(raw_key or '').strip().lower()
            if not account:
                die('account_settings contains empty account key')
            if account not in known_accounts:
                die(f'account_settings.{account} must also appear in top-level accounts')
            if not isinstance(raw_value, dict):
                die(f'account_settings.{account} must be an object')
            acct_type = str(raw_value.get('type') or '').strip().lower()
            if acct_type not in ACCOUNT_TYPES:
                die(f'account_settings.{account}.type must be one of: {", ".join(ACCOUNT_TYPES)}')
            holdings_account = raw_value.get('holdings_account')
            if holdings_account is not None and not str(holdings_account).strip():
                die(f'account_settings.{account}.holdings_account must be a non-empty string when set')
        account_settings_from_config(cfg)

    trade_intake = cfg.get('trade_intake') or {}
    if trade_intake and not isinstance(trade_intake, dict):
        die('trade_intake must be an object')
    if isinstance(trade_intake, dict):
        try:
            resolve_trade_intake_config(cfg)
        except ValueError as exc:
            die(str(exc))

    option_positions = cfg.get('option_positions') or {}
    if option_positions and not isinstance(option_positions, dict):
        die('option_positions must be an object')
    if isinstance(option_positions, dict):
        if 'sync_to_feishu' in option_positions:
            die('option_positions.sync_to_feishu has been removed; local SQLite trade_events are the source of truth')
        if 'bootstrap_from_feishu' in option_positions:
            die('option_positions.bootstrap_from_feishu has been removed; use explicit legacy inspect/migrate commands')
        auto_close = option_positions.get('auto_close') or {}
        if auto_close and not isinstance(auto_close, dict):
            die('option_positions.auto_close must be an object')
        if isinstance(auto_close, dict):
            if 'enabled' in auto_close and auto_close.get('enabled') is not None and not isinstance(auto_close.get('enabled'), bool):
                die('option_positions.auto_close.enabled must be a boolean')
            for key, min_value in (('grace_days', 0), ('max_close_per_run', 1), ('max_close', 1)):
                if key not in auto_close or auto_close.get(key) in (None, ''):
                    continue
                value = auto_close.get(key)
                if isinstance(value, bool):
                    die(f'option_positions.auto_close.{key} must be an integer')
                    continue
                if isinstance(value, int):
                    resolved = value
                elif isinstance(value, str) and value.strip().lstrip('-').isdigit():
                    resolved = int(value)
                else:
                    die(f'option_positions.auto_close.{key} must be an integer')
                    continue
                if resolved < min_value:
                    die(f'option_positions.auto_close.{key} must be >= {min_value}')
            try:
                resolve_auto_close_receipt_config(auto_close.get('receipt'))
            except ValueError as exc:
                die(str(exc))

    raw_templates = cfg.get('templates')
    if raw_templates is not None and not isinstance(raw_templates, dict):
        die('templates must be an object')
    templates = resolve_templates_config(cfg)

    # Strict config contract: global liquidity filters only support 3 hard fields.
    if isinstance(templates, dict):
        for profile_name, profile in templates.items():
            if not isinstance(profile, dict):
                continue
            for side in ('sell_put', 'sell_call'):
                side_cfg = profile.get(side)
                if not isinstance(side_cfg, dict):
                    continue
                bad_keys = [k for k in REMOVED_STRATEGY_FILTER_FIELDS if k in side_cfg]
                if bad_keys:
                    die(
                        f"templates.{profile_name}.{side} has unsupported strategy filter keys: "
                        f"{', '.join(bad_keys)}; only {', '.join(LIQUIDITY_ALLOWED_GLOBAL_FIELDS)} are allowed"
                    )
                _validate_score_weights(side_cfg, f'templates.{profile_name}.{side}')
                if side == 'sell_call':
                    _validate_optional_positive_number(
                        side_cfg,
                        'min_strike_cost_multiplier',
                        f'templates.{profile_name}.{side}',
                    )
                    unsupported_fetch_keys = [k for k in LEGACY_SELL_CALL_FETCH_FIELDS if k in side_cfg]
                    if unsupported_fetch_keys:
                        die(
                            f"templates.{profile_name}.{side} has removed legacy fetch planning keys: "
                            f"{', '.join(unsupported_fetch_keys)}; use min_strike/max_strike only"
                        )
                _validate_opening_strategy_config(
                    side_cfg,
                    f'templates.{profile_name}.{side}',
                )
                if side == 'sell_put':
                    unsupported_put_otm_keys = [k for k in LEGACY_SELL_PUT_OTM_FIELDS if k in side_cfg]
                    if unsupported_put_otm_keys:
                        die(
                            f"templates.{profile_name}.{side} has removed OTM fields: "
                            f"{', '.join(unsupported_put_otm_keys)}; use min_strike/max_strike only"
                        )
                    nested_combo_yield_keys = [key for key in ("combo_yield", "yield_enhancement") if side_cfg.get(key) is not None]
                    if nested_combo_yield_keys:
                        die(
                            f"templates.{profile_name}.sell_put.{nested_combo_yield_keys[0]} has been removed; "
                            f"use templates.{profile_name}.combo_yield instead"
                        )
            yield_enhancement_cfg = profile.get('combo_yield')
            if yield_enhancement_cfg is not None:
                _validate_yield_enhancement_cfg(
                    yield_enhancement_cfg,
                    f'templates.{profile_name}.combo_yield',
                )
            if profile.get('yield_enhancement') is not None:
                die(
                    f"templates.{profile_name}.yield_enhancement has been removed; "
                    "use templates.<name>.combo_yield instead"
                )
            if profile.get('rebound_combo') is not None:
                die(
                    f"templates.{profile_name}.rebound_combo has been removed; "
                    "use templates.<name>.combo_yield instead"
                )

    seen = set()
    for i, item in enumerate(cfg['symbols']):
        if not isinstance(item, dict):
            die(f"symbols[{i}] must be an object")
        sym = item.get('symbol')
        if not sym or not isinstance(sym, str):
            die(f"symbols[{i}].symbol is required")
        if sym in seen:
            die(f"duplicate symbol: {sym}")
        seen.add(sym)

        fetch = item.get('fetch') or {}
        if fetch and not isinstance(fetch, dict):
            die(f"{sym}.fetch must be an object")
        if isinstance(fetch, dict):
            src_raw = fetch.get('source', 'futu')
            src = normalize_fetch_source(src_raw)
            if src != 'opend':
                die(f"{sym}.fetch.source unsupported: {src_raw}; use futu")
            if str(src_raw or '').strip().lower() == 'opend':
                warn(f"{sym}.fetch.source=opend is legacy; prefer futu")

        # sell_put basic checks if enabled
        sp = item.get('sell_put') or {}
        if sp and not isinstance(sp, dict):
            die(f"{sym}.sell_put must be an object")
        _validate_score_weights(sp, f'{sym}.sell_put')
        _validate_opening_strategy_config(sp, f'{sym}.sell_put')
        unsupported_put_otm_keys = [k for k in LEGACY_SELL_PUT_OTM_FIELDS if k in sp]
        if unsupported_put_otm_keys:
            die(
                f"{sym}.sell_put has removed OTM fields: "
                f"{', '.join(unsupported_put_otm_keys)}; use min_strike/max_strike only"
            )
        bad_keys = [k for k in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS if k in sp]
        if bad_keys:
            die(f"{sym}.sell_put has forbidden symbol-level strategy filter keys: {', '.join(bad_keys)}")
        nested_combo_yield_keys = [key for key in ("combo_yield", "yield_enhancement") if sp.get(key) is not None]
        if nested_combo_yield_keys:
            die(f"{sym}.sell_put.{nested_combo_yield_keys[0]} has been removed; use {sym}.combo_yield instead")
        yield_enhancement_cfg = item.get('combo_yield')
        if yield_enhancement_cfg is not None:
            _validate_yield_enhancement_cfg(
                yield_enhancement_cfg,
                f'{sym}.combo_yield',
            )
        if item.get('yield_enhancement') is not None:
            die(f"{sym}.yield_enhancement has been removed; use {sym}.combo_yield instead")
        if sp.get('enabled'):
            _validate_enabled_side_template_strategy(
                sym=sym,
                side='sell_put',
                side_cfg=sp,
                item=item,
                templates=templates,
            )
            for k in ('min_dte', 'max_dte'):
                if k not in sp:
                    die(f"{sym}.sell_put enabled but missing {k}")
            if sp['min_dte'] > sp['max_dte']:
                die(f"{sym}.sell_put min_dte > max_dte")
            if ('min_strike' in sp) and (sp['min_strike'] is not None) and (float(sp['min_strike']) <= 0):
                die(f"{sym}.sell_put min_strike must be > 0; use null or omit it instead of 0")
            if ('max_strike' in sp) and (sp['max_strike'] is not None) and (float(sp['max_strike']) <= 0):
                die(f"{sym}.sell_put max_strike must be > 0")
            if (
                ('min_strike' in sp) and (sp['min_strike'] is not None)
                and ('max_strike' in sp) and (sp['max_strike'] is not None)
                and (float(sp['min_strike']) > float(sp['max_strike']))
            ):
                die(f"{sym}.sell_put min_strike > max_strike")
            if ('min_strike' in sp) and (sp.get('max_strike') is None):
                warn(f"{sym}.sell_put only sets min_strike; near-bound max_strike is recommended")
        elif isinstance(item.get('combo_yield'), dict) and item['combo_yield'].get('enabled'):
            warn(f"{sym}.combo_yield is enabled but sell_put is disabled; it will be ignored")

        sc = item.get('sell_call') or {}
        if sc and not isinstance(sc, dict):
            die(f"{sym}.sell_call must be an object")
        _validate_score_weights(sc, f'{sym}.sell_call')
        _validate_opening_strategy_config(sc, f'{sym}.sell_call')
        bad_keys = [k for k in SYMBOL_LEVEL_FORBIDDEN_STRATEGY_FIELDS if k in sc]
        if bad_keys:
            die(f"{sym}.sell_call has forbidden symbol-level strategy filter keys: {', '.join(bad_keys)}")
        unsupported_fetch_keys = [k for k in LEGACY_SELL_CALL_FETCH_FIELDS if k in sc]
        if unsupported_fetch_keys:
            die(
                f"{sym}.sell_call has removed legacy fetch planning keys: {', '.join(unsupported_fetch_keys)}; "
                "use min_strike/max_strike only"
            )
        _validate_optional_positive_number(sc, 'min_strike_cost_multiplier', f'{sym}.sell_call')
        if sc.get('enabled'):
            _validate_enabled_side_template_strategy(
                sym=sym,
                side='sell_call',
                side_cfg=sc,
                item=item,
                templates=templates,
            )
            # NOTE:
            # - sell_call cost basis/shares come from account portfolio_context at runtime.
            # - portfolio_context may be backed by OpenD or holdings depending on account/runtime settings.
            # - Therefore, do not require them in config validation.
            # - If portfolio_context is unavailable for an account, pipeline will skip sell_call for that account.
            for k in ('min_dte', 'max_dte'):
                if k not in sc:
                    die(f"{sym}.sell_call enabled but missing {k}")

            if sc['min_dte'] > sc['max_dte']:
                die(f"{sym}.sell_call min_dte > max_dte")
            if ('min_strike' in sc) and (sc['min_strike'] is not None) and (float(sc['min_strike']) <= 0):
                die(f"{sym}.sell_call min_strike must be > 0")
            if ('max_strike' in sc) and (sc['max_strike'] is not None) and (float(sc['max_strike']) <= 0):
                die(f"{sym}.sell_call max_strike must be > 0 when set")
            if (
                ('min_strike' in sc) and (sc['min_strike'] is not None)
                and ('max_strike' in sc) and (sc['max_strike'] is not None)
                and (float(sc['min_strike']) > float(sc['max_strike']))
            ):
                die(f"{sym}.sell_call min_strike > max_strike")
            if ('max_strike' in sc) and (sc.get('min_strike') is None):
                warn(f"{sym}.sell_call only sets max_strike; near-bound min_strike is recommended")

        if item.get('rebound_combo') is not None:
            die(f"{sym}.rebound_combo has been removed; use {sym}.combo_yield instead")
