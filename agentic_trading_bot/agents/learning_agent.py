"""Learning agent: reflect, hypothesize, experiment, and decide by taxonomy.

Triggered after a closed trade or as a periodic batch. It proposes only. It never
executes an order, edits the risk gate, edits the execution path, or auto-deploys
a strategy. Its entire reach is the skills registry, the approval queue, and the
audit trail (invariant 9).

Decision rules (the asymmetric automation core, invariant 10):
  - analysis-only skill that wins its shadow A/B  -> auto-promote (reversible, audited).
  - signal-shaping skill that passes the gate     -> ENQUEUE for human approval; never
    auto-promote. Promotion later requires paper-forward confirmation AND a human
    approval through the same queue.
  - anything touching risk or execution           -> log a suggestion only, never apply.

All randomness/parameters flow from the pre-registered criteria fixed at reflect
time (invariant 13). The cumulative trial ledger and holdout budget govern the
experiment (invariants 11 and 12). The budget meter skips LLM steps when the
per-run token/credit budget is exhausted.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from agents.provider import LLMProvider, audit_agent_usage
from config import Settings, settings as default_settings
from core.contracts import (
    Experiment,
    ExperimentVerdict,
    HypothesisStatus,
    LearningResult,
    PreRegisteredCriteria,
    Proposal,
    ProposalValidation,
    Reflection,
    Skill,
    SkillStatus,
    SkillType,
    StrategyProposal,
    TradeTrace,
)
from learning.budget_meter import BudgetMeter
from learning.holdout_budget import BudgetExhaustedError
from strategies.base import GateAdapter
from strategies.registry import build_strategy, known_templates
from utils.logging import get_logger

_log = get_logger(__name__)


class CandidateSkillBatch(BaseModel):
    """Envelope so one constrained generation can return several candidate skills."""

    skills: list[Skill] = Field(default_factory=list)


_REFLECT_SYSTEM = (
    "You are a trading research post-mortem analyst. Read the full trace of a "
    "closed trade and reflect honestly: what happened, was the original thesis "
    "right, and what are the lessons. Then propose 1 to 3 SINGLE-VARIABLE "
    "hypotheses versus a frozen baseline, each with a pre-registered success "
    "metric and threshold. You never write executable code and never trade."
)

_PROPOSE_SYSTEM = (
    "You turn hypotheses into candidate skills. A signal-shaping skill may ONLY be "
    f"expressed as one of these registry templates {known_templates()} plus params; "
    "never free-form code. An analysis-only skill is a prompt/framing refinement. "
    "Risk or execution ideas may only be a suggestion, never an applied change."
)


def shadow_ab(skill: Skill, scorer: Optional[Callable[[Skill], tuple[float, float]]] = None) -> tuple[bool, str]:
    """Run an analysis skill's shadow A/B and return (won, detail).

    The scorer returns (candidate_score, baseline_score) on held-out cases. The
    default reads the scores from the skill's performance_metrics, which keeps the
    comparison deterministic and testable.
    """
    if scorer is not None:
        candidate, baseline = scorer(skill)
    else:
        metrics = skill.performance_metrics
        candidate = float(metrics.get("shadow_candidate", 0.0))
        baseline = float(metrics.get("shadow_baseline", 0.0))
    won = candidate > baseline
    return won, f"shadow A/B candidate={candidate:.4f} vs baseline={baseline:.4f}"


def run_auto_demotion(
    registry: Any,
    audit: Any,
    live_key: str = "rolling_live",
    baseline_key: str = "baseline",
) -> int:
    """Demote any promoted skill whose live performance has drifted below baseline.

    Auto-demotion is a safe, reducing action and is applied automatically and
    audited. Returns the number of skills demoted.
    """
    demoted = 0
    for skill in registry.list_by_status(SkillStatus.PROMOTED):
        metrics = skill.performance_metrics
        if live_key in metrics and baseline_key in metrics and metrics[live_key] < metrics[baseline_key]:
            registry.demote(
                skill.skill_id,
                audit,
                reason=f"live {metrics[live_key]:.3f} drifted below baseline {metrics[baseline_key]:.3f}",
            )
            demoted += 1
    return demoted


async def run_learning_cycle(
    provider: LLMProvider,
    trace: TradeTrace,
    *,
    audit: Any,
    gate: Any,
    ledger: Any,
    budget: Any,
    registry: Any,
    queue: Any,
    tranche_id: str,
    detector: Any = None,
    family: Optional[str] = None,
    symbol: str = "LRN",
    shadow_scorer: Optional[Callable[[Skill], tuple[float, float]]] = None,
    n_trials_per_run: int = 1,
    budget_meter: Optional[BudgetMeter] = None,
    settings: Settings = default_settings,
) -> LearningResult:
    """Run one learning cycle over a closed-trade trace and return a summary."""
    family = family or trace.family or "default"
    meter = budget_meter or BudgetMeter(settings.learning_token_budget, settings.learning_cost_budget_usd)
    result = LearningResult(period=trace.trace_ref)

    # 1. Reflect (cheap LLM, analysis only).
    if meter.exhausted:
        _budget_skip(audit, "reflect", meter)
        return result
    reflection = await _reflect(provider, trace, audit, meter)
    result.reflections_count = 1

    # 2. Propose candidate skills (constrained LLM).
    if meter.exhausted:
        _budget_skip(audit, "propose_skill", meter)
        return result
    candidates = await _propose(provider, reflection, audit, meter)

    # Out-of-budget data blocks ALL experiments and promotions (invariant 12).
    try:
        tranche = budget.serve(tranche_id)
        result.holdout_budget_consumed = 1
    except BudgetExhaustedError as exc:
        audit.record(
            "LEARNING_BUDGET_EXHAUSTED",
            {"tranche_id": tranche_id, "error": str(exc)},
            "Holdout budget exhausted: no experiments or promotions this cycle",
        )
        return result

    hypotheses = reflection.hypotheses
    for index, skill in enumerate(candidates):
        hypothesis = hypotheses[index] if index < len(hypotheses) else None
        criteria = hypothesis.pre_registered_criteria if hypothesis else PreRegisteredCriteria()
        registry.upsert(skill)  # store as CANDIDATE before any decision

        if skill.skill_type is SkillType.RISK_SUGGESTION:
            audit.record(
                "SUGGESTION_LOGGED",
                {"skill_id": skill.skill_id, "name": skill.name, "description": skill.description},
                "Risk/execution suggestion logged for a human; never auto-applied",
            )
            result.suggestions_logged += 1
            continue

        if skill.skill_type is SkillType.ANALYSIS:
            result.experiments_run += 1
            self_promoted = _decide_analysis(
                skill, hypothesis, tranche.tranche_id, registry, audit, shadow_scorer
            )
            if self_promoted:
                result.skills_promoted += 1
            continue

        if skill.skill_type is SkillType.SIGNAL_SHAPING:
            queued = _decide_signal(
                skill, hypothesis, criteria, tranche, family, gate, ledger, queue, audit,
                detector, symbol, n_trials_per_run, result,
            )
            if queued:
                result.skills_queued_for_approval += 1

    audit.record(
        "LEARNING_RESULT",
        result.model_dump(),
        f"Learning cycle complete for {trace.trace_ref}",
    )
    _log.info("learning_cycle_complete", **result.model_dump())
    return result


# ----------------------------------------------------------------- decisions


def _decide_analysis(
    skill: Skill,
    hypothesis: Any,
    tranche_id: str,
    registry: Any,
    audit: Any,
    shadow_scorer: Optional[Callable[[Skill], tuple[float, float]]],
) -> bool:
    """Analysis-only: auto-promote (via shadow) if it wins its shadow A/B."""
    won, detail = shadow_ab(skill, shadow_scorer)
    experiment = Experiment(
        hypothesis_id=hypothesis.hypothesis_id if hypothesis else "",
        candidate_skill_id=skill.skill_id,
        verdict=ExperimentVerdict.PASS if won else ExperimentVerdict.FAIL,
        reasons=[detail],
        holdout_tranche_id=tranche_id,
    )
    audit.record(
        "EXPERIMENT_RESULT",
        {"skill_id": skill.skill_id, "skill_type": "analysis", "verdict": experiment.verdict.value,
         "detail": detail},
        f"Analysis shadow A/B for {skill.skill_id}: {experiment.verdict.value}",
    )
    if not won:
        return False
    # Reversible, audited auto-promotion: candidate -> shadow -> promoted.
    registry.set_shadow(skill.skill_id, audit)
    registry.promote(skill.skill_id, experiment, audit)
    return True


def _decide_signal(
    skill: Skill,
    hypothesis: Any,
    criteria: PreRegisteredCriteria,
    tranche: Any,
    family: str,
    gate: Any,
    ledger: Any,
    queue: Any,
    audit: Any,
    detector: Any,
    symbol: str,
    n_trials_per_run: int,
    result: LearningResult,
) -> bool:
    """Signal-shaping: run the gate experiment; if PASS, ENQUEUE (never promote)."""
    try:
        baseline = GateAdapter(build_strategy(skill.template, {}, symbol))  # frozen default baseline
        candidate = GateAdapter(build_strategy(skill.template, skill.params, symbol))  # one-variable change
    except KeyError:
        audit.record(
            "EXPERIMENT_SKIPPED",
            {"skill_id": skill.skill_id, "template": skill.template},
            "Signal-shaping skill dropped: unknown template (invariant 14)",
        )
        return False

    exp_result = gate.experiment(
        baseline, candidate, family, tranche, criteria, ledger,
        trials_charged=1, n_trials_per_run=n_trials_per_run, detector=detector,
    )
    result.experiments_run += 1
    audit.record(
        "EXPERIMENT_RESULT",
        {
            "skill_id": skill.skill_id,
            "skill_type": "signal_shaping",
            "verdict": "pass" if exp_result.passed else "fail",
            "cumulative_trials": exp_result.cumulative_trials,
            "cumulative_deflated_sharpe": exp_result.cumulative_deflated_sharpe,
            "before_after": exp_result.before_after,
            "reasons": exp_result.reasons,
        },
        f"Signal-shaping experiment for {skill.skill_id}: {'PASS' if exp_result.passed else 'FAIL'}",
    )
    if not exp_result.passed:
        return False

    # PASS does NOT promote. Enqueue as a new strategy needing paper-forward
    # confirmation and human approval through the same queue.
    candidate_vr = exp_result.candidate
    proposal = Proposal(
        spec=StrategyProposal(
            name=skill.name,
            hypothesis=hypothesis.statement if hypothesis else skill.description,
            template=skill.template,
            parameters=skill.params,
            universe=[symbol],
            intended_stop=skill.description or f"{skill.template} template stop",
            proposed_by="learning-loop",
        ),
        validations=[ProposalValidation(symbol=symbol, result=candidate_vr)],
        passed=True,
        summary=(
            "Auto-proposed by the learning loop. It passed the gate experiment but is "
            "NOT promoted: it requires paper-forward confirmation AND human approval "
            "before it can shape trades."
        ),
        applied_skills=[skill.as_applied()],
    )
    queue.enqueue(proposal)
    audit.record(
        "PROPOSAL_ENQUEUED",
        {"proposal_id": proposal.proposal_id, "name": skill.name, "source": "learning-loop",
         "skill_id": skill.skill_id, "passed": True},
        f"Signal-shaping skill {skill.skill_id} enqueued for human approval (not promoted)",
    )
    return True


# -------------------------------------------------------------- LLM steps


async def _reflect(provider: LLMProvider, trace: TradeTrace, audit: Any, meter: BudgetMeter) -> Reflection:
    prompt = (
        f"Trace: {trace.trace_ref}\nTheme: {trace.theme}\nRegime: {trace.regime}\n"
        f"Brief: {trace.brief_summary}\nSpec: {trace.spec_summary}\n"
        f"Validation: {trace.validation_summary}\nOutcome: {trace.outcome}\n"
        f"PnL: {trace.pnl}  Costs: {trace.costs}\n\n"
        "Reflect, then propose 1 to 3 single-variable hypotheses with pre-registered criteria."
    )
    response = await provider.structured(
        agent="reflect", system=_REFLECT_SYSTEM, prompt=prompt, schema=Reflection
    )
    meter.spend(response.usage)
    audit_agent_usage(audit, "reflect", response.usage, extra={"trace": trace.trace_ref})

    reflection: Reflection = response.data
    if not reflection.trace_ref:
        reflection.trace_ref = trace.trace_ref
    audit.record(
        "REFLECTION_CREATED",
        {"reflection_id": reflection.reflection_id, "trace_ref": reflection.trace_ref,
         "thesis_correctness": reflection.thesis_correctness, "n_hypotheses": len(reflection.hypotheses)},
        "Learning agent created a reflection",
    )
    for hypothesis in reflection.hypotheses:
        hypothesis.status = HypothesisStatus.REGISTERED
        audit.record(
            "HYPOTHESIS_REGISTERED",
            {
                "hypothesis_id": hypothesis.hypothesis_id,
                "statement": hypothesis.statement,
                "single_variable": hypothesis.single_variable,
                "target_metric": hypothesis.pre_registered_criteria.target_metric,
                "dsr_threshold": hypothesis.pre_registered_criteria.dsr_threshold,
            },
            "Hypothesis pre-registered before any test",
        )
    return reflection


async def _propose(provider: LLMProvider, reflection: Reflection, audit: Any, meter: BudgetMeter) -> list[Skill]:
    prompt = (
        f"Reflection: {reflection.what_happened}\nLessons: {reflection.lessons}\n"
        f"Hypotheses: {[h.statement for h in reflection.hypotheses]}\n\n"
        "Propose one candidate skill per hypothesis."
    )
    response = await provider.structured(
        agent="propose_skill", system=_PROPOSE_SYSTEM, prompt=prompt, schema=CandidateSkillBatch
    )
    meter.spend(response.usage)
    audit_agent_usage(audit, "propose_skill", response.usage, extra={"reflection": reflection.reflection_id})

    candidates: list[Skill] = []
    dropped: list[str] = []
    for skill in response.data.skills:
        skill.provenance_reflection_id = reflection.reflection_id
        skill.status = SkillStatus.CANDIDATE  # never trust an LLM-provided status
        if skill.skill_type is SkillType.SIGNAL_SHAPING:
            if not skill.template or skill.template not in known_templates():
                dropped.append(f"{skill.name}:{skill.template}")
                continue
            skill.content_or_template = skill.template
        elif skill.skill_type is SkillType.ANALYSIS:
            skill.content_or_template = skill.prompt_addendum or skill.content_or_template
        candidates.append(skill)

    audit.record(
        "SKILLS_PROPOSED",
        {"count": len(candidates), "dropped_unknown_template": dropped},
        "Candidate skills proposed (unknown templates dropped, invariant 14)",
    )
    return candidates


def _budget_skip(audit: Any, step: str, meter: BudgetMeter) -> None:
    audit.record(
        "AGENT_BUDGET_EXHAUSTED",
        {"step": step, **meter.snapshot()},
        f"Skipped LLM step '{step}': per-run budget exhausted",
    )
    _log.warning("learning_budget_exhausted", step=step, **meter.snapshot())
