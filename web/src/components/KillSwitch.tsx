import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";

// A kill-switch state change pulses once. Engaged is short/red; released is calm.
// Click toggles via the API; the parent refreshes from the next poll.
export function KillSwitch({
  engaged,
  onToggle,
  busy,
}: {
  engaged: boolean;
  onToggle: (next: boolean) => void;
  busy?: boolean;
}) {
  const reduce = useReducedMotion();
  const controls = usePulseOnChange(engaged, reduce);

  return (
    <motion.button
      type="button"
      className={`kill ${engaged ? "engaged" : ""}`}
      onClick={() => onToggle(!engaged)}
      disabled={busy}
      animate={controls}
      aria-pressed={engaged}
      title={engaged ? "Release the kill switch" : "Engage the kill switch (halts new orders)"}
    >
      <span className="kdot" />
      {engaged ? "KILL ENGAGED" : "ARMED · OK"}
    </motion.button>
  );
}

function usePulseOnChange(value: boolean, reduce: boolean | null) {
  const [scale, setScale] = useState(1);
  const prev = useRef(value);
  useEffect(() => {
    if (value === prev.current) return;
    prev.current = value;
    if (reduce) return;
    setScale(1.05);
    const t = window.setTimeout(() => setScale(1), 200);
    return () => window.clearTimeout(t);
  }, [value, reduce]);
  return { scale };
}
