import {
  fetchConfigSummaries,
  fetchEditor,
  fetchHistory,
  fetchMeta,
  fetchVersionCheck,
  fetchWatchlist,
  postAccountDelete,
  postAccountUpsert,
  postGlobalUpdate,
  postNotificationsCheck,
  postNotificationsPreview,
  postNotificationsTestSend,
  postToolRun,
  postWatchlistDelete,
  postWatchlistUpsert,
} from './webuiApi.js';
import { buildStrategySidePayload, nowId } from './webuiState.js';

export function pushToastFactory(setToasts) {
  return function pushToast(kind, text, ms = 3000) {
    const id = nowId();
    setToasts((prev) => [...prev, { id, kind, text }]);
    window.setTimeout(() => setToasts((prev) => prev.filter((item) => item.id !== id)), ms);
  };
}

export async function loadMetaAction(setTokenRequired) {
  const data = await fetchMeta();
  setTokenRequired(!!data.tokenRequired);
}

export async function loadVersionCheckAction(setVersionCheck) {
  const data = await fetchVersionCheck();
  setVersionCheck(data || null);
}

export async function loadSummariesAction(setConfigSummaries) {
  const data = await fetchConfigSummaries();
  setConfigSummaries(data.configs || {});
}

export async function loadEditorAction(configKey, emptyEditor, setEditorData) {
  const data = await fetchEditor(configKey);
  setEditorData(data.editor || emptyEditor(configKey));
}

export async function loadRowsAction(setRows) {
  const data = await fetchWatchlist();
  setRows(data.rows || []);
}

export async function loadHistoryAction(configKey, setHistoryData) {
  const data = await fetchHistory(configKey);
  setHistoryData(data || null);
}

export function withWriteTokenFactory({ tokenRequired, setTokenDlgError, setTokenDlgValue, setTokenDlgAction, setTokenDlgOnOk, setTokenDlgOpen }) {
  return function withWriteToken(actionName, doReq) {
    if (!tokenRequired) return doReq('');
    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction(actionName);
    setTokenDlgOnOk(() => async (token) => doReq(token));
    setTokenDlgOpen(true);
    return Promise.resolve();
  };
}

export function createSaveGlobalAction(ctx) {
  return async function saveGlobal() {
    const { globalForm, selectedMarket, marketMeta, withWriteToken, loadEditor, setConfigSummaries, pushToast } = ctx;
    const quietStart = String(globalForm.notifications.quiet_hours_start || '').trim();
    const quietEnd = String(globalForm.notifications.quiet_hours_end || '').trim();
    const payload = {
      configKey: selectedMarket,
      marketData: {
        source: globalForm.marketData.source,
        host: String(globalForm.marketData.host || '').trim(),
        port: String(globalForm.marketData.port || '').trim() === '' ? null : Number(globalForm.marketData.port),
        mode: globalForm.marketData.mode,
      },
      strategies: {
        sell_put: buildStrategySidePayload(ctx.STRATEGY_FIELDS, globalForm.strategy.sell_put),
        sell_call: buildStrategySidePayload(ctx.STRATEGY_FIELDS, globalForm.strategy.sell_call),
      },
      closeAdvice: {
        enabled: !!globalForm.closeAdvice.enabled,
        quote_source: String(globalForm.closeAdvice.quote_source || '').trim() || null,
        notify_levels: String(globalForm.closeAdvice.notify_levels || '').split(',').map((item) => item.trim()).filter(Boolean),
        max_items_per_account: globalForm.closeAdvice.max_items_per_account === '' ? null : Number(globalForm.closeAdvice.max_items_per_account),
        max_spread_ratio: globalForm.closeAdvice.max_spread_ratio === '' ? null : Number(globalForm.closeAdvice.max_spread_ratio),
        strong_remaining_annualized_max: globalForm.closeAdvice.strong_remaining_annualized_max === '' ? null : Number(globalForm.closeAdvice.strong_remaining_annualized_max),
        medium_remaining_annualized_max: globalForm.closeAdvice.medium_remaining_annualized_max === '' ? null : Number(globalForm.closeAdvice.medium_remaining_annualized_max),
      },
      notifications: {
        enabled: !!globalForm.notifications.enabled,
        channel: String(globalForm.notifications.channel || '').trim() || null,
        target: String(globalForm.notifications.target || '').trim() || null,
        include_cash_footer: !!globalForm.notifications.include_cash_footer,
        cash_footer_accounts: ctx.toAccountsList(globalForm.notifications.cash_footer_accounts),
        quiet_hours_beijing: (quietStart || quietEnd) ? { start: quietStart, end: quietEnd } : null,
        ...(String(globalForm.notifications.appId || '').trim() ? { appId: String(globalForm.notifications.appId || '').trim() } : {}),
        ...(String(globalForm.notifications.appSecret || '').trim() ? { appSecret: String(globalForm.notifications.appSecret || '').trim() } : {}),
        secretsFile: String(globalForm.notifications.secretsFile || '').trim() || 'secrets/notifications.feishu.app.json',
      },
    };
    return withWriteToken('保存模块设置', async (token) => {
      const out = await postGlobalUpdate(payload, token);
      setConfigSummaries(out.configs || {});
      await loadEditor(selectedMarket);
      pushToast('ok', `${marketMeta.name} 设置已保存`);
    });
  };
}

