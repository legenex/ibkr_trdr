import { motion, useReducedMotion } from "framer-motion";
import type { Meter as MeterData } from "../lib/types";
import { fmtPct } from "../lib/format";

// A linear risk gauge: fill grows toward its limit and recolors by level
// (ok -> long, caution -> caution, breach -> short). Width animates only when
// the value changes. Semantic color only, never gold.
export function Meter({ name, meter }: { name: string; meter: MeterData }) {
  const reduce = useReducedMotion();
  const frac = meter.limit_pct > 0 ? Math.min(meter.used_pct / meter.limit_pct, 1) : 0;
  return (
    <div className="meter">
      <div className="meter-row">
        <span className="meter-name">{name}</span>
        <span className="meter-read">
          <span className={`lvl-${meter.level}`}>{fmtPct(meter.used_pct)}</span>
          <span className="limit"> / {fmtPct(meter.limit_pct, 0)}</span>
        </span>
      </div>
      <div className="meter-track" role="meter" aria-valuenow={meter.used_pct} aria-valuemax={meter.limit_pct}>
        <motion.div
          className={`meter-fill lvl-${meter.level}-bg`}
          initial={false}
          animate={{ width: `${frac * 100}%` }}
          transition={reduce ? { duration: 0 } : { duration: 0.45, ease: "easeOut" }}
        />
      </div>
    </div>
  );
}
