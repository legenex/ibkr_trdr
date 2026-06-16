import type { CommandSnapshot } from "../lib/types";
import { Card, Eyebrow, EmptyState } from "../components/Primitives";
import { InstrumentCluster } from "../components/InstrumentCluster";
import { ActivityFeed } from "../components/ActivityFeed";
import { AnimatedNumber } from "../components/AnimatedNumber";
import { fmtMoney, fmtNum } from "../lib/format";

export function Command({ snap, error }: { snap: CommandSnapshot | null; error: string | null }) {
  if (!snap) {
    return (
      <div className="grid">
        <PageTitle />
        {error ? <ApiErrorBanner message={error} /> : <Card><EmptyState title="Connecting to the deck…">Reading the latest snapshot.</EmptyState></Card>}
      </div>
    );
  }

  return (
    <div className="grid">
      <PageTitle />
      {error && <ApiErrorBanner message={error} />}

      <InstrumentCluster snap={snap} />

      <div className="grid cols-3">
        <Card title="Net Liquidation">
          <div className="stat-value">
            {snap.portfolio.connected ? (
              <AnimatedNumber value={snap.portfolio.net_liquidation ?? 0} format={fmtMoney} polarity="neutral" />
            ) : (
              <span className="mono muted">offline</span>
            )}
          </div>
          <div className="stat-sub">{snap.portfolio.connected ? "paper account" : snap.portfolio.note}</div>
        </Card>
        <Card title="Approval Queue">
          <div className="stat-value">
            <AnimatedNumber value={snap.queue_pending} format={(n) => fmtNum(n)} polarity="neutral" />
            <span className="stat-unit">pending</span>
          </div>
          <div className="stat-sub">awaiting human approval</div>
        </Card>
        <Card title="Holdout Budget">
          <div className="stat-value">
            <AnimatedNumber value={snap.holdout_remaining} format={(n) => fmtNum(n)} polarity="neutral" />
            <span className="stat-unit">evals</span>
          </div>
          <div className="stat-sub">{snap.holdout_remaining > 0 ? "unseen data remaining" : "exhausted — promotions paused"}</div>
        </Card>
      </div>

      <div className="grid cols-2">
        <Card title="Open Positions">
          {snap.portfolio.connected ? (
            snap.portfolio.positions.length ? (
              <PositionsTable snap={snap} />
            ) : (
              <EmptyState title="Flat book">No open positions right now.</EmptyState>
            )
          ) : (
            <EmptyState
              title="Broker not connected"
              command="streamlit run ui/dashboard.py  →  Positions & Orders → Connect"
            >
              Start paper TWS or IB Gateway with the API port enabled, then connect the broker.
            </EmptyState>
          )}
        </Card>

        <Card title="Activity">
          <ActivityFeed items={snap.activity} />
        </Card>
      </div>
    </div>
  );
}

function PositionsTable({ snap }: { snap: CommandSnapshot }) {
  return (
    <div className="feed">
      {snap.portfolio.positions.map((p) => (
        <div className="feed-row" key={p.symbol} style={{ gridTemplateColumns: "1fr auto auto" }}>
          <span className="mono" style={{ fontSize: 13 }}>{p.symbol}</span>
          <span className="mono" style={{ fontSize: 13, color: p.quantity >= 0 ? "var(--long)" : "var(--short)" }}>
            {fmtNum(p.quantity)}
          </span>
          <span className="mono muted" style={{ fontSize: 13 }}>{fmtMoney(p.avg_cost)}</span>
        </div>
      ))}
    </div>
  );
}

function PageTitle() {
  return (
    <div>
      <Eyebrow>Command</Eyebrow>
      <h1 className="display" style={{ fontSize: 28, margin: "6px 0 0" }}>
        Instrument Cluster
      </h1>
    </div>
  );
}

function ApiErrorBanner({ message }: { message: string }) {
  return (
    <div className="banner-error">
      <div className="display" style={{ marginBottom: 6 }}>Can’t reach the API</div>
      <div className="muted" style={{ marginBottom: 8 }}>{message}</div>
      <code className="empty-cmd">cd agentic_trading_bot &amp;&amp; uvicorn api.server:app --port 8000</code>
    </div>
  );
}
