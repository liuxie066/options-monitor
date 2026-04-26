import React from 'react';
import { Dialog, Field, InlineNote, SaveBar, formatAccounts, formatBool } from './webuiShared.jsx';
import { STRATEGY_FIELDS } from './webuiModel.js';

export function MarketPanel({ globalForm, setGlobalForm, onSave }) {
  return (
    <div className="GlobalPanel">
      <section className="StrategyCard">
        <div className="StrategyHeader"><div><div className="StrategyTitle">行情设置</div><div className="StrategySub">market_data + legacy fetch</div></div><span className="StrategyPill">OPEN</span></div>
        <div className="StrategyGrid">
          <Field label="行情来源"><input className="Control" value={globalForm.marketData.source} onChange={(e) => setGlobalForm((prev) => ({ ...prev, marketData: { ...prev.marketData, source: e.target.value } }))} /></Field>
          <Field label="OpenD 地址"><input className="Control" value={globalForm.marketData.host} onChange={(e) => setGlobalForm((prev) => ({ ...prev, marketData: { ...prev.marketData, host: e.target.value } }))} placeholder="127.0.0.1" /></Field>
          <Field label="OpenD 端口"><input className="Control" type="number" value={globalForm.marketData.port} onChange={(e) => setGlobalForm((prev) => ({ ...prev, marketData: { ...prev.marketData, port: e.target.value } }))} placeholder="11111" /></Field>
        </div>
        <div className="PreviewPanel"><InlineNote>当前版本使用兼容双写：这里保存后，会同步更新旧的 symbol fetch 配置，避免现有扫描链路失效。</InlineNote></div>
      </section>
      <SaveBar title="保存行情设置" desc="保存时会回填旧 fetch 字段。" label="保存行情设置" onSave={onSave} />
    </div>
  );
}

export function AccountsPanel({ accounts, form, setForm, onEdit, onDelete, onSave, onReset }) {
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">账户设置</div>
          <h2 className="PanelTitle">账户管理</h2>
          <p className="PanelText">保留旧 account_settings/source_by_account/trade_intake.account_mapping.futu，并在其上追加 UI 需要的新字段。</p>
        </div>
      </div>
      <div className="Box BoxScroll">
        <table>
          <thead><tr><th>账户</th><th>市场</th><th>类型</th><th>自动入账</th><th>持仓映射</th><th>操作</th></tr></thead>
          <tbody>
            {(accounts || []).map((item) => (
              <tr key={item.accountLabel}>
                <td><strong>{item.accountLabel}</strong></td>
                <td>{item.market || '-'}</td>
                <td>{item.accountType}</td>
                <td>{formatBool(item.tradeIntakeEnabled)}</td>
                <td>{item.holdingsAccount || '未设置'}</td>
                <td><button className="LinkBtn" onClick={() => onEdit(item)}>编辑</button>{' · '}<button className="LinkBtn" onClick={() => onDelete(item)}>删除</button></td>
              </tr>
            ))}
            {!(accounts || []).length && <tr><td colSpan="6"><span className="MutedText">当前市场暂无账户</span></td></tr>}
          </tbody>
        </table>
      </div>
      <section className="StrategyCard">
        <div className="StrategyHeader"><div><div className="StrategyTitle">{form.mode === 'edit' ? '编辑账户' : '新增账户'}</div><div className="StrategySub">兼容写回 account_settings / source_by_account / trade_intake</div></div><span className="StrategyPill">ACCT</span></div>
        <div className="StrategyGrid">
          <Field label="模式"><select className="Control" value={form.mode} onChange={(e) => setForm((prev) => ({ ...prev, mode: e.target.value }))}><option value="add">新增</option><option value="edit">编辑</option></select></Field>
          <Field label="账户名称"><input className="Control" value={form.accountLabel} onChange={(e) => setForm((prev) => ({ ...prev, accountLabel: e.target.value }))} /></Field>
          <Field label="所属市场"><select className="Control" value={form.market} onChange={(e) => setForm((prev) => ({ ...prev, market: e.target.value }))}><option value="US">US</option><option value="HK">HK</option></select></Field>
          <Field label="数据来源"><select className="Control" value={form.accountType} onChange={(e) => setForm((prev) => ({ ...prev, accountType: e.target.value, tradeIntakeEnabled: e.target.value === 'futu' }))}><option value="futu">富途 OpenD</option><option value="external_holdings">飞书多维表</option></select></Field>
          <Field label="启用状态"><select className="Control" value={form.enabled ? 'true' : 'false'} onChange={(e) => setForm((prev) => ({ ...prev, enabled: e.target.value === 'true' }))}><option value="true">启用</option><option value="false">关闭</option></select></Field>
          <Field label="自动入账"><select className="Control" value={form.tradeIntakeEnabled ? 'true' : 'false'} onChange={(e) => setForm((prev) => ({ ...prev, tradeIntakeEnabled: e.target.value === 'true' }))}><option value="true">开启</option><option value="false">关闭</option></select></Field>
          <Field label="持仓映射名"><input className="Control" value={form.holdingsAccount} onChange={(e) => setForm((prev) => ({ ...prev, holdingsAccount: e.target.value }))} /></Field>
          {form.accountType === 'futu' ? (
            <>
              <Field label="富途账户 ID"><input className="Control" value={form.futuAccId} onChange={(e) => setForm((prev) => ({ ...prev, futuAccId: e.target.value }))} /></Field>
              <Field label="持仓 OpenD 地址"><input className="Control" value={form.futuHost} onChange={(e) => setForm((prev) => ({ ...prev, futuHost: e.target.value }))} /></Field>
              <Field label="持仓 OpenD 端口"><input className="Control" type="number" value={form.futuPort} onChange={(e) => setForm((prev) => ({ ...prev, futuPort: e.target.value }))} /></Field>
            </>
          ) : (
            <>
              <Field label="App Token"><input className="Control" value={form.bitableAppToken} onChange={(e) => setForm((prev) => ({ ...prev, bitableAppToken: e.target.value }))} /></Field>
              <Field label="数据表 ID"><input className="Control" value={form.bitableTableId} onChange={(e) => setForm((prev) => ({ ...prev, bitableTableId: e.target.value }))} /></Field>
              <Field label="视图名称"><input className="Control" value={form.bitableViewName} onChange={(e) => setForm((prev) => ({ ...prev, bitableViewName: e.target.value }))} /></Field>
            </>
          )}
        </div>
        <div className="PreviewPanel"><InlineNote>{form.accountType === 'futu' ? '兼容版本下，账户级持仓 OpenD 参数会被保留，但运行时仍以现有兼容路径为准。' : '飞书多维表仅展示非敏感连接信息；敏感 token 不会从后端回传。'}</InlineNote></div>
      </section>
      <div className="OpsToolbar"><button className="Button" onClick={onReset}>重置</button><button className="Button ButtonPrimary" onClick={onSave}>保存账户</button></div>
    </div>
  );
}

