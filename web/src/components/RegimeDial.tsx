import { motion, useReducedMotion } from "framer-motion";
import { REGIME_ORDER } from "../lib/types";

const CX = 140;
const CY = 134;
const R = 104;

function polar(angleDeg: number, radius: number) {
  const rad = (angleDeg * Math.PI) / 180;
  return { x: CX + radius * Math.cos(rad), y: CY - radius * Math.sin(rad) };
}

// Regimes sit on a top semicircle, low (Crash) at the left, high (Euphoria) at
// the right. The gold ring and the gold needle are the cluster's identity; no
// semantic color appears here (regime is not money).
export function RegimeDial({
  label,
  confidence,
  available,
}: {
  label: string;
  confidence: number;
  available: boolean;
}) {
  const reduce = useReducedMotion();
  const activeIndex = Math.max(0, REGIME_ORDER.indexOf(label));
  const angleFor = (i: number) => 180 - i * (180 / (REGIME_ORDER.length - 1));
  const needle = polar(angleFor(activeIndex), R * 0.72);

  const left = polar(180, R);
  const right = polar(0, R);
  const arc = `M ${left.x} ${left.y} A ${R} ${R} 0 0 1 ${right.x} ${right.y}`;

  return (
    <svg width="280" height="170" viewBox="0 0 280 170" role="img" aria-label={`Regime ${label}`}>
      {/* base ring */}
      <path d={arc} fill="none" stroke="var(--ink-600)" strokeWidth={10} strokeLinecap="round" />
      {/* gold cluster ring (identity), faint */}
      <path d={arc} fill="none" stroke="var(--gold)" strokeWidth={1.5} opacity={0.5} />

      {/* regime ticks + labels */}
      {REGIME_ORDER.map((name, i) => {
        const p = polar(angleFor(i), R);
        const lp = polar(angleFor(i), R + 19);
        const isActive = i === activeIndex && available;
        return (
          <g key={name}>
            <circle
              cx={p.x}
              cy={p.y}
              r={isActive ? 5 : 3}
              fill={isActive ? "var(--gold)" : "var(--text-dim)"}
            />
            <text
              x={lp.x}
              y={lp.y}
              textAnchor={i === 0 ? "start" : i === REGIME_ORDER.length - 1 ? "end" : "middle"}
              dominantBaseline="middle"
              fontFamily="var(--font-display)"
              fontSize="9.5"
              letterSpacing="0.08em"
              fill={isActive ? "var(--gold)" : "var(--text-dim)"}
              style={{ textTransform: "uppercase" }}
            >
              {name}
            </text>
          </g>
        );
      })}

      {/* needle (animates only when the regime actually changes) */}
      {available && (
        <>
          <motion.line
            x1={CX}
            y1={CY}
            initial={false}
            animate={{ x2: needle.x, y2: needle.y }}
            transition={reduce ? { duration: 0 } : { duration: 0.5, ease: [0.2, 0.8, 0.2, 1] }}
            stroke="var(--gold)"
            strokeWidth={2.5}
            strokeLinecap="round"
          />
          <circle cx={CX} cy={CY} r={6} fill="var(--ink-700)" stroke="var(--gold)" strokeWidth={2} />
        </>
      )}

      {/* confidence readout */}
      <text
        x={CX}
        y={CY - 26}
        textAnchor="middle"
        fontFamily="var(--font-mono)"
        fontSize="13"
        fill="var(--text-mid)"
        style={{ fontVariantNumeric: "tabular-nums" } as React.CSSProperties}
      >
        {available ? `${(confidence * 100).toFixed(0)}%` : "—"}
      </text>
      <text
        x={CX}
        y={CY - 11}
        textAnchor="middle"
        fontFamily="var(--font-display)"
        fontSize="8.5"
        letterSpacing="0.16em"
        fill="var(--text-dim)"
      >
        CONFIDENCE
      </text>
    </svg>
  );
}
