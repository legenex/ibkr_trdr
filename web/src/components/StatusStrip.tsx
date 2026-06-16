import { useEffect, useState } from "react";
import { useLiveStore } from "../lib/store";
import { KillSwitch } from "./KillSwitch";

// The persistent top strip: mode (PAPER/LIVE), kill-switch state + toggle,
// broker connection, live-channel status, and a ticking ET clock. Reads live
// state from the store so it reflects WebSocket updates instantly.
export function StatusStrip({
  onToggleKill,
  killBusy,
}: {
  onToggleKill: (next: boolean) => void;
  killBusy: boolean;
}) {
  const snap = useLiveStore((s) => s.snapshot);
  const wsStatus = useLiveStore((s) => s.wsStatus);
  const clock = useClock();

  const mode = snap?.mode ?? "PAPER";
  const engaged = snap?.kill_switch.engaged ?? false;
  const connected = snap?.connection?.connected ?? snap?.portfolio.connected ?? false;

  return (
    <header className="header">
      <div className="header-title">
        <span className="eyebrow">Agentic Trading</span>
        <span className="display" style={{ fontSize: 14 }}>
          Flight Deck
        </span>
      </div>
      <div className="header-spacer" />

      <span className={`chip ${mode === "LIVE" ? "live" : "paper"}`}>
        <span className="dot" />
        {mode}
      </span>

      <span className="chip" title={connected ? "Broker connected" : "Broker offline"}>
        <span className="dot" style={{ background: connected ? "var(--long)" : "var(--text-dim)" }} />
        {connected ? "IBKR" : "OFFLINE"}
      </span>

      <span className="chip" title={`Live channel ${wsStatus}`}>
        <span className={`wsdot ${wsStatus === "open" ? "open" : wsStatus === "closed" ? "closed" : ""}`} />
        {wsStatus === "open" ? "LIVE" : wsStatus === "connecting" ? "..." : "POLL"}
      </span>

      <span className="mono" style={{ fontSize: 12.5, color: "var(--text-mid)" }}>
        {clock} ET
      </span>

      <KillSwitch engaged={engaged} onToggle={onToggleKill} busy={killBusy} />
    </header>
  );
}

function useClock(): string {
  const [now, setNow] = useState(() => fmtET());
  useEffect(() => {
    const t = window.setInterval(() => setNow(fmtET()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return now;
}

function fmtET(): string {
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
  } catch {
    return new Date().toISOString().slice(11, 19);
  }
}
