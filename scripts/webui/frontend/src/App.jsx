import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

function nowId(){
  return `${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function badgeOnOff(v){
  return v
    ? <span className="Badge BadgeOn">ON</span>
    : <span className="Badge BadgeOff">OFF</span>;
}

function fmtRange(a,b){
  if (a==null && b==null) return '';
  return `${a ?? ''}~${b ?? ''}`;
}

const MARKETS = [
  { key: 'hk', label: 'HK', name: '港股市场', exchange: 'HK' },
  { key: 'us', label: 'US', name: '美股市场', exchange: 'US' },
];

const GLOBAL_STRATEGY_FIELDS = [
  ['min_annualized_net_return', '收益率', '0.1', '年化净收益率阈值，例如 0.1 = 10%'],
  ['min_net_income', '收益(CNY)', '100', '单笔最小净收益，统一按 CNY 配置，运行时换算为标的交易币种'],
  ['min_open_interest', 'min_open_interest', '50', '最小未平仓量'],
  ['min_volume', 'min_volume', '10', '最小成交量'],
  ['max_spread_ratio', 'max_spread_ratio', '0.3', '最大买卖价差比例'],
];

async function api(path, opts={}){
  const r = await fetch(path, opts);
  const j = await r.json().catch(()=>({detail: r.statusText}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

function toAccountsList(s){
  const t = (s||'').trim();
  if (!t) return null;
  return t.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean);
}

function parseRoute(){
  const h = (window.location.hash || '#/').replace(/^#/, '');
  const [rawPath, rawQuery] = h.split('?');
  const pathname = rawPath || '/';
  const qs = new URLSearchParams(rawQuery || '');
  if (pathname === '/edit'){
    return {
      name: 'edit',
      configKey: (qs.get('configKey') || 'us').toLowerCase(),
      symbol: (qs.get('symbol') || '').toUpperCase(),
    };
  }
  return { name: 'list' };
}

function toEditHash({configKey, symbol}){
  const qs = new URLSearchParams();
  if (configKey) qs.set('configKey', String(configKey));
  if (symbol) qs.set('symbol', String(symbol));
  const q = qs.toString();
  return `#/edit${q ? `?${q}` : ''}`;
}

function selectedMarketMeta(key){
  return MARKETS.find(m=>m.key === key) || MARKETS[0];
}

function createNewForm(configKey){
  const key = (configKey || 'us').toLowerCase();
  return {
    configKey: key,
    symbol: '',
    market: key.toUpperCase(),
    accounts: '',
    limit_expirations: '8',
    sell_put_enabled: 'true',
    sell_call_enabled: 'false',
    sell_put_min_dte: '',
    sell_put_max_dte: '',
    sell_put_min_strike: '',
    sell_put_max_strike: '',
    sell_call_min_dte: '',
    sell_call_max_dte: '',
    sell_call_min_strike: '',
    sell_call_max_strike: ''
  };
}

function emptyGlobalForm(){
  const mk = () => Object.fromEntries(GLOBAL_STRATEGY_FIELDS.map(([key, _label, fallback]) => [key, fallback]));
  return {
    sell_put: mk(),
    sell_call: mk(),
    closeAdvice: {
      enabled: true,
      quote_source: 'auto',
      notify_levels: 'strong,medium',
      max_items_per_account: '',
      max_spread_ratio: '',
      strong_remaining_annualized_max: '',
      medium_remaining_annualized_max: '',
    },
    notifications: {
      enabled: false,
      channel: 'feishu',
      target: '',
      include_cash_footer: true,
      cash_footer_accounts: '',
      cash_footer_timeout_sec: '',
      cash_snapshot_max_age_sec: '',
      quiet_hours_start: '',
      quiet_hours_end: '',
      opend_alert_cooldown_sec: '',
      opend_alert_burst_window_sec: '',
      opend_alert_burst_max: '',
    },
  };
}

function globalFormFromSummary(summary){
  const strategy = summary?.globalStrategy || {};
  const notifications = summary?.sections?.notifications || {};
  const closeAdvice = summary?.sections?.close_advice || {};
  const quietHours = notifications?.quiet_hours_beijing || {};
  const fromSide = (side) => {
    const values = strategy[side] || {};
    return Object.fromEntries(GLOBAL_STRATEGY_FIELDS.map(([key, _label, fallback]) => [
      key,
      values[key] == null ? fallback : String(values[key]),
    ]));
  };
  return {
    sell_put: fromSide('sell_put'),
    sell_call: fromSide('sell_call'),
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
      channel: notifications.channel == null ? 'feishu' : String(notifications.channel),
      target: notifications.target == null ? '' : String(notifications.target),
      include_cash_footer: notifications.include_cash_footer !== false,
      cash_footer_accounts: Array.isArray(notifications.cash_footer_accounts) ? notifications.cash_footer_accounts.join(',') : '',
      cash_footer_timeout_sec: notifications.cash_footer_timeout_sec == null ? '' : String(notifications.cash_footer_timeout_sec),
      cash_snapshot_max_age_sec: notifications.cash_snapshot_max_age_sec == null ? '' : String(notifications.cash_snapshot_max_age_sec),
      quiet_hours_start: quietHours.start == null ? '' : String(quietHours.start),
      quiet_hours_end: quietHours.end == null ? '' : String(quietHours.end),
      opend_alert_cooldown_sec: notifications.opend_alert_cooldown_sec == null ? '' : String(notifications.opend_alert_cooldown_sec),
      opend_alert_burst_window_sec: notifications.opend_alert_burst_window_sec == null ? '' : String(notifications.opend_alert_burst_window_sec),
      opend_alert_burst_max: notifications.opend_alert_burst_max == null ? '' : String(notifications.opend_alert_burst_max),
    },
  };
}

function numberPayloadFromGlobalForm(globalForm){
  const out = { sell_put: {}, sell_call: {} };
  for (const side of Object.keys(out)){
    for (const [key] of GLOBAL_STRATEGY_FIELDS){
      const raw = String(globalForm?.[side]?.[key] ?? '').trim();
      out[side][key] = raw === '' ? null : Number(raw);
    }
  }
  return out;
}

function notificationsPayloadFromGlobalForm(globalForm){
  const notifications = globalForm?.notifications || {};
  const parseNumber = (value) => {
    const raw = String(value ?? '').trim();
    return raw === '' ? null : Number(raw);
  };
  const quietStart = String(notifications.quiet_hours_start ?? '').trim();
  const quietEnd = String(notifications.quiet_hours_end ?? '').trim();
  return {
    enabled: !!notifications.enabled,
    channel: String(notifications.channel ?? '').trim() || null,
    target: String(notifications.target ?? '').trim() || null,
    include_cash_footer: !!notifications.include_cash_footer,
    cash_footer_accounts: toAccountsList(notifications.cash_footer_accounts),
    cash_footer_timeout_sec: parseNumber(notifications.cash_footer_timeout_sec),
    cash_snapshot_max_age_sec: parseNumber(notifications.cash_snapshot_max_age_sec),
    quiet_hours_beijing: (quietStart || quietEnd) ? { start: quietStart, end: quietEnd } : null,
    opend_alert_cooldown_sec: parseNumber(notifications.opend_alert_cooldown_sec),
    opend_alert_burst_window_sec: parseNumber(notifications.opend_alert_burst_window_sec),
    opend_alert_burst_max: parseNumber(notifications.opend_alert_burst_max),
  };
}

function closeAdvicePayloadFromGlobalForm(globalForm){
  const closeAdvice = globalForm?.closeAdvice || {};
  const parseNumber = (value) => {
    const raw = String(value ?? '').trim();
    return raw === '' ? null : Number(raw);
  };
  const notifyLevels = String(closeAdvice.notify_levels ?? '').split(',').map(x=>x.trim()).filter(Boolean);
  return {
    enabled: !!closeAdvice.enabled,
    quote_source: String(closeAdvice.quote_source ?? '').trim() || null,
    notify_levels: notifyLevels.length ? notifyLevels : null,
    max_items_per_account: parseNumber(closeAdvice.max_items_per_account),
    max_spread_ratio: parseNumber(closeAdvice.max_spread_ratio),
    strong_remaining_annualized_max: parseNumber(closeAdvice.strong_remaining_annualized_max),
    medium_remaining_annualized_max: parseNumber(closeAdvice.medium_remaining_annualized_max),
  };
}

