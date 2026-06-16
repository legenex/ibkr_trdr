import { useState } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { ActionButton, Badge, PageHeader, Tabs } from "../components/Ui";
import { useResource } from "../hooks/useResource";
import { useLiveStore } from "../lib/store";
import { ApiError, demoteSkill, getLearning, getSkills, promoteSkill } from "../lib/api";
import { fmtNum, fmtTime } from "../lib/format";
import type {
  Experiment,
  HoldoutBudget,
  LearningResponse,
  Skill,
  SkillStatus,
  SkillType,
  SkillsResponse,
  Tranche,
} from "../lib/types";

// The self-learning loop view. The loop only proposes; the validation gate and
// the human dispose. Demote (a reducing, safe action) is one click. Promote is
// disabled until a stored PASS experiment id is supplied, and the server still
// enforces the real rule (PASS experiment, plus approval and passing forward
// result for signal-shaping skills).

type TabId = "registry" | "history" | "holdout" | "experiments";

const TABS: { id: TabId; label: string }[] = [
  { id: "registry", label: "Registry" },
  { id: "history", label: "History" },
  { id: "holdout", label: "Holdout" },
  { id: "experiments", label: "Experiments" },
];

const TYPE_BADGE: Record<SkillType, string> = {
  analysis: "gold",
  signal_shaping: "caution",
  risk_suggestion: "",
};

const STATUS_BADGE: Record<SkillStatus, string> = {
  promoted: "promoted",
  shadow: "shadow",
  demoted: "demoted",
  candidate: "",
};

const VERDICT_BADGE: Record<Experiment["verdict"], string> = {
  pass: "pass",
  fail: "fail",
  pending: "pending",
};

function fmtPerf(p: number | null): string {
  return p === null ? "—" : p.toFixed(2);
}

export function Learning() {
  const eventSeq = useLiveStore((s) => s.eventSeq);
  const skillsRes = useResource<SkillsResponse>(getSkills, { intervalMs: 8000, deps: [eventSeq] });
  const learningRes = useResource<LearningResponse>(getLearning, {
    intervalMs: 10000,
    deps: [eventSeq],
  });

  const [tab, setTab] = useState<TabId>("registry");

  const refreshAll = () => {
    skillsRes.refresh();
    learningRes.refresh();
  };

  return (
    <div className="grid">
      <PageHeader eyebrow="Self-learning loop" title="Learning" />
      <Tabs<TabId> tabs={TABS} value={tab} onChange={setTab} />

      {tab === "registry" && (
        <RegistryTab skills={skillsRes.data?.skills ?? []} onChanged={refreshAll} />
      )}
      {tab === "history" && <HistoryTab learning={learningRes.data} />}
      {tab === "holdout" && <HoldoutTab holdout={learningRes.data?.holdout ?? null} />}
      {tab === "experiments" && <ExperimentsTab experiments={learningRes.data?.experiments ?? []} />}
    </div>
  );
}

// ------------------------------------------------------------------ registry