export function CloseAdvicePanel({ globalForm, setGlobalForm, onSave }) {
  const cfg = globalForm.closeAdvice;
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview"><div><div className="Eyebrow">平仓建议</div><h2 className="PanelTitle">Close Advice</h2><p className="PanelText">独立功能开关与参数，直接映射现有 close_advice。</p></div></div>
      <section className="StrategyCard">
        <div className="StrategyHeader"><div><div className="StrategyTitle">平仓建议</div><div className="StrategySub">close_advice</div></div><span className="StrategyPill">EXIT</span></div>
        <div className="StrategyGrid">
          <Field label="功能开关"><select className="Control" value={cfg.enabled ? 'true' : 'false'} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, enabled: e.target.value === 'true' } }))}><option value="true">开启</option><option value="false">关闭</option></select></Field>
          <Field label="行情来源"><input className="Control" value={cfg.quote_source} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, quote_source: e.target.value } }))} /></Field>
          <Field label="提醒级别"><input className="Control" value={cfg.notify_levels} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, notify_levels: e.target.value } }))} /></Field>
          <Field label="每账户最多条数"><input className="Control" type="number" value={cfg.max_items_per_account} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, max_items_per_account: e.target.value } }))} /></Field>
          <Field label="最大价差比"><input className="Control" type="number" step="any" value={cfg.max_spread_ratio} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, max_spread_ratio: e.target.value } }))} /></Field>
          <Field label="强提醒阈值"><input className="Control" type="number" step="any" value={cfg.strong_remaining_annualized_max} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, strong_remaining_annualized_max: e.target.value } }))} /></Field>
          <Field label="中提醒阈值"><input className="Control" type="number" step="any" value={cfg.medium_remaining_annualized_max} onChange={(e) => setGlobalForm((prev) => ({ ...prev, closeAdvice: { ...prev.closeAdvice, medium_remaining_annualized_max: e.target.value } }))} /></Field>
        </div>
      </section>
      <SaveBar title="保存平仓建议" desc="直接写回 close_advice。" label="保存平仓建议" onSave={onSave} />
    </div>
  );
}

