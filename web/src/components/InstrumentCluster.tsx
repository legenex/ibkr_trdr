import type { CommandSnapshot } from "../lib/types";
import { Eyebrow } from "./Primitives";
import { RegimeDial } from "./RegimeDial";
import { Meter } from "./Meter";
import { AnimatedNumber } from "./AnimatedNumber";
import { fmtSignedMoney, fmtPct } from "../lib/format";

// The one bold place: the flight-deck instrument cluster. Regime dial at the
// center, the risk meters and P&L beside it, the kill / breaker state below.
export function InstrumentCluster({ snap }: { snap: CommandSnapshot }) {
  const { regime, risk, portfolio, circuit_breaker, kill_switch } = snap;
  const pnl = portfolio.pnl_day ?? 0;

  return (
    <div className="cluster" aria-label="Instrument cluster">
      <div className="cluster-left">
        <Eyebrow>Detected Regime</Eyebrow>
        <RegimeDial label={regime.label} confidence={regime.confidence} available={regime.available} />
        <div className="cluster-regime">{regime.available ? regime.label : "Unavailable"}</div>
        <div className="cluster-conf">proxy {regime.proxy}</div>
      </div>

      <div className="cluster-right">
        <div>
          <Eyebrow>Day P&amp;L</Eyebrow>
          <div className="stat-value">
            {portfolio.connected ? (
              <AnimatedNumber value={pnl} format={fmtSignedMoney} polarity="profit" />
            ) : (
              <span className="mono muted">—</span>
            )}
          </div>
          <div className="stat-sub">{portfolio.connected ? `${portfolio.open_positions} open positions` : "broker offline"}</div>
        </div>

        <div>
          <Eyebrow>Risk / Trade</Eyebrow>
          <div className="stat-value">
            <AnimatedNumber value={risk.risk_per_trade_pct} format={(n) => fmtPct(n)} polarity="neutral" />
          </div>
          <div className="stat-sub">of equity, per position</div>
        </div>

        <Meter name="Gross exposure" meter={risk.gross_exposure} />
        <Meter name="Daily drawdown" meter={risk.daily_drawdown} />
        <Meter name="Weekly drawdown" meter={risk.weekly_drawdown} />
        <div>
          <Eyebrow>Circuit Breaker</Eyebrow>
          <div className="row" style={{ marginTop: 8 }}>
            <span
              className="mono"
              style={{
                fontSize: 14,
                color: circuit_breaker.tripped ? "var(--short)" : "var(--long)",
              }}
            >
              {circuit_breaker.tripped ? "TRIPPED" : "CLEAR"}
            </span>
          </div>
          {circuit_breaker.reason && <div className="stat-sub">{circuit_breaker.reason}</div>}
        </div>

        <div className="cluster-meta">
          <span className="eyebrow">State</span>
          <span className="mono" style={{ fontSize: 12, color: "var(--text-mid)" }}>
            kill {kill_switch.engaged ? "engaged" : "armed"}
          </span>
          <span className="muted">·</span>
          <span className="mono" style={{ fontSize: 12, color: "var(--text-mid)" }}>
            {snap.mode.toLowerCase()} mode
          </span>
        </div>
      </div>
    </div>
  );
}
