import { useEffect, useState } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { PriceChart } from "../components/Charts";
import { Badge, PageHeader } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import { getAudit, getBars, getTrades } from "../lib/api";
import { fmtMoney, fmtNum, fmtTime } from "../lib/format";
import type { AuditResponse, BarsResponse, Fill } from "../lib/types";

// A stable key for a fill. exec_id is unique when present; otherwise fall back to
// the order id and timestamp so rows stay addressable for selection.
function fillKey(f: Fill): string {
  return f.exec_id ?? `${f.order_id ?? "na"}-${f.ts_utc}-${f.symbol}`;
}

export function Trades() {
  const seq = useLiveStore((s) => s.eventSeq);
  const trades = useResource(getTrades, { intervalMs: 6000, deps: [seq] }).data;
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const fills = trades?.fills ?? [];
  const connected = trades?.connected ?? false;
  const selected = fills.find((f) => fillKey(f) === selectedKey) ?? null;

  return (
    <div className="grid">
      <PageHeader eyebrow="Execution" title="Trades" />

      <div className="grid cols-2">
        <Card title="Blotter">
          {connected && fills.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Qty</th>
                  <th className="num">Price</th>
                  <th className="num">Commission</th>
                </tr>
              </thead>
              <tbody>
                {fills.map((f) => {
                  const key = fillKey(f);
                  const buy = f.side.toUpperCase() === "BUY";
                  return (
                    <tr
                      key={key}
                      className={`clickable ${key === selectedKey ? "selected" : ""}`}
                      onClick={() => setSelectedKey(key)}
                    >
                      <td className="mono">{fmtTime(f.ts_utc)}</td>
                      <td>{f.symbol}</td>
                      <td>
                        <Badge kind={buy ? "long" : "short"}>{buy ? "BUY" : "SELL"}</Badge>
                      </td>
                      <td className="num">{fmtNum(f.quantity)}</td>
                      <td className="num">{fmtMoney(f.price)}</td>
                      <td className="num">
                        {f.commission != null ? fmtMoney(f.commission) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <EmptyState title="No fills yet">
              {trades?.note ??
                "Fills appear here once the orchestrator trades on the connected paper broker."}
            </EmptyState>
          )}
        </Card>

        {selected ? (
          <TradeDetail key={fillKey(selected)} fill={selected} />
        ) : (
          <Card title="Trade detail">
            <EmptyState title="Select a fill">
              Pick a row in the blotter to inspect its execution, price action, and the related
              audit trail.
            </EmptyState>
          </Card>
        )}
      </div>
    </div>
  );
}

function TradeDetail({ fill }: { fill: Fill }) {
  const [bars, setBars] = useState<BarsResponse | null>(null);
  const [barsLoading, setBarsLoading] = useState(true);
  const [audit, setAudit] = useState<AuditResponse | null>(null);
  const [auditLoading, setAuditLoading] = useState(true);

  useEffect(() => {
    let active = true;
    setBarsLoading(true);
    getBars(fill.symbol)
      .then((res) => {
        if (active) setBars(res);
      })
      .catch(() => {
        if (active) setBars(null);
      })
      .finally(() => {
        if (active) setBarsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [fill.symbol]);

  useEffect(() => {
    let active = true;
    setAuditLoading(true);
    getAudit({ contains: fill.symbol, limit: 25 })
      .then((res) => {
        if (active) setAudit(res);
      })
      .catch(() => {
        if (active) setAudit(null);
      })
      .finally(() => {
        if (active) setAuditLoading(false);
      });
    return () => {
      active = false;
    };
  }, [fill.symbol]);

  const buy = fill.side.toUpperCase() === "BUY";
  const events = audit?.events ?? [];

  return (
    <Card
      title="Trade detail"
      aside={<Badge kind={buy ? "long" : "short"}>{buy ? "BUY" : "SELL"}</Badge>}
    >
      <dl className="kv">
        <dt>Symbol</dt>
        <dd>{fill.symbol}</dd>
        <dt>Side</dt>
        <dd>{fill.side.toUpperCase()}</dd>
        <dt>Quantity</dt>
        <dd>{fmtNum(fill.quantity)}</dd>
        <dt>Price</dt>
        <dd>{fmtMoney(fill.price)}</dd>
        <dt>Exec ID</dt>
        <dd>{fill.exec_id ?? "—"}</dd>
        <dt>Order ID</dt>
        <dd>{fill.order_id != null ? String(fill.order_id) : "—"}</dd>
        <dt>Commission</dt>
        <dd>{fill.commission != null ? fmtMoney(fill.commission) : "—"}</dd>
        <dt>Time</dt>
        <dd>{fmtTime(fill.ts_utc)}</dd>
      </dl>

      <div className="subpanel">
        <div className="eyebrow" style={{ marginBottom: 12 }}>
          Price action · {fill.symbol}
        </div>
        {barsLoading ? (
          <EmptyState title="Loading bars…">
            Fetching recent daily candles for {fill.symbol}.
          </EmptyState>
        ) : bars?.available && bars.bars.length > 0 ? (
          <PriceChart bars={bars.bars} />
        ) : (
          <EmptyState title="No price data">
            {bars?.note ?? `No bars are available for ${fill.symbol} right now.`}
          </EmptyState>
        )}
      </div>

      <div className="subpanel">
        <div className="eyebrow" style={{ marginBottom: 12 }}>
          Related audit &amp; reflections
        </div>
        {auditLoading ? (
          <div className="muted" style={{ fontSize: 12.5 }}>
            Loading audit entries…
          </div>
        ) : events.length > 0 ? (
          <div className="feed">
            {events.map((e) => (
              <div key={e.id} className="feed-row">
                <span className="feed-time">{fmtTime(e.ts_utc)}</span>
                <div className="stack" style={{ gap: 5 }}>
                  <Badge>{e.event_type}</Badge>
                  <span className="feed-reason">{e.reason}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 12.5 }}>
            No related audit entries for {fill.symbol} yet.
          </div>
        )}
      </div>
    </Card>
  );
}
