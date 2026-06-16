import { useCallback, useState } from "react";
import { Routes, Route, useLocation } from "react-router-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { AppShell } from "./components/AppShell";
import { KillSwitch } from "./components/KillSwitch";
import { Command } from "./pages/Command";
import { Stub } from "./pages/Stub";
import { usePolling } from "./hooks/usePolling";
import { getCommand, setKillSwitch } from "./lib/api";
import { pageVariants, pageTransition } from "./lib/motion";
import type { CommandSnapshot } from "./lib/types";

export default function App() {
  const location = useLocation();
  const reduce = useReducedMotion();
  const { data, error } = usePolling<CommandSnapshot>(getCommand, 4000);
  const [killBusy, setKillBusy] = useState(false);
  const [killOverride, setKillOverride] = useState<boolean | null>(null);

  const engaged = killOverride ?? data?.kill_switch.engaged ?? false;
  const mode = data?.mode ?? "PAPER";

  const onToggle = useCallback(async (next: boolean) => {
    setKillBusy(true);
    setKillOverride(next); // optimistic; the next poll reconciles
    try {
      const res = await setKillSwitch(next);
      setKillOverride(res.engaged);
    } catch {
      setKillOverride(null);
    } finally {
      setKillBusy(false);
    }
  }, []);

  const header = (
    <header className="header">
      <div className="header-title">
        <span className="eyebrow">Agentic Trading</span>
        <span className="display" style={{ fontSize: 14 }}>Operations</span>
      </div>
      <div className="header-spacer" />
      <span className={`chip ${mode === "LIVE" ? "live" : "paper"}`}>
        <span className="dot" />
        {mode}
      </span>
      <KillSwitch engaged={engaged} onToggle={onToggle} busy={killBusy} />
    </header>
  );

  return (
    <AppShell header={header}>
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
            <Route path="/" element={<Command snap={data} error={error} />} />
            <Route path="/approvals" element={<Stub name="Approvals" />} />
            <Route path="/positions" element={<Stub name="Positions" />} />
            <Route path="/backtests" element={<Stub name="Backtests" />} />
            <Route path="/audit" element={<Stub name="Audit" />} />
            <Route path="/skills" element={<Stub name="Skill Registry" />} />
            <Route path="/learning" element={<Stub name="Learning History" />} />
            <Route path="/holdout" element={<Stub name="Holdout Budget" />} />
            <Route path="*" element={<Stub name="Not Found" />} />
          </Routes>
        </motion.div>
      </AnimatePresence>
    </AppShell>
  );
}