export function NotificationPanel({ globalForm, setGlobalForm, notificationCheck, notificationPreview, notificationSendResult, onSave, onCheck, onPreview, onDryRun, onSend }) {
  const cfg = globalForm.notifications;
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview"><div><div className="Eyebrow">消息通知</div><h2 className="PanelTitle">飞书通知</h2><p className="PanelText">凭证在 UI 中编辑，但仍落到 notifications.secrets_file 指向的 secrets 文件。</p></div></div>
      <section className="StrategyCard">
        <div className="StrategyHeader"><div><div className="StrategyTitle">消息配置</div><div className="StrategySub">notifications + secrets file</div></div><span className="StrategyPill">SEND</span></div>
        <div className="StrategyGrid">
          <Field label="通知渠道"><input className="Control" value={cfg.channel} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, channel: e.target.value } }))} /></Field>
          <Field label="接收对象 open_id"><input className="Control" value={cfg.target} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, target: e.target.value } }))} /></Field>
          <Field label="App ID"><input className="Control" value={cfg.appId} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, appId: e.target.value } }))} placeholder={cfg.hasCredentials ? '已保存，留空表示不修改' : 'cli_xxx'} /></Field>
          <Field label="App Secret"><input className="Control" type="password" value={cfg.appSecret} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, appSecret: e.target.value } }))} placeholder={cfg.hasCredentials ? '已保存，留空表示不修改' : 'app_secret'} /></Field>
          <Field label="静默开始"><input className="Control" value={cfg.quiet_hours_start} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, quiet_hours_start: e.target.value } }))} placeholder="23:00" /></Field>
          <Field label="静默结束"><input className="Control" value={cfg.quiet_hours_end} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, quiet_hours_end: e.target.value } }))} placeholder="08:30" /></Field>
          <Field label="附带现金信息"><select className="Control" value={cfg.include_cash_footer ? 'true' : 'false'} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, include_cash_footer: e.target.value === 'true' } }))}><option value="true">开启</option><option value="false">关闭</option></select></Field>
          <Field label="现金适用账户"><input className="Control" value={cfg.cash_footer_accounts} onChange={(e) => setGlobalForm((prev) => ({ ...prev, notifications: { ...prev.notifications, cash_footer_accounts: e.target.value } }))} placeholder="lx,sy" /></Field>
        </div>
        <div className="PreviewPanel"><InlineNote>{cfg.hasCredentials ? '已检测到已保存的飞书凭证。留空不会覆盖旧凭证。' : '当前还没有检测到已保存凭证；保存前请填写 App ID 与 App Secret。'}</InlineNote></div>
      </section>
      <div className="OpsToolbar"><button className="Button" onClick={onCheck}>连通检测</button><button className="Button" onClick={onPreview}>消息预览</button><button className="Button" onClick={onDryRun}>dry-run</button><button className="Button ButtonDanger" onClick={onSend}>测试发送</button></div>
      {!!notificationCheck?.checks?.length && <div className="CheckList">{notificationCheck.checks.map((item) => <div key={item.name} className={`CheckItem ${item.ok ? 'CheckItemOk' : 'CheckItemBad'}`}><strong>{item.name}</strong><span>{item.message}</span></div>)}</div>}
      {!!notificationPreview && <div className="PreviewPanel"><div className="SectionTitle">通知预览</div><pre className="JsonPreview">{notificationPreview}</pre></div>}
      {!!notificationSendResult && <div className="PreviewPanel"><div className="SectionTitle">发送结果</div><pre className="JsonPreview">{JSON.stringify(notificationSendResult, null, 2)}</pre></div>}
      <SaveBar title="保存消息通知" desc="保存 notifications 并同步 secrets 文件。" label="保存消息通知" onSave={onSave} />
    </div>
  );
}

