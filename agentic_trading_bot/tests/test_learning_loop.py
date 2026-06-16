"""Tests for the self-learning loop (ScriptedProvider only; no live LLM).

Proves the safety properties:
  - a signal_shaping skill cannot reach promoted without BOTH a PASS experiment
    and a human queue approval (and a passing forward result),
  - an analysis_only skill auto-promotes after a shadow A/B win,
  - a risk-touching suggestion is logged and never applied,
  - an out-of-budget (burned) tranche blocks all promotion,
  - the per-run budget meter skips LLM steps when exhausted,
  - auto-demotion demotes a drifting skill,
  - the meta-reviewer only writes a note.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from agents.learning_agent import CandidateSkillBatch, run_auto_demotion, run_learning_cycle
from agents.meta_reviewer import run_meta_review
from agents.provider import ProviderUsage, ScriptedProvider
from backtest.validator import ValidationGate
from core.contracts import (
    Experiment,
    ExperimentResult,
    ExperimentVerdict,
    ForwardResult,
    Hypothesis,
    LearningResult,
    PreRegisteredCriteria,
    Proposal,
    ProposalStatus,
    Reflection,
    Skill,
    SkillStatus,
    SkillType,
    StrategyProposal,
    TradeTrace,
    ValidationResult,
)
from discovery.approval_queue import ApprovalQueue
from learning.budget_meter import BudgetMeter
from learning.holdout_budget import HoldoutBudget
from learning.registry import LearningError, SkillRegistry
from learning.trial_ledger import TrialLedger
from utils.audit import AuditTrail


# --------------------------------------------------------------- helpers


def small_bars(n: int = 200, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
         "volume": rng.uniform(1e6, 5e6, n)},
        index=idx,
    )


def _vr(passed: bool) -> ValidationResult:
    return ValidationResult(
        passed=passed, strategy_name="x", n_trials=1, n_trades=50, calendar_days=400,
        deflated_sharpe=0.99 if passed else 0.1,
    )


class FakeGate:
    """A deterministic stand-in for the gate's experiment runner."""

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.calls = 0

    def experiment(self, baseline, candidate, family, holdout_tranche, criteria, ledger, *,
                   trials_charged=1, n_trials_per_run=1, capital=None, detector=None):
        self.calls += 1
        cumulative = int(ledger.charge(family, trials_charged))  # honor the ledger
        return ExperimentResult(
            family=family, tranche_id=holdout_tranche.tranche_id, target_metric=criteria.target_metric,
            passed=self.passed, reasons=[] if self.passed else ["candidate did not pass"],
            trials_charged=trials_charged, cumulative_trials=cumulative,
            per_run_deflated_sharpe=0.99, cumulative_deflated_sharpe=0.99 if self.passed else 0.2,
            criteria=criteria,
            before_after={"oos_net_sharpe": {"baseline": -1.0, "candidate": 2.0, "delta": 3.0}},
            baseline=_vr(False), candidate=_vr(self.passed),
        )


def make_provider(candidates: list[Skill], tokens: int = 20) -> ScriptedProvider:
    reflection = Reflection(
        trace_ref="trace-1",
        what_happened="The breakout entered late and the stop was too wide.",
        thesis_correctness="partially correct",
        lessons=["Stops were too wide for the regime."],
        hypotheses=[Hypothesis(statement="A tighter ATR stop improves net Sharpe.",
                               single_variable="atr_mult",
                               pre_registered_criteria=PreRegisteredCriteria())],
    )
    return ScriptedProvider(
        responses={"Reflection": reflection, "CandidateSkillBatch": CandidateSkillBatch(skills=candidates)},
        summary="The system overestimates catalysts in the Bear regime.",
        usage=ProviderUsage(model="scripted", input_tokens=tokens // 2, output_tokens=tokens // 2,
                            total_tokens=tokens),
    )


