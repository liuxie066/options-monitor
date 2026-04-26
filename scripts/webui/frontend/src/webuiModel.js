export const MARKETS = [
  { key: 'hk', label: 'HK', name: '港股市场' },
  { key: 'us', label: 'US', name: '美股市场' },
];

export const MODULES = [
  ['market', '行情设置'],
  ['accounts', '账户设置'],
  ['strategy', '选股策略'],
  ['closeAdvice', '平仓建议'],
  ['notifications', '消息通知'],
  ['advanced', '高级设置'],
];

export const STRATEGY_FIELDS = [
  ['min_dte', '最小 DTE'],
  ['max_dte', '最大 DTE'],
  ['min_strike', '最低行权价'],
  ['max_strike', '最高行权价'],
  ['min_annualized_net_return', '最低年化收益'],
  ['min_net_income', '最低净收入'],
  ['min_open_interest', '最低持仓量'],
  ['min_volume', '最低成交量'],
  ['max_spread_ratio', '最大价差比'],
];

export function toAccountsList(raw) {
  const text = String(raw || '').trim();
  if (!text) return null;
  return text.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
}

export function marketMetaFor(key) {
  return MARKETS.find((item) => item.key === key) || MARKETS[0];
}

export function emptyEditor(configKey) {
  return {
    configKey,
    marketData: { source: 'OpenD', host: '', port: '', mode: 'compat_global' },
    accounts: [],
    notifications: {
      channel: 'feishu', target: '', appId: '', appSecret: '',
      secretsFile: 'secrets/notifications.feishu.app.json',
      includeCashFooter: true, cashFooterAccounts: [], quietHoursStart: '', quietHoursEnd: '', hasCredentials: false,
    },
  };
}

export function buildGlobalForm(summary, editor) {
  const sections = summary?.sections || {};
  const closeAdvice = sections.close_advice || {};
  const notifications = sections.notifications || {};
  const quiet = notifications.quiet_hours_beijing || {};
  const marketData = sections.market_data || editor?.marketData || {};
  const buildSide = (src) => Object.fromEntries(STRATEGY_FIELDS.map(([key]) => [key, src?.[key] == null ? '' : String(src[key])]));
  return {
    marketData: {
      source: String(marketData.source || 'OpenD'),
      host: marketData.host == null ? '' : String(marketData.host),
      port: marketData.port == null ? '' : String(marketData.port),
      mode: String(marketData.mode || 'compat_global'),
    },
    strategy: {
      sell_put: buildSide(summary?.globalStrategy?.sell_put || {}),
      sell_call: buildSide(summary?.globalStrategy?.sell_call || {}),
    },
    closeAdvice: {
      enabled: closeAdvice.enabled !== false,
      quote_source: closeAdvice.quote_source == null ? 'auto' : String(closeAdvice.quote_source),
      notify_levels: Array.isArray(closeAdvice.notify_levels) ? closeAdvice.notify_levels.join(',') : 'strong,medium',
      max_items_per_account: closeAdvice.max_items_per_account == null ? '' : String(closeAdvice.max_items_per_account),
      max_spread_ratio: closeAdvice.max_spread_ratio == null ? '' : String(closeAdvice.max_spread_ratio),
      strong_remaining_annualized_max: closeAdvice.strong_remaining_annualized_max == null ? '' : String(closeAdvice.strong_remaining_annualized_max),
      medium_remaining_annualized_max: closeAdvice.medium_remaining_annualized_max == null ? '' : String(closeAdvice.medium_remaining_annualized_max),
    },
    notifications: {
      enabled: !!notifications.enabled,
      channel: String(editor?.notifications?.channel || notifications.channel || 'feishu'),
      target: String(editor?.notifications?.target || notifications.target || ''),
      appId: String(editor?.notifications?.appId || ''),
      appSecret: String(editor?.notifications?.appSecret || ''),
      secretsFile: String(editor?.notifications?.secretsFile || 'secrets/notifications.feishu.app.json'),
      hasCredentials: !!editor?.notifications?.hasCredentials,
      include_cash_footer: editor?.notifications?.includeCashFooter ?? notifications.include_cash_footer !== false,
      cash_footer_accounts: Array.isArray(editor?.notifications?.cashFooterAccounts) ? editor.notifications.cashFooterAccounts.join(',') : Array.isArray(notifications.cash_footer_accounts) ? notifications.cash_footer_accounts.join(',') : '',
      quiet_hours_start: String(editor?.notifications?.quietHoursStart || quiet.start || ''),
      quiet_hours_end: String(editor?.notifications?.quietHoursEnd || quiet.end || ''),
    },
    schedule: {
      enabled: sections.schedule?.enabled !== false,
      market_open: sections.schedule?.market_open == null ? '' : String(sections.schedule.market_open),
      market_close: sections.schedule?.market_close == null ? '' : String(sections.schedule.market_close),
      first_notify_after_open_min: sections.schedule?.first_notify_after_open_min == null ? '' : String(sections.schedule.first_notify_after_open_min),
      notify_interval_min: sections.schedule?.notify_interval_min == null ? '' : String(sections.schedule.notify_interval_min),
      final_notify_before_close_min: sections.schedule?.final_notify_before_close_min == null ? '' : String(sections.schedule.final_notify_before_close_min),
    },
  };
}