export function createSaveAccountAction(ctx) {
  return async function saveAccount() {
    const { accountForm, selectedMarket, withWriteToken, loadSummaries, loadEditor, setAccountForm, emptyAccountForm, pushToast } = ctx;
    const payload = {
      configKey: selectedMarket,
      mode: accountForm.mode,
      accountLabel: String(accountForm.accountLabel || '').trim(),
      market: String(accountForm.market || selectedMarket).trim().toLowerCase(),
      enabled: !!accountForm.enabled,
      accountType: String(accountForm.accountType || 'futu').trim(),
      tradeIntakeEnabled: !!accountForm.tradeIntakeEnabled,
      holdingsAccount: String(accountForm.holdingsAccount || '').trim() || null,
      futuAccId: String(accountForm.futuAccId || '').trim() || null,
      futu: { host: String(accountForm.futuHost || '').trim(), port: String(accountForm.futuPort || '').trim() === '' ? null : Number(accountForm.futuPort) },
      bitable: { app_token: String(accountForm.bitableAppToken || '').trim(), table_id: String(accountForm.bitableTableId || '').trim(), view_name: String(accountForm.bitableViewName || '').trim() },
    };
    return withWriteToken('保存账户', async (token) => {
      await postAccountUpsert(payload, token);
      await loadSummaries();
      await loadEditor(selectedMarket);
      setAccountForm(emptyAccountForm(selectedMarket));
      pushToast('ok', `账户 ${payload.accountLabel} 已保存`);
    });
  };
}

export function createRemoveAccountAction(ctx) {
  return async function removeAccount(item) {
    const { selectedMarket, withWriteToken, loadSummaries, loadEditor, setAccountForm, emptyAccountForm, pushToast } = ctx;
    return withWriteToken('删除账户', async (token) => {
      await postAccountDelete({ configKey: selectedMarket, accountLabel: item.accountLabel }, token);
      await loadSummaries();
      await loadEditor(selectedMarket);
      setAccountForm(emptyAccountForm(selectedMarket));
      pushToast('ok', `账户 ${item.accountLabel} 已删除`);
    });
  };
}

export function createSaveSymbolAction(ctx) {
  return async function saveSymbol() {
    const { symbolForm, selectedMarket, withWriteToken, setRows, loadSummaries, setSymbolForm, emptySymbolForm, pushToast } = ctx;
    const payload = {
      configKey: selectedMarket,
      symbol: String(symbolForm.symbol || '').trim().toUpperCase(),
      broker: String(symbolForm.broker || selectedMarket.toUpperCase()).trim(),
      accounts: ctx.toAccountsList(symbolForm.accounts),
      limit_expirations: symbolForm.limit_expirations === '' ? null : Number(symbolForm.limit_expirations),
      sell_put_enabled: symbolForm.sell_put_enabled === 'true',
      sell_call_enabled: symbolForm.sell_call_enabled === 'true',
      sell_put_min_dte: symbolForm.sell_put_min_dte === '' ? null : Number(symbolForm.sell_put_min_dte),
      sell_put_max_dte: symbolForm.sell_put_max_dte === '' ? null : Number(symbolForm.sell_put_max_dte),
      sell_put_min_strike: symbolForm.sell_put_min_strike === '' ? null : Number(symbolForm.sell_put_min_strike),
      sell_put_max_strike: symbolForm.sell_put_max_strike === '' ? null : Number(symbolForm.sell_put_max_strike),
      sell_call_min_dte: symbolForm.sell_call_min_dte === '' ? null : Number(symbolForm.sell_call_min_dte),
      sell_call_max_dte: symbolForm.sell_call_max_dte === '' ? null : Number(symbolForm.sell_call_max_dte),
      sell_call_min_strike: symbolForm.sell_call_min_strike === '' ? null : Number(symbolForm.sell_call_min_strike),
      sell_call_max_strike: symbolForm.sell_call_max_strike === '' ? null : Number(symbolForm.sell_call_max_strike),
    };
    return withWriteToken('保存标的', async (token) => {
      const out = await postWatchlistUpsert(payload, token);
      setRows(out.rows || []);
      await loadSummaries();
      setSymbolForm(emptySymbolForm(selectedMarket));
      pushToast('ok', `标的 ${payload.symbol} 已保存`);
    });
  };
}

