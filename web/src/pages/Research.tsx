import { useState } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { ActionButton, Badge, PageHeader, Tabs } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import {
  ApiError,
  approveProposal,
  getProposals,
  rejectProposal,
  runResearch,
} from "../lib/api";
import { fmtNum, fmtTime } from "../lib/format";
import type { Proposal, ProposalValidation, ValidationResult } from "../lib/types";

type Filter = "pending" | "all";

// The Research and Approvals page. The orchestrator and the discovery pipeline
// only propose; nothing here can execute. The single most important rule, baked
// into the UI: a proposal that did not pass the validation gate can never be
// approved, and the failing reasons are always shown.
export function Research() {
  const researchRunning = useLiveStore((s) => s.researchRunning);
  const eventSeq = useLiveStore((s) => s.eventSeq);

  const [filter, setFilter] = useState<Filter>("pending");
  const [theme, setTheme] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data, refresh } = useResource(() => getProposals(filter), {
    intervalMs: 6000,
    deps: [eventSeq, filter],
  });

  const proposals = data?.proposals ?? [];
  const selected = proposals.find((p) => p.proposal_id === selectedId) ?? proposals[0] ?? null;

  const runTheme = async () => {
    const t = theme.trim();
    if (!t) return;
    await runResearch(t);
  };

  return (
    <div className="grid">
      <PageHeader
        eyebrow="Discovery"
        title="Research & Approvals"
        actions={
          <>
            {researchRunning && <Badge kind="gold">running</Badge>}
            <input
              className="input"
              placeholder="Research theme, e.g. mean reversion in semis"
              value={theme}
              onChange={(e) => setTheme(e.target.value)}
              style={{ width: 280 }}
            />
            <button
              className="btn-primary btn-sm"
              disabled={researchRunning || theme.trim().length === 0}
              onClick={() => {
                void runTheme();
              }}
            >
              Run
            </button>
          </>
        }
      />

      <div className="muted" style={{ fontSize: 12.5 }}>
        The pipeline only proposes. Every proposal still passes the validation gate and human
        approval.
      </div>

      <Tabs<Filter>
        tabs={[
          { id: "pending", label: "Pending" },
          { id: "all", label: "All" },
        ]}
        value={filter}
        onChange={setFilter}
      />

      {proposals.length === 0 ? (
        <Card>
          <EmptyState title="No proposals yet" command="POST /api/research/run">
            Run the pipeline with a theme to generate proposals, or let the orchestrator enqueue
            them on its schedule. Each one arrives with its full validation result attached.
          </EmptyState>
        </Card>
      ) : (
        <div className="grid cols-2">
          <Card title="Proposals">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Template</th>
                  <th>Gate</th>
                  <th>Status</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {proposals.map((p) => (
                  <tr
                    key={p.proposal_id}
                    className={`clickable ${
                      selected?.proposal_id === p.proposal_id ? "selected" : ""
                    }`}
                    onClick={() => setSelectedId(p.proposal_id)}
                  >
                    <td>{p.spec.name}</td>
                    <td className="mono">{p.spec.template}</td>
                    <td>
                      <Badge kind={p.passed ? "pass" : "fail"}>{p.passed ? "PASS" : "FAIL"}</Badge>
                    </td>
                    <td>
                      <Badge kind={p.status}>{p.status}</Badge>
                    </td>
                    <td className="mono">{fmtTime(p.created_ts)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {selected ? (
            <ProposalDetail proposal={selected} refresh={refresh} />
          ) : (
            <Card>
              <EmptyState title="Select a proposal">
                Pick a proposal on the left to review its validation result and decide.
              </EmptyState>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}

function ProposalDetail({ proposal, refresh }: { proposal: Proposal; refresh: () => void }) {
  const [approver, setApprover] = useState("");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [banner, setBanner] = useState<string | null>(null);

  const isPending = proposal.status === "pending";
  const canApprove = proposal.passed && isPending && approver.trim().length > 0;
  const canReject = isPending && approver.trim().length > 0;

  const approveTitle = !proposal.passed
    ? "Cannot approve: the proposal did not pass the validation gate"
    : !isPending
      ? "Already decided"
      : approver.trim().length === 0
        ? "Enter an approver name first"
        : undefined;

  const handleApprove = async () => {
    setBanner(null);
    try {
      await approveProposal(proposal.proposal_id, approver.trim(), note.trim());
      refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setBanner(err.detail ?? err.message);
      } else {
        setBanner((err as Error).message);
      }
    }
  };

  const handleReject = async () => {
    setBanner(null);
    try {
      await rejectProposal(proposal.proposal_id, approver.trim(), reason.trim());
      refresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setBanner(err.detail ?? err.message);
      } else {
        setBanner((err as Error).message);
      }
    }
  };

  return (
    <Card
      title={proposal.spec.name}
      aside={
        <span className="row" style={{ gap: 6 }}>
          <Badge kind={proposal.passed ? "pass" : "fail"}>
            {proposal.passed ? "PASS" : "FAIL"}
          </Badge>
          <Badge kind={proposal.status}>{proposal.status}</Badge>
        </span>
      }
    >
      <div className="stack">
        <div>
          <div className="eyebrow" style={{ marginBottom: 4 }}>
            Hypothesis
          </div>
          <div style={{ fontSize: 13 }}>{proposal.spec.hypothesis}</div>
        </div>

        <div>
          <div className="eyebrow" style={{ marginBottom: 4 }}>
            Rationale
          </div>
          <div className="muted" style={{ fontSize: 12.5 }}>
            {proposal.spec.rationale}
          </div>
        </div>

        <div className="spread">
          <span className="meter-name">Template</span>
          <span className="mono" style={{ fontSize: 12.5 }}>
            {proposal.spec.template}
          </span>
        </div>

        <div className="spread">
          <span className="meter-name">Intended stop</span>
          <span className="mono" style={{ fontSize: 12.5 }}>
            {proposal.spec.intended_stop}
          </span>
        </div>

        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Universe
          </div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {proposal.spec.universe.map((sym) => (
              <Badge key={sym}>{sym}</Badge>
            ))}
          </div>
        </div>

        {proposal.spec.intended_regimes.length > 0 && (
          <div>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              Intended regimes
            </div>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {proposal.spec.intended_regimes.map((r) => (
                <Badge key={r}>{r}</Badge>
              ))}
            </div>
          </div>
        )}

        <div className="subpanel">
          <div className="eyebrow" style={{ marginBottom: 10 }}>
            Validation
          </div>
          <div className="stack">
            {proposal.validations.map((v) => (
              <ValidationBlock key={v.symbol} validation={v} />
            ))}
          </div>
        </div>

        {!proposal.passed && (
          <div className="banner-warn">
            This proposal FAILED the validation gate and cannot be approved. Reasons below.
          </div>
        )}

        {!isPending && (
          <div className="muted" style={{ fontSize: 12 }}>
            {proposal.status} by {proposal.decided_by ?? "unknown"}
            {proposal.decided_ts ? ` at ${fmtTime(proposal.decided_ts)}` : ""}
            {proposal.decision_reason ? ` - ${proposal.decision_reason}` : ""}
          </div>
        )}

        <div className="subpanel">
          <div className="eyebrow" style={{ marginBottom: 10 }}>
            Decision
          </div>

          {banner && <div className="banner-warn">{banner}</div>}

          <div className="field" style={{ marginTop: banner ? 12 : 0 }}>
            <label className="field-label" htmlFor="approver">
              Approver (required)
            </label>
            <input
              id="approver"
              className="input"
              placeholder="your name"
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="note">
              Approval note
            </label>
            <input
              id="note"
              className="input"
              placeholder="optional note recorded with the approval"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="reason">
              Rejection reason
            </label>
            <input
              id="reason"
              className="input"
              placeholder="optional reason recorded with the rejection"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>

          <div className="row" style={{ gap: 8 }}>
            <ActionButton
              variant="btn-primary"
              disabled={!canApprove}
              title={approveTitle}
              onClick={handleApprove}
            >
              Approve
            </ActionButton>
            <ActionButton
              variant="btn-danger"
              disabled={!canReject}
              title={canReject ? undefined : "Enter an approver name first"}
              onClick={handleReject}
            >
              Reject
            </ActionButton>
          </div>
        </div>
      </div>
    </Card>
  );
}

function ValidationBlock({ validation }: { validation: ProposalValidation }) {
  const r: ValidationResult = validation.result;
  return (
    <div className="subpanel" style={{ marginTop: 0, paddingTop: 12 }}>
      <div className="spread" style={{ marginBottom: 10 }}>
        <span className="display" style={{ fontSize: 14 }}>
          {validation.symbol}
        </span>
        <Badge kind={r.passed ? "pass" : "fail"}>{r.passed ? "PASS" : "FAIL"}</Badge>
      </div>

      <dl className="kv">
        <dt>Deflated Sharpe</dt>
        <dd>{typeof r.deflated_sharpe === "number" ? r.deflated_sharpe.toFixed(2) : "—"}</dd>
        <dt>Trials</dt>
        <dd>{fmtNum(r.n_trials)}</dd>
        <dt>Trades</dt>
        <dd>{fmtNum(r.n_trades)}</dd>
        <dt>Calendar days</dt>
        <dd>{fmtNum(r.calendar_days)}</dd>
      </dl>

      {Object.keys(r.metrics).length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Metrics (net)
          </div>
          <NumberKv values={r.metrics} />
        </div>
      )}

      {Object.keys(r.walk_forward_summary).length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Walk-forward summary
          </div>
          <NumberKv values={r.walk_forward_summary} />
        </div>
      )}

      {Object.keys(r.regime_breakdown).length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Regime breakdown
          </div>
          <div className="stack" style={{ gap: 8 }}>
            {Object.entries(r.regime_breakdown).map(([regime, metrics]) => (
              <div key={regime}>
                <div className="meter-name" style={{ marginBottom: 4 }}>
                  {regime}
                </div>
                <NumberKv values={metrics} />
              </div>
            ))}
          </div>
        </div>
      )}

      {!r.passed && r.reasons.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="eyebrow" style={{ marginBottom: 6 }}>
            Reasons for FAIL
          </div>
          <ul className="reasons">
            {r.reasons.map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function fmtCell(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? fmtNum(value) : value.toFixed(2);
  if (value == null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function NumberKv({ values }: { values: Record<string, unknown> }) {
  return (
    <dl className="kv">
      {Object.entries(values).map(([key, value]) => (
        <div key={key} style={{ display: "contents" }}>
          <dt>{key}</dt>
          <dd>{fmtCell(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
