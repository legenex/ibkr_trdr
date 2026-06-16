import { useCallback, useEffect, useState } from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { AppShell } from "./components/AppShell";
import { StatusStrip } from "./components/StatusStrip";
import { useLiveConnection } from "./hooks/useLive";
import { useResource } from "./hooks/useResource";
import { useLiveStore } from "./lib/store";
import { getCommand, setKillSwitch } from "./lib/api";
import { pageVariants, pageTransition } from "./lib/motion";

import { Command } from "./pages/Command";
import { Portfolio } from "./pages/Portfolio";
import { Trades } from "./pages/Trades";
import { Research } from "./pages/Research";
import { Strategies } from "./pages/Strategies";
import { Learning } from "./pages/Learning";
import { Settings } from "./pages/Settings";
import { Audit } from "./pages/Audit";
import { Stub } from "./pages/Stub";

export default function App() {
  const location = useLocation();
  const reduce = useReducedMotion();

  // Live channel feeds the store; a slow command poll is the resilient baseline.
  useLiveConnection();
  const setSnapshot = useLiveStore((s) => s.setSnapshot);
  const wsStatus = useLiveStore((s) => s.wsStatus);
  const engaged = useLiveStore((s) => s.snapshot?.kill_switch.engaged ?? false);
  const { data: command, error } = useResource(getCommand, { intervalMs: 5000 });
  useEffect(() => {
    // While the WebSocket is down, the poll keeps the store fresh.
    if (command && wsStatus !== "open") setSnapshot(command);
  }, [command, wsStatus, setSnapshot]);
  useEffect(() => {
    // Seed the store immediately on first load regardless of ws state.
    if (command) setSnapshot(command);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command?.ts_utc]);

  const [killBusy, setKillBusy] = useState(false);
  const onToggleKill = useCallback(
    async (next: boolean) => {
      setKillBusy(true);
      try {
        const res = await setKillSwitch(next);
        useLiveStore.getState().patchSnapshot({ kill_switch: { engaged: res.engaged } });
      } catch {
        /* the next poll reconciles */
      } finally {
        setKillBusy(false);
      }
    },
    [],
  );

  return (
    <AppShell header={<StatusStrip onToggleKill={onToggleKill} killBusy={killBusy} />}>
      {engaged && (
        <div className="banner-halt" style={{ marginBottom: "var(--gutter)" }}>
          <span className="kdot" style={{ background: "var(--short)" }} />
          <span>
            <b>Kill switch engaged.</b> New order submission is halted across the harness. Flatten is
            a separate, confirmed action.
          </span>
        </div>
      )}
      {error && wsStatus !== "open" && (
        <div className="banner-error" style={{ marginBottom: "var(--gutter)" }}>
          <div className="display" style={{ marginBottom: 6 }}>
            Cannot reach the API
          </div>
          <div className="muted" style={{ marginBottom: 8 }}>
            {error}
          </div>
          <code className="empty-cmd">
            cd agentic_trading_bot && uvicorn api.server:app --port 8000
          </code>
        </div>
      )}
      <AnimatePresence mode="wait">
        <motion.div
          key={location.pathname}
          variants={pageVariants}
          initial="initial"
          animate="enter"
          exit="exit"
          transition={reduce ? { duration: 0 } : pageTransition}
        >
          <Routes location={location}>
            <Route path="/" element={<Command />} />
            <Route path="/portfolio" element={<Portfolio />} />
            <Route path="/trades" element={<Trades />} />
            <Route path="/research" element={<Research />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/learning" element={<Learning />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/audit" element={<Audit />} />
            <Route path="*" element={<Stub name="Not Found" />} />
          </Routes>
        </motion.div>
      </AnimatePresence>
    </AppShell>
  );
}
