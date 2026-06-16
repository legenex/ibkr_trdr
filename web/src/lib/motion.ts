import type { Variants, Transition } from "framer-motion";

// Route changes: a fast 150ms cross-fade with an 8px rise, nothing more.
export const pageVariants: Variants = {
  initial: { opacity: 0, y: 8 },
  enter: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: 0 },
};

export const pageTransition: Transition = { duration: 0.15, ease: "easeOut" };

// A single pulse for a real state change (new fill, kill-switch toggle). No
// looping ambient glow anywhere.
export const pulseOnce = {
  scale: [1, 1.04, 1],
  transition: { duration: 0.35, ease: "easeOut" },
};
