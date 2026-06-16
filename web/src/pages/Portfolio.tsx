import { useEffect, useState } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { Meter } from "../components/Meter";
import { PriceChart } from "../components/Charts";
import { ActionButton, Badge, Modal, PageHeader } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import { flatten, getAccount, getBars } from "../lib/api";
import { fmtMoney, fmtNum, fmtPct } from "../lib/format";
import type { BarsResponse, OrderPlacementResult, Portfolio as PortfolioData, Position } from "../lib/types";

// The IBKR account tags we surface as headline metrics, in display order. Each
// is read from accounts[0].values, which is a Record<string,string> of raw IBKR
// tag strings. Missing tags render as an em dash rather than breaking the grid.
const ACCOUNT_TAGS: { tag: string; label: string }[] = [
  { tag: "NetLiquidation", label: "Net Liquidation" },
  { tag: "BuyingPower", label: "Buying Power" },
  { tag: "GrossPositionValue", label: "Gross Position Value" },
  { tag: "AvailableFunds", label: "Available Funds" },
  { tag: "MaintMarginReq", label: "Maint Margin Req" },
  { tag: "UnrealizedPnL", label: "Unrealized P&L" },
];

// Parse a raw IBKR tag string into a money display. Tolerates absent or
// non-numeric values by returning the em dash the rest of the console uses.
function tagMoney(values: Record<string, string> | undefined, tag: string): string {
  const raw = values?.[tag];
  if (raw === undefined || raw === null || raw === "") return "—";
  const n = parseFloat(raw);
  if (Number.isNaN(n)) return "—";
  return fmtMoney(n);
}

// A position's notional value: prefer the broker's mark, fall back to cost basis.
function positionValue(p: Position): number {
  if (p.market_value != null) return p.market_value;
  return p.quantity * p.avg_cost;
}