function RegistryTab({ skills, onChanged }: { skills: Skill[]; onChanged: () => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = skills.find((s) => s.skill_id === selectedId) ?? null;

  return (
    <div className="grid cols-2">
      <Card title="Skill Registry">
        {skills.length === 0 ? (
          <EmptyState title="No skills yet">
            The loop reflects on outcomes and proposes reusable skills. They land here as candidates,
            then earn shadow and promoted status only through the validation gate.
          </EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Status</th>
                <th>Version</th>
                <th className="num">Live perf</th>
                <th className="num">Trials</th>
              </tr>
            </thead>
            <tbody>
              {skills.map((s) => (
                <tr
                  key={s.skill_id}
                  className={`clickable ${s.skill_id === selectedId ? "selected" : ""}`}
                  onClick={() => setSelectedId(s.skill_id)}
                >
                  <td>{s.name}</td>
                  <td>
                    <Badge kind={TYPE_BADGE[s.skill_type]}>{s.skill_type.replace("_", " ")}</Badge>
                  </td>
                  <td>
                    <Badge kind={STATUS_BADGE[s.status]}>{s.status}</Badge>
                  </td>
                  <td className="mono">v{s.version}</td>
                  <td className="num">{fmtPerf(s.live_performance)}</td>
                  <td className="num">{fmtNum(s.trials)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {selected ? (
        <SkillDetail skill={selected} onChanged={onChanged} />
      ) : (
        <Card title="Why Promoted / Provenance">
          <EmptyState title="Select a skill">
            Pick a row to see its provenance, the performance metrics behind it, and the demote and
            promote controls.
          </EmptyState>
        </Card>
      )}
    </div>
  );
}

function SkillDetail({ skill, onChanged }: { skill: Skill; onChanged: () => void }) {
  const [expId, setExpId] = useState("");
  const [approvalId, setApprovalId] = useState("");
  const [warn, setWarn] = useState<string | null>(null);

  const metricEntries = Object.entries(skill.performance_metrics);
  const canPromote = expId.trim().length > 0;

  const surface = (err: unknown): void => {
    setWarn(err instanceof ApiError ? err.message : String(err));
  };

  return (
    <Card
      title="Why Promoted / Provenance"
      aside={<Badge kind={STATUS_BADGE[skill.status]}>{skill.status}</Badge>}
    >
      <div className="stack">
        <div>
          <div className="eyebrow stat-label">{skill.name}</div>
          <div className="stat-sub">{skill.description}</div>
        </div>

        <div className="subpanel">
          <div className="eyebrow stat-label">Provenance</div>
          <div className="stat-sub">{skill.provenance}</div>
          {skill.provenance_reflection_id && (
            <div className="stat-sub mono">reflection: {skill.provenance_reflection_id}</div>
          )}
        </div>

        {metricEntries.length > 0 && (
          <div className="subpanel">
            <div className="eyebrow stat-label">Performance metrics</div>
            <dl className="kv">
              {metricEntries.map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <dt>{k}</dt>
                  <dd>{v.toFixed(2)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}

        {(skill.regimes.length > 0 || skill.theme_tags.length > 0) && (
          <div className="subpanel">
            <div className="eyebrow stat-label">Regimes &amp; themes</div>
            <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
              {skill.regimes.map((r) => (
                <Badge key={`r-${r}`}>{r}</Badge>
              ))}
              {skill.theme_tags.map((t) => (
                <Badge key={`t-${t}`} kind="gold">
                  {t}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {warn && <div className="banner-warn">{warn}</div>}

        <div className="subpanel">
          <div className="eyebrow stat-label">Actions</div>
          <div className="row" style={{ marginBottom: 12 }}>
            <ActionButton
              variant="btn-danger"
              onClick={async () => {
                setWarn(null);
                try {
                  await demoteSkill(skill.skill_id, "manual demote from console");
                  onChanged();
                } catch (err) {
                  surface(err);
                }
              }}
            >
              Demote
            </ActionButton>
            <span className="stat-sub">Always allowed. One click; it reduces reliance.</span>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="exp-id">
              Experiment id
            </label>
            <input
              id="exp-id"
              className="input mono"
              value={expId}
              onChange={(e) => setExpId(e.target.value)}
              placeholder="exp_…"
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="approval-id">
              Approval id (signal-shaping skills)
            </label>
            <input
              id="approval-id"
              className="input mono"
              value={approvalId}
              onChange={(e) => setApprovalId(e.target.value)}
              placeholder="optional"
            />
          </div>

          <div className="row">
            <ActionButton
              variant="btn-primary"
              disabled={!canPromote}
              title="Promotion needs a stored PASS experiment and (for signal skills) an approval; trigger it from the learning loop, not by hand."
              onClick={async () => {
                if (!canPromote) return;
                setWarn(null);
                try {
                  await promoteSkill(skill.skill_id, expId.trim(), approvalId.trim() || undefined);
                  onChanged();
                } catch (err) {
                  surface(err);
                }
              }}
            >
              Promote
            </ActionButton>
            <span className="field-hint">
              The server enforces the real rule: a stored PASS experiment, plus a human approval and
              passing forward result for signal-shaping skills. Without evidence it rejects.
            </span>
          </div>
        </div>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------- history

function HistoryTab({ learning }: { learning: LearningResponse | null }) {
  const history = learning ? [...learning.history].reverse() : [];
  return (
    <Card title="Learning History">
      {history.length === 0 ? (
        <EmptyState title="No learning runs yet">
          Reflections, experiments, promotions, and demotions stream into this feed as the loop runs.
        </EmptyState>
      ) : (
        <div className="feed">
          {history.map((h, i) => (
            <div className="feed-row" key={`${h.ts_utc}-${i}`}>
              <span className="feed-time">{fmtTime(h.ts_utc)}</span>
              <div>
                <span className="feed-type">{h.type}</span>
                <div className="feed-reason">{h.reason}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------- holdout

function HoldoutTab({ holdout }: { holdout: HoldoutBudget | null }) {
  if (!holdout) {
    return (
      <Card title="Holdout Budget">
        <EmptyState title="No holdout budget yet">
          The vault of truly-unseen data, released in tranches, appears here once it is configured.
        </EmptyState>
      </Card>
    );
  }

  return (
    <div className="grid">
      <Card
        title="Holdout Budget"
        aside={
          <Badge kind={holdout.any_available ? "ok" : "short"}>
            {holdout.any_available ? "available" : "exhausted"}
          </Badge>
        }
      >
        <div className="stat-value">
          {fmtNum(holdout.total_remaining)}
          <span className="stat-unit">evals left</span>
        </div>
        <div className="stat-sub muted">
          Out of budget means promotions are paused until new data accrues.
        </div>
      </Card>

      <Card title="Tranches">
        {holdout.tranches.length === 0 ? (
          <EmptyState title="No tranches">No holdout tranches are configured yet.</EmptyState>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Tranche</th>
                <th className="num">Remaining</th>
                <th className="num">Bars</th>
                <th>Burned</th>
                <th>Date range</th>
              </tr>
            </thead>
            <tbody>
              {holdout.tranches.map((t: Tranche) => (
                <tr key={t.tranche_id}>
                  <td className="mono">{t.tranche_id}</td>
                  <td className="num">
                    {fmtNum(t.remaining)} / {fmtNum(t.max_evaluations)}
                  </td>
                  <td className="num">{fmtNum(t.n_bars)}</td>
                  <td>
                    <Badge kind={t.burned ? "short" : "ok"}>{t.burned ? "burned" : "live"}</Badge>
                  </td>
                  <td className="mono">
                    {t.start_ts ? fmtTime(t.start_ts) : "—"} .. {t.end_ts ? fmtTime(t.end_ts) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

// --------------------------------------------------------------- experiments

function ExperimentsTab({ experiments }: { experiments: Experiment[] }) {
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <Card title="Experiments">
      {experiments.length === 0 ? (
        <EmptyState title="No experiments recorded">
          Each controlled test snapshots a frozen baseline, changes exactly one variable, and
          records a verdict against pre-registered criteria. They appear here once the loop runs.
        </EmptyState>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Experiment</th>
              <th>Candidate</th>
              <th>Verdict</th>
              <th className="num">Trials</th>
              <th>Forward</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {experiments.map((e) => (
              <ExperimentRows
                key={e.experiment_id}
                exp={e}
                open={e.experiment_id === openId}
                onToggle={() => setOpenId(e.experiment_id === openId ? null : e.experiment_id)}
              />
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function ExperimentRows({
  exp,
  open,
  onToggle,
}: {
  exp: Experiment;
  open: boolean;
  onToggle: () => void;
}) {
  const forwardPassed = exp.forward_result?.passed;
  return (
    <>
      <tr className="clickable" onClick={onToggle}>
        <td className="mono">{exp.experiment_id}</td>
        <td className="mono">{exp.candidate_skill_id ?? "—"}</td>
        <td>
          <Badge kind={VERDICT_BADGE[exp.verdict]}>{exp.verdict}</Badge>
        </td>
        <td className="num">{fmtNum(exp.trials_charged)}</td>
        <td>
          {forwardPassed === undefined ? (
            <span className="muted">—</span>
          ) : (
            <Badge kind={forwardPassed ? "pass" : "fail"}>
              {forwardPassed ? "passed" : "failed"}
            </Badge>
          )}
        </td>
        <td className="mono">{fmtTime(exp.created_at)}</td>
      </tr>
      {open && (
        <tr>
          <td colSpan={6}>
            {exp.reasons.length === 0 ? (
              <span className="muted">No reasons recorded.</span>
            ) : (
              <ul className="reasons">
                {exp.reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
