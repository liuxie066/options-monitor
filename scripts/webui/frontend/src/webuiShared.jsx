import React from 'react';
import { createPortal } from 'react-dom';

export function Field({ label, children }) {
  return <div className="FormField"><div className="FieldLabel">{label}</div>{children}</div>;
}

export function InlineNote({ children }) {
  return <div className="MutedText" style={{ padding: 0 }}>{children}</div>;
}

export function SaveBar({ title, desc, label, onSave }) {
  return (
    <div className="GlobalSaveBar">
      <div><strong>{title}</strong><span>{desc}</span></div>
      <button className="Button ButtonPrimary" onClick={onSave}>{label}</button>
    </div>
  );
}

export function Dialog({ open, title, subtitle, children, actions, onClose, width = 980 }) {
  if (!open) return null;
  return createPortal(
    <div className="ModalOverlay" onClick={onClose}>
      <div className="Modal" style={{ width: `min(${width}px, calc(100vw - 24px))` }} onClick={(e) => e.stopPropagation()}>
        <div className="DialogHeader">
          <div>
            <div className="PanelTitle DialogTitle">{title}</div>
            {subtitle ? <div className="StrategySub">{subtitle}</div> : null}
          </div>
        </div>
        <div className="DialogBody">{children}</div>
        <div className="DialogActions">{actions}</div>
      </div>
    </div>,
    document.body,
  );
}

export function TokenDialog({ open, action, value, setValue, error, setOpen, onConfirm, tokenInputRef }) {
  if (!open) return null;
  return createPortal(
    <div className="TokenOverlay" onClick={() => setOpen(false)}>
      <div className="TokenModal" onClick={(e) => e.stopPropagation()}>
        <div className="TokenModalInner">
          <div className="ConfirmTitle">请输入 Token</div>
          <div className="ConfirmBody">{action} 需要 Token</div>
          <input ref={tokenInputRef} className="Control TokenControl" type="password" value={value} onChange={(e) => setValue(e.target.value)} placeholder="Token" autoFocus />
          {error && <div style={{ color: 'var(--danger)', marginTop: 8 }}>{error}</div>}
          <div className="ConfirmActions TokenActions">
            <button className="Button" onClick={() => setOpen(false)}>取消</button>
            <button className="Button ButtonPrimary" onClick={onConfirm}>确定</button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function formatBool(v) {
  return v ? '已开启' : '已关闭';
}

export function formatAccounts(accounts) {
  return Array.isArray(accounts) && accounts.length ? accounts.join(', ') : '全部账户';
}
