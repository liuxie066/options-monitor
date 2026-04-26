import React, { useEffect, useMemo, useRef, useState } from 'react';
import { TokenDialog } from './webuiShared.jsx';
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
  postWatchlistUpsert,
} from './webuiApi.js';
import {
  buildGlobalForm,
  emptyAccountForm,
  emptyEditor,
  emptySymbolForm,
  accountFormFromItem,
  marketMetaFor,
  MODULES,
  MARKETS,
  STRATEGY_FIELDS,
  symbolFormFromRow,
  toAccountsList,
} from './webuiModel.js';
import { buildStrategySidePayload, filterRowsByKeyword, nowId } from './webuiState.js';
import {
  AccountsPanel,
  AdvancedPanel,
  CloseAdvicePanel,
  MarketPanel,
  NotificationPanel,
  StrategyPanel,
} from './webuiPanels.jsx';

export default function App() {
  const [selectedMarket, setSelectedMarket] = useState('hk');
  const [activeModule, setActiveModule] = useState('market');
  const [status, setStatus] = useState('-');
  const [versionStatus, setVersionStatus] = useState('版本检查中');
  const [toasts, setToasts] = useState([]);
  const [tokenRequired, setTokenRequired] = useState(false);
  const [tokenDlgOpen, setTokenDlgOpen] = useState(false);
  const [tokenDlgAction, setTokenDlgAction] = useState('');
  const [tokenDlgValue, setTokenDlgValue] = useState('');
  const [tokenDlgError, setTokenDlgError] = useState('');
  const [tokenDlgOnOk, setTokenDlgOnOk] = useState(() => null);
  const [editorData, setEditorData] = useState(() => emptyEditor('hk'));
  const [configSummaries, setConfigSummaries] = useState({});
  const [globalForm, setGlobalForm] = useState(() => buildGlobalForm(null, null));
  const [accountForm, setAccountForm] = useState(() => emptyAccountForm('hk'));
  const [symbolForm, setSymbolForm] = useState(() => emptySymbolForm('hk'));
  const [rows, setRows] = useState([]);
  const [q, setQ] = useState('');
  const [toolRunning, setToolRunning] = useState('');
  const [toolResult, setToolResult] = useState(null);
  const [toolRepairHint, setToolRepairHint] = useState(null);
  const [historyData, setHistoryData] = useState(null);
  const [notificationCheck, setNotificationCheck] = useState(null);
  const [notificationPreview, setNotificationPreview] = useState('');
  const [notificationSendResult, setNotificationSendResult] = useState(null);
  const tokenInputRef = useRef(null);

  function pushToast(kind, text, ms = 3000) {
    const id = nowId();
    setToasts((prev) => [...prev, { id, kind, text }]);
    window.setTimeout(() => setToasts((prev) => prev.filter((item) => item.id !== id)), ms);
  }

  async function loadMeta() {
    const data = await fetchMeta();
    setTokenRequired(!!data.tokenRequired);
  }

  async function loadVersionCheck() {
    const data = await fetchVersionCheck();
    if (!data || data.ok === false) {
      setVersionStatus('版本检查失败');
      return;
    }
    setVersionStatus(String(data.message || '版本信息不可用'));
  }

  async function loadSummaries() {
    const data = await fetchConfigSummaries();
    setConfigSummaries(data.configs || {});
  }

  async function loadEditor(configKey = selectedMarket) {
    const data = await fetchEditor(configKey);
    setEditorData(data.editor || emptyEditor(configKey));
  }

  async function loadRows() {
    const data = await fetchWatchlist();
    setRows(data.rows || []);
  }

  async function loadHistory(configKey = selectedMarket) {
    const data = await fetchHistory(configKey);
    setHistoryData(data || null);
  }

  useEffect(() => {
    loadMeta().catch((e) => pushToast('error', e.message));
    loadVersionCheck().catch(() => setVersionStatus('版本检查失败'));
    loadRows().catch((e) => pushToast('error', e.message));
    loadSummaries().catch((e) => pushToast('error', e.message));
  }, []);

  useEffect(() => {
    loadEditor(selectedMarket).catch((e) => pushToast('error', e.message));
    loadHistory(selectedMarket).catch((e) => pushToast('error', e.message));
    setAccountForm(emptyAccountForm(selectedMarket));
    setSymbolForm(emptySymbolForm(selectedMarket));
    setQ('');
  }, [selectedMarket]);

  useEffect(() => {
    setGlobalForm(buildGlobalForm(configSummaries[selectedMarket], editorData));
  }, [configSummaries, selectedMarket, editorData]);

  const currentRows = useMemo(() => rows.filter((row) => row.configKey === selectedMarket), [rows, selectedMarket]);
  const filteredRows = useMemo(() => filterRowsByKeyword(currentRows, q), [currentRows, q]);
  const marketMeta = marketMetaFor(selectedMarket);

  useEffect(() => {
    setStatus(`标的 ${filteredRows.length}/${currentRows.length}`);
  }, [filteredRows.length, currentRows.length]);

  const withWriteToken = withWriteTokenFactory({ tokenRequired, setTokenDlgError, setTokenDlgValue, setTokenDlgAction, setTokenDlgOnOk, setTokenDlgOpen });
  const saveGlobal = createSaveGlobalAction({ globalForm, selectedMarket, marketMeta, withWriteToken, loadEditor, setConfigSummaries, pushToast, STRATEGY_FIELDS, toAccountsList });
  const saveAccount = createSaveAccountAction({ accountForm, selectedMarket, withWriteToken, loadSummaries, loadEditor, setAccountForm, emptyAccountForm, pushToast });
  const removeAccount = createRemoveAccountAction({ selectedMarket, withWriteToken, loadSummaries, loadEditor, setAccountForm, emptyAccountForm, pushToast });
  const saveSymbol = createSaveSymbolAction({ symbolForm, selectedMarket, withWriteToken, setRows, loadSummaries, setSymbolForm, emptySymbolForm, pushToast, toAccountsList });
  const runTool = createRunToolAction({ selectedMarket, setToolRunning, setToolResult, setToolRepairHint, loadHistory, pushToast });
  const checkNotifications = createCheckNotificationsAction({ selectedMarket, setNotificationCheck, pushToast });
  const previewNotifications = createPreviewNotificationsAction({ selectedMarket, editorData, setNotificationPreview, pushToast });
  const sendNotification = createSendNotificationAction({ selectedMarket, notificationPreview, withWriteToken, setNotificationSendResult, pushToast });
  const confirmToken = () => confirmTokenAction({ tokenDlgValue, setTokenDlgError, tokenDlgOnOk, setTokenDlgOpen, pushToast });

  return (
    <>
      <div className="Header">
        <div className="HeaderInner">
          <div className="Title"><span className="Mark">OM</span> 配置中心</div>
          <div className="HeaderTabs" role="tablist" aria-label="市场">
            {MARKETS.map((item) => (
              <button key={item.key} className={`HeaderTab ${selectedMarket === item.key ? 'HeaderTabActive' : ''}`} onClick={() => setSelectedMarket(item.key)}>{item.label}</button>
            ))}
          </div>
          <div className="Status">{status}</div>
          <div className="Status">{versionStatus}</div>
        </div>
      </div>

      <div className="Page">
        <section className="HeroPanel">
          <div>
            <div className="Eyebrow">兼容视图</div>
            <h1 className="HeroTitle">按模块管理运行配置</h1>
            <p className="HeroText">新的六模块页面建立在现有 runtime config 之上。当前版本对每账户不同 OpenD 持仓仍采用兼容处理，不改变底层运行语义。</p>
          </div>
          <div className="HeroStats">
            <div className="StatCard"><span>市场</span><strong>{marketMeta.label}</strong></div>
            <div className="StatCard"><span>标的</span><strong>{currentRows.length}</strong></div>
            <div className="StatCard"><span>账户</span><strong>{(editorData.accounts || []).length}</strong></div>
          </div>
        </section>

        <div className="ModuleTabs" role="tablist" aria-label="配置模块">
          {MODULES.map(([key, label]) => <button key={key} className={`ModuleTab ${activeModule === key ? 'ModuleTabActive' : ''}`} onClick={() => setActiveModule(key)}>{label}</button>)}
          <span className="Spacer ModuleTabsSpacer" />
          <button className="Button ButtonPrimary BtnNew" onClick={() => saveGlobal().catch((e) => pushToast('error', e.message))}>保存当前市场</button>
        </div>

        <div className="PreviewPanel" style={{ marginBottom: 16 }}>
          <div className="SectionTitle">当前兼容边界</div>
          <div className="CheckList" style={{ padding: 0 }}>
            <div className="CheckItem"><strong>行情来源</strong><span>UI 使用 marketData，但保存时仍会同步旧的 symbols[].fetch 字段。</span></div>
            <div className="CheckItem"><strong>账户配置</strong><span>保留旧 account_settings / source_by_account / trade_intake.account_mapping.futu，不做硬切换。</span></div>
            <div className="CheckItem"><strong>消息凭证</strong><span>凭证仍写入 secrets 文件，不放进 runtime config，也不会从后端明文回传。</span></div>
          </div>
        </div>

        {activeModule === 'market' && <MarketPanel globalForm={globalForm} setGlobalForm={setGlobalForm} onSave={() => saveGlobal().catch((e) => pushToast('error', e.message))} />}
        {activeModule === 'accounts' && <AccountsPanel selectedMarket={selectedMarket} accounts={editorData.accounts || []} form={accountForm} setForm={setAccountForm} onEdit={(item) => setAccountForm(accountFormFromItem(item, selectedMarket))} onDelete={(item) => removeAccount(item).catch((e) => pushToast('error', e.message))} onSave={() => saveAccount().catch((e) => pushToast('error', e.message))} onReset={() => setAccountForm(emptyAccountForm(selectedMarket))} />}
        {activeModule === 'strategy' && <StrategyPanel rows={filteredRows} q={q} setQ={setQ} form={symbolForm} setForm={setSymbolForm} globalForm={globalForm} setGlobalForm={setGlobalForm} onEdit={(row) => setSymbolForm(symbolFormFromRow(row))} onSaveSymbol={() => saveSymbol().catch((e) => pushToast('error', e.message))} onSaveTemplate={() => saveGlobal().catch((e) => pushToast('error', e.message))} />}
        {activeModule === 'closeAdvice' && <CloseAdvicePanel globalForm={globalForm} setGlobalForm={setGlobalForm} onSave={() => saveGlobal().catch((e) => pushToast('error', e.message))} />}
        {activeModule === 'notifications' && <NotificationPanel globalForm={globalForm} setGlobalForm={setGlobalForm} notificationCheck={notificationCheck} notificationPreview={notificationPreview} notificationSendResult={notificationSendResult} onSave={() => saveGlobal().catch((e) => pushToast('error', e.message))} onCheck={() => checkNotifications().catch((e) => pushToast('error', e.message))} onPreview={() => previewNotifications().catch((e) => pushToast('error', e.message))} onDryRun={() => sendNotification(false).catch((e) => pushToast('error', e.message))} onSend={() => sendNotification(true).catch((e) => pushToast('error', e.message))} />}
        {activeModule === 'advanced' && <AdvancedPanel globalForm={globalForm} setGlobalForm={setGlobalForm} history={historyData} toolResult={toolResult} repairHint={toolRepairHint} runningTool={toolRunning} onRun={(toolName) => runTool(toolName).catch((e) => pushToast('error', e.message))} onRefreshHistory={() => loadHistory(selectedMarket).catch((e) => pushToast('error', e.message))} />}
      </div>

      <TokenDialog open={tokenDlgOpen} action={tokenDlgAction} value={tokenDlgValue} setValue={setTokenDlgValue} error={tokenDlgError} setOpen={setTokenDlgOpen} onConfirm={confirmToken} tokenInputRef={tokenInputRef} />
    </>
  );
}
