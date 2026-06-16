import type { ReactNode } from "react";

export function Eyebrow({ children }: { children: ReactNode }) {
  return <div className="eyebrow">{children}</div>;
}

export function Card({
  title,
  aside,
  children,
  className = "",
}: {
  title?: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || aside) && (
        <div className="card-head">
          {title ? <Eyebrow>{title}</Eyebrow> : <span />}
          {aside}
        </div>
      )}
      {children}
    </section>
  );
}

export function EmptyState({
  title,
  children,
  command,
}: {
  title: string;
  children: ReactNode;
  command?: string;
}) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      <div className="muted">{children}</div>
      {command && <code className="empty-cmd">{command}</code>}
    </div>
  );
}