export function createRemoveSymbolAction(ctx) {
  return async function removeSymbol(item) {
    const { selectedMarket, withWriteToken, setRows, loadSummaries, setSymbolForm, emptySymbolForm, pushToast } = ctx;
    const symbol = String(item?.symbol || '').trim().toUpperCase();
    if (!symbol) throw new Error('symbol is required');
    return withWriteToken('删除标的', async (token) => {
      const out = await postWatchlistDelete({ configKey: selectedMarket, symbol }, token);
      setRows(out.rows || []);
      await loadSummaries();
      setSymbolForm(emptySymbolForm(selectedMarket));
      pushToast('ok', `标的 ${symbol} 已删除`);
    });
  };
}

export function createRunToolAction(ctx) {
  return async function runTool(toolName) {
    const { selectedMarket, setToolRunning, setToolResult, setToolRepairHint, loadHistory, pushToast } = ctx;
    setToolRunning(toolName);
    try {
      const out = await postToolRun({ toolName, configKey: selectedMarket, input: { config_key: selectedMarket } });
      setToolResult(out);
      setToolRepairHint(out?.repairHint || null);
      await loadHistory(selectedMarket);
      pushToast('ok', `${toolName} 已完成`);
    } finally {
      setToolRunning('');
    }
  };
}

export function createCheckNotificationsAction(ctx) {
  return async function checkNotifications() {
    const { selectedMarket, setNotificationCheck, pushToast } = ctx;
    const out = await postNotificationsCheck({ configKey: selectedMarket });
    setNotificationCheck(out);
    pushToast('ok', '通知链路检查完成');
  };
}

export function createPreviewNotificationsAction(ctx) {
  return async function previewNotifications() {
    const { selectedMarket, editorData, setNotificationPreview, pushToast } = ctx;
    const out = await postNotificationsPreview({ configKey: selectedMarket, accountLabel: editorData.accounts?.[0]?.accountLabel || 'user1' });
    setNotificationPreview(out?.result?.data?.notification_text || '');
    pushToast('ok', '通知预览已生成');
  };
}

export function createSendNotificationAction(ctx) {
  return async function sendNotification(confirm) {
    const { selectedMarket, notificationPreview, withWriteToken, setNotificationSendResult, pushToast } = ctx;
    const message = String(notificationPreview || '').trim();
    if (!message) throw new Error('请先生成通知预览');
    const run = async (token) => {
      const out = await postNotificationsTestSend({ configKey: selectedMarket, message, confirm: !!confirm }, token);
      setNotificationSendResult(out);
      pushToast('ok', confirm ? '测试发送完成' : 'dry-run 完成');
    };
    if (!confirm) return run('');
    return withWriteToken('测试发送通知', run);
  };
}

export function confirmTokenAction({ tokenDlgValue, setTokenDlgError, tokenDlgOnOk, setTokenDlgOpen, pushToast }) {
  const tok = String(tokenDlgValue || '').trim();
  if (!tok) {
    setTokenDlgError('Token required');
    return;
  }
  const fn = tokenDlgOnOk;
  setTokenDlgOpen(false);
  Promise.resolve(fn ? fn(tok) : null).catch((e) => pushToast('error', e.message));
}
