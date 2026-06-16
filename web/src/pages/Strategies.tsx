import { useState } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { Badge, PageHeader, Toggle } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import { getStrategies, setStrategyEnabled } from "../lib/api";
import { fmtNum } from "../lib/format";
import type { StrategyRow } from "../lib/types";

// One-line descriptions for the validated strategy templates an LLM proposal
// must map onto. Unknown templates fall through to showing just the name.
const TEMPLATE_DESCRIPTIONS: Record<string, string> = {
  mean_reversion: "Bollinger-style mean reversion",
  trend_breakout: "Donchian breakout",
};

function fmtSharpe(n: number | undefined): string {
  if (n === undefined || n === null || Number.isNaN(n)) return "—";
  return n.toFixed(2);
}

export function Strategies() {
  const eventSeq = useLiveStore((s) => s.eventSeq);
  const { data, refresh } = useResource(getStrategies, { intervalMs: 8000, deps: [eventSeq] });
  const [actionError, setActionError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const strategies = data?.strategies ?? [];
  const templates = data?.templates ?? [];

  const toggle = async (row: StrategyRow, next: boolean): Promise<void> => {
    setActionError(null);
    try {
      await setStrategyEnabled(row.proposal_id, next);
      refresh();
    } catch (err) {
      setActionError((err as Error).message);
    }
  };

  return (
    <div className="grid">
      <PageHeader eyebrow="Approved" title="Strategies" />

      {actionError && <div className="banner-warn">{actionError}</div>}

      <Card title="Deployed strategies">
        {strategies.length === 0 ? (
          <EmptyState title="No deployed strategies yet">
            Strategies appear here once a proposal is approved on the Research &amp; Approvals page.
          </EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Template</th>
                <th>Mode</th>
                <th className="num">Deflated Sharpe</th>
                <th className="num">Trades</th>
                <th>Gate</th>
                <th>Enabled</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((row) => {
                const isOpen = expanded === row.proposal_id;
                const metrics = row.performance.metrics ?? {};
                const metricKeys = Object.keys(metrics);
                return (
                  <StrategyRows
                    key={row.proposal_id}
                    row={row}
                    isOpen={isOpen}
                    metricKeys={metricKeys}
                    metrics={metrics}
                    onToggleRow={() => setExpanded(isOpen ? null : row.proposal_id)}
                    onToggleEnabled={(next) => toggle(row, next)}
                  />
                );
              })}
            </tbody>
          </table>
        )}

        <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>
          Approved strategies execute on PAPER only. Disabling stops the orchestrator from acting on
          a strategy; it does not cancel resting orders.
        </p>
      </Card>

      <Card title="Strategy templates">
        {templates.length === 0 ? (
          <EmptyState title="No templates registered">
            Validated strategy templates appear here as the registry loads.
          </EmptyState>
        ) : (
          <div className="stack" style={{ gap: 10 }}>
            {templates.map((tpl) => (
              <div key={tpl} className="row" style={{ gap: 10 }}>
                <Badge>{tpl}</Badge>
                <span className="muted" style={{ fontSize: 12.5 }}>
                  {TEMPLATE_DESCRIPTIONS[tpl] ?? "Validated template"}
                </span>
              </div>
            ))}
          </div>
        )}
        <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>
          An LLM proposal must map onto one of these validated templates as parameters. No free-form
          executable code is ever accepted.
        </p>
      </Card>
    </div>
  );
}

function StrategyRows({
  row,
  isOpen,
  metricKeys,
  metrics,
  onToggleRow,
  onToggleEnabled,
}: {
  row: StrategyRow;
  isOpen: boolean;
  metricKeys: string[];
  metrics: Record<string, number>;
  onToggleRow: () => void;
  onToggleEnabled: (next: boolean) => Promise<void>;
}) {
  const passed = row.performance.passed;
  const gateKind = passed === true ? "pass" : passed === false ? "fail" : undefined;
  const gateLabel = passed === true ? "pass" : passed === false ? "fail" : "—";

  return (
    <>
      <tr className={`clickable ${isOpen ? "selected" : ""}`}>
        <td onClick={onToggleRow}>{row.name}</td>
        <td onClick={onToggleRow}>{row.template}</td>
        <td onClick={onToggleRow}>
          <Badge kind={row.mode === "LIVE" ? "short" : "ok"}>{row.mode}</Badge>
        </td>
        <td className="num" onClick={onToggleRow}>
          {fmtSharpe(row.performance.deflated_sharpe)}
        </td>
        <td className="num" onClick={onToggleRow}>
          {row.performance.n_trades !== undefined ? fmtNum(row.performance.n_trades) : "—"}
        </td>
        <td onClick={onToggleRow}>
          <Badge kind={gateKind}>{gateLabel}</Badge>
        </td>
        <td>
          <Toggle
            on={row.enabled}
            onChange={async (next) => {
              await onToggleEnabled(next);
            }}
            label={row.enabled ? "on" : "off"}
          />
        </td>
      </tr>
      {isOpen && (
        <tr>
          <td colSpan={7}>
            <div className="subpanel" style={{ paddingTop: 12 }}>
              {metricKeys.length === 0 ? (
                <span className="muted" style={{ fontSize: 12.5 }}>
                  No metrics recorded for this strategy.
                </span>
              ) : (
                <dl className="kv">
                  {metricKeys.map((key) => (
                    <div key={key} style={{ display: "contents" }}>
                      <dt>{key}</dt>
                      <dd>{fmtNum(metrics[key], 2)}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
