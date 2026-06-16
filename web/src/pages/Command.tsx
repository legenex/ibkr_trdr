import { Card, EmptyState } from "../components/Primitives";
import { InstrumentCluster } from "../components/InstrumentCluster";
import { ActivityFeed } from "../components/ActivityFeed";
import { AnimatedNumber } from "../components/AnimatedNumber";
import { EquityChart } from "../components/Charts";
import { Badge, PageHeader } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import { getEquityCurve, getLearning } from "../lib/api";
import { fmtMoney, fmtNum, fmtPct, fmtSignedMoney } from "../lib/format";
import type { EquityCurve, LearningResponse } from "../lib/types";

export function Command() {
  const snap = useLiveStore((s) => s.snapshot);
  const seq = useLiveStore((s) => s.eventSeq);
  const equity = useResource<EquityCurve>(getEquityCurve, { intervalMs: 6000, deps: [seq] }).data;
  const learning = useResource<LearningResponse>(getLearning, { intervalMs: 15000, deps: [seq] }).data;

  if (!snap) {
    return (
      <div className="grid">
        <PageHeader eyebrow="Mission Control" title="Command" />
        <Card>
          <EmptyState title="Connecting to the deck…">Reading the latest snapshot.</EmptyState>
        </Card>
      </div>
    );
  }

  const sessionPnl =
    equity?.available && equity.first != null && equity.last != null ? equity.last - equity.first : null;

  return (
    <div className="grid">
      <PageHeader eyebrow="Mission Control" title="Command" />

      <InstrumentCluster snap={snap} />

      <div className="grid cols-4">
        <Card title="Net Liquidation">
          <div className="stat-value">
            {snap.portfolio.connected ? (
              <AnimatedNumber value={snap.portfolio.net_liquidation ?? 0} format={fmtMoney} />
            ) : (
              <span className="mono muted">offline</span>
            )}
          </div>
          <div className="stat-sub">{snap.portfolio.connected ? "paper account" : snap.portfolio.note}</div>
        </Card>
        <Card title="Session P&L">
          <div className="stat-value">
            {sessionPnl != null ? (
              <AnimatedNumber value={sessionPnl} format={fmtSignedMoney} polarity="profit" />
            ) : (
              <span className="mono muted">—</span>
            )}
          </div>
          <div className="stat-sub">since the API came up</div>
        </Card>
        <Card title="Approval Queue">
          <div className="stat-value">
            <AnimatedNumber value={snap.queue_pending} format={(n) => fmtNum(n)} />
            <span className="stat-unit">pending</span>
          </div>
          <div className="stat-sub">awaiting human approval</div>
        </Card>
        <Card title="Holdout Budget">
          <div className="stat-value">
            <AnimatedNumber value={snap.holdout_remaining} format={(n) => fmtNum(n)} />
            <span className="stat-unit">evals</span>
          </div>
          <div className="stat-sub">
            {snap.holdout_remaining > 0 ? "unseen data remaining" : "exhausted, promotions paused"}
          </div>
        </Card>
      </div>

      <div className="grid cols-2">
        <Card
          title="Session Equity"
          aside={
            equity?.available ? (
              <span className="mono" style={{ fontSize: 12, color: "var(--text-mid)" }}>
                max dd {fmtPct(equity.max_drawdown_pct)}
              </span>
            ) : undefined
          }
        >
          {equity?.available && equity.points.length > 1 ? (
            <>
              <EquityChart points={equity.points} />
              <div className="chart-foot">
                <span>
                  peak <b>{fmtMoney(equity.peak)}</b>
                </span>
                <span>
                  last <b>{fmtMoney(equity.last)}</b>
                </span>
                <span>
                  max dd <b>{fmtPct(equity.max_drawdown_pct)}</b>
                </span>
              </div>
            </>
          ) : (
            <EmptyState title="Equity curve is building">
              The curve plots real net-liquidation samples the API has seen this session. It fills in
              once the broker is connected and a few polls have landed.
            </EmptyState>
          )}
        </Card>

        <Card title="Activity">
          <ActivityFeed items={snap.activity} />
        </Card>
      </div>

      <div className="grid cols-2">
        <Card title="Circuit Breakers">
          <div className="stack">
            <BreakerRow name="Daily drawdown" meter={snap.risk.daily_drawdown} />
            <BreakerRow name="Weekly drawdown" meter={snap.risk.weekly_drawdown} />
            <BreakerRow name="Gross exposure" meter={snap.risk.gross_exposure} />
            <div className="spread">
              <span className="meter-name">Kill switch</span>
              <Badge kind={snap.kill_switch.engaged ? "short" : "ok"}>
                {snap.kill_switch.engaged ? "engaged" : "armed"}
              </Badge>
            </div>
          </div>
        </Card>

        <Card title="Learning Status" aside={<Badge kind="gold">self-learning loop</Badge>}>
          {learning ? (
            <div className="stack">
              <div className="spread">
                <span className="meter-name">Skills by status</span>
                <span className="row" style={{ gap: 6 }}>
                  <Badge kind="promoted">{learning.skills_by_status.promoted ?? 0} promoted</Badge>
                  <Badge kind="shadow">{learning.skills_by_status.shadow ?? 0} shadow</Badge>
                  <Badge kind="demoted">{learning.skills_by_status.demoted ?? 0} demoted</Badge>
                </span>
              </div>
              <div className="spread">
                <span className="meter-name">Experiments recorded</span>
                <span className="mono">{fmtNum(learning.experiments.length)}</span>
              </div>
              <div className="spread">
                <span className="meter-name">Holdout budget</span>
                <span className="mono">
                  {fmtNum(learning.holdout.total_remaining)} evals · {learning.holdout.tranches.length} tranches
                </span>
              </div>
              <div className="meter-track">
                <div
                  className="meter-fill lvl-caution-bg"
                  style={{ width: `${Math.min(100, learning.holdout.total_remaining * 12)}%` }}
                />
              </div>
              {learning.history[0] && (
                <div className="stat-sub">last: {learning.history[0].reason}</div>
              )}
            </div>
          ) : (
            <EmptyState title="No learning runs yet">
              The loop reflects, proposes skills, and tests them. Its history and holdout meter land
              here once it has run.
            </EmptyState>
          )}
        </Card>
      </div>
    </div>
  );
}

function BreakerRow({ name, meter }: { name: string; meter: { used_pct: number; limit_pct: number; level: string } }) {
  return (
    <div className="spread">
      <span className="meter-name">{name}</span>
      <span className="row" style={{ gap: 8 }}>
        <span className="mono" style={{ fontSize: 12.5 }}>
          {fmtPct(meter.used_pct)} / {fmtPct(meter.limit_pct, 0)}
        </span>
        <Badge kind={meter.level === "ok" ? "ok" : meter.level === "caution" ? "caution" : "short"}>
          {meter.level === "ok" ? "armed" : meter.level}
        </Badge>
      </span>
    </div>
  );
}