def make_env(tmp_path, max_evaluations: int = 3):
    audit = AuditTrail(tmp_path / "audit.db")
    ledger = TrialLedger(tmp_path / "learning.db")
    budget = HoldoutBudget(tmp_path / "budget.db", max_evaluations=max_evaluations)
    tranche_ids = budget.reserve(small_bars(), n_tranches=1, label="v1")
    registry = SkillRegistry(tmp_path / "skills.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    return audit, ledger, budget, tranche_ids[0], registry, queue


def _events(audit, kind):
    return [e for e in audit.read_all() if e.event_type == kind]


def _approved_proposal() -> Proposal:
    return Proposal(
        spec=StrategyProposal(name="x", hypothesis="h", template="trend_breakout",
                              intended_stop="atr", universe=["AAPL"]),
        passed=True, status=ProposalStatus.APPROVED, decided_by="alice",
    )


# --------------------------------------- signal-shaping promotion is gated


def test_signal_skill_cannot_promote_without_approval_and_forward(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    registry = SkillRegistry(tmp_path / "skills.db")
    registry.upsert(Skill(skill_id="sig", skill_type=SkillType.SIGNAL_SHAPING, name="tight",
                          template="trend_breakout", params={"atr_mult": 2.0},
                          status=SkillStatus.CANDIDATE))
    exp = Experiment(verdict=ExperimentVerdict.PASS, candidate_skill_id="sig")

    # PASS experiment but no approval -> refused.
    with pytest.raises(LearningError):
        registry.promote("sig", exp, audit)
    # Approval but no passing forward result -> refused.
    with pytest.raises(LearningError):
        registry.promote("sig", exp, audit, approval=_approved_proposal())
    # FAIL experiment, even with everything -> refused.
    exp_fail = Experiment(verdict=ExperimentVerdict.FAIL, candidate_skill_id="sig",
                          forward_result=ForwardResult(passed=True))
    with pytest.raises(LearningError):
        registry.promote("sig", exp_fail, audit, approval=_approved_proposal())

    # PASS + approval + passing forward -> promoted, audited with before/after.
    exp.forward_result = ForwardResult(passed=True, n_trades=40, sharpe=1.2, baseline_sharpe=0.3)
    promoted = registry.promote("sig", exp, audit, approval=_approved_proposal())
    assert promoted.status is SkillStatus.PROMOTED
    ev = _events(audit, "SKILL_PROMOTED")[-1]
    assert ev.payload["before"] == "candidate" and ev.payload["after"] == "promoted"
    registry.close()


def test_loop_enqueues_signal_skill_and_does_not_promote(tmp_path):
    audit, ledger, budget, tid, registry, queue = make_env(tmp_path)
    candidate = Skill(skill_id="sig1", skill_type=SkillType.SIGNAL_SHAPING, name="tighter",
                      template="trend_breakout", params={"atr_mult": 2.0}, status=SkillStatus.CANDIDATE)
    provider = make_provider([candidate])
    trace = TradeTrace(trace_ref="trace-1", family="trend")

    result = asyncio.run(run_learning_cycle(
        provider, trace, audit=audit, gate=FakeGate(passed=True), ledger=ledger, budget=budget,
        registry=registry, queue=queue, tranche_id=tid,
    ))
    assert result.skills_queued_for_approval == 1
    assert result.skills_promoted == 0
    assert registry.get("sig1").status is SkillStatus.CANDIDATE  # NOT promoted
    assert len(queue.list_pending()) == 1
    assert _events(audit, "PROPOSAL_ENQUEUED") and _events(audit, "EXPERIMENT_RESULT")
    registry.close(); queue.close()


# --------------------------------------- analysis auto-promote on shadow win


def test_analysis_skill_auto_promotes_on_shadow_win(tmp_path):
    audit, ledger, budget, tid, registry, queue = make_env(tmp_path)
    candidate = Skill(skill_id="an1", skill_type=SkillType.ANALYSIS, name="weight-8k",
                      prompt_addendum="Weight recent 8-K filings.", status=SkillStatus.CANDIDATE,
                      performance_metrics={"shadow_candidate": 0.8, "shadow_baseline": 0.5})
    result = asyncio.run(run_learning_cycle(
        make_provider([candidate]), TradeTrace(trace_ref="trace-1", family="research"),
        audit=audit, gate=FakeGate(), ledger=ledger, budget=budget, registry=registry, queue=queue,
        tranche_id=tid,
    ))
    assert result.skills_promoted == 1
    assert registry.get("an1").status is SkillStatus.PROMOTED
    assert _events(audit, "SKILL_PROMOTED")
    registry.close(); queue.close()


def test_analysis_skill_not_promoted_on_shadow_loss(tmp_path):
    audit, ledger, budget, tid, registry, queue = make_env(tmp_path)
    candidate = Skill(skill_id="an2", skill_type=SkillType.ANALYSIS, name="bad-frame",
                      prompt_addendum="x", status=SkillStatus.CANDIDATE,
                      performance_metrics={"shadow_candidate": 0.3, "shadow_baseline": 0.5})
    result = asyncio.run(run_learning_cycle(
        make_provider([candidate]), TradeTrace(trace_ref="trace-1"),
        audit=audit, gate=FakeGate(), ledger=ledger, budget=budget, registry=registry, queue=queue,
        tranche_id=tid,
    ))
    assert result.skills_promoted == 0
    assert registry.get("an2").status is SkillStatus.CANDIDATE
    registry.close(); queue.close()


# --------------------------------------- risk suggestion is logged only


def test_risk_suggestion_is_logged_never_applied(tmp_path):
    audit, ledger, budget, tid, registry, queue = make_env(tmp_path)
    candidate = Skill(skill_id="rk1", skill_type=SkillType.RISK_SUGGESTION, name="widen-stops",
                      description="Consider wider daily drawdown limit.", status=SkillStatus.CANDIDATE)
    result = asyncio.run(run_learning_cycle(
        make_provider([candidate]), TradeTrace(trace_ref="trace-1"),
        audit=audit, gate=FakeGate(), ledger=ledger, budget=budget, registry=registry, queue=queue,
        tranche_id=tid,
    ))
    assert result.suggestions_logged == 1
    assert _events(audit, "SUGGESTION_LOGGED")
    assert registry.get("rk1").status is SkillStatus.CANDIDATE
    # And it can never be promoted.
    with pytest.raises(LearningError):
        registry.promote("rk1", Experiment(verdict=ExperimentVerdict.PASS), audit)
    registry.close(); queue.close()


# --------------------------------------- out-of-budget blocks promotion


def test_out_of_budget_tranche_blocks_promotion(tmp_path):
    audit, ledger, budget, tid, registry, queue = make_env(tmp_path, max_evaluations=1)
    budget.serve(tid)  # burn the only tranche before the loop runs
    candidate = Skill(skill_id="an3", skill_type=SkillType.ANALYSIS, name="would-win",
                      prompt_addendum="x", status=SkillStatus.CANDIDATE,
                      performance_metrics={"shadow_candidate": 0.9, "shadow_baseline": 0.1})
    result = asyncio.run(run_learning_cycle(
        make_provider([candidate]), TradeTrace(trace_ref="trace-1"),
        audit=audit, gate=FakeGate(), ledger=ledger, budget=budget, registry=registry, queue=queue,
        tranche_id=tid,
    ))
    assert result.skills_promoted == 0
    assert result.holdout_budget_consumed == 0
    assert _events(audit, "LEARNING_BUDGET_EXHAUSTED")
    # The candidate was never even stored, let alone promoted.
    assert registry.get("an3") is None
    registry.close(); queue.close()


# --------------------------------------- budget meter skips LLM steps


def test_budget_meter_skips_llm_steps_when_exhausted(tmp_path):
    audit, ledger, budget, tid, registry, queue = make_env(tmp_path)
    candidate = Skill(skill_id="an4", skill_type=SkillType.ANALYSIS, name="x", status=SkillStatus.CANDIDATE)
    provider = make_provider([candidate], tokens=100)
    meter = BudgetMeter(token_budget=100, cost_budget_usd=1e9)  # reflect spends all of it

    result = asyncio.run(run_learning_cycle(
        provider, TradeTrace(trace_ref="trace-1"), audit=audit, gate=FakeGate(), ledger=ledger,
        budget=budget, registry=registry, queue=queue, tranche_id=tid, budget_meter=meter,
    ))
    assert result.reflections_count == 1  # reflect ran
    assert result.experiments_run == 0  # propose was skipped, so nothing followed
    skipped = _events(audit, "AGENT_BUDGET_EXHAUSTED")
    assert skipped and skipped[-1].payload["step"] == "propose_skill"
    registry.close(); queue.close()


# --------------------------------------- auto-demotion watcher


def test_auto_demotion_demotes_drifting_skill(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    registry = SkillRegistry(tmp_path / "skills.db")
    registry.upsert(Skill(skill_id="drift", skill_type=SkillType.SIGNAL_SHAPING, name="d",
                          status=SkillStatus.PROMOTED,
                          performance_metrics={"rolling_live": 0.2, "baseline": 0.5}))
    registry.upsert(Skill(skill_id="healthy", skill_type=SkillType.SIGNAL_SHAPING, name="h",
                          status=SkillStatus.PROMOTED,
                          performance_metrics={"rolling_live": 0.8, "baseline": 0.5}))
    n = run_auto_demotion(registry, audit)
    assert n == 1
    assert registry.get("drift").status is SkillStatus.DEMOTED
    assert registry.get("healthy").status is SkillStatus.PROMOTED
    assert _events(audit, "SKILL_DEMOTED")
    registry.close()


# --------------------------------------- meta-reviewer suggests only


def test_meta_reviewer_writes_note_and_changes_nothing(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    provider = make_provider([])
    note = asyncio.run(run_meta_review(
        provider, [make_provider([]).responses["Reflection"]], [LearningResult(period="w1")], audit
    ))
    assert "Bear" in note
    assert _events(audit, "META_REVIEW_NOTE")
    assert _events(audit, "AGENT_RUN")  # metered