export function emptyAccountForm(configKey) {
  return {
    mode: 'add', accountLabel: '', market: configKey.toUpperCase(), enabled: true,
    accountType: 'futu', tradeIntakeEnabled: true, holdingsAccount: '',
    futuAccId: '', futuHost: '', futuPort: '', bitableAppToken: '', bitableTableId: '', bitableViewName: '',
  };
}

export function accountFormFromItem(item, configKey) {
  return {
    mode: 'edit',
    accountLabel: item.accountLabel || '',
    market: String(item.market || configKey).toUpperCase(),
    enabled: item.enabled !== false,
    accountType: item.accountType || 'futu',
    tradeIntakeEnabled: item.tradeIntakeEnabled !== false,
    holdingsAccount: item.holdingsAccount || '',
    futuAccId: item.futu?.account_id || '',
    futuHost: item.futu?.host || '',
    futuPort: item.futu?.port == null ? '' : String(item.futu.port),
    bitableAppToken: '',
    bitableTableId: item.bitable?.table_id || '',
    bitableViewName: item.bitable?.view_name || '',
  };
}

export function emptySymbolForm(configKey) {
  return {
    configKey, symbol: '', broker: configKey.toUpperCase(), accounts: '', limit_expirations: '8',
    sell_put_enabled: 'true', sell_call_enabled: 'false',
    sell_put_min_dte: '', sell_put_max_dte: '', sell_put_min_strike: '', sell_put_max_strike: '',
    sell_call_min_dte: '', sell_call_max_dte: '', sell_call_min_strike: '', sell_call_max_strike: '',
  };
}

export function symbolFormFromRow(row) {
  return {
    configKey: row.configKey,
    symbol: row.symbol || '',
    broker: row.market || row.configKey.toUpperCase(),
    accounts: Array.isArray(row.accounts) ? row.accounts.join(',') : '',
    limit_expirations: row.limit_expirations == null ? '' : String(row.limit_expirations),
    sell_put_enabled: row.sell_put_enabled ? 'true' : 'false',
    sell_call_enabled: row.sell_call_enabled ? 'true' : 'false',
    sell_put_min_dte: row.sell_put_min_dte == null ? '' : String(row.sell_put_min_dte),
    sell_put_max_dte: row.sell_put_max_dte == null ? '' : String(row.sell_put_max_dte),
    sell_put_min_strike: row.sell_put_min_strike == null ? '' : String(row.sell_put_min_strike),
    sell_put_max_strike: row.sell_put_max_strike == null ? '' : String(row.sell_put_max_strike),
    sell_call_min_dte: row.sell_call_min_dte == null ? '' : String(row.sell_call_min_dte),
    sell_call_max_dte: row.sell_call_max_dte == null ? '' : String(row.sell_call_max_dte),
    sell_call_min_strike: row.sell_call_min_strike == null ? '' : String(row.sell_call_min_strike),
    sell_call_max_strike: row.sell_call_max_strike == null ? '' : String(row.sell_call_max_strike),
  };
}
