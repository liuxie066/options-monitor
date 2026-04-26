import React from 'react';
import { Field, InlineNote, SaveBar, formatAccounts, formatBool } from './webuiShared.jsx';
import { STRATEGY_FIELDS } from './webuiModel.js';

export function MarketPanel({ globalForm, setGlobalForm, onSave }) {
  return (
    <div className="GlobalPanel">
      <div className="GlobalOverview">
        <div>
          <div className="Eyebrow">行情设置</div>
          <h2 className="PanelTitle">全局行情连接</h2>
          <p className="PanelText">共享 OpenD 参数会双写到新 DTO 与旧的 symbols[].fetch 字段，保证现有运行链路兼容。</p>
        </div>
        <div className="ConfigPath"><span>模式</span><code>{globalForm.marketData.mode}</code></div>
      </div>
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
