import { motion, useReducedMotion } from "framer-motion";
import type { ActivityItem } from "../lib/types";
import { fmtTime } from "../lib/format";
import { EmptyState } from "./Primitives";

// A new fill (or any new event at the top) pulses once. No looping glow.
export function ActivityFeed({ items }: { items: ActivityItem[] }) {
  const reduce = useReducedMotion();
  if (items.length === 0) {
    return (
      <EmptyState title="No activity yet">
        Once the orchestrator runs a cycle, every order, fill, veto, and approval lands here.
      </EmptyState>
    );
  }
  const topKey = `${items[0].ts_utc}-${items[0].type}`;
  return (
    <div className="feed">
      {items.map((item, i) => (
        <motion.div
          key={`${item.ts_utc}-${i}`}
          className="feed-row"
          initial={i === 0 && !reduce ? { backgroundColor: "rgba(232,179,65,0.10)" } : false}
          animate={{ backgroundColor: "rgba(0,0,0,0)" }}
          transition={{ duration: 0.6, ease: "easeOut" }}
          data-top={topKey}
        >
          <span className="feed-time">{fmtTime(item.ts_utc)}</span>
          <div>
            <div className="feed-type">{item.type.replace(/_/g, " ")}</div>
            <div className="feed-reason">{item.reason}</div>
          </div>
        </motion.div>
      ))}
    </div>
  );
}
