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
  return { sell_put: mk(), sell_call: mk() };
}

function globalFormFromSummary(summary){
  const strategy = summary?.globalStrategy || {};
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
  const [configSummaries, setConfigSummaries] = useState({});
  const [tokenRequired, setTokenRequired] = useState(false);
  const [accountOptions, setAccountOptions] = useState([]);

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
  const [configModule, setConfigModule] = useState('global');
  const [globalForm, setGlobalForm] = useState(()=>emptyGlobalForm());

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

  useEffect(()=>{ loadRows().catch(e=>pushToast('error', e.message)); },[]);
  useEffect(()=>{ loadConfigSummaries().catch(e=>pushToast('error', e.message)); },[]);

  useEffect(()=>{
    setGlobalForm(globalFormFromSummary(configSummaries[selectedMarket]));
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
      if (qq && !String(r.symbol||'').toUpperCase().includes(qq)) return false;
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
    };

    for (const side of Object.keys(payload.strategies)){
      for (const [key, label] of GLOBAL_STRATEGY_FIELDS){
        const v = payload.strategies[side][key];
        if (!Number.isFinite(v)) throw new Error(`${side} ${label} 需要填写数字`);
      }
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
              <button className={`ModuleTab ${configModule === 'symbols' ? 'ModuleTabActive' : ''}`} onClick={()=>setConfigModule('symbols')}>标的配置</button>
            </div>

            {configModule === 'global' && (
              <GlobalConfigPanel
                summary={currentSummary}
                marketMeta={marketMeta}
                form={globalForm}
                setForm={setGlobalForm}
                onSave={()=>saveGlobalConfig().catch(e=>pushToast('error', e.message))}
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

                  <span className="Spacer ToolbarSpacer" />

                  <button className="Button ButtonPrimary BtnNew" onClick={openNew}>新增 {marketMeta.label} 标的</button>
                </div>

                <div className="Box BoxScroll">
                  <table>
                    <thead>
                      <tr>
                        <th>exchange</th><th>symbol</th><th>accounts</th><th>put</th><th>call</th>
                        <th>limit_exp</th><th>put dte</th><th>put strike</th><th>call dte</th><th>call strike</th><th>ops</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((r)=> (
                        <tr key={`${r.configKey}-${r.symbol}`}>
                          <td>{r.market ?? marketMeta.exchange}</td>
                          <td><strong>{r.symbol}</strong></td>
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
                          <td colSpan="11"><span style={{color:'var(--muted)'}}>当前筛选下没有标的配置</span></td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </>
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

function GlobalConfigPanel({summary, marketMeta, form, setForm, onSave}){
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
          <p className="PanelText">维护市场级策略模板字段。保存时只更新 put_base / call_base 的收益率、收益和 D3 硬过滤参数。</p>
        </div>
        <div className="ConfigPath"><span>文件</span><code>{summary.path}</code></div>
      </div>

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

      <div className="GlobalSaveBar">
        <div>
          <strong>保存全局配置</strong>
          <span>会先备份配置文件，再运行 validate_config 校验。</span>
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
