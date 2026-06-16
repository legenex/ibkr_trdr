import { useEffect, useRef, useState } from "react";
import { animate, useReducedMotion } from "framer-motion";

type Polarity = "profit" | "risk" | "neutral";

// A number that, when it changes, flashes its semantic color once and counts to
// the new value over ~400ms. Tabular monospace so it never jitters width.
// polarity decides what a rise means: profit -> up is green, risk -> up is red.
export function AnimatedNumber({
  value,
  format,
  polarity = "neutral",
  className = "",
}: {
  value: number;
  format: (n: number) => string;
  polarity?: Polarity;
  className?: string;
}) {
  const reduce = useReducedMotion();
  const [display, setDisplay] = useState(value);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  const prev = useRef(value);

  useEffect(() => {
    if (value === prev.current) return;
    const direction = value > prev.current ? "up" : "down";
    setFlash(direction);
    const flashTimer = window.setTimeout(() => setFlash(null), 450);

    if (reduce) {
      setDisplay(value);
      prev.current = value;
      return () => window.clearTimeout(flashTimer);
    }
    const controls = animate(prev.current, value, {
      duration: 0.4,
      ease: "easeOut",
      onUpdate: (v) => setDisplay(v),
    });
    prev.current = value;
    return () => {
      controls.stop();
      window.clearTimeout(flashTimer);
    };
  }, [value, reduce]);

  let color: string | undefined;
  if (flash && polarity !== "neutral") {
    const good = polarity === "profit" ? flash === "up" : flash === "down";
    color = good ? "var(--long)" : "var(--short)";
  }

  return (
    <span
      className={`mono ${className}`}
      style={{ color, transition: "color 240ms ease" }}
    >
      {format(display)}
    </span>
  );
}
