from __future__ import annotations

from difflib import get_close_matches
import math
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
from src.application.account_config import (
    ACCOUNT_TYPES,
    account_settings_from_config,
    accounts_from_config,
    normalize_account_label,
    normalize_accounts,
    parse_lossless_integer,
)
from src.application.config_sections import resolve_templates_config, resolve_watchlist_config, set_watchlist_config
from src.application.llm_provider_registry import supported_llm_providers
from src.application.trades.account_mapping import resolve_trade_intake_config
from src.application.portfolio_management import (
    normalize_portfolio_management_config,
)
from src.application.positions.maintenance_receipt import resolve_auto_close_receipt_config
from src.application.opend_fetch_config import OPEND_RATE_LIMIT_ENDPOINT_KEYS
from src.application.combo_yield_config import (
    COMBO_YIELD_LEGACY_CALL_BOUND_FIELDS,
    COMBO_YIELD_LEGACY_CALL_OTM_FIELDS,
    COMBO_YIELD_LEGACY_OPTIMIZER_FIELDS,
    COMBO_YIELD_LEGACY_PUT_OTM_FIELDS,
    COMBO_YIELD_LEGACY_SCENARIO_FIELDS,
    COMBO_YIELD_OBJECTIVES,
    COMBO_YIELD_STRUCTURE_MODES,
    COMBO_YIELD_VARIANTS,
)

