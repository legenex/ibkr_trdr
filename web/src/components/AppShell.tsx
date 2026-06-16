import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useLiveStore } from "../lib/store";

// The eight console pages, in the order of the design mockup's nav rail.
const NAV = [
  { to: "/", label: "Command", end: true },
  { to: "/portfolio", label: "Portfolio" },
  { to: "/trades", label: "Trades" },
  { to: "/research", label: "Research & Approvals" },
  { to: "/strategies", label: "Strategies" },
  { to: "/learning", label: "Learning" },
  { to: "/settings", label: "Settings" },
  { to: "/audit", label: "Audit" },
];

export function AppShell({ header, children }: { header: ReactNode; children: ReactNode }) {
  const mode = useLiveStore((s) => s.snapshot?.mode ?? "PAPER");
  const engaged = useLiveStore((s) => s.snapshot?.kill_switch.engaged ?? false);

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
          </NavLink>
        ))}
        <div style={{ flex: 1 }} />
        <div className="brand-sub" style={{ padding: "8px 11px" }}>
          {mode.toLowerCase()} · {engaged ? "halted" : "armed"}
        </div>
      </nav>
      <div className="main">
        {header}
        <div className="content">{children}</div>
      </div>
    </div>
  );
}
