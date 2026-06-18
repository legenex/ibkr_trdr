import type { CommandSnapshot } from "../lib/types";
import { Eyebrow } from "./Primitives";
import { RegimeDial } from "./RegimeDial";
import { Meter } from "./Meter";
import { AnimatedNumber } from "./AnimatedNumber";
import { fmtMoney, fmtPct } from "../lib/format";

// The one bold place: the flight-deck instrument cluster. Regime dial at the
// center, the risk meters and net liquidation beside it, the kill / breaker
// state below.
export function InstrumentCluster({ snap }: { snap: CommandSnapshot }) {
  const { regime, risk, portfolio, circuit_breaker, kill_switch } = snap;

  return (
    <div className="cluster" aria-label="Instrument cluster">
      <div className="cluster-left">
        <Eyebrow>Detected Regime</Eyebrow>
        <RegimeDial label={regime.regime} confidence={regime.confidence} available={regime.available} />
        <div className="cluster-regime">{regime.available ? regime.regime : "Unavailable"}</div>
        <div className="cluster-conf">proxy {regime.proxy}</div>
      </div>

      <div className="cluster-right">
        <div>
          <Eyebrow>Net Liquidation</Eyebrow>
          <div className="stat-value">
            {portfolio.connected ? (
              <AnimatedNumber value={portfolio.net_liquidation ?? 0} format={fmtMoney} polarity="neutral" />
            ) : (
              <span className="mono muted">offline</span>
            )}
          </div>
          <div className="stat-sub">
            {portfolio.connected ? `${portfolio.open_positions} open positions` : "broker offline"}
          </div>
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
        {risk.session_risk_budget?.enabled && (
          <div>
            <Eyebrow>Session Risk Budget</Eyebrow>
            <div className="stat-value">
              <AnimatedNumber
                value={risk.session_risk_budget.remaining_usd ?? 0}
                format={fmtMoney}
                polarity="neutral"
              />
            </div>
            <div className="stat-sub">
              of {fmtMoney(risk.session_risk_budget.budget_usd)} left today ·{" "}
              {fmtMoney(risk.session_risk_budget.committed_usd)} committed
            </div>
          </div>
        )}
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
