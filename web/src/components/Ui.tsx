import type { ReactNode } from "react";
import { useState } from "react";

// Small shared atoms used across pages. They lean on the CSS design tokens so
// every page looks like one console.

export function Badge({ kind, children }: { kind?: string; children: ReactNode }) {
  return <span className={`badge ${kind ?? ""}`}>{children}</span>;
}

export function PageHeader({
  eyebrow,
  title,
  actions,
}: {
  eyebrow: string;
  title: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1 className="page-title">{title}</h1>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          className={`tab ${value === t.id ? "active" : ""}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

// A toggle that can require a typed/clicked confirmation before flipping. Used
// for enabling a strategy, engaging LIVE, etc. `danger` paints the on-state red.
export function Toggle({
  on,
  onChange,
  danger,
  disabled,
  label,
  confirm,
}: {
  on: boolean;
  onChange: (next: boolean) => void;
  danger?: boolean;
  disabled?: boolean;
  label?: string;
  confirm?: string;
}) {
  const click = () => {
    if (disabled) return;
    if (!on && confirm && !window.confirm(confirm)) return;
    onChange(!on);
  };
  return (
    <button
      type="button"
      className={`toggle ${disabled ? "disabled" : ""}`}
      onClick={click}
      aria-pressed={on}
      aria-label={label}
      style={{ background: "none", border: "none", padding: 0 }}
    >
      <span className={`toggle-track ${on ? "on" : ""} ${danger ? "danger" : ""}`}>
        <span className="toggle-knob" />
      </span>
      {label && <span style={{ fontSize: 12.5, color: "var(--text-mid)" }}>{label}</span>}
    </button>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className="spread" style={{ marginBottom: 14 }}>
          <h2 className="display" style={{ fontSize: 16, margin: 0 }}>
            {title}
          </h2>
          <button className="btn btn-sm" onClick={onClose}>
            Close
          </button>
        </div>
        {children}
        {footer && (
          <div className="row" style={{ marginTop: 16, justifyContent: "flex-end" }}>
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

// A button that runs an async action, showing a busy state and surfacing the
// error message inline if it throws. Keeps every page's action buttons honest.
export function ActionButton({
  onClick,
  children,
  variant = "btn",
  disabled,
  title,
}: {
  onClick: () => Promise<void>;
  children: ReactNode;
  variant?: "btn" | "btn-primary" | "btn-danger";
  disabled?: boolean;
  title?: string;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      className={`${variant} btn-sm`}
      disabled={disabled || busy}
      title={title}
      onClick={async () => {
        setBusy(true);
        try {
          await onClick();
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? "..." : children}
    </button>
  );
}