export function Portfolio() {
  const snap = useLiveStore((s) => s.snapshot);
  const seq = useLiveStore((s) => s.eventSeq);
  const account = useResource<PortfolioData>(getAccount, { intervalMs: 6000, deps: [seq] });

  // Inline status from the most recent flatten action, surfaced above the table.
  const [flattenStatus, setFlattenStatus] = useState<OrderPlacementResult | null>(null);
  // The position whose chart modal is open, if any.
  const [detail, setDetail] = useState<Position | null>(null);

  const portfolio = account.data;
  const connected = portfolio?.connected ?? false;
  const values = portfolio?.accounts?.[0]?.values;
  const positions = portfolio?.positions ?? [];
  const netLiq = snap?.portfolio.net_liquidation ?? portfolio?.net_liquidation ?? null;
  const singleNameLimit = snap?.risk.single_name_weight_pct ?? null;

  return (
    <div className="grid">
      <PageHeader eyebrow="Account" title="Portfolio" />

      {!connected ? (
        <Card title="Broker offline">
          <EmptyState title="No broker connection">
            The account snapshot reads live from Interactive Brokers. Start paper TWS or IB Gateway
            and connect the API so balances, positions, and exposure populate here.
            {portfolio?.note ? ` ${portfolio.note}` : ""}
          </EmptyState>
        </Card>
      ) : (
        <div className="grid cols-4">
          {ACCOUNT_TAGS.map(({ tag, label }) => (
            <Card key={tag} title={label}>
              <div className="stat-value mono">{tagMoney(values, tag)}</div>
              <div className="stat-sub">IBKR {tag}</div>
            </Card>
          ))}
        </div>
      )}

      <div className="grid cols-2">
        <Card title="Exposure">
          {snap ? (
            <div className="stack">
              <Meter name="Gross exposure" meter={snap.risk.gross_exposure} />
              <Meter name="Daily drawdown" meter={snap.risk.daily_drawdown} />
              <Meter name="Weekly drawdown" meter={snap.risk.weekly_drawdown} />
              <div className="spread">
                <span className="meter-name">Max single-name weight</span>
                <span className="mono">{fmtPct(snap.risk.single_name_weight_pct, 0)}</span>
              </div>
              <div className="spread">
                <span className="meter-name">Max leverage</span>
                <span className="mono">{fmtNum(snap.risk.max_leverage, 2)}x</span>
              </div>
            </div>
          ) : (
            <EmptyState title="Awaiting live risk">
              Exposure meters read from the live command snapshot. They populate once the API stream
              is connected.
            </EmptyState>
          )}
        </Card>

        <Card title="Concentration / clusters">
          {positions.length === 0 || netLiq == null || netLiq <= 0 ? (
            <EmptyState title="Flat book">
              No open positions, so there is nothing to concentrate. Per-name weights appear here
              once the book carries risk.
            </EmptyState>
          ) : (
            <>
              <table className="table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="num">Weight</th>
                    <th className="num">vs limit</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => {
                    const weight = (Math.abs(positionValue(p)) / netLiq) * 100;
                    const limit = singleNameLimit;
                    let kind = "ok";
                    let label = "ok";
                    if (limit != null) {
                      if (weight >= limit) {
                        kind = "breach";
                        label = "breach";
                      } else if (weight >= limit * 0.8) {
                        kind = "caution";
                        label = "caution";
                      }
                    }
                    return (
                      <tr key={p.symbol}>
                        <td className="mono">{p.symbol}</td>
                        <td className="num">{fmtPct(weight)}</td>
                        <td className="num">{limit != null ? fmtPct(limit, 0) : "—"}</td>
                        <td>
                          <Badge kind={kind}>{label}</Badge>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="stat-sub">
                Correlation-cluster detection runs server-side in the risk gate; this table shows
                per-name weight only.
              </div>
            </>
          )}
        </Card>
      </div>

      <Card title="Positions">
        {flattenStatus && (
          <div className="banner-warn" style={{ marginBottom: 14 }}>
            {flattenStatus.accepted
              ? `Flatten accepted for ${flattenStatus.symbol}: ${flattenStatus.reason}`
              : `Flatten vetoed for ${flattenStatus.symbol}: ${flattenStatus.reason}`}
          </div>
        )}
        {positions.length === 0 ? (
          <EmptyState title="Flat book">
            {connected
              ? "The broker reports no open positions."
              : "Connect the broker to read open positions."}
          </EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="num">Qty</th>
                <th className="num">Avg Cost</th>
                <th className="num">Mkt Price</th>
                <th className="num">Mkt Value</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol} className="clickable" onClick={() => setDetail(p)}>
                  <td className="mono">{p.symbol}</td>
                  <td className={`num ${p.quantity >= 0 ? "pos" : "neg"}`}>{fmtNum(p.quantity)}</td>
                  <td className="num">{fmtMoney(p.avg_cost)}</td>
                  <td className="num">{p.market_price != null ? fmtMoney(p.market_price) : "—"}</td>
                  <td className="num">
                    {p.market_value != null ? fmtMoney(p.market_value) : fmtMoney(positionValue(p))}
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <ActionButton
                      variant="btn-danger"
                      onClick={async () => {
                        if (
                          !window.confirm(
                            `Flatten ${p.symbol}? This routes a closing order through the risk gate.`,
                          )
                        )
                          return;
                        const r = await flatten(p.symbol, true);
                        setFlattenStatus(r);
                        account.refresh();
                      }}
                    >
                      Flatten
                    </ActionButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {detail && <PositionModal position={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}

// A per-position detail modal: a daily price chart fetched lazily on open, plus
// the position's key fields. Bars load only when the modal mounts.
function PositionModal({ position, onClose }: { position: Position; onClose: () => void }) {
  const [bars, setBars] = useState<BarsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setBars(null);
    setError(null);
    getBars(position.symbol)
      .then((res) => {
        if (active) setBars(res);
      })
      .catch((err: unknown) => {
        if (active) setError((err as Error).message);
      });
    return () => {
      active = false;
    };
  }, [position.symbol]);

  const value = position.market_value != null ? position.market_value : positionValue(position);

  return (
    <Modal title={position.symbol} onClose={onClose}>
      {error ? (
        <EmptyState title="No price history">{error}</EmptyState>
      ) : bars ? (
        bars.available && bars.bars.length > 1 ? (
          <PriceChart bars={bars.bars} />
        ) : (
          <EmptyState title="No price history">
            {bars.note ?? "The data source returned no bars for this symbol."}
          </EmptyState>
        )
      ) : (
        <EmptyState title="Loading price history…">Fetching daily bars for {position.symbol}.</EmptyState>
      )}

      <div className="subpanel">
        <dl className="kv">
          <dt>Quantity</dt>
          <dd className={position.quantity >= 0 ? "pos" : "neg"}>{fmtNum(position.quantity)}</dd>
          <dt>Avg cost</dt>
          <dd>{fmtMoney(position.avg_cost)}</dd>
          <dt>Market price</dt>
          <dd>{position.market_price != null ? fmtMoney(position.market_price) : "—"}</dd>
          <dt>Market value</dt>
          <dd>{fmtMoney(value)}</dd>
          {position.account && (
            <>
              <dt>Account</dt>
              <dd>{position.account}</dd>
            </>
          )}
        </dl>
      </div>
    </Modal>
  );
}
