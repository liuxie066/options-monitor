import React, { useEffect, useMemo, useRef, useState } from 'react';
import { TokenDialog } from './webuiShared.jsx';
import {
  createCheckNotificationsAction,
  createPreviewNotificationsAction,
  createRemoveAccountAction,
  createRemoveSymbolAction,
  createSaveAccountAction,
  createSaveGlobalAction,
  createSaveSymbolAction,
  createSendNotificationAction,
  confirmTokenAction,
  loadEditorAction,
  loadMetaAction,
  loadRowsAction,
  loadSummariesAction,
  loadVersionCheckAction,
  withWriteTokenFactory,
} from './webuiActions.js';
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
import { filterRowsByKeyword, nowId } from './webuiState.js';
import {
  AccountsPanel,
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
  const [symbolDialogOpen, setSymbolDialogOpen] = useState(false);
  const [rows, setRows] = useState([]);
  const [q, setQ] = useState('');
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
    return loadMetaAction(setTokenRequired);
  }

  async function loadVersionCheck() {
    return loadVersionCheckAction(setVersionStatusFromPayload);
  }

  async function loadSummaries() {
    return loadSummariesAction(setConfigSummaries);
  }

  async function loadEditor(configKey = selectedMarket) {
    return loadEditorAction(configKey, emptyEditor, setEditorData);
  }

  async function loadRows() {
    return loadRowsAction(setRows);
  }

  useEffect(() => {
    loadMeta().catch((e) => pushToast('error', e.message));
    loadVersionCheck().catch(() => setVersionStatus('版本检查失败'));
    loadRows().catch((e) => pushToast('error', e.message));
    loadSummaries().catch((e) => pushToast('error', e.message));
  }, []);

  useEffect(() => {
    loadEditor(selectedMarket).catch((e) => pushToast('error', e.message));
    setAccountForm(emptyAccountForm(selectedMarket));
    setSymbolForm(emptySymbolForm(selectedMarket));
    setSymbolDialogOpen(false);
    setQ('');
  }, [selectedMarket]);

  useEffect(() => {
    setGlobalForm(buildGlobalForm(configSummaries[selectedMarket], editorData));
  }, [configSummaries, selectedMarket, editorData]);

  const currentRows = useMemo(() => rows.filter((row) => row.configKey === selectedMarket), [rows, selectedMarket]);
  const filteredRows = useMemo(() => filterRowsByKeyword(currentRows, q), [currentRows, q]);
  const marketMeta = marketMetaFor(selectedMarket);

  function setVersionStatusFromPayload(payload) {
    if (!payload || payload.ok === false) {
      setVersionStatus('版本检查失败');
      return;
    }
    setVersionStatus(String(payload.message || '版本信息不可用'));
  }

  useEffect(() => {
    setStatus(`标的 ${filteredRows.length}/${currentRows.length}`);
  }, [filteredRows.length, currentRows.length]);

  const withWriteToken = withWriteTokenFactory({ tokenRequired, setTokenDlgError, setTokenDlgValue, setTokenDlgAction, setTokenDlgOnOk, setTokenDlgOpen });
  const saveGlobal = createSaveGlobalAction({ globalForm, selectedMarket, marketMeta, withWriteToken, loadEditor, setConfigSummaries, pushToast, STRATEGY_FIELDS, toAccountsList });
  const saveAccount = createSaveAccountAction({ accountForm, selectedMarket, withWriteToken, loadSummaries, loadEditor, setAccountForm, emptyAccountForm, pushToast });
  const removeAccount = createRemoveAccountAction({ selectedMarket, withWriteToken, loadSummaries, loadEditor, setAccountForm, emptyAccountForm, pushToast });
  const saveSymbol = createSaveSymbolAction({ symbolForm, selectedMarket, withWriteToken, setRows, loadSummaries, setSymbolForm, emptySymbolForm, pushToast, toAccountsList });
  const removeSymbol = createRemoveSymbolAction({ selectedMarket, withWriteToken, setRows, loadSummaries, setSymbolForm, emptySymbolForm, pushToast });
  const checkNotifications = createCheckNotificationsAction({ selectedMarket, setNotificationCheck, pushToast });
  const previewNotifications = createPreviewNotificationsAction({ selectedMarket, editorData, setNotificationPreview, pushToast });
  const sendNotification = createSendNotificationAction({ selectedMarket, notificationPreview, withWriteToken, setNotificationSendResult, pushToast });
  const confirmToken = () => confirmTokenAction({ tokenDlgValue, setTokenDlgError, tokenDlgOnOk, setTokenDlgOpen, pushToast });
  const activeModuleLabel = MODULES.find(([key]) => key === activeModule)?.[1] || '配置模块';
  const openCreateSymbolDialog = () => {
    setSymbolForm(emptySymbolForm(selectedMarket));
    setSymbolDialogOpen(true);
  };
  const openEditSymbolDialog = (row) => {
    setSymbolForm(symbolFormFromRow(row));
    setSymbolDialogOpen(true);
  };
  const closeSymbolDialog = () => {
    setSymbolDialogOpen(false);
    setSymbolForm(emptySymbolForm(selectedMarket));
  };

  return (
    <>
      <div className="Header">
        <div className="HeaderInner">
          <div className="Title"><span className="Mark">OM</span> 配置中心</div>
          <div className="Status">{status}</div>
          <div className="Status">{versionStatus}</div>
        </div>
      </div>

      <div className="Page">
        <div className="WorkspaceShell">
          <aside className="ModuleNav">
            <div className="ModuleTabs ModuleTabsVertical" role="tablist" aria-label="配置模块">
              {MODULES.map(([key, label]) => (
                <button key={key} className={`ModuleTab ModuleTabVertical ${activeModule === key ? 'ModuleTabActive' : ''}`} onClick={() => setActiveModule(key)}>
                  {label}
                </button>
              ))}
            </div>
          </aside>

          <section className="WorkspaceContent">
            {activeModule === 'market' && <MarketPanel globalForm={globalForm} setGlobalForm={setGlobalForm} onSave={() => saveGlobal().catch((e) => pushToast('error', e.message))} />}
            {activeModule === 'accounts' && <AccountsPanel selectedMarket={selectedMarket} accounts={editorData.accounts || []} form={accountForm} setForm={setAccountForm} onEdit={(item) => setAccountForm(accountFormFromItem(item, selectedMarket))} onDelete={(item) => removeAccount(item).catch((e) => pushToast('error', e.message))} onSave={() => saveAccount().catch((e) => pushToast('error', e.message))} onReset={() => setAccountForm(emptyAccountForm(selectedMarket))} />}
            {activeModule === 'strategy' && <StrategyPanel rows={filteredRows} q={q} setQ={setQ} form={symbolForm} setForm={setSymbolForm} globalForm={globalForm} setGlobalForm={setGlobalForm} selectedMarket={selectedMarket} setSelectedMarket={setSelectedMarket} markets={MARKETS} symbolDialogOpen={symbolDialogOpen} setSymbolDialogOpen={setSymbolDialogOpen} onCreate={openCreateSymbolDialog} onEdit={openEditSymbolDialog} onCancelSymbolEdit={closeSymbolDialog} onDeleteSymbol={(item) => removeSymbol(item).then(() => setSymbolDialogOpen(false)).catch((e) => pushToast('error', e.message))} onSaveSymbol={() => saveSymbol().then(() => setSymbolDialogOpen(false)).catch((e) => pushToast('error', e.message))} onSaveTemplate={() => saveGlobal().catch((e) => pushToast('error', e.message))} />}
            {activeModule === 'closeAdvice' && <CloseAdvicePanel globalForm={globalForm} setGlobalForm={setGlobalForm} onSave={() => saveGlobal().catch((e) => pushToast('error', e.message))} />}
            {activeModule === 'notifications' && <NotificationPanel globalForm={globalForm} setGlobalForm={setGlobalForm} notificationCheck={notificationCheck} notificationPreview={notificationPreview} notificationSendResult={notificationSendResult} onSave={() => saveGlobal().catch((e) => pushToast('error', e.message))} onCheck={() => checkNotifications().catch((e) => pushToast('error', e.message))} onPreview={() => previewNotifications().catch((e) => pushToast('error', e.message))} onDryRun={() => sendNotification(false).catch((e) => pushToast('error', e.message))} onSend={() => sendNotification(true).catch((e) => pushToast('error', e.message))} />}
          </section>
        </div>
      </div>

      <TokenDialog open={tokenDlgOpen} action={tokenDlgAction} value={tokenDlgValue} setValue={setTokenDlgValue} error={tokenDlgError} setOpen={setTokenDlgOpen} onConfirm={confirmToken} tokenInputRef={tokenInputRef} />
    </>
  );
}