function createAccountForm(configKey){
  return {
    configKey: configKey || 'us',
    mode: 'add',
    accountLabel: '',
    accountType: 'futu',
    futuAccId: '',
    holdingsAccount: '',
  };
}

function formFromRow(row){
  return {
    configKey: row.configKey,
    symbol: row.symbol,
    market: row.market ?? '',
    accounts: (row.accounts && row.accounts.length) ? row.accounts.join(',') : '',
    limit_expirations: row.limit_expirations ?? '',
    // use "(keep)" for edit
    sell_put_enabled: '',
    sell_call_enabled: '',
    sell_put_min_dte: row.sell_put_min_dte ?? '',
    sell_put_max_dte: row.sell_put_max_dte ?? '',
    sell_put_min_strike: row.sell_put_min_strike ?? '',
    sell_put_max_strike: row.sell_put_max_strike ?? '',
    sell_call_min_dte: row.sell_call_min_dte ?? '',
    sell_call_max_dte: row.sell_call_max_dte ?? '',
    sell_call_min_strike: row.sell_call_min_strike ?? '',
    sell_call_max_strike: row.sell_call_max_strike ?? ''
  };
}

export default function App(){
  const [route, setRoute] = useState(()=>parseRoute());

  const [rows, setRows] = useState([]);
  const [accountRows, setAccountRows] = useState([]);
  const [configSummaries, setConfigSummaries] = useState({});
  const [tokenRequired, setTokenRequired] = useState(false);
  const [accountOptions, setAccountOptions] = useState([]);
  const [recommendedFlow, setRecommendedFlow] = useState([]);

  const tokenInputRef = useRef(null);
  const prevBodyStyleRef = useRef(null);
  const tokenDlgScrollYRef = useRef(0);

  const [tokenDlgOpen, setTokenDlgOpen] = useState(false);
  const [tokenDlgAction, setTokenDlgAction] = useState('');
  const [tokenDlgValue, setTokenDlgValue] = useState('');
  const [tokenDlgOnOk, setTokenDlgOnOk] = useState(()=>null);
  const [tokenDlgError, setTokenDlgError] = useState('');

  const [status, setStatus] = useState('-');
  const [toasts, setToasts] = useState([]);
  const [selectedMarket, setSelectedMarket] = useState('hk');
  const [configModule, setConfigModule] = useState('symbols');
  const [globalForm, setGlobalForm] = useState(()=>emptyGlobalForm());
  const [accountForm, setAccountForm] = useState(()=>createAccountForm('hk'));
  const [toolResult, setToolResult] = useState(null);
  const [toolRunning, setToolRunning] = useState('');
  const [toolRepairHint, setToolRepairHint] = useState(null);
  const [notificationCheck, setNotificationCheck] = useState(null);
  const [notificationPreview, setNotificationPreview] = useState('');
  const [notificationSendResult, setNotificationSendResult] = useState(null);
  const [historyData, setHistoryData] = useState(null);

  function pushToast(kind, text, ms=3000){
    const id = nowId();
    const item = { id, kind, text };
    setToasts(prev => [...prev, item]);
    window.setTimeout(()=>{
      setToasts(prev => prev.filter(t=>t.id !== id));
    }, ms);
  }

  // filters
  const [q, setQ] = useState('');
  const [account, setAccount] = useState('');
  const [putOn, setPutOn] = useState(false);
  const [callOn, setCallOn] = useState(false);

  // delete confirm
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmText, setConfirmText] = useState({ title: '', body: '' });

  // edit form state
  const [form, setForm] = useState(()=>createNewForm('us'));
  const lastEditKeyRef = useRef('');

  useEffect(()=>{
    const onHash = ()=>setRoute(parseRoute());
    window.addEventListener('hashchange', onHash);
    onHash();
    return ()=>window.removeEventListener('hashchange', onHash);
  },[]);

  useEffect(()=>{
    if (!tokenDlgOpen) {
      if (prevBodyStyleRef.current) {
        const { position, top, left, right, width, overflow } = prevBodyStyleRef.current;
        document.body.style.position = position;
        document.body.style.top = top;
        document.body.style.left = left;
        document.body.style.right = right;
        document.body.style.width = width;
        document.body.style.overflow = overflow;
        prevBodyStyleRef.current = null;

        // restore scroll position (body was fixed)
        try { window.scrollTo(0, tokenDlgScrollYRef.current || 0); } catch {}
      }
      return;
    }

    // iOS/webview-safe scroll lock: fix body + restore later
    tokenDlgScrollYRef.current = window.scrollY || 0;
    if (!prevBodyStyleRef.current) {
      prevBodyStyleRef.current = {
        position: document.body.style.position || '',
        top: document.body.style.top || '',
        left: document.body.style.left || '',
        right: document.body.style.right || '',
        width: document.body.style.width || '',
        overflow: document.body.style.overflow || '',
      };
    }

    document.body.style.position = 'fixed';
    document.body.style.top = `-${tokenDlgScrollYRef.current}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    document.body.style.overflow = 'hidden';

    const focus = () => {
      const el = tokenInputRef.current;
      if (el && typeof el.focus === 'function') {
        try { el.focus({ preventScroll: true }); } catch { el.focus(); }
        try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch {}
      }
    };

    const t = setTimeout(focus, 80);
    focus();
    return ()=>{
      clearTimeout(t);
      if (prevBodyStyleRef.current) {
        const { position, top, left, right, width, overflow } = prevBodyStyleRef.current;
        document.body.style.position = position;
        document.body.style.top = top;
        document.body.style.left = left;
        document.body.style.right = right;
        document.body.style.width = width;
        document.body.style.overflow = overflow;
        prevBodyStyleRef.current = null;
        try { window.scrollTo(0, tokenDlgScrollYRef.current || 0); } catch {}
      }
    };
  },[tokenDlgOpen]);

  useEffect(()=>{
    (async ()=>{
      const m = await api('/api/meta');
      setTokenRequired(!!m.tokenRequired);
      setAccountOptions(Array.isArray(m.accounts) ? m.accounts : []);
      setRecommendedFlow(Array.isArray(m.recommendedFlow) ? m.recommendedFlow : []);
    })().catch(e=>pushToast('error', e.message));
  },[]);

  async function loadConfigSummaries(){
    const data = await api('/api/configs/summary');
    setConfigSummaries(data.configs || {});
    return data.configs || {};
  }

  async function loadRows(){
    setStatus('loading...');
    const data = await api('/api/watchlist');
    const r = data.rows || [];
    setRows(r);
    setStatus(`rows=${r.length}`);
    return r;
  }

  async function loadAccounts(configKey=selectedMarket){
    const data = await api(`/api/accounts?configKey=${encodeURIComponent(configKey)}`);
    const rows = data.rows || [];
    setAccountRows(rows);
    return rows;
  }

  async function loadHistory(configKey=selectedMarket){
    const data = await api(`/api/history?configKey=${encodeURIComponent(configKey)}&limit=20`);
    setHistoryData(data || null);
    return data;
  }

  useEffect(()=>{ loadRows().catch(e=>pushToast('error', e.message)); },[]);
  useEffect(()=>{ loadConfigSummaries().catch(e=>pushToast('error', e.message)); },[]);
  useEffect(()=>{ loadAccounts(selectedMarket).catch(e=>pushToast('error', e.message)); },[selectedMarket]);
  useEffect(()=>{ loadHistory(selectedMarket).catch(e=>pushToast('error', e.message)); },[selectedMarket]);

  useEffect(()=>{
    setGlobalForm(globalFormFromSummary(configSummaries[selectedMarket]));
    setAccountForm(createAccountForm(selectedMarket));
  }, [configSummaries, selectedMarket]);

  useEffect(()=>{
    if (route.name !== 'edit') return;
    const editKey = `${route.configKey || 'us'}|${route.symbol || ''}`;
    if (lastEditKeyRef.current === editKey) return;
    lastEditKeyRef.current = editKey;

    const configKey = (route.configKey || 'us').toLowerCase();
    const symbol = (route.symbol || '').toUpperCase();
    setSelectedMarket(configKey === 'us' ? 'us' : 'hk');

    if (!symbol){
      setForm(createNewForm(configKey));
      return;
    }

    const found = rows.find(r=>r.configKey === configKey && String(r.symbol||'').toUpperCase() === symbol);
    if (found){
      setForm(formFromRow(found));
      return;
    }

    // refresh rows once in case user opened edit URL directly
    (async ()=>{
      try {
        const latest = await loadRows();
        const hit = latest.find(r=>r.configKey === configKey && String(r.symbol||'').toUpperCase() === symbol);
        if (hit) setForm(formFromRow(hit));
        else setForm(f=>({ ...createNewForm(configKey), symbol }));
      } catch (e){
        setForm(f=>({ ...createNewForm(configKey), symbol }));
      }
    })();
  }, [route, rows]);

  const filtered = useMemo(()=>{
    const qq = q.trim().toUpperCase();
    return rows.filter(r=>{
      if (r.configKey !== selectedMarket) return false;
      const searchable = `${r.symbol || ''} ${r.name || ''}`.toUpperCase();
      if (qq && !searchable.includes(qq)) return false;
      if (account){
        const a = (r.accounts||[]).map(x=>String(x).toLowerCase());
        if (a.length>0 && !a.includes(account)) return false;
      }
      if (putOn && !r.sell_put_enabled) return false;
      if (callOn && !r.sell_call_enabled) return false;
      return true;
    });
  }, [rows,q,selectedMarket,account,putOn,callOn]);

  useEffect(()=>{ setStatus(`rows=${filtered.length}/${rows.length}`); }, [filtered.length, rows.length]);

  function goList(){
    setConfigModule('symbols');
    window.location.hash = '#/';
  }

  function goEdit(configKey, symbol){
    window.location.hash = toEditHash({ configKey, symbol });
  }

  function openNew(){
    goEdit(selectedMarket, '');
  }

  function openEdit(row){
    setSelectedMarket(row.configKey || selectedMarket);
    goEdit(row.configKey, row.symbol);
  }

  async function saveGlobalConfig(){
    const payload = {
      configKey: selectedMarket,
      strategies: numberPayloadFromGlobalForm(globalForm),
      closeAdvice: closeAdvicePayloadFromGlobalForm(globalForm),
      notifications: notificationsPayloadFromGlobalForm(globalForm),
    };

    for (const side of Object.keys(payload.strategies)){
      for (const [key, label] of GLOBAL_STRATEGY_FIELDS){
        const v = payload.strategies[side][key];
        if (!Number.isFinite(v)) throw new Error(`${side} ${label} 需要填写数字`);
      }
    }
    if (payload.notifications.quiet_hours_beijing) {
      const quiet = payload.notifications.quiet_hours_beijing;
      if (!quiet.start || !quiet.end) throw new Error('quiet hours 需要同时填写开始和结束时间');
    }

    const doReq = async (tok) => {
      const headers = {'content-type':'application/json'};
      if (tok) headers['x-om-token'] = String(tok).trim();
      const out = await api('/api/configs/global/update', {method:'POST', headers, body: JSON.stringify(payload)});
      setConfigSummaries(out.configs || {});
      pushToast('ok', `${selectedMarket.toUpperCase()} 全局配置已保存`);
    };

    if (!tokenRequired) {
      await doReq('');
      return;
    }

    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction('保存全局配置');
    setTokenDlgOnOk(()=>async (tok)=>{ await doReq(tok); });
    setTokenDlgOpen(true);
  }

  async function saveAccountForm(){
    const payload = {
      configKey: selectedMarket,
      mode: accountForm.mode,
      accountLabel: String(accountForm.accountLabel || '').trim(),
      accountType: String(accountForm.accountType || '').trim(),
      futuAccId: String(accountForm.futuAccId || '').trim() || null,
      holdingsAccount: String(accountForm.holdingsAccount || '').trim() || null,
    };
    if (!payload.accountLabel) throw new Error('account label is required');
    if (payload.accountType === 'futu' && payload.mode === 'add' && !payload.futuAccId) throw new Error('futu account 需要 futu acc id');

    const doReq = async (tok) => {
      const headers = {'content-type':'application/json'};
      if (tok) headers['x-om-token'] = String(tok).trim();
      const out = await api('/api/accounts/upsert', {method:'POST', headers, body: JSON.stringify(payload)});
      setAccountRows(out.rows || []);
      setConfigSummaries(out.configs || {});
      setAccountForm(createAccountForm(selectedMarket));
      pushToast('ok', `${payload.accountLabel} 已保存`);
    };
    if (!tokenRequired) return doReq('');
    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction('保存账户');
    setTokenDlgOnOk(()=>async (tok)=>{ await doReq(tok); });
    setTokenDlgOpen(true);
  }

  async function deleteAccount(row){
    const doReq = async (tok) => {
      const headers = {'content-type':'application/json'};
      if (tok) headers['x-om-token'] = String(tok).trim();
      const out = await api('/api/accounts/delete', {method:'POST', headers, body: JSON.stringify({configKey: row.configKey, accountLabel: row.account_label})});
      setAccountRows(out.rows || []);
      setConfigSummaries(out.configs || {});
      setAccountForm(createAccountForm(selectedMarket));
      pushToast('ok', `${row.account_label} 已删除`);
    };
    if (!tokenRequired) return doReq('');
    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction('删除账户');
    setTokenDlgOnOk(()=>async (tok)=>{ await doReq(tok); });
    setTokenDlgOpen(true);
  }

  async function runTool(toolName){
    setToolRunning(toolName);
    try {
      const out = await api('/api/tools/run', {
        method: 'POST',
        headers: {'content-type':'application/json'},
        body: JSON.stringify({ toolName, configKey: selectedMarket, input: { config_key: selectedMarket } }),
      });
      setToolResult(out);
      setToolRepairHint(out?.repairHint || null);
      loadHistory(selectedMarket).catch(e=>pushToast('error', e.message));
      pushToast('ok', `${toolName} 已完成`);
    } finally {
      setToolRunning('');
    }
  }

  async function checkNotifications(){
    const out = await api('/api/notifications/check', {
      method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({ configKey: selectedMarket }),
    });
    setNotificationCheck(out);
    pushToast('ok', '通知发送前检查已完成');
  }

  async function previewNotifications(){
    const out = await api('/api/notifications/preview', {
      method:'POST',
      headers:{'content-type':'application/json'},
      body: JSON.stringify({ configKey: selectedMarket, accountLabel: (accountRows[0]?.account_label || 'user1') }),
    });
    setNotificationPreview(out?.result?.data?.notification_text || '');
    pushToast('ok', '通知预览已生成');
  }

  async function testSendNotifications(confirmSend=false){
    const message = String(notificationPreview || '').trim();
    if (!message) throw new Error('先生成通知预览');
    const doReq = async (tok) => {
      const headers = {'content-type':'application/json'};
      if (tok) headers['x-om-token'] = String(tok).trim();
      const out = await api('/api/notifications/test-send', {
        method:'POST',
        headers,
        body: JSON.stringify({ configKey: selectedMarket, message, confirm: !!confirmSend }),
      });
      setNotificationSendResult(out);
      pushToast('ok', confirmSend ? '测试发送已完成' : 'dry-run 已生成');
    };
    if (!confirmSend || !tokenRequired) return doReq('');
    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction('测试发送通知');
    setTokenDlgOnOk(()=>async (tok)=>{ await doReq(tok); });
    setTokenDlgOpen(true);
  }

  async function save(){
    const payload = {
      configKey: form.configKey,
      symbol: String(form.symbol||'').trim().toUpperCase()
    };
    if (!payload.symbol) throw new Error('symbol is required');

    delete payload.sell_call_avg_cost;
    delete payload.sell_call_shares;

    if (form.market !== '') payload.market = form.market;
    payload.accounts = toAccountsList(form.accounts);

    // For numeric inputs: empty string means "clear" (send null) instead of "keep".
    payload.limit_expirations = (form.limit_expirations === '') ? null : Number(form.limit_expirations);

    const boolMap = { sell_put_enabled: form.sell_put_enabled, sell_call_enabled: form.sell_call_enabled };
    for (const k of Object.keys(boolMap)){
      const v = boolMap[k];
      if (v === 'true') payload[k] = true;
      if (v === 'false') payload[k] = false;
    }

    const numFields = [
      'sell_put_min_dte','sell_put_max_dte','sell_put_min_strike','sell_put_max_strike',
      'sell_call_min_dte','sell_call_max_dte','sell_call_min_strike','sell_call_max_strike'
    ];
    for (const k of numFields){
      const v = form[k];
      if (k === 'sell_put_min_strike' && v === '') {
        // empty means 0 for put min_strike
        payload[k] = 0;
        continue;
      }
      payload[k] = (v === '') ? null : Number(v);
    }

    const doReq = async (tok) => {
      const headers = {'content-type':'application/json'};
      if (tok) headers['x-om-token'] = String(tok).trim();
      const out = await api('/api/watchlist/upsert', {method:'POST', headers, body: JSON.stringify(payload)});
      setRows(out.rows || []);
      loadConfigSummaries().catch(e=>pushToast('error', e.message));
      setStatus('saved');
      pushToast('ok', '已保存');
      goList();
    };

    if (!tokenRequired) {
      await doReq('');
      return;
    }

    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction('保存');
    setTokenDlgOnOk(()=>async (tok)=>{ await doReq(tok); });
    setTokenDlgOpen(true);
  }

  async function doDelete(){
    const symbol = String(form.symbol||'').trim().toUpperCase();
    if (!symbol) return;

    const doReq = async (tok) => {
      const headers = {'content-type':'application/json'};
      if (tok) headers['x-om-token'] = String(tok).trim();
      const out = await api('/api/watchlist/delete', {method:'POST', headers, body: JSON.stringify({configKey: form.configKey, symbol})});
      setRows(out.rows || []);
      loadConfigSummaries().catch(e=>pushToast('error', e.message));
      pushToast('ok', '已删除');
      goList();
    };

    if (!tokenRequired) {
      await doReq('');
      return;
    }

    setTokenDlgError('');
    setTokenDlgValue('');
    setTokenDlgAction('删除');
    setTokenDlgOnOk(()=>async (tok)=>{ await doReq(tok); });
    setTokenDlgOpen(true);
  }

  function askDelete(){
    const symbol = String(form.symbol||'').trim().toUpperCase();
    if (!symbol) return;
    setConfirmText({
      title: '确认删除',
      body: `删除 ${form.configKey}:${symbol} ?`,
    });
    setConfirmOpen(true);
  }

  const isEdit = route.name === 'edit';
  const marketMeta = selectedMarketMeta(selectedMarket);
  const currentSummary = configSummaries[selectedMarket] || {};
  const marketRows = rows.filter(r=>r.configKey === selectedMarket);
  const enabledRows = marketRows.filter(r=>r.sell_put_enabled || r.sell_call_enabled);

  return (
    <>
      <div className="Header">
        <div className="HeaderInner">
          <div className="Title"><span className="Mark">OM</span> 配置中心</div>
          <div className="HeaderTabs" role="tablist" aria-label="市场">
            {MARKETS.map(m=>(
              <button
                key={m.key}
                className={`HeaderTab ${selectedMarket === m.key ? 'HeaderTabActive' : ''}`}
                onClick={()=>{
                  setSelectedMarket(m.key);
                  setQ('');
                  setAccount('');
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
          <div className="Status">{status}</div>
        </div>
      </div>

      <div className="Page">
        {!isEdit && (
          <>
            <section className="HeroPanel">
              <div>
                <div className="Eyebrow">Runtime Config</div>
                <h1 className="HeroTitle">按市场管理配置</h1>
                <p className="HeroText">先选择 HK 或 US，再维护该市场下的全局配置与标的配置，避免在合并列表里来回确认来源。</p>
              </div>
              <div className="HeroStats">
                <div className="StatCard"><span>标的</span><strong>{marketRows.length}</strong></div>
                <div className="StatCard"><span>启用</span><strong>{enabledRows.length}</strong></div>
                <div className="StatCard"><span>账户</span><strong>{(currentSummary.accounts || []).length || '-'}</strong></div>
              </div>
            </section>

            <div className="ModuleTabs" role="tablist" aria-label="配置模块">
              <button className={`ModuleTab ${configModule === 'global' ? 'ModuleTabActive' : ''}`} onClick={()=>setConfigModule('global')}>全局配置</button>
              <button className={`ModuleTab ${configModule === 'accounts' ? 'ModuleTabActive' : ''}`} onClick={()=>setConfigModule('accounts')}>账户管理</button>
              <button className={`ModuleTab ${configModule === 'symbols' ? 'ModuleTabActive' : ''}`} onClick={()=>setConfigModule('symbols')}>标的配置</button>
              <button className={`ModuleTab ${configModule === 'results' ? 'ModuleTabActive' : ''}`} onClick={()=>setConfigModule('results')}>运行结果</button>
              <button className={`ModuleTab ${configModule === 'history' ? 'ModuleTabActive' : ''}`} onClick={()=>setConfigModule('history')}>审计历史</button>
              <span className="Spacer ModuleTabsSpacer" />
              <button className="Button ButtonPrimary BtnNew" onClick={openNew}>新增 {marketMeta.label} 标的</button>
            </div>

            {configModule === 'global' && (
              <GlobalConfigPanel
                summary={currentSummary}
                marketMeta={marketMeta}
                form={globalForm}
                setForm={setGlobalForm}
                notificationCheck={notificationCheck}
                notificationPreview={notificationPreview}
                notificationSendResult={notificationSendResult}
                onCheckNotifications={()=>checkNotifications().catch(e=>pushToast('error', e.message))}
                onPreviewNotifications={()=>previewNotifications().catch(e=>pushToast('error', e.message))}
                onDryRunNotifications={()=>testSendNotifications(false).catch(e=>pushToast('error', e.message))}
                onSendTestNotifications={()=>testSendNotifications(true).catch(e=>pushToast('error', e.message))}
                onSave={()=>saveGlobalConfig().catch(e=>pushToast('error', e.message))}
              />
            )}

            {configModule === 'accounts' && (
              <AccountsPanel
                rows={accountRows.filter(r=>r.configKey === selectedMarket)}
                form={accountForm}
                setForm={setAccountForm}
                recommendedFlow={recommendedFlow}
                onEdit={(row)=>setAccountForm({
                  configKey: row.configKey,
                  mode: 'edit',
                  accountLabel: row.account_label,
                  accountType: row.account_type,
                  futuAccId: '',
                  holdingsAccount: row.holdings_account || '',
                })}
                onDelete={(row)=>deleteAccount(row).catch(e=>pushToast('error', e.message))}
                onSave={()=>saveAccountForm().catch(e=>pushToast('error', e.message))}
                onReset={()=>setAccountForm(createAccountForm(selectedMarket))}
              />
            )}

            {configModule === 'symbols' && (
              <>
                <div className="Toolbar">
                  <input className={`Control ControlSearch`} value={q} onChange={e=>setQ(e.target.value)} placeholder={`搜索 ${marketMeta.label} 标的（NVDA / 0700.HK）`} />

                  <select className="Control SelectAccount" value={account} onChange={e=>setAccount(e.target.value)}>
                    <option value="">account: all</option>
                    {accountOptions.map(acct => <option key={acct} value={acct}>{acct}</option>)}
                  </select>

                  <div className="ToggleGroup">
                    <label className="Toggle TogglePut"><input type="checkbox" checked={putOn} onChange={e=>setPutOn(e.target.checked)} /> put</label>
                    <label className="Toggle ToggleCall"><input type="checkbox" checked={callOn} onChange={e=>setCallOn(e.target.checked)} /> call</label>
                  </div>
                </div>

                <div className="Box BoxScroll">
                  <table>
                    <thead>
                      <tr>
                        <th>exchange</th><th>symbol</th><th>标的名</th><th>accounts</th><th>put</th><th>call</th>
                        <th>limit_exp</th><th>put dte</th><th>put strike</th><th>call dte</th><th>call strike</th><th>ops</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((r)=> (
                        <tr key={`${r.configKey}-${r.symbol}`}>
                          <td>{r.market ?? marketMeta.exchange}</td>
                          <td><strong>{r.symbol}</strong></td>
                          <td>{r.name || <span style={{color:'var(--muted)'}}>-</span>}</td>
                          <td>{(r.accounts && r.accounts.length)? r.accounts.join(',') : <span style={{color:'var(--muted)'}}>all</span>}</td>
                          <td>{badgeOnOff(r.sell_put_enabled)}</td>
                          <td>{badgeOnOff(r.sell_call_enabled)}</td>
                          <td>{r.limit_expirations ?? ''}</td>
                          <td>{fmtRange(r.sell_put_min_dte, r.sell_put_max_dte)}</td>
                          <td>{fmtRange(r.sell_put_min_strike, r.sell_put_max_strike)}</td>
                          <td>{fmtRange(r.sell_call_min_dte, r.sell_call_max_dte)}</td>
                          <td>{fmtRange(r.sell_call_min_strike, r.sell_call_max_strike)}</td>
                          <td><button className="LinkBtn" onClick={()=>openEdit(r)}>Edit</button></td>
                        </tr>
                      ))}
                      {!filtered.length && (
                        <tr>
                          <td colSpan="12"><span style={{color:'var(--muted)'}}>当前筛选下没有标的配置</span></td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {configModule === 'results' && (
              <ResultsPanel
                marketMeta={marketMeta}
                runningTool={toolRunning}
                toolResult={toolResult}
                repairHint={toolRepairHint}
                accountRows={accountRows.filter(r=>r.configKey === selectedMarket)}
                onRun={(toolName)=>runTool(toolName).catch(e=>pushToast('error', e.message))}
              />
            )}

            {configModule === 'history' && (
              <HistoryPanel
                marketMeta={marketMeta}
                history={historyData}
                onRefresh={()=>loadHistory(selectedMarket).catch(e=>pushToast('error', e.message))}
              />
            )}
          </>
        )}

        {isEdit && (
          <>
            <div className="Box" style={{padding: 12}}>
              <div style={{display:'flex', alignItems:'center', gap: 12, flexWrap:'wrap'}}>
                <div style={{fontWeight: 800}}>编辑 {form.configKey?.toUpperCase()} 标的 {form.symbol || ''}</div>
                <div style={{color:'var(--muted)', fontSize: 12}}>写操作需要 token（弹框输入）</div>
              </div>

              <div style={{marginTop: 12}}>
                <div className="FormSection">
                  <div className="SectionTitle">基础</div>
                  <div className="FormGrid">
                    <Field label="市场配置"><select className="Control" value={form.configKey} onChange={e=>setForm({...form, configKey:e.target.value, market:e.target.value.toUpperCase()})}><option value="hk">hk</option><option value="us">us</option></select></Field>
                    <Field label="symbol"><input className="Control" value={form.symbol} onChange={e=>setForm({...form, symbol:e.target.value})} placeholder="NVDA / 0700.HK" /></Field>
                    <Field label="exchange"><select className="Control" value={form.market} onChange={e=>setForm({...form, market:e.target.value})}><option value="">(keep)</option><option value="US">US</option><option value="HK">HK</option></select></Field>
                    <Field label="accounts（逗号分隔，空=all）"><input className="Control" value={form.accounts} onChange={e=>setForm({...form, accounts:e.target.value})} placeholder={accountOptions.length ? accountOptions.join(',') : 'account1,account2'} /></Field>

                    <Field label="limit_expirations"><input className="Control" type="number" value={form.limit_expirations} onChange={e=>setForm({...form, limit_expirations:e.target.value})} /></Field>
                    <Field label="put enabled"><select className="Control" value={form.sell_put_enabled} onChange={e=>setForm({...form, sell_put_enabled:e.target.value})}><option value="">(keep)</option><option value="true">true</option><option value="false">false</option></select></Field>
                    <Field label="call enabled"><select className="Control" value={form.sell_call_enabled} onChange={e=>setForm({...form, sell_call_enabled:e.target.value})}><option value="">(keep)</option><option value="true">true</option><option value="false">false</option></select></Field>
                    <div />
                  </div>
                </div>

                <div className="FormSection">
                  <div className="SectionTitle">Put</div>
                  <div className="FormGrid">
                    <Field label="put min_dte"><input className="Control" type="number" value={form.sell_put_min_dte} onChange={e=>setForm({...form, sell_put_min_dte:e.target.value})} /></Field>
                    <Field label="put max_dte"><input className="Control" type="number" value={form.sell_put_max_dte} onChange={e=>setForm({...form, sell_put_max_dte:e.target.value})} /></Field>
                    <Field label="put min_strike（空=0）"><input className="Control" type="number" step="any" value={form.sell_put_min_strike} onChange={e=>setForm({...form, sell_put_min_strike:e.target.value})} /></Field>
                    <Field label="put max_strike"><input className="Control" type="number" step="any" value={form.sell_put_max_strike} onChange={e=>setForm({...form, sell_put_max_strike:e.target.value})} /></Field>
                  </div>
                </div>

                <div className="FormSection">
                  <div className="SectionTitle">Call</div>
                  <div className="FormGrid">
                    <Field label="call min_dte"><input className="Control" type="number" value={form.sell_call_min_dte} onChange={e=>setForm({...form, sell_call_min_dte:e.target.value})} /></Field>
                    <Field label="call max_dte"><input className="Control" type="number" value={form.sell_call_max_dte} onChange={e=>setForm({...form, sell_call_max_dte:e.target.value})} /></Field>
                    <Field label="call min_strike"><input className="Control" type="number" step="any" value={form.sell_call_min_strike} onChange={e=>setForm({...form, sell_call_min_strike:e.target.value})} /></Field>
                    <Field label="call max_strike"><input className="Control" type="number" step="any" value={form.sell_call_max_strike} onChange={e=>setForm({...form, sell_call_max_strike:e.target.value})} /></Field>
                  </div>
                </div>

                <div className="EditActions">
                  <button className="Button ButtonPrimary" onClick={()=>save().catch(e=>pushToast('error', e.message))}>保存</button>
                  <button className="Button" onClick={goList}>取消</button>
                  <button className="Button ButtonDanger" onClick={askDelete}>删除</button>
                </div>
              </div>
            </div>
          </>
        )}

        {confirmOpen && (
          <div className="ConfirmOverlay" onClick={()=>setConfirmOpen(false)}>
            <div className="ConfirmModal" onClick={(e)=>e.stopPropagation()}>
              <div className="ConfirmTitle">{confirmText.title}</div>
              <div className="ConfirmBody">{confirmText.body}</div>
              <div className="ConfirmActions">
                <button className="Button" onClick={()=>setConfirmOpen(false)}>取消</button>
                <button className="Button ButtonDanger" onClick={()=>{
                  setConfirmOpen(false);
                  doDelete().catch(e=>pushToast('error', e.message));
                }}>确定删除</button>
              </div>
            </div>
          </div>
        )}

        {/* Toasts */}
        {!!toasts.length && (
          <div className="ToastHost">
            {toasts.map(t=>(
              <div key={t.id} className={`Toast Toast-${t.kind}`}>{t.text}</div>
            ))}
          </div>
        )}

        {tokenDlgOpen && createPortal(
          <div className="TokenOverlay" onClick={()=>setTokenDlgOpen(false)}>
            <div className="TokenModal" onClick={(e)=>e.stopPropagation()}>
              <div className="TokenModalInner">
              <div className="ConfirmTitle">请输入 Token</div>
              <div className="ConfirmBody">{tokenDlgAction}需要 Token</div>
              <div style={{marginTop: 10}}>
                <input
                  ref={tokenInputRef}
                  className="Control TokenControl"
                  type="password"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  autoComplete="new-password"
                  placeholder="Token"
                  value={tokenDlgValue}
                  onChange={(e)=>setTokenDlgValue(e.target.value)}
                  style={{ WebkitTextSecurity: 'disc' }}
                  autoFocus
                />
                {tokenDlgError && <div style={{color:'var(--danger)', marginTop: 8}}>{tokenDlgError}</div>}
              </div>
              <div className="ConfirmActions TokenActions">
                <button className="Button" onClick={()=>setTokenDlgOpen(false)}>取消</button>
                <button className="Button ButtonPrimary" onClick={()=>{
                  const tok = String(tokenDlgValue || '').trim();
                  if (!tok) { setTokenDlgError('Token required'); return; }
                  const fn = tokenDlgOnOk;
                  setTokenDlgOpen(false);
                  Promise.resolve(fn ? fn(tok) : null).catch(e=>pushToast('error', e.message));
                }}>确定</button>
              </div>
            </div>
          </div>
        </div>,
          document.body
        )}
      </div>
    </>
  );
}

function Field({label, children}){
  return (
    <div className="FormField">
      <div className="FieldLabel">{label}</div>
      {children}
    </div>
  );
}

function GlobalConfigPanel({summary, marketMeta, form, setForm, notificationCheck, notificationPreview, notificationSendResult, onCheckNotifications, onPreviewNotifications, onDryRunNotifications, onSendTestNotifications, onSave}){
  if (!summary || !summary.exists){
    return (
      <div className="Box EmptyState">
        <div className="EmptyTitle">{marketMeta.label} 全局配置未就绪</div>
        <div className="EmptyText">{summary?.error || '未找到本地 runtime config 文件'}</div>
        <code>{summary?.path || ''}</code>
      </div>
    );
  }

  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">{marketMeta.label} Global</div>
          <h2 className="PanelTitle">全局策略阈值</h2>
          <p className="PanelText">维护市场级策略模板字段。保存时只更新 put_base / call_base 的收益率、收益和流动性/价差硬过滤参数。</p>
        </div>
        <div className="ConfigPath">
          <span>文件</span>
          <code>{summary.resolvedPath || summary.path}</code>
        </div>
      </div>

      {summary.canonicalPathWarning && (
        <div className="Box" style={{padding: 14, borderColor: 'rgba(255,107,95,.45)', background: 'rgba(255,107,95,.08)'}}>
          <div style={{fontWeight: 800, marginBottom: 6}}>当前 WebUI 写入路径不是推荐 canonical runtime config</div>
          <div style={{color:'var(--muted)', fontSize: 13, lineHeight: 1.6}}>
            当前写入：<code>{summary.resolvedPath || summary.path}</code>
          </div>
          <div style={{color:'var(--muted)', fontSize: 13, lineHeight: 1.6}}>
            推荐 canonical：<code>{summary.recommendedPath || '-'}</code>
          </div>
        </div>
      )}

      <div className="SummaryGrid">
        <div className="SummaryCard"><span>账户</span><strong>{(summary.accounts || []).join(', ') || '-'}</strong></div>
        <div className="SummaryCard"><span>标的总数</span><strong>{summary.symbolCount ?? 0}</strong></div>
        <div className="SummaryCard"><span>启用标的</span><strong>{summary.enabledSymbolCount ?? 0}</strong></div>
        <div className="SummaryCard"><span>当前市场</span><strong>{marketMeta.name}</strong></div>
      </div>

      <div className="GlobalFormShell">
        <StrategyForm
          title="Sell Put"
          subtitle="templates.put_base.sell_put"
          side="sell_put"
          form={form}
          setForm={setForm}
        />
        <StrategyForm
          title="Covered Call"
          subtitle="templates.call_base.sell_call"
          side="sell_call"
          form={form}
          setForm={setForm}
        />
      </div>

      <CloseAdviceConfigCard form={form} setForm={setForm} />
      <NotificationConfigCard form={form} setForm={setForm} />
      <NotificationOpsCard
        notificationCheck={notificationCheck}
        notificationPreview={notificationPreview}
        notificationSendResult={notificationSendResult}
        onCheckNotifications={onCheckNotifications}
        onPreviewNotifications={onPreviewNotifications}
        onDryRunNotifications={onDryRunNotifications}
        onSendTestNotifications={onSendTestNotifications}
      />

      <div className="GlobalSaveBar">
        <div>
          <strong>安装后引导</strong>
          <span>推荐顺序：healthcheck -&gt; scan_opportunities -&gt; get_close_advice。</span>
        </div>
        <div className="FlowInline">
          <span className="StrategyPill">1. healthcheck</span>
          <span className="StrategyPill">2. scan</span>
          <span className="StrategyPill">3. close advice</span>
        </div>
      </div>

      <div className="GlobalSaveBar">
        <div>
          <strong>保存全局配置</strong>
          <span>会先备份配置文件，再运行 validate_config 校验，并同步写入通知路由配置。</span>
        </div>
        <button className="Button ButtonPrimary" onClick={onSave}>保存 {marketMeta.label} 全局配置</button>
      </div>
    </div>
  );
}

function StrategyForm({title, subtitle, side, form, setForm}){
  function updateField(key, value){
    setForm(prev => ({
      ...prev,
      [side]: {
        ...(prev?.[side] || {}),
        [key]: value,
      },
    }));
  }

  return (
    <section className="StrategyCard">
      <div className="StrategyHeader">
        <div>
          <div className="StrategyTitle">{title}</div>
          <div className="StrategySub">{subtitle}</div>
        </div>
        <span className="StrategyPill">{side === 'sell_put' ? 'PUT' : 'CALL'}</span>
      </div>
      <div className="StrategyGrid">
        {GLOBAL_STRATEGY_FIELDS.map(([key, label, _fallback, help])=>(
          <Field key={key} label={label}>
            <input
              className="Control"
              type="number"
              step="any"
              value={form?.[side]?.[key] ?? ''}
              onChange={e=>updateField(key, e.target.value)}
              title={help}
            />
          </Field>
        ))}
      </div>
    </section>
  );
}

function NotificationConfigCard({ form, setForm }){
  function updateField(key, value){
    setForm(prev => ({
      ...prev,
      notifications: {
        ...(prev?.notifications || {}),
        [key]: value,
      },
    }));
  }

  const notifications = form?.notifications || {};
  return (
    <section className="StrategyCard">
      <div className="StrategyHeader">
        <div>
          <div className="StrategyTitle">Feishu 推送</div>
          <div className="StrategySub">notifications</div>
        </div>
        <span className="StrategyPill">SEND</span>
      </div>
      <div className="StrategyGrid">
        <Field label="enabled">
          <select className="Control" value={notifications.enabled ? 'true' : 'false'} onChange={e=>updateField('enabled', e.target.value === 'true')}>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </Field>
        <Field label="channel">
          <input className="Control" value={notifications.channel ?? ''} onChange={e=>updateField('channel', e.target.value)} placeholder="feishu" />
        </Field>
        <Field label="target">
          <input className="Control" value={notifications.target ?? ''} onChange={e=>updateField('target', e.target.value)} placeholder="user:U_xxx / chat:xxx" />
        </Field>
        <Field label="include_cash_footer">
          <select className="Control" value={notifications.include_cash_footer ? 'true' : 'false'} onChange={e=>updateField('include_cash_footer', e.target.value === 'true')}>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </Field>
        <Field label="cash_footer_accounts">
          <input className="Control" value={notifications.cash_footer_accounts ?? ''} onChange={e=>updateField('cash_footer_accounts', e.target.value)} placeholder="user1,sy" />
        </Field>
        <Field label="cash_footer_timeout_sec">
          <input className="Control" type="number" value={notifications.cash_footer_timeout_sec ?? ''} onChange={e=>updateField('cash_footer_timeout_sec', e.target.value)} />
        </Field>
        <Field label="cash_snapshot_max_age_sec">
          <input className="Control" type="number" value={notifications.cash_snapshot_max_age_sec ?? ''} onChange={e=>updateField('cash_snapshot_max_age_sec', e.target.value)} />
        </Field>
        <Field label="quiet_hours start">
          <input className="Control" value={notifications.quiet_hours_start ?? ''} onChange={e=>updateField('quiet_hours_start', e.target.value)} placeholder="02:00" />
        </Field>
        <Field label="quiet_hours end">
          <input className="Control" value={notifications.quiet_hours_end ?? ''} onChange={e=>updateField('quiet_hours_end', e.target.value)} placeholder="08:00" />
        </Field>
        <Field label="opend_alert_cooldown_sec">
          <input className="Control" type="number" value={notifications.opend_alert_cooldown_sec ?? ''} onChange={e=>updateField('opend_alert_cooldown_sec', e.target.value)} />
        </Field>
        <Field label="opend_alert_burst_window_sec">
          <input className="Control" type="number" value={notifications.opend_alert_burst_window_sec ?? ''} onChange={e=>updateField('opend_alert_burst_window_sec', e.target.value)} />
        </Field>
        <Field label="opend_alert_burst_max">
          <input className="Control" type="number" value={notifications.opend_alert_burst_max ?? ''} onChange={e=>updateField('opend_alert_burst_max', e.target.value)} />
        </Field>
      </div>
    </section>
  );
}

function CloseAdviceConfigCard({ form, setForm }){
  function updateField(key, value){
    setForm(prev => ({
      ...prev,
      closeAdvice: {
        ...(prev?.closeAdvice || {}),
        [key]: value,
      },
    }));
  }
  const closeAdvice = form?.closeAdvice || {};
  return (
    <section className="StrategyCard">
      <div className="StrategyHeader">
        <div>
          <div className="StrategyTitle">Close Advice</div>
          <div className="StrategySub">close_advice</div>
        </div>
        <span className="StrategyPill">EXIT</span>
      </div>
      <div className="StrategyGrid">
        <Field label="enabled">
          <select className="Control" value={closeAdvice.enabled ? 'true' : 'false'} onChange={e=>updateField('enabled', e.target.value === 'true')}>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </Field>
        <Field label="quote_source">
          <input className="Control" value={closeAdvice.quote_source ?? ''} onChange={e=>updateField('quote_source', e.target.value)} placeholder="auto" />
        </Field>
        <Field label="notify_levels">
          <input className="Control" value={closeAdvice.notify_levels ?? ''} onChange={e=>updateField('notify_levels', e.target.value)} placeholder="strong,medium" />
        </Field>
        <Field label="max_items_per_account">
          <input className="Control" type="number" value={closeAdvice.max_items_per_account ?? ''} onChange={e=>updateField('max_items_per_account', e.target.value)} />
        </Field>
        <Field label="max_spread_ratio">
          <input className="Control" type="number" step="any" value={closeAdvice.max_spread_ratio ?? ''} onChange={e=>updateField('max_spread_ratio', e.target.value)} />
        </Field>
        <Field label="strong_remaining_annualized_max">
          <input className="Control" type="number" step="any" value={closeAdvice.strong_remaining_annualized_max ?? ''} onChange={e=>updateField('strong_remaining_annualized_max', e.target.value)} />
        </Field>
        <Field label="medium_remaining_annualized_max">
          <input className="Control" type="number" step="any" value={closeAdvice.medium_remaining_annualized_max ?? ''} onChange={e=>updateField('medium_remaining_annualized_max', e.target.value)} />
        </Field>
      </div>
    </section>
  );
}

function NotificationOpsCard({ notificationCheck, notificationPreview, notificationSendResult, onCheckNotifications, onPreviewNotifications, onDryRunNotifications, onSendTestNotifications }){
  return (
    <section className="StrategyCard">
      <div className="StrategyHeader">
        <div>
          <div className="StrategyTitle">发送前检查</div>
          <div className="StrategySub">preview / dry-run / send check</div>
        </div>
        <span className="StrategyPill">CHECK</span>
      </div>
      <div className="OpsToolbar">
        <button className="Button" onClick={onCheckNotifications}>检查发送链路</button>
        <button className="Button" onClick={onPreviewNotifications}>生成通知预览</button>
        <button className="Button" onClick={onDryRunNotifications}>dry-run</button>
        <button className="Button ButtonDanger" onClick={onSendTestNotifications}>测试真实发送</button>
      </div>
      {!!notificationCheck?.checks?.length && (
        <div className="CheckList">
          {notificationCheck.checks.map(item=>(
            <div key={item.name} className={`CheckItem ${item.ok ? 'CheckItemOk' : 'CheckItemBad'}`}>
              <strong>{item.name}</strong>
              <span>{item.message}</span>
            </div>
          ))}
        </div>
      )}
      {!!notificationPreview && (
        <div className="PreviewPanel">
          <div className="SectionTitle">通知预览</div>
          <pre className="JsonPreview">{notificationPreview}</pre>
        </div>
      )}
      {!!notificationSendResult && (
        <div className="PreviewPanel">
          <div className="SectionTitle">发送结果</div>
          <pre className="JsonPreview">{JSON.stringify(notificationSendResult, null, 2)}</pre>
        </div>
      )}
    </section>
  );
}

function AccountsPanel({ rows, form, setForm, recommendedFlow, onEdit, onDelete, onSave, onReset }){
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">Accounts</div>
          <h2 className="PanelTitle">账户管理</h2>
          <p className="PanelText">维护 `futu` 与 `external_holdings` 账号，映射会直接写回 runtime config。</p>
        </div>
        <div className="ConfigPath">
          <span>推荐 flow</span>
          <code>{(recommendedFlow || []).join(' -> ') || 'healthcheck -> scan_opportunities -> get_close_advice'}</code>
        </div>
      </div>

      <div className="Box BoxScroll">
        <table>
          <thead>
            <tr>
              <th>account</th>
              <th>type</th>
              <th>primary</th>
              <th>fallback</th>
              <th>futu acc ids</th>
              <th>holdings_account</th>
              <th>portfolio source</th>
              <th>ops</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(row=>(
              <tr key={row.account_label}>
                <td><strong>{row.account_label}</strong></td>
                <td>{row.account_type}</td>
                <td>{row.primary_source || '-'} / {row.primary_ready ? 'ready' : 'missing'}</td>
                <td>{row.fallback_enabled ? `${row.fallback_source || 'holdings'} / ${row.fallback_ready ? 'ready' : 'missing'}` : '-'}</td>
                <td>{(row.futu_acc_ids || []).join(', ') || '-'}</td>
                <td>{row.holdings_account || '-'}</td>
                <td>{row.portfolio_source || '-'}</td>
                <td>
                  <button className="LinkBtn" onClick={()=>onEdit(row)}>Edit</button>
                  <span> · </span>
                  <button className="LinkBtn" onClick={()=>onDelete(row)}>Delete</button>
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan="8"><span style={{color:'var(--muted)'}}>当前市场没有账户</span></td></tr>
            )}
          </tbody>
        </table>
      </div>

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">{form.mode === 'edit' ? '编辑账户' : '新增账户'}</div>
            <div className="StrategySub">account_settings / trade_intake.account_mapping.futu</div>
          </div>
          <span className="StrategyPill">{form.mode === 'edit' ? 'EDIT' : 'ADD'}</span>
        </div>
        <div className="StrategyGrid">
          <Field label="mode">
            <select className="Control" value={form.mode} onChange={e=>setForm(prev=>({...prev, mode:e.target.value}))}>
              <option value="add">add</option>
              <option value="edit">edit</option>
            </select>
          </Field>
          <Field label="account label">
            <input className="Control" value={form.accountLabel} onChange={e=>setForm(prev=>({...prev, accountLabel:e.target.value}))} placeholder="user1 / sy" />
          </Field>
          <Field label="account type">
            <select className="Control" value={form.accountType} onChange={e=>setForm(prev=>({...prev, accountType:e.target.value}))}>
              <option value="futu">futu</option>
              <option value="external_holdings">external_holdings</option>
            </select>
          </Field>
          <Field label="futu acc id">
            <input className="Control" value={form.futuAccId} onChange={e=>setForm(prev=>({...prev, futuAccId:e.target.value}))} placeholder="281756..." />
          </Field>
          <Field label="holdings_account">
            <input className="Control" value={form.holdingsAccount} onChange={e=>setForm(prev=>({...prev, holdingsAccount:e.target.value}))} placeholder="lx / Feishu EXT" />
          </Field>
        </div>
        <div className="GlobalSaveBar">
          <div>
            <strong>账户写回</strong>
            <span>保存后会直接校验 runtime config。</span>
          </div>
          <div className="OpsToolbar">
            <button className="Button" onClick={onReset}>重置</button>
            <button className="Button ButtonPrimary" onClick={onSave}>保存账户</button>
          </div>
        </div>
      </section>
    </div>
  );
}

function ResultsPanel({ marketMeta, runningTool, toolResult, repairHint, accountRows, onRun }){
  const accountPaths = toolResult?.result?.data?.account_paths || {};
  const resultData = toolResult?.result?.data || {};
  const summary = resultData?.summary || null;
  const topCandidates = resultData?.top_candidates || [];
  const topRows = resultData?.top_rows || [];
  const notificationPreview = resultData?.notification_preview || '';
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">{marketMeta.label} Results</div>
          <h2 className="PanelTitle">运行结果与状态</h2>
          <p className="PanelText">直接触发只读工具，查看结构化结果与最新产物路径。</p>
        </div>
        <div className="ConfigPath">
          <span>工具流</span>
          <code>healthcheck → scan_opportunities → get_close_advice</code>
        </div>
      </div>

      <div className="OpsToolbar">
        <button className="Button" disabled={!!runningTool} onClick={()=>onRun('healthcheck')}>运行 healthcheck</button>
        <button className="Button" disabled={!!runningTool} onClick={()=>onRun('scan_opportunities')}>运行 scan_opportunities</button>
        <button className="Button ButtonPrimary" disabled={!!runningTool} onClick={()=>onRun('get_close_advice')}>运行 get_close_advice</button>
        {!!runningTool && <span className="MutedInline">running: {runningTool}</span>}
      </div>

      {!!toolResult && (
        <>
          {!!repairHint && (
            <section className="StrategyCard">
              <div className="StrategyHeader">
                <div>
                  <div className="StrategyTitle">修复建议</div>
                  <div className="StrategySub">{repairHint.code || 'tool error'}</div>
                </div>
                <span className="StrategyPill">FIX</span>
              </div>
              <div className="CheckList">
                <div className="CheckItem CheckItemBad">
                  <strong>{repairHint.code || 'ERROR'}</strong>
                  <span>{repairHint.summary}</span>
                </div>
                {(repairHint.actions || []).map((item, idx)=>(
                  <div key={idx} className="CheckItem">
                    <strong>Action {idx + 1}</strong>
                    <span>{item}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
          {!!accountRows?.length && (
            <section className="StrategyCard">
              <div className="StrategyHeader">
                <div>
                  <div className="StrategyTitle">账户主路径 / 兜底路径</div>
                  <div className="StrategySub">primary / fallback visibility</div>
                </div>
                <span className="StrategyPill">PATHS</span>
              </div>
              <div className="CheckList">
                {accountRows.map((row)=>(
                  <div key={row.account_label} className={`CheckItem ${row.primary_ready ? 'CheckItemOk' : 'CheckItemBad'}`}>
                    <strong>{row.account_label}</strong>
                    <span>
                      主路径: {row.primary_source || '-'} / {row.primary_ready ? 'ready' : 'missing'}
                      {' · '}
                      兜底: {row.fallback_enabled ? `${row.fallback_source || 'holdings'} / ${row.fallback_ready ? 'ready' : 'missing'}` : 'disabled'}
                    </span>
                  </div>
                ))}
              </div>
              {!!Object.keys(accountPaths).length && (
                <div className="PreviewPanel">
                  <div className="SectionTitle">本次 healthcheck 视图</div>
                  <pre className="JsonPreview">{JSON.stringify(accountPaths, null, 2)}</pre>
                </div>
              )}
            </section>
          )}
          {!!summary && (
            <section className="StrategyCard">
              <div className="StrategyHeader">
                <div>
                  <div className="StrategyTitle">摘要</div>
                  <div className="StrategySub">ui-facing summary</div>
                </div>
                <span className="StrategyPill">SUMMARY</span>
              </div>
              <div className="PreviewPanel">
                <pre className="JsonPreview">{JSON.stringify(summary, null, 2)}</pre>
              </div>
            </section>
          )}
          {!!topCandidates.length && (
            <section className="StrategyCard">
              <div className="StrategyHeader">
                <div>
                  <div className="StrategyTitle">Top Candidates</div>
                  <div className="StrategySub">scan_opportunities</div>
                </div>
                <span className="StrategyPill">{topCandidates.length}</span>
              </div>
              <div className="PreviewPanel">
                <pre className="JsonPreview">{JSON.stringify(topCandidates, null, 2)}</pre>
              </div>
            </section>
          )}
          {!!topRows.length && (
            <section className="StrategyCard">
              <div className="StrategyHeader">
                <div>
                  <div className="StrategyTitle">Top Close Advice</div>
                  <div className="StrategySub">get_close_advice / close_advice</div>
                </div>
                <span className="StrategyPill">{topRows.length}</span>
              </div>
              <div className="PreviewPanel">
                <pre className="JsonPreview">{JSON.stringify(topRows, null, 2)}</pre>
              </div>
              {!!notificationPreview && (
                <div className="PreviewPanel">
                  <div className="SectionTitle">通知预览</div>
                  <pre className="JsonPreview">{notificationPreview}</pre>
                </div>
              )}
            </section>
          )}
          <section className="StrategyCard">
            <div className="StrategyHeader">
              <div>
                <div className="StrategyTitle">结构化返回</div>
                <div className="StrategySub">{toolResult?.result?.tool_name || toolResult?.result?.toolName || 'tool result'}</div>
              </div>
              <span className="StrategyPill">{toolResult?.result?.ok ? 'OK' : 'ERR'}</span>
            </div>
            <div className="PreviewPanel">
              <pre className="JsonPreview">{JSON.stringify(toolResult.result, null, 2)}</pre>
            </div>
          </section>
          <section className="StrategyCard">
            <div className="StrategyHeader">
              <div>
                <div className="StrategyTitle">产物快照</div>
                <div className="StrategySub">latest output/agent_plugin snapshot</div>
              </div>
              <span className="StrategyPill">FILES</span>
            </div>
            <div className="PreviewPanel">
              <pre className="JsonPreview">{JSON.stringify(toolResult.snapshot, null, 2)}</pre>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function HistoryPanel({ marketMeta, history, onRefresh }){
  const toolExecutions = history?.toolExecutions || [];
  const auditEvents = history?.auditEvents || [];
  const tickMetrics = history?.tickMetrics || [];
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">{marketMeta.label} History</div>
          <h2 className="PanelTitle">审计历史与最近运行</h2>
          <p className="PanelText">查看最近工具执行、审计事件、last_run 和 tick 指标，不需要翻目录。</p>
        </div>
        <div className="OpsToolbar" style={{padding:0, justifyContent:'flex-end'}}>
          <button className="Button" onClick={onRefresh}>刷新历史</button>
        </div>
      </div>

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">最近工具执行</div>
            <div className="StrategySub">tool_execution_audit.jsonl</div>
          </div>
          <span className="StrategyPill">{toolExecutions.length}</span>
        </div>
        {!toolExecutions.length ? <div className="MutedText">当前还没有可展示的工具执行记录。</div> : (
          <div className="PreviewPanel">
            <pre className="JsonPreview">{JSON.stringify(toolExecutions, null, 2)}</pre>
          </div>
        )}
      </section>

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">最近审计事件</div>
            <div className="StrategySub">audit_events.jsonl</div>
          </div>
          <span className="StrategyPill">{auditEvents.length}</span>
        </div>
        {!auditEvents.length ? <div className="MutedText">当前还没有审计事件。</div> : (
          <div className="PreviewPanel">
            <pre className="JsonPreview">{JSON.stringify(auditEvents, null, 2)}</pre>
          </div>
        )}
      </section>

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">Last Run / Tick Metrics</div>
            <div className="StrategySub">shared current read model</div>
          </div>
          <span className="StrategyPill">STATE</span>
        </div>
        <div className="PreviewPanel">
          <pre className="JsonPreview">{JSON.stringify({ lastRun: history?.lastRun || null, latestAudit: history?.latestAudit || null, tickMetrics }, null, 2)}</pre>
        </div>
      </section>
    </div>
  );
}