LIQUIDITY_ALLOWED_GLOBAL_FIELDS = (
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
)
COMBO_YIELD_LIQUIDITY_FIELDS = LIQUIDITY_ALLOWED_GLOBAL_FIELDS + (
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
WHEEL_ALLOWED_FIELDS = {
    'enabled',
    'accounts',
    'min_dte',
    'max_dte',
    'min_delta',
    'min_annualized_net_premium_return',
    'min_net_premium_cny',
    'max_spread_ratio',
    'min_iv_rv_ratio',
    'min_iv_minus_rv',
}
COMBO_YIELD_REMOVED_TARGET_FIELDS = (
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
    'api_key',
    'app_secret',
    'bot_token',
    'client_secret',
    'password',
    'private_key',
    'refresh_token',
    'secret',
    'token',
    'tenant_access_token',
}
INLINE_SECRET_CONFIG_SUFFIXES = (
    '_access_token',
    '_api_key',
    '_app_secret',
    '_bot_token',
    '_client_secret',
    '_password',
    '_private_key',
    '_refresh_token',
    '_secret',
)
NOTIFICATION_CONFIG_KEYS = {
    'cash_footer_accounts',
    'channel',
    'daily_brief',
    'enabled',
    'opend_alert_after_consecutive_failures',
    'opend_alert_burst_max',
    'opend_alert_burst_window_sec',
    'opend_alert_cooldown_sec',
    'opend_alert_send_recovery_notice',
    'provider',
    'render_style',
    'target',
    'transport_channel',
    'wechat_clawbot_label',
    'wechat_clawbot_state_dir',
}
NOTIFICATION_DAILY_BRIEF_KEYS = {
    'enabled',
    'max_actions_per_priority',
    'max_candidates_per_strategy',
    'max_rejection_reasons',
}
RETIRED_AI_ADVICE_CONFIG_KEYS = ('ai_interpretation', 'ai_strategy_advice')
WATCHDOG_CONFIG_KEYS = {
    'retry_enabled',
    'retry_interval_sec',
    'retry_timeout_sec',
    'success_threshold',
}
VALID_FUTU_TRD_ENVS = {'REAL', 'SIMULATE'}
ASSISTANT_CONFIG_KEYS = {
    'active_model',
    'context_window_messages',
    'copilot',
    'default_market_scope',
    'enabled',
    'llm',
    'models',
}
COPILOT_TOOLSET_KEYS = {'portfolio'}
COPILOT_CONFIG_KEYS = {'enabled', 'toolsets', 'tool_loading_mode'}
RETIRED_FEISHU_CALLBACK_KEYS = {
    'encrypt_key',
    'encrypt_key_env',
    'verification_token',
    'verification_token_env',
}
OPENING_STRATEGY_ALLOWED_FIELDS = {
    'enabled',
    'strategy',
    'min_dte',
    'max_dte',
    'min_strike',
    'max_strike',
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
    'min_net_income',
    'min_annualized_net_return',
    'min_annualized_net_premium_return',
    'min_iv_rv_ratio',
    'min_iv_minus_rv',
    'min_strike_cost_multiplier',
    # Retired fields stay recognized so their targeted migration errors remain
    # more useful than a generic unknown-key error.
    'strategy_profile',
    'pricing',
    'premium_score_cap',
    'score_weights',
    'short_vol',
    'concentration',
    'combo_yield',
    'yield_enhancement',
    *REMOVED_STRATEGY_FILTER_FIELDS,
    *LEGACY_SELL_CALL_FETCH_FIELDS,
    *LEGACY_SELL_PUT_OTM_FIELDS,
}
COMBO_YIELD_CALL_ALLOWED_FIELDS = {
    'min_delta',
    'max_delta',
    'min_strike',
    'max_strike',
    'min_dte',
    'max_dte',
    *COMBO_YIELD_LEGACY_CALL_OTM_FIELDS,
}
COMBO_YIELD_ALLOWED_FIELDS = {
    'enabled',
    'structure_mode',
    'objective',
    'variant',
    'output_mode',
    'min_combo_net_credit',
    'min_net_credit_annualized',
    'min_net_credit_retention',
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
    'max_combo_spread_ratio',
    'min_dte',
    'max_dte',
    'call',
    '_explicit_fields',
    '_explicit_call_fields',
    'strategy',
    'strategy_profile',
    *REMOVED_STRATEGY_FILTER_FIELDS,
    *COMBO_YIELD_REMOVED_TARGET_FIELDS,
    *COMBO_YIELD_LEGACY_SCENARIO_FIELDS,
    *COMBO_YIELD_LEGACY_OPTIMIZER_FIELDS,
    *COMBO_YIELD_LEGACY_CALL_BOUND_FIELDS,
    *COMBO_YIELD_LEGACY_PUT_OTM_FIELDS,
}


def die(msg: str):
    raise SystemExit(f"[CONFIG_ERROR] {msg}")


def warn(msg: str):
    print(f"[CONFIG_WARN] {msg}", file=sys.stderr)


def _reject_unknown_keys(cfg: dict, allowed: set[str], path: str) -> None:
    unknown = sorted(str(key) for key in cfg if str(key) not in allowed)
    if not unknown:
        return
    suggestions: list[str] = []
    choices = sorted(allowed)
    for key in unknown:
        match = get_close_matches(key, choices, n=1, cutoff=0.6)
        if match:
            suggestions.append(f'{key}->{match[0]}')
    hint = f"; did you mean: {', '.join(suggestions)}" if suggestions else ''
    die(f"{path} contains unsupported keys: {', '.join(unknown)}{hint}")


def _finite_number(value, path: str, *, expected: str = "a number") -> float:
    if isinstance(value, bool):
        die(f'{path} must be {expected}')
    try:
        parsed = float(value)
    except Exception:
        die(f'{path} must be {expected}')
    if not math.isfinite(parsed):
        die(f'{path} must be a finite number')
    return parsed


def validate_positive_number(value, path: str):
    if _finite_number(value, path) <= 0:
        die(f'{path} must be > 0')


def validate_positive_integer(value, path: str):
    numeric = _finite_number(value, path, expected="an integer")
    if not numeric.is_integer():
        die(f'{path} must be an integer')
    if int(numeric) <= 0:
        die(f'{path} must be > 0')


def validate_rate_limit_object(raw: dict, path: str):
    for key in ('window_sec', 'max_wait_sec'):
        if key in raw and raw.get(key) is not None:
            validate_positive_number(raw.get(key), f'{path}.{key}')
    if 'max_calls' in raw and raw.get('max_calls') is not None:
        validate_positive_integer(raw.get('max_calls'), f'{path}.max_calls')


def validate_non_negative_integer(value, path: str):
    numeric = _finite_number(value, path, expected="an integer")
    if not numeric.is_integer():
        die(f'{path} must be an integer')
    if int(numeric) < 0:
        die(f'{path} must be >= 0')


def _validate_wheel_config(raw, path: str, market_accounts: list[str]) -> None:
    if not isinstance(raw, dict):
        die(f'{path} must be an object')
    _reject_unknown_keys(raw, WHEEL_ALLOWED_FIELDS, path)
    enabled = raw.get('enabled')
    if not isinstance(enabled, bool):
        die(f'{path}.enabled must be a boolean')
    accounts = raw.get('accounts')
    if not isinstance(accounts, list):
        die(f'{path}.accounts must be a list')
    normalized = [str(value or '').strip().lower() for value in accounts]
    if any(not value for value in normalized):
        die(f'{path}.accounts contains an empty account')
    if len(normalized) != len(set(normalized)):
        die(f'{path}.accounts contains duplicate labels after trim + lowercase normalization')
    unknown = sorted(set(normalized) - set(market_accounts))
    if unknown:
        die(f'{path}.accounts contains accounts outside the current market: {", ".join(unknown)}')
    if enabled and not normalized:
        die(f'{path}.accounts must not be empty when enabled')
    for key in ('min_dte', 'max_dte'):
        validate_positive_integer(raw.get(key), f'{path}.{key}')
    if int(raw['min_dte']) > int(raw['max_dte']):
        die(f'{path}.min_dte must be <= {path}.max_dte')
    for key in ('min_delta', 'min_annualized_net_premium_return', 'max_spread_ratio'):
        value = _finite_number(raw.get(key), f'{path}.{key}')
        if value <= 0 or value > 1:
            die(f'{path}.{key} must be within (0, 1]')
    validate_positive_number(raw.get('min_net_premium_cny'), f'{path}.min_net_premium_cny')
    for key in ('min_iv_rv_ratio', 'min_iv_minus_rv'):
        if _finite_number(raw.get(key), f'{path}.{key}') < 0:
            die(f'{path}.{key} must be >= 0')


def _validate_optional_non_negative_number(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    if _finite_number(cfg.get(key), f'{path}.{key}') < 0:
        die(f'{path}.{key} must be >= 0')


def _validate_optional_positive_number(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    if _finite_number(cfg.get(key), f'{path}.{key}') <= 0:
        die(f'{path}.{key} must be > 0')


def _validate_optional_unit_interval_number(cfg: dict, key: str, path: str):
    if key not in cfg or cfg.get(key) is None:
        return
    value = _finite_number(cfg.get(key), f'{path}.{key}')
    if value < 0 or value > 1:
        die(f'{path}.{key} must be between 0 and 1')


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
    if 'context_window_tokens' in llm_cfg and llm_cfg.get('context_window_tokens') is not None:
        validate_positive_integer(llm_cfg.get('context_window_tokens'), f'{path}.context_window_tokens')
        context_window_tokens = int(llm_cfg.get('context_window_tokens'))
        if context_window_tokens < 4096:
            die(f'{path}.context_window_tokens must be >= 4096')
        if context_window_tokens > 2_000_000:
            die(f'{path}.context_window_tokens must be <= 2000000')
        max_output_tokens = int(llm_cfg.get('max_output_tokens') or 2048)
        if context_window_tokens <= max_output_tokens + 2000:
            die(f'{path}.context_window_tokens must exceed max_output_tokens by more than 2000')
    llm_provider = str(llm_cfg.get('provider') or '').strip()
    supported_providers = supported_llm_providers()
    if llm_provider and llm_provider not in supported_providers:
        die(f"{path}.provider must be one of: {', '.join(supported_providers)}")
    if enabled:
        if not llm_provider:
            die(f'{path}.provider is required when {required_reason}')
        if not str(llm_cfg.get('model') or '').strip():
            die(f'{path}.model is required when {required_reason}')
        if llm_cfg.get('context_window_tokens') is None:
            die(f'{path}.context_window_tokens is required when {required_reason}')


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
            _validate_optional_non_negative_number(wechat_clawbot, 'keepalive_interval_sec', 'inbound.wechat_clawbot')


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
    copilot = assistant.get('copilot')
    if copilot is None:
        copilot = {}
    if not isinstance(copilot, dict):
        die('assistant.copilot must be an object')
    unsupported_copilot = sorted(str(key) for key in copilot if key not in COPILOT_CONFIG_KEYS)
    if unsupported_copilot:
        die(f'assistant.copilot contains unsupported keys: {", ".join(unsupported_copilot)}')
    if 'enabled' in copilot and copilot.get('enabled') is not None and not isinstance(copilot.get('enabled'), bool):
        die('assistant.copilot.enabled must be a boolean')
    if 'tool_loading_mode' in copilot:
        mode = str(copilot.get('tool_loading_mode') or '').strip().lower()
        if mode not in {'eager', 'directory'}:
            die('assistant.copilot.tool_loading_mode must be one of: eager, directory')
    toolsets = copilot.get('toolsets')
    if toolsets is None:
        toolsets = {}
    if not isinstance(toolsets, dict):
        die('assistant.copilot.toolsets must be an object')
    unsupported_toolsets = sorted(str(key) for key in toolsets if key not in COPILOT_TOOLSET_KEYS)
    if unsupported_toolsets:
        die(f'assistant.copilot.toolsets contains unsupported keys: {", ".join(unsupported_toolsets)}')
    for name, value in toolsets.items():
        if not isinstance(value, bool):
            die(f'assistant.copilot.toolsets.{name} must be a boolean')
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
        die('assistant.llm.enabled is retired; use assistant.copilot.enabled')
    _validate_llm_config(
        llm,
        path='assistant.llm',
        enabled=bool(
            assistant.get('enabled') is not False
            and copilot.get('enabled') is True
            and str(llm.get('provider') or '').strip()
            and str(llm.get('model') or '').strip()
        ),
        required_reason='assistant Copilot uses LLM',
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


def _validate_opening_strategy_config(cfg: dict, path: str) -> None:
    _reject_unknown_keys(cfg, OPENING_STRATEGY_ALLOWED_FIELDS, path)
    _validate_optional_bool(cfg, 'enabled', path)
    if 'strategy_profile' in cfg:
        die(f'{path}.strategy_profile has been removed; use {path}.strategy')
    if 'pricing' in cfg:
        die(f'{path}.pricing has been removed; put opening thresholds directly on {path}')
    if 'premium_score_cap' in cfg:
        die(f'{path}.premium_score_cap is not a supported opening config field')
    if cfg.get('score_weights') is not None:
        die(
            f'{path}.score_weights has been removed from opening config; '
            'formal recommendation ranking uses annualized net return with fixed tie-breaks'
        )
    _validate_optional_dte_window(cfg, path)
    _validate_optional_strike_bounds(cfg, path)
    for key in ('min_open_interest', 'min_volume', 'max_spread_ratio', 'min_net_income'):
        _validate_optional_non_negative_number(cfg, key, path)
    for key in ('min_annualized_net_return', 'min_annualized_net_premium_return'):
        _validate_optional_unit_interval_number(cfg, key, path)

    strategy = 'insurance_underwriting'
    if 'strategy' in cfg and cfg.get('strategy') is not None:
        strategy = str(cfg.get('strategy') or '').strip().lower()
        if strategy != 'insurance_underwriting':
            die(
                f'{path}.strategy={strategy or "<empty>"} is no longer supported for opening config; '
                'use insurance_underwriting'
            )
    short_vol = cfg.get('short_vol')
    if short_vol is not None:
        die(
            f'{path}.short_vol has been removed from opening config; '
            'put min_iv_rv_ratio and min_iv_minus_rv on the opening config'
        )
    for key in ('min_iv_rv_ratio', 'min_iv_minus_rv'):
        _validate_optional_non_negative_number(cfg, key, path)
    concentration = cfg.get('concentration')
    if concentration is not None:
        die(f'{path}.concentration has been removed from opening config; manage assignment exposure outside candidate scan')


def validate_resolved_watchlist_item_runtime_config(item: dict) -> None:
    if not isinstance(item, dict):
        die('resolved watchlist item must be an object')
    symbol = str(item.get('symbol') or 'UNKNOWN')
    for side in ('sell_put', 'sell_call'):
        side_cfg = item.get(side) or {}
        if not isinstance(side_cfg, dict):
            die(f'{symbol}.{side} must be an object after template merge')
        _validate_opening_strategy_config(side_cfg, f'{symbol}.{side}')
        if side == 'sell_call':
            _validate_optional_positive_number(
                side_cfg,
                'min_strike_cost_multiplier',
                f'{symbol}.{side}',
            )
    combo_cfg = item.get('combo_yield')
    if combo_cfg is not None:
        _validate_combo_yield_cfg(combo_cfg, f'{symbol}.combo_yield')
    if item.get('yield_enhancement') is not None:
        die(f'{symbol}.yield_enhancement has been removed; use {symbol}.combo_yield instead')


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


def _validate_optional_strike_bounds(cfg: dict, path: str):
    min_strike = cfg.get('min_strike')
    max_strike = cfg.get('max_strike')
    if min_strike is not None:
        _validate_optional_positive_number(cfg, 'min_strike', path)
    if max_strike is not None:
        _validate_optional_positive_number(cfg, 'max_strike', path)
    if (min_strike is not None) and (max_strike is not None):
        if _finite_number(min_strike, f'{path}.min_strike') > _finite_number(max_strike, f'{path}.max_strike'):
            die(f'{path}.min_strike > {path}.max_strike')


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


def _validate_combo_yield_cfg(cfg: dict, path: str):
    if not isinstance(cfg, dict):
        die(f'{path} must be an object')
    removed_gap_keys = [
        key
        for key in ('min_expiry_gap_days', 'max_expiry_gap_days')
        if key in cfg
    ]
    if removed_gap_keys:
        die(
            f"{path} has removed staggered-expiry gap fields: {', '.join(removed_gap_keys)}; "
            "Combo Yield supports same_expiry_pair only"
        )
    _reject_unknown_keys(cfg, COMBO_YIELD_ALLOWED_FIELDS, path)
    _validate_optional_bool(cfg, 'enabled', path)
    removed_funding_keys = [
        key
        for key in ('funding_mode', 'max_call_cost_to_put_credit', 'max_debit', 'max_debit_native')
        if key in cfg
    ]
    if removed_funding_keys:
        die(
            f"{path} has removed funding-mode fields: {', '.join(removed_funding_keys)}; "
            "use min_net_credit_retention as the single call-cost constraint"
        )
    if 'strategy' in cfg or 'strategy_profile' in cfg:
        die(f'{path}.strategy is not supported; combo_yield is isolated from sell_put.strategy')
    bad_keys = [k for k in REMOVED_STRATEGY_FILTER_FIELDS if k in cfg]
    if bad_keys:
        die(f"{path} has unsupported strategy filter keys: {', '.join(bad_keys)}")
    removed_target_keys = [k for k in COMBO_YIELD_REMOVED_TARGET_FIELDS if k in cfg]
    if removed_target_keys:
        die(
            f"{path} has removed target-price fields: {', '.join(removed_target_keys)}; "
            "combo_yield uses price bounds and funding economics"
        )
    legacy_scenario_keys = [k for k in COMBO_YIELD_LEGACY_SCENARIO_FIELDS if k in cfg]
    if legacy_scenario_keys:
        die(
            f"{path} has removed scenario fields: {', '.join(legacy_scenario_keys)}; "
            "use funding, call cost, strike bounds, and delta controls instead"
        )
    legacy_optimizer_keys = [k for k in COMBO_YIELD_LEGACY_OPTIMIZER_FIELDS if k in cfg]
    if legacy_optimizer_keys:
        die(
            f"{path} has removed optimizer fields: {', '.join(legacy_optimizer_keys)}; "
            "use funding, call cost, strike bounds, and delta controls instead"
        )
    legacy_call_bound_keys = [k for k in COMBO_YIELD_LEGACY_CALL_BOUND_FIELDS if k in cfg]
    if legacy_call_bound_keys:
        die(
            f"{path} has removed call OTM fields: {', '.join(legacy_call_bound_keys)}; "
            "use combo_yield.call.min_strike/max_strike instead"
        )
    legacy_put_otm_keys = [k for k in COMBO_YIELD_LEGACY_PUT_OTM_FIELDS if k in cfg]
    if legacy_put_otm_keys:
        die(
            f"{path} has removed put OTM fields: {', '.join(legacy_put_otm_keys)}; "
            "use sell_put.min_strike/max_strike assignment bounds instead"
        )
    if 'objective' in cfg and cfg.get('objective') is not None:
        objective = str(cfg.get('objective') or '').strip().lower()
        if objective not in COMBO_YIELD_OBJECTIVES:
            die(f"{path}.objective must be one of: {', '.join(sorted(COMBO_YIELD_OBJECTIVES))}")
    if 'output_mode' in cfg:
        die(
            f"{path}.output_mode has been removed; Combo Yield candidate output is sealed JSON only; "
            "rebuild runtime config with ./om config build"
        )
    if 'structure_mode' in cfg and cfg.get('structure_mode') is not None:
        structure_mode = str(cfg.get('structure_mode') or '').strip().lower()
        if structure_mode not in COMBO_YIELD_STRUCTURE_MODES:
            die(f"{path}.structure_mode must be one of: {', '.join(sorted(COMBO_YIELD_STRUCTURE_MODES))}")
    if 'variant' in cfg and cfg.get('variant') is not None:
        variant = str(cfg.get('variant') or '').strip().lower()
        if variant not in COMBO_YIELD_VARIANTS:
            die(f"{path}.variant must be one of: {', '.join(sorted(COMBO_YIELD_VARIANTS))}")
    for key in COMBO_YIELD_LIQUIDITY_FIELDS:
        _validate_optional_non_negative_number(cfg, key, path)
    for key in (
        'min_combo_net_credit',
        'min_net_credit_annualized',
    ):
        _validate_optional_non_negative_number(cfg, key, path)
    _validate_optional_unit_interval_number(cfg, 'min_net_credit_retention', path)
    _validate_optional_dte_window(cfg, path)

    call_leg = cfg.get('call')
    if call_leg is not None and not isinstance(call_leg, dict):
        die(f'{path}.call must be an object')
    if isinstance(call_leg, dict):
        _reject_unknown_keys(call_leg, COMBO_YIELD_CALL_ALLOWED_FIELDS, f'{path}.call')
        redundant_call_dte_keys = [
            key for key in ('min_dte', 'max_dte') if call_leg.get(key) is not None
        ]
        if redundant_call_dte_keys:
            die(
                f"{path}.call has unsupported absolute DTE fields: "
                f"{', '.join(redundant_call_dte_keys)}; "
                "combo_yield.call DTE is derived from sell_put; use sell_put.min_dte/max_dte instead"
            )
        _validate_optional_dte_window(call_leg, f'{path}.call')
        _validate_optional_strike_bounds(call_leg, f'{path}.call')
        legacy_call_otm_keys = [k for k in COMBO_YIELD_LEGACY_CALL_OTM_FIELDS if k in call_leg]
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
            if _finite_number(min_delta, f'{path}.call.min_delta') > _finite_number(max_delta, f'{path}.call.max_delta'):
                die(f'{path}.call.min_delta > {path}.call.max_delta')


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
    _reject_unknown_keys(
        raw,
        {'enabled', 'timezone', 'beijing_timezone', 'cron_interval_min', 'run_window', 'run_points', 'gates'},
        path,
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
        _reject_unknown_keys(run_window, {'start', 'end', 'breaks'}, f'{path}.run_window')
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
            _reject_unknown_keys(item, {'start', 'end'}, f'{path}.run_window.breaks[{index}]')
            _validate_hhmm(item.get('start'), f'{path}.run_window.breaks[{index}].start')
            _validate_hhmm(item.get('end'), f'{path}.run_window.breaks[{index}].end')

    run_points = raw.get('run_points')
    if run_points is not None:
        if not isinstance(run_points, dict):
            die(f'{path}.run_points must be an object')
        _reject_unknown_keys(
            run_points,
            {'start_plus_min', 'hourly_minute', 'end_minus_min'},
            f'{path}.run_points',
        )
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
            _reject_unknown_keys(
                gate,
                {'type', 'timezone', 'time', 'day_offset_from_window_start'},
                f'{path}.gates[{index}]',
            )
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
                die(
                    f'{child_path} is no longer supported; Feishu inbound uses long-connection '
                    'Bot settings and the feishu.bot.app_secret logical credential'
                )
            is_secret_key = (
                key_lower in INLINE_SECRET_CONFIG_KEYS
                or key_lower.endswith(INLINE_SECRET_CONFIG_SUFFIXES)
            )
            if (
                is_secret_key
                and not key_lower.endswith('_env')
                and isinstance(item, str)
                and item.strip()
            ):
                die(
                    f'{child_path} must not contain inline secret material; provision the '
                    'corresponding logical credential instead'
                )
            _validate_no_inline_secrets_or_retired_callback_cfg(item, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_no_inline_secrets_or_retired_callback_cfg(item, f'{path}[{index}]')


def _validate_futu_trd_env(value, path: str) -> None:
    if value in (None, ''):
        return
    normalized = str(value).strip().upper()
    if normalized not in VALID_FUTU_TRD_ENVS:
        die(f'{path} must be one of: REAL, SIMULATE')


def _validate_watchdog_config(value) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        die('watchdog must be an object')
    unknown = sorted(str(key) for key in value if str(key) not in WATCHDOG_CONFIG_KEYS)
    if unknown:
        die(f"watchdog contains unsupported keys: {', '.join(unknown)}")
    if 'retry_enabled' in value and not isinstance(value.get('retry_enabled'), bool):
        die('watchdog.retry_enabled must be a boolean')
    for key in ('retry_interval_sec', 'retry_timeout_sec'):
        if key not in value:
            continue
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            die(f'watchdog.{key} must be a number')
        if raw <= 0:
            die(f'watchdog.{key} must be > 0')
    if 'success_threshold' in value:
        raw = value.get('success_threshold')
        if isinstance(raw, bool) or not isinstance(raw, int):
            die('watchdog.success_threshold must be an integer')
        if raw < 1:
            die('watchdog.success_threshold must be >= 1')


def _validate_template_use(item: dict, *, templates: dict, symbol: str) -> None:
    if 'use' not in item:
        return
    raw = item.get('use')
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            die(f'{symbol}.use must not be empty')
        names = [name]
    elif isinstance(raw, list):
        if not raw:
            die(f'{symbol}.use must not be empty')
        if any(not isinstance(value, str) or not value.strip() for value in raw):
            die(f'{symbol}.use must contain only non-empty strings')
        names = [value.strip() for value in raw]
    else:
        die(f'{symbol}.use must be a string or list of strings')
        return
    if len(set(names)) != len(names):
        die(f'{symbol}.use must not contain duplicate template references')
    unknown = [name for name in names if not isinstance(templates.get(name), dict)]
    if unknown:
        die(f"{symbol}.use references unknown templates: {', '.join(unknown)}")


def validate_config(cfg: dict):
    try:
        normalize_portfolio_management_config(cfg)
    except ValueError as exc:
        die(str(exc))
    if 'watchlist' in cfg:
        die('watchlist is no longer supported; use symbols')
    if 'profiles' in cfg:
        die('profiles is no longer supported; use templates')
    if 'fees' in cfg:
        die('fees is no longer supported; fee rules are built in')
    if 'ai_decision_advice' in cfg:
        die('ai_decision_advice is retired and must be removed')

    for retired_key in RETIRED_AI_ADVICE_CONFIG_KEYS:
        if retired_key in cfg:
            die(
                f'{retired_key} is retired and must be removed'
            )

    if 'accounts' in cfg:
        raw_accounts = cfg.get('accounts')
        try:
            normalized_accounts = normalize_accounts(
                raw_accounts,
                fallback=(),
                strict=True,
            )
        except ValueError as exc:
            die(f'accounts contains invalid label or scope: {exc}')
        raw_account_count = (
            1 if isinstance(raw_accounts, str) else len(raw_accounts)
        )
        if len(normalized_accounts) != raw_account_count:
            die('accounts contains duplicate labels after trim + lowercase normalization')

    wheel = cfg.get('wheel')
    if wheel is not None:
        _validate_wheel_config(wheel, 'wheel', accounts_from_config(cfg))

    _validate_no_inline_secrets_or_retired_callback_cfg(cfg)
    _validate_watchdog_config(cfg.get('watchdog'))

    _validate_schedule_cfg(cfg.get('schedule'), 'schedule')
    _validate_schedule_cfg(cfg.get('schedule_hk'), 'schedule_hk')

    portfolio_cfg = cfg.get('portfolio') or {}
    if portfolio_cfg and not isinstance(portfolio_cfg, dict):
        die('portfolio must be an object')
    if isinstance(portfolio_cfg, dict):
        portfolio_futu = portfolio_cfg.get('futu')
        if portfolio_futu is not None and not isinstance(portfolio_futu, dict):
            die('portfolio.futu must be an object')
        if isinstance(portfolio_futu, dict):
            _validate_futu_trd_env(
                portfolio_futu.get('trd_env'),
                'portfolio.futu.trd_env',
            )

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
        unknown_notification_keys = sorted(
            str(key)
            for key in notifications
            if str(key) not in NOTIFICATION_CONFIG_KEYS
        )
        if unknown_notification_keys:
            die(
                'notifications contains unsupported keys: '
                + ', '.join(unknown_notification_keys)
            )
        daily_brief = notifications.get('daily_brief')
        if daily_brief is not None:
            if not isinstance(daily_brief, dict):
                die('notifications.daily_brief must be an object')
            unknown_daily_brief_keys = sorted(
                str(key)
                for key in daily_brief
                if str(key) not in NOTIFICATION_DAILY_BRIEF_KEYS
            )
            if unknown_daily_brief_keys:
                die(
                    'notifications.daily_brief contains unsupported keys: '
                    + ', '.join(unknown_daily_brief_keys)
                )
            if 'enabled' in daily_brief and not isinstance(daily_brief.get('enabled'), bool):
                die('notifications.daily_brief.enabled must be a boolean')
            if 'enabled' in daily_brief:
                warn(
                    'NOTIFICATIONS_DAILY_BRIEF_ENABLED_DEPRECATED: '
                    'notifications.daily_brief.enabled is deprecated and ignored; '
                    'scheduled ordinary notifications always use daily_brief'
                )
            for key in ('max_actions_per_priority', 'max_candidates_per_strategy', 'max_rejection_reasons'):
                if key not in daily_brief:
                    continue
                value = daily_brief.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    die(f'notifications.daily_brief.{key} must be an integer')
                if value < 1 or value > 20:
                    die(f'notifications.daily_brief.{key} must be between 1 and 20')

        if 'render_style' in notifications:
            render_style = notifications.get('render_style')
            if not isinstance(render_style, str):
                die('notifications.render_style must be one of: compact, legacy')
            normalized_render_style = render_style.strip().lower()
            if normalized_render_style not in {'compact', 'legacy'}:
                die('notifications.render_style must be one of: compact, legacy')
            warn(
                'NOTIFICATIONS_RENDER_STYLE_DEPRECATED: '
                f'notifications.render_style={normalized_render_style} is deprecated and ignored; '
                'scheduled ordinary notifications always use daily_brief'
            )

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
        if 'enabled' in notifications and not isinstance(notifications.get('enabled'), bool):
            die('notifications.enabled must be a boolean')
        cash_footer_accounts = notifications.get('cash_footer_accounts')
        if cash_footer_accounts is not None:
            if not isinstance(cash_footer_accounts, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in cash_footer_accounts
            ):
                die('notifications.cash_footer_accounts must be a list of non-empty strings')
        for key in (
            'opend_alert_cooldown_sec',
            'opend_alert_burst_window_sec',
            'opend_alert_burst_max',
            'opend_alert_after_consecutive_failures',
        ):
            if key not in notifications:
                continue
            value = notifications.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                die(f'notifications.{key} must be an integer')
            if value < 1:
                die(f'notifications.{key} must be >= 1')
        if (
            'opend_alert_send_recovery_notice' in notifications
            and not isinstance(notifications.get('opend_alert_send_recovery_notice'), bool)
        ):
            die('notifications.opend_alert_send_recovery_notice must be a boolean')

    close_advice = cfg.get('close_advice') or {}
    if close_advice and not isinstance(close_advice, dict):
        die('close_advice must be an object')
    if isinstance(close_advice, dict):
        if 'position_advice_authority' in close_advice:
            die(
                'close_advice.position_advice_authority has been removed; '
                'there is no Position Advice v2 authority or control plane'
            )
        if 'strategy' in close_advice or 'strategy_profile' in close_advice:
            die(
                'close_advice.strategy is not supported; '
                'close_advice uses the fixed strict_profit_capture.v1 policy'
            )
        if 'optimizer' in close_advice:
            die('close_advice.optimizer has been removed')
        quote_source = str(close_advice.get('quote_source') or '').strip().lower()
        if quote_source and quote_source not in {'auto', 'required_data'}:
            die('close_advice.quote_source must be auto or required_data')
        ignored_strict_policy_keys = sorted(
            key
            for key in (
                'notify_levels',
                'max_spread_ratio',
                'strong_remaining_annualized_max',
                'medium_remaining_annualized_max',
                'quote_max_age_sec',
            )
            if key in close_advice
        )
        if ignored_strict_policy_keys:
            warn(
                'CLOSE_ADVICE_STRICT_POLICY_KEYS_IGNORED: '
                'strict_profit_capture.v1 uses fixed versioned thresholds; '
                'remove close_advice.'
                + ', close_advice.'.join(ignored_strict_policy_keys)
            )
        if 'max_items_per_account' in close_advice and close_advice.get('max_items_per_account') is not None:
            validate_non_negative_integer(
                close_advice.get('max_items_per_account'),
                'close_advice.max_items_per_account',
            )
    alert_policy = cfg.get('alert_policy')
    if alert_policy is not None and not isinstance(alert_policy, (dict, str)):
        die('alert_policy must be an object or a path string')
    if isinstance(alert_policy, dict):
        if 'change_annual_threshold' in alert_policy and alert_policy.get('change_annual_threshold') is not None:
            if _finite_number(
                alert_policy.get('change_annual_threshold'),
                'alert_policy.change_annual_threshold',
            ) < 0:
                die('alert_policy.change_annual_threshold must be >= 0')
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
                if _finite_number(v, f'alert_policy.{sub_key}.{k}') < 0:
                    die(f'alert_policy.{sub_key}.{k} must be >= 0')

    account_settings = cfg.get('account_settings') or {}
    if account_settings and not isinstance(account_settings, dict):
        die('account_settings must be an object')
    if isinstance(account_settings, dict):
        normalized_setting_labels = [
            str(item or '').strip().lower()
            for item in account_settings
            if str(item or '').strip()
        ]
        if len(normalized_setting_labels) != len(
            set(normalized_setting_labels)
        ):
            die(
                'account_settings contains duplicate labels after '
                'trim + lowercase normalization'
            )
        known_accounts = set(accounts_from_config(cfg))
        futu_account_settings = []
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
            if 'trade_intake_enabled' in raw_value and not isinstance(raw_value.get('trade_intake_enabled'), bool):
                die(f'account_settings.{account}.trade_intake_enabled must be a boolean')
            holdings_account = raw_value.get('holdings_account')
            if holdings_account is not None and not str(holdings_account).strip():
                die(f'account_settings.{account}.holdings_account must be a non-empty string when set')
            if acct_type == 'futu':
                futu_cfg = raw_value.get('futu')
                if not isinstance(futu_cfg, dict):
                    die(f'account_settings.{account}.futu must be an object')
                if not str(futu_cfg.get('account_id') or '').strip():
                    die(f'account_settings.{account}.futu.account_id must be a non-empty string')
                _validate_futu_trd_env(
                    futu_cfg.get('trd_env'),
                    f'account_settings.{account}.futu.trd_env',
                )
                for key in ('port', 'telnet_port'):
                    if key not in futu_cfg or futu_cfg.get(key) in (None, ''):
                        continue
                    value = futu_cfg.get(key)
                    parsed_port = parse_lossless_integer(value)
                    if parsed_port is None:
                        die(f'account_settings.{account}.futu.{key} must be an integer')
                    if parsed_port < 1 or parsed_port > 65535:
                        die(f'account_settings.{account}.futu.{key} must be between 1 and 65535')
                futu_account_settings.append((account, futu_cfg))
        if len(futu_account_settings) > 1:
            for account, futu_cfg in futu_account_settings:
                if not str(futu_cfg.get('host') or '').strip():
                    die(f'account_settings.{account}.futu.host must be set when multiple futu accounts are configured')
                if futu_cfg.get('port') in (None, ''):
                    die(f'account_settings.{account}.futu.port must be set when multiple futu accounts are configured')
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
            combo_yield_cfg = profile.get('combo_yield')
            if combo_yield_cfg is not None:
                _validate_combo_yield_cfg(
                    combo_yield_cfg,
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
        _validate_template_use(item, templates=templates, symbol=sym)

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
            _validate_futu_trd_env(
                fetch.get('trd_env'),
                f'{sym}.fetch.trd_env',
            )
            if fetch.get('limit_expirations') is not None:
                warn(
                    f"{sym}.fetch.limit_expirations is ignored by strategy required-data "
                    "planning; it remains a generic compatibility-fetch limit only"
                )

        # sell_put basic checks if enabled
        sp = item.get('sell_put') or {}
        if sp and not isinstance(sp, dict):
            die(f"{sym}.sell_put must be an object")
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
        combo_yield_cfg = item.get('combo_yield')
        if combo_yield_cfg is not None:
            _validate_combo_yield_cfg(
                combo_yield_cfg,
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
            if ('min_strike' in sp) and (sp['min_strike'] is not None) and (_finite_number(sp['min_strike'], f'{sym}.sell_put.min_strike') <= 0):
                die(f"{sym}.sell_put min_strike must be > 0; use null or omit it instead of 0")
            if ('max_strike' in sp) and (sp['max_strike'] is not None) and (_finite_number(sp['max_strike'], f'{sym}.sell_put.max_strike') <= 0):
                die(f"{sym}.sell_put max_strike must be > 0")
            if (
                ('min_strike' in sp) and (sp['min_strike'] is not None)
                and ('max_strike' in sp) and (sp['max_strike'] is not None)
                and (_finite_number(sp['min_strike'], f'{sym}.sell_put.min_strike') > _finite_number(sp['max_strike'], f'{sym}.sell_put.max_strike'))
            ):
                die(f"{sym}.sell_put min_strike > max_strike")
            if ('min_strike' in sp) and (sp.get('max_strike') is None):
                warn(f"{sym}.sell_put only sets min_strike; near-bound max_strike is recommended")
        sc = item.get('sell_call') or {}
        if sc and not isinstance(sc, dict):
            die(f"{sym}.sell_call must be an object")
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
            if ('min_strike' in sc) and (sc['min_strike'] is not None) and (_finite_number(sc['min_strike'], f'{sym}.sell_call.min_strike') <= 0):
                die(f"{sym}.sell_call min_strike must be > 0")
            if ('max_strike' in sc) and (sc['max_strike'] is not None) and (_finite_number(sc['max_strike'], f'{sym}.sell_call.max_strike') <= 0):
                die(f"{sym}.sell_call max_strike must be > 0 when set")
            if (
                ('min_strike' in sc) and (sc['min_strike'] is not None)
                and ('max_strike' in sc) and (sc['max_strike'] is not None)
                and (_finite_number(sc['min_strike'], f'{sym}.sell_call.min_strike') > _finite_number(sc['max_strike'], f'{sym}.sell_call.max_strike'))
            ):
                die(f"{sym}.sell_call min_strike > max_strike")
            if ('max_strike' in sc) and (sc.get('min_strike') is None):
                warn(f"{sym}.sell_call only sets max_strike; near-bound min_strike is recommended")

        if item.get('rebound_combo') is not None:
            die(f"{sym}.rebound_combo has been removed; use {sym}.combo_yield instead")
