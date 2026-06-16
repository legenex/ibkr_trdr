import { useMemo, useState } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { Badge, PageHeader } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import { getAudit } from "../lib/api";
import { fmtTime } from "../lib/format";
import type { AuditEvent } from "../lib/types";

// The audit trail view. Every order, fill, veto, approval, rejection, and agent
// decision is written to an append-only log with UTC timestamps and a reason.
// This page is read-only: it filters and inspects, it never mutates.

// Curated event types worth a one-click filter, so the picker is useful even
// before an event of a given kind has landed. The empty string means "all".
const CURATED_TYPES: readonly string[] = [
  "",
  "KILL_SWITCH_ENGAGED",
  "KILL_SWITCH_RELEASED",
  "RISK_VETO",
  "RISK_DECISION",
  "ORDER_SUBMITTED",
  "ORDER_REJECTED",
  "FLATTEN",
  "APPROVAL",
  "APPROVAL_DENIED",
  "REJECTION",
  "STRATEGY_ENABLED",
  "STRATEGY_DISABLED",
  "SKILL_PROMOTED",
  "SKILL_DEMOTED",
  "PROMOTE_DENIED",
  "RISK_LIMIT_CHANGED",
  "PROPOSAL_ENQUEUED",
  "PIPELINE_COMPLETE",
  "RESEARCH_RUN_REQUESTED",
];

const LIMIT_OPTIONS: readonly number[] = [100, 250, 500, 1000];

// Map an event_type to a Badge kind by family. Reducing or negative actions
// paint red (short), positive or enabling actions paint green (ok/promoted),
// pending or limit changes paint gold (caution); everything else stays neutral.
function badgeKind(eventType: string): string {
  const t = eventType.toLowerCase();
  if (
    t.includes("kill_switch") ||
    t.includes("veto") ||
    t.includes("denied") ||
    t.includes("rejected") ||
    t.includes("rejection") ||
    t.includes("disabled") ||
    t.includes("demoted") ||
    t.includes("flatten")
  ) {
    return "short";
  }
  if (t.includes("promoted")) return "promoted";
  if (t.includes("approval") || t.includes("enabled") || t.includes("complete")) return "ok";
  if (t.includes("risk_limit_changed") || t.includes("pending") || t.includes("enqueued")) {
    return "caution";
  }
  return "";
}

export function Audit() {
  const eventSeq = useLiveStore((s) => s.eventSeq);
  const [contains, setContains] = useState("");
  const [eventType, setEventType] = useState("");
  const [limit, setLimit] = useState(200);
  const [expanded, setExpanded] = useState<number | null>(null);

  const { data } = useResource(
    () =>
      getAudit({
        eventType: eventType || undefined,
        contains: contains || undefined,
        limit,
      }),
    { intervalMs: 6000, deps: [eventSeq, eventType, contains, limit] },
  );

  const events: AuditEvent[] = data?.events ?? [];
  const count = data?.count ?? 0;

  // Curated types plus any types present in the current result set, deduped and
  // with the curated order preserved up front.
  const typeOptions = useMemo(() => {
    const seen = new Set(CURATED_TYPES);
    const extra: string[] = [];
    for (const e of events) {
      if (!seen.has(e.event_type)) {
        seen.add(e.event_type);
        extra.push(e.event_type);
      }
    }
    extra.sort();
    return [...CURATED_TYPES, ...extra];
  }, [events]);

  return (
    <div className="grid">
      <PageHeader eyebrow="Compliance" title="Audit Log" />

      <Card>
        <div className="page-actions" style={{ justifyContent: "flex-start" }}>
          <input
            className="input"
            style={{ width: 240 }}
            type="text"
            placeholder="Filter by reason…"
            value={contains}
            onChange={(e) => setContains(e.target.value)}
            aria-label="Filter audit reasons"
          />
          <select
            className="select"
            style={{ width: 220 }}
            value={eventType}
            onChange={(e) => setEventType(e.target.value)}
            aria-label="Filter by event type"
          >
            {typeOptions.map((t) => (
              <option key={t || "__all__"} value={t}>
                {t === "" ? "All event types" : t}
              </option>
            ))}
          </select>
          <select
            className="select"
            style={{ width: 110 }}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            aria-label="Result limit"
          >
            {LIMIT_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <span className="muted mono">{count} events</span>
        </div>
      </Card>

      <Card>
        {events.length === 0 ? (
          <EmptyState title="The audit log is empty">
            Every order, fill, veto, approval, rejection, kill-switch flip, and learning decision is
            written here with a UTC timestamp and a reason. This view fills in as the system acts.
          </EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 96 }}>Time</th>
                <th style={{ width: 220 }}>Type</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => {
                const open = expanded === e.id;
                return (
                  <AuditRow
                    key={e.id}
                    event={e}
                    open={open}
                    onToggle={() => setExpanded(open ? null : e.id)}
                  />
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

function AuditRow({
  event,
  open,
  onToggle,
}: {
  event: AuditEvent;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className={`clickable ${open ? "selected" : ""}`} onClick={onToggle}>
        <td className="mono">{fmtTime(event.ts_utc)}</td>
        <td>
          <Badge kind={badgeKind(event.event_type)}>{event.event_type}</Badge>
        </td>
        <td>{event.reason}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={3}>
            <pre className="json">{JSON.stringify(event.payload, null, 2)}</pre>
          </td>
        </tr>
      )}
    </>
  );
}
