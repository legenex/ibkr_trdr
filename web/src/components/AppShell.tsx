import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

const NAV = [
  { to: "/", label: "Command", end: true, ready: true },
  { to: "/approvals", label: "Approvals", ready: false },
  { to: "/positions", label: "Positions", ready: false },
  { to: "/backtests", label: "Backtests", ready: false },
  { to: "/audit", label: "Audit", ready: false },
  { to: "/skills", label: "Skills", ready: false },
  { to: "/learning", label: "Learning", ready: false },
  { to: "/holdout", label: "Holdout", ready: false },
];

export function AppShell({ header, children }: { header: ReactNode; children: ReactNode }) {
  return (
    <div className="app">
      <nav className="nav" aria-label="Primary">
        <div className="brand">
          <div className="brand-mark" aria-hidden />
          <div>
            <div className="brand-name">Flight Deck</div>
            <div className="brand-sub">Trading Harness</div>
          </div>
        </div>
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
          >
            <span className="nav-dot" />
            {item.label}
            {!item.ready && <span className="nav-soon">soon</span>}
          </NavLink>
        ))}
      </nav>
      <div className="main">
        {header}
        <div className="content">{children}</div>
      </div>
    </div>
  );
}