export function StrategyPanel({ rows, q, setQ, form, setForm, globalForm, setGlobalForm, selectedMarket, setSelectedMarket, markets, symbolDialogOpen, setSymbolDialogOpen, onCreate, onEdit, onCancelSymbolEdit, onDeleteSymbol, onSaveSymbol, onSaveTemplate }) {
  function updateSymbolField(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function updateTemplate(side, key, value) {
    setGlobalForm((prev) => ({
      ...prev,
      strategy: {
        ...(prev?.strategy || {}),
        [side]: {
          ...(prev?.strategy?.[side] || {}),
          [key]: value,
        },
      },
    }));
  }

  function renderTemplateCard(title, side, pill) {
    const data = globalForm?.strategy?.[side] || {};
    return (
      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">{title}</div>
            <div className="StrategySub">templates.{side}</div>
          </div>
          <span className="StrategyPill">{pill}</span>
        </div>
        <div className="StrategyGrid">
          {STRATEGY_FIELDS.map(([key, label]) => (
            <Field key={`${side}-${key}`} label={label}>
              <input
                className="Control"
                type="number"
                step="any"
                value={data?.[key] ?? ''}
                onChange={(e) => updateTemplate(side, key, e.target.value)}
              />
            </Field>
          ))}
        </div>
      </section>
    );
  }

  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">选股策略</div>
          <h2 className="PanelTitle">标的与模板</h2>
          <p className="PanelText">上半区维护市场级策略模板，下半区维护单个标的的开关与边界参数。</p>
        </div>
        <div className="StrategyOverviewSide">
          <div className="HeaderTabs" role="tablist" aria-label="策略市场">
            {(markets || []).map((item) => (
              <button key={item.key} className={`HeaderTab ${selectedMarket === item.key ? 'HeaderTabActive' : ''}`} onClick={() => setSelectedMarket(item.key)}>
                {item.label}
              </button>
            ))}
          </div>
          <div className="ConfigPath"><span>当前标的</span><code>{rows.length}</code></div>
        </div>
      </div>

      <div className="GlobalFormShell">
        {renderTemplateCard('Sell Put 模板', 'sell_put', 'PUT')}
        {renderTemplateCard('Covered Call 模板', 'sell_call', 'CALL')}
      </div>

      <SaveBar title="保存市场模板" desc="保存 put/call 模板阈值。" label="保存策略模板" onSave={onSaveTemplate} />

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">标的列表</div>
            <div className="StrategySub">watchlist</div>
          </div>
          <span className="StrategyPill">LIST</span>
        </div>
        <div className="OpsToolbar">
          <input className="Control" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索 symbol / account" />
          <span className="Spacer" />
          <button className="Button ButtonPrimary" onClick={onCreate}>新增标的</button>
        </div>
        <div className="Box BoxScroll">
          <table>
            <thead>
              <tr>
                <th>symbol</th>
                <th>market</th>
                <th>accounts</th>
                <th>put</th>
                <th>call</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(rows || []).map((row) => (
                <tr key={`${row.configKey}-${row.symbol}`}>
                  <td><strong>{row.symbol}</strong></td>
                  <td>{row.market || '-'}</td>
                  <td>{formatAccounts(row.accounts)}</td>
                  <td>{formatBool(row.sell_put_enabled)}</td>
                  <td>{formatBool(row.sell_call_enabled)}</td>
                  <td><button className="LinkBtn" onClick={() => onEdit(row)}>编辑</button></td>
                </tr>
              ))}
              {!(rows || []).length && (
                <tr><td colSpan="6"><span className="MutedText">当前筛选条件下没有标的</span></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <Dialog
        open={symbolDialogOpen}
        title={form?.symbol ? `编辑 ${form.symbol}` : '新增标的'}
        subtitle="symbol-level overrides"
        onClose={onCancelSymbolEdit}
        actions={(
          <>
            <button className="Button ButtonDanger" onClick={() => onDeleteSymbol(form)} disabled={!form?.symbol}>删除</button>
            <span className="Spacer" />
            <button className="Button" onClick={onCancelSymbolEdit}>取消</button>
            <button className="Button ButtonPrimary" onClick={onSaveSymbol}>保存</button>
          </>
        )}
      >
        <div className="StrategyGrid DialogGrid">
          <Field label="Symbol"><input className="Control" value={form.symbol ?? ''} onChange={(e) => updateSymbolField('symbol', e.target.value.toUpperCase())} /></Field>
          <Field label="Broker"><input className="Control" value={form.broker ?? ''} onChange={(e) => updateSymbolField('broker', e.target.value)} /></Field>
          <Field label="Accounts"><input className="Control" value={form.accounts ?? ''} onChange={(e) => updateSymbolField('accounts', e.target.value)} placeholder="lx,sy" /></Field>
          <Field label="Limit expirations"><input className="Control" type="number" value={form.limit_expirations ?? ''} onChange={(e) => updateSymbolField('limit_expirations', e.target.value)} /></Field>
          <Field label="Sell Put enabled"><select className="Control" value={form.sell_put_enabled ?? 'true'} onChange={(e) => updateSymbolField('sell_put_enabled', e.target.value)}><option value="true">开启</option><option value="false">关闭</option></select></Field>
          <Field label="Covered Call enabled"><select className="Control" value={form.sell_call_enabled ?? 'false'} onChange={(e) => updateSymbolField('sell_call_enabled', e.target.value)}><option value="true">开启</option><option value="false">关闭</option></select></Field>
          <Field label="Put min DTE"><input className="Control" type="number" value={form.sell_put_min_dte ?? ''} onChange={(e) => updateSymbolField('sell_put_min_dte', e.target.value)} /></Field>
          <Field label="Put max DTE"><input className="Control" type="number" value={form.sell_put_max_dte ?? ''} onChange={(e) => updateSymbolField('sell_put_max_dte', e.target.value)} /></Field>
          <Field label="Put min strike"><input className="Control" type="number" step="any" value={form.sell_put_min_strike ?? ''} onChange={(e) => updateSymbolField('sell_put_min_strike', e.target.value)} /></Field>
          <Field label="Put max strike"><input className="Control" type="number" step="any" value={form.sell_put_max_strike ?? ''} onChange={(e) => updateSymbolField('sell_put_max_strike', e.target.value)} /></Field>
          <Field label="Call min DTE"><input className="Control" type="number" value={form.sell_call_min_dte ?? ''} onChange={(e) => updateSymbolField('sell_call_min_dte', e.target.value)} /></Field>
          <Field label="Call max DTE"><input className="Control" type="number" value={form.sell_call_max_dte ?? ''} onChange={(e) => updateSymbolField('sell_call_max_dte', e.target.value)} /></Field>
          <Field label="Call min strike"><input className="Control" type="number" step="any" value={form.sell_call_min_strike ?? ''} onChange={(e) => updateSymbolField('sell_call_min_strike', e.target.value)} /></Field>
          <Field label="Call max strike"><input className="Control" type="number" step="any" value={form.sell_call_max_strike ?? ''} onChange={(e) => updateSymbolField('sell_call_max_strike', e.target.value)} /></Field>
        </div>
      </Dialog>
    </div>
  );
}

export function AdvancedPanel({ history, toolResult, repairHint, runningTool, onRun, onRefreshHistory }) {
  const tools = [
    ['healthcheck', '健康检查'],
    ['scan_opportunities', '扫描机会'],
    ['get_close_advice', '平仓建议'],
  ];

  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">高级设置</div>
          <h2 className="PanelTitle">工具运行与历史</h2>
          <p className="PanelText">用于手动触发 agent tool，并检查最近运行历史和修复提示。</p>
        </div>
      </div>

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">工具运行</div>
            <div className="StrategySub">agent tools</div>
          </div>
          <span className="StrategyPill">RUN</span>
        </div>
        <div className="OpsToolbar">
          {tools.map(([toolName, label]) => (
            <button key={toolName} className="Button" disabled={runningTool === toolName} onClick={() => onRun(toolName)}>
              {runningTool === toolName ? `运行中: ${label}` : label}
            </button>
          ))}
          <button className="Button" onClick={onRefreshHistory}>刷新历史</button>
        </div>
        {!!toolResult && (
          <div className="PreviewPanel">
            <div className="SectionTitle">最近工具结果</div>
            <pre className="JsonPreview">{JSON.stringify(toolResult, null, 2)}</pre>
          </div>
        )}
        {!!repairHint && (
          <div className="PreviewPanel">
            <div className="SectionTitle">修复提示</div>
            <pre className="JsonPreview">{JSON.stringify(repairHint, null, 2)}</pre>
          </div>
        )}
      </section>

      <section className="StrategyCard">
        <div className="StrategyHeader">
          <div>
            <div className="StrategyTitle">历史记录</div>
            <div className="StrategySub">history snapshot</div>
          </div>
          <span className="StrategyPill">LOG</span>
        </div>
        <div className="PreviewPanel">
          <pre className="JsonPreview">{JSON.stringify(history, null, 2)}</pre>
        </div>
      </section>
    </div>
  );
}
