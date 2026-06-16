"""Pydantic message contracts that cross module boundaries.

These are the single source of truth for Order and RiskDecision (and the
supporting Fill, Position, AccountSummary, and result types). The broker, the
risk gate, strategies, and the UI all speak in terms of these models. Do not
redefine them elsewhere; import from here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def _utc_now_iso() -> str:
    """Current time as a UTC ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class OrderSide(str, Enum):
    """Direction of an order."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "OrderSide":
        """The closing side for a protective child order."""
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class OrderType(str, Enum):
    """Supported entry order types. There is no naked-market path; a market
    entry is only ever submitted inside a bracket or with an attached stop."""

    MKT = "MKT"
    LMT = "LMT"


class TimeInForce(str, Enum):
    """Order time-in-force."""

    DAY = "DAY"
    GTC = "GTC"


class Order(BaseModel):
    """An intended order, broker-agnostic.

    `stop_price` is the protective stop. A single order without a stop is a bug
    and is rejected by the broker. `target_price` is used for bracket orders.
    """

    symbol: str
    side: OrderSide
    quantity: float = Field(gt=0)
    order_type: OrderType = OrderType.LMT
    limit_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    target_price: Optional[float] = Field(default=None, gt=0)
    tif: TimeInForce = TimeInForce.DAY

    # Contract details (equities by default).
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"

    # Provenance: which source proposed this (agent, strategy, manual UI).
    source: str = "manual"
    client_tag: Optional[str] = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _limit_requires_price(self) -> "Order":
        if self.order_type is OrderType.LMT and self.limit_price is None:
            raise ValueError("a LMT order requires limit_price")
        return self

    @property
    def has_stop(self) -> bool:
        """True if a protective stop is attached."""
        return self.stop_price is not None


class RiskDecision(BaseModel):
    """The result of the risk guardrails gate. The broker submits nothing unless
    `approved` is True. Stage 3 fills in the real logic behind the gate; this
    contract is stable."""

    approved: bool
    reason: str = ""
    # The full, human-readable list of reasons and notes (vetoes plus any
    # informational notes such as a size shrink). `vetoes` is the blocking
    # subset that caused approved to be False.
    reasons: list[str] = Field(default_factory=list)
    vetoes: list[str] = Field(default_factory=list)
    # If the gate trims size rather than vetoing outright, the broker uses this.
    # It may shrink the requested size but never grow it.
    adjusted_quantity: Optional[float] = None
    evaluator: str = "guardrails"
    context: dict[str, Any] = Field(default_factory=dict)
    ts_utc: str = Field(default_factory=_utc_now_iso)

    @classmethod
    def approve(cls, reason: str = "", **kwargs: Any) -> "RiskDecision":
        """Convenience constructor for an approval."""
        return cls(approved=True, reason=reason, **kwargs)

    @classmethod
    def veto(cls, reason: str, vetoes: Optional[list[str]] = None, **kwargs: Any) -> "RiskDecision":
        """Convenience constructor for a veto."""
        return cls(approved=False, reason=reason, vetoes=vetoes or [reason], **kwargs)


class Fill(BaseModel):
    """An execution report."""

    symbol: str
    side: OrderSide
    quantity: float
    price: float
    ts_utc: str
    exec_id: Optional[str] = None
    order_id: Optional[int] = None
    commission: Optional[float] = None


class Position(BaseModel):
    """A broker-reported position."""

    symbol: str
    quantity: float
    avg_cost: float
    account: Optional[str] = None
    market_price: Optional[float] = None
    market_value: Optional[float] = None


class AccountState(BaseModel):
    """Snapshot of account and portfolio state the risk gate evaluates against.

    This is everything the gate needs that is not a static config limit. It is
    broker-agnostic: the broker (or a backtest) builds it, the gate consumes it.
    The gate never reaches back into the broker.
    """

    # Current account equity (net liquidation). Drives sizing and exposure.
    equity: float
    # Equity at the start of the day / week, for the drawdown circuit breakers.
    # If None, that breaker is skipped (cannot be computed).
    day_start_equity: Optional[float] = None
    week_start_equity: Optional[float] = None

    # Current open positions.
    positions: list[Position] = Field(default_factory=list)
    # Latest reference prices per symbol (used to size and value orders when the
    # order itself has no limit price, and to value existing positions).
    prices: dict[str, float] = Field(default_factory=dict)
    # Average daily volume in shares per symbol, for the liquidity filter.
    average_daily_volume: dict[str, float] = Field(default_factory=dict)
    # Recent per-bar returns per symbol, for correlation clustering.
    recent_returns: dict[str, list[float]] = Field(default_factory=dict)

    ts_utc: str = Field(default_factory=_utc_now_iso)

    def position_for(self, symbol: str) -> Optional[Position]:
        """Return the open position in symbol, or None."""
        for position in self.positions:
            if position.symbol == symbol:
                return position
        return None

    def price_for(self, symbol: str) -> Optional[float]:
        """Best available reference price for symbol.

        Prefers an explicit reference price, then the position's market price,
        then its average cost. Returns None if nothing is known.
        """
        if symbol in self.prices:
            return self.prices[symbol]
        position = self.position_for(symbol)
        if position is not None:
            if position.market_price is not None:
                return position.market_price
            if position.avg_cost:
                return position.avg_cost
        return None

    def signed_value(self, symbol: str) -> float:
        """Signed market value of the existing position in symbol (0 if none)."""
        position = self.position_for(symbol)
        if position is None:
            return 0.0
        price = self.price_for(symbol)
        if price is None:
            return 0.0
        return position.quantity * price


class AccountSummary(BaseModel):
    """Account values keyed by IBKR tag (values kept as raw strings)."""

    account: str
    values: dict[str, str] = Field(default_factory=dict)

    def get_float(self, tag: str) -> Optional[float]:
        """Return a tag's value as a float, or None if missing or non-numeric."""
        raw = self.values.get(tag)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


class OrderKind(str, Enum):
    """How an order was submitted."""

    SINGLE_WITH_STOP = "single_with_stop"
    BRACKET = "bracket"


class OrderPlacementResult(BaseModel):
    """Outcome of an order submission attempt, including the risk decision."""

    accepted: bool
    reason: str
    kind: OrderKind
    risk_decision: RiskDecision
    ib_order_ids: list[int] = Field(default_factory=list)
    symbol: Optional[str] = None
    ts_utc: str = Field(default_factory=_utc_now_iso)


class ReconciliationReport(BaseModel):
    """Comparison of locally intended state against broker-reported state."""

    ts_utc: str = Field(default_factory=_utc_now_iso)
    in_sync: bool
    position_drift: list[dict[str, Any]] = Field(default_factory=list)
    order_drift: list[dict[str, Any]] = Field(default_factory=list)
    details: str = ""


class ValidationResult(BaseModel):
    """Structured verdict from the validation gate.

    This is the object the approval flow and UI consume. `passed` is the only
    thing that makes a strategy approvable, and `approvable` is derived from it
    so a FAIL can never be marked approvable downstream.

    `metrics` is nested as period -> {"gross"|"net"} -> metric name -> value, so
    gross and net are always reported side by side.
    """

    passed: bool
    strategy_name: str
    n_trials: int
    n_trades: int
    calendar_days: float
    deflated_sharpe: float
    metrics: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)
    walk_forward: list[dict[str, Any]] = Field(default_factory=list)
    walk_forward_summary: dict[str, float] = Field(default_factory=dict)
    sensitivity: dict[str, Any] = Field(default_factory=dict)
    regime_breakdown: dict[str, dict[str, float]] = Field(default_factory=dict)
    dsr_detail: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    ts_utc: str = Field(default_factory=_utc_now_iso)

    @property
    def approvable(self) -> bool:
        """A strategy is approvable only if it passed. FAIL is never approvable."""
        return self.passed


class Regime(str, Enum):
    """Market regime labels, ordered from most bearish to most bullish.

    The ordering is meaningful: CRASH < BEAR < NEUTRAL < BULL < EUPHORIA along a
    return axis. `ORDERED_REGIMES` and `rank` expose that ordering.
    """

    CRASH = "Crash"
    BEAR = "Bear"
    NEUTRAL = "Neutral"
    BULL = "Bull"
    EUPHORIA = "Euphoria"

    @property
    def rank(self) -> int:
        """Position on the ordered axis (0 = CRASH ... 4 = EUPHORIA)."""
        return ORDERED_REGIMES.index(self)


# The canonical low-to-high ordering used to map ranked HMM states to labels.
ORDERED_REGIMES: list[Regime] = [
    Regime.CRASH,
    Regime.BEAR,
    Regime.NEUTRAL,
    Regime.BULL,
    Regime.EUPHORIA,
]


class RegimeState(BaseModel):
    """The detected regime for a single bar.

    `probabilities` is the distribution over the ordered regime labels and sums
    to approximately 1. `state_index` is the raw HMM hidden state that the label
    was mapped from, kept for diagnostics.
    """

    ts_utc: str
    regime: Regime
    state_index: int
    probabilities: dict[str, float] = Field(default_factory=dict)

    @property
    def confidence(self) -> float:
        """Probability mass on the chosen regime label."""
        return self.probabilities.get(self.regime.value, 0.0)


class StrategySpec(BaseModel):
    """Metadata describing a strategy. Crosses module boundaries, so a model.

    `params` are the strategy's current parameter values. `key_parameters` maps
    the parameters that matter to the perturbation STEP used by the validation
    gate's sensitivity sweep.
    """

    name: str
    category: str  # for example "trend", "breakout", "mean_reversion"
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    key_parameters: dict[str, float] = Field(default_factory=dict)
    symbols: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    """A target position for one bar, carrying its intended protective stop.

    `target_weight` is the desired portfolio weight in [-1, 1]. Every non-flat
    signal carries a `stop_price` so the risk gate can size the position from the
    distance to the stop. A flat signal (target_weight 0) has no stop.
    """

    ts_utc: str
    symbol: str = ""
    target_weight: float = Field(ge=-1.0, le=1.0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    reference_price: Optional[float] = Field(default=None, gt=0)
    reason: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _nonflat_requires_stop(self) -> "Signal":
        """A non-flat target must carry a stop; risk sizing depends on it."""
        if abs(self.target_weight) > 1e-9 and self.stop_price is None:
            raise ValueError("a non-flat signal must carry an intended stop_price")
        return self

    @property
    def is_flat(self) -> bool:
        """True if this signal targets no position."""
        return abs(self.target_weight) <= 1e-9


# ---------------------------------------------------------------------------
# Agentic discovery contracts (research -> signal -> validation pipeline)
# ---------------------------------------------------------------------------


class Source(BaseModel):
    """A cited source backing a research brief."""

    title: str
    url: str = ""
    kind: str = "web"  # web | news | filing
    snippet: str = ""


class ResearchBrief(BaseModel):
    """Thematic context gathered by the research agent. Read-only product."""

    theme: str
    summary: str
    key_points: list[str] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    ts_utc: str = Field(default_factory=_utc_now_iso)


class StrategyProposal(BaseModel):
    """A candidate strategy spec proposed by the signal agent. Rules only.

    `template` must name a known strategy template (the agent proposes which
    parameterized template to use, never arbitrary code). `intended_stop` is
    required: a proposal without a stop is not a proposal.
    """

    name: str
    hypothesis: str
    template: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    universe: list[str] = Field(default_factory=list)
    intended_regimes: list[str] = Field(default_factory=list)
    intended_stop: str
    rationale: str = ""
    proposed_by: str = "signal-agent"
    ts_utc: str = Field(default_factory=_utc_now_iso)

    @field_validator("intended_stop")
    @classmethod
    def _stop_required(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("a strategy proposal must state an intended stop")
        return value


class ProposalStatus(str, Enum):
    """Lifecycle of a proposal in the approval queue."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SkillType(str, Enum):
    """Skill taxonomy by blast radius (see Self-learning discipline)."""

    ANALYSIS = "analysis"  # prompt/framing refinements; never shapes what is traded
    SIGNAL_SHAPING = "signal_shaping"  # registry template + params; treated as a strategy
    RISK_SUGGESTION = "risk_suggestion"  # suggestion only, never auto-applied


class SkillStatus(str, Enum):
    """Lifecycle of a skill: candidate -> shadow -> promoted, or demoted."""

    CANDIDATE = "candidate"  # newly proposed, not yet earned anything
    SHADOW = "shadow"  # running in shadow A/B (analysis-only) before promotion
    PROMOTED = "promoted"
    DEMOTED = "demoted"


class AppliedSkill(BaseModel):
    """A compact record of a skill that was active during a run, for provenance."""

    skill_id: str
    version: int = 1
    skill_type: str
    name: str = ""
    live_performance: float = 0.0


class Skill(BaseModel):
    """A reusable, versioned skill stored in the learning registry.

    Analysis skills carry a `prompt_addendum` only. Signal-shaping skills carry a
    `template` (which MUST already exist in the strategy registry) plus `params`;
    they never carry executable code (invariant 14).
    """

    skill_id: str
    version: int = 1
    skill_type: SkillType
    name: str
    description: str = ""
    status: SkillStatus = SkillStatus.PROMOTED
    regimes: list[str] = Field(default_factory=list)  # empty = applies in all regimes
    theme_tags: list[str] = Field(default_factory=list)  # empty = applies to all themes
    prompt_addendum: str = ""  # analysis skills only
    template: Optional[str] = None  # signal-shaping skills only
    # Canonical content: the prompt content (analysis) or the template name
    # (signal-shaping). Kept alongside prompt_addendum/template for the agents.
    content_or_template: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    live_performance: float = 0.0
    trials: int = 0
    provenance: str = ""
    provenance_reflection_id: Optional[str] = None
    performance_metrics: dict[str, float] = Field(default_factory=dict)
    created_ts: str = Field(default_factory=_utc_now_iso)
    updated_ts: str = Field(default_factory=_utc_now_iso)

    @property
    def created_at(self) -> str:
        """Alias for created_ts (Stage 7.5 naming)."""
        return self.created_ts

    def as_applied(self) -> AppliedSkill:
        """Return the compact provenance record for this skill."""
        return AppliedSkill(
            skill_id=self.skill_id,
            version=self.version,
            skill_type=self.skill_type.value,
            name=self.name,
            live_performance=self.live_performance,
        )


class ProposalValidation(BaseModel):
    """One symbol's validation result for a proposal."""

    symbol: str
    result: ValidationResult


class Proposal(BaseModel):
    """A candidate spec plus its validation, as queued for human approval.

    `passed` is derived only from the ValidationResults; the validation agent's
    plain-language `summary` can never change it. Approval is a separate human
    action recorded later; this object is created PENDING and never self-approves.
    """

    proposal_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    spec: StrategyProposal
    validations: list[ProposalValidation] = Field(default_factory=list)
    passed: bool = False
    summary: str = ""
    # Provenance: which learning skills were active when this proposal was made.
    applied_skills: list[AppliedSkill] = Field(default_factory=list)
    status: ProposalStatus = ProposalStatus.PENDING
    created_ts: str = Field(default_factory=_utc_now_iso)
    decided_by: Optional[str] = None
    decided_ts: Optional[str] = None
    decision_reason: str = ""

    @property
    def approvable(self) -> bool:
        """Eligible for approval only if it passed the gate. A FAIL never is."""
        return self.passed


# ---------------------------------------------------------------------------
# Self-learning contracts (Stage 7.5: experiments against a frozen baseline)
# ---------------------------------------------------------------------------


class PreRegisteredCriteria(BaseModel):
    """Success criteria fixed and stored BEFORE an experiment runs (invariant 13).

    The verdict reads only these fields, never a metric computed after seeing the
    result. `target_metric` is the single pre-registered objective the candidate
    must improve; the remaining fields are the regression guard that forbids
    buying that improvement with degradation elsewhere.
    """

    target_metric: str = "oos_net_sharpe"
    # Candidate target must exceed baseline target by at least this much.
    min_improvement: float = 0.0
    # The CUMULATIVE-trial deflated Sharpe must clear this (invariant 11).
    dsr_threshold: float = 0.95
    # The candidate must also pass the full validation gate in its own right.
    require_candidate_gate_pass: bool = True
    # Regression guard tolerances (must not materially degrade these).
    max_drawdown_degradation: float = 0.05  # candidate OOS maxDD may worsen by at most this
    min_profit_factor_ratio: float = 0.9  # candidate PF >= baseline PF * this
    regime_degradation_tolerance: float = 0.25  # no regime's net Sharpe may drop more than this
    notes: str = ""


class ExperimentResult(BaseModel):
    """Outcome of one controlled experiment: candidate versus frozen baseline.

    The verdict (`passed`) is computed only against the pre-registered criteria.
    `cumulative_deflated_sharpe` is recomputed with the running family trial
    count, so an edge that exists only because many things were tried fails here.
    A FAIL is never promotable.
    """

    family: str
    tranche_id: str
    target_metric: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    trials_charged: int = 0
    cumulative_trials: int = 0
    per_run_deflated_sharpe: float = 0.0
    cumulative_deflated_sharpe: float = 0.0
    criteria: PreRegisteredCriteria
    before_after: dict[str, dict[str, float]] = Field(default_factory=dict)
    baseline: ValidationResult
    candidate: ValidationResult
    ts_utc: str = Field(default_factory=_utc_now_iso)

    @property
    def promotable(self) -> bool:
        """A FAIL is never promotable downstream; mirrors `passed`."""
        return self.passed


# ---------------------------------------------------------------------------
# Self-learning loop contracts (Stage 7.5: reflect -> hypothesize -> experiment)
# ---------------------------------------------------------------------------


class ForwardResult(BaseModel):
    """Paper-forward confirmation: did a skill beat its baseline on NEW paper data.

    Signal-shaping skills cannot be promoted on backtest evidence alone; they need
    a passing forward result on genuinely new incoming paper data first.
    """

    passed: bool
    period_days: int = 0
    n_trades: int = 0
    sharpe: float = 0.0
    baseline_sharpe: float = 0.0
    notes: str = ""


class HypothesisStatus(str, Enum):
    """Lifecycle of a hypothesis."""

    PROPOSED = "proposed"
    REGISTERED = "registered"
    TESTED = "tested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Hypothesis(BaseModel):
    """A single-variable, pre-registered hypothesis versus a frozen baseline."""

    hypothesis_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    statement: str
    single_variable: str
    baseline_ref: str = ""
    pre_registered_criteria: PreRegisteredCriteria = Field(default_factory=PreRegisteredCriteria)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    created_at: str = Field(default_factory=_utc_now_iso)


class Reflection(BaseModel):
    """The learning agent's analysis-only reflection on a closed trade or batch.

    Produced by a cheap LLM step. It explains what happened, whether the thesis
    was correct, the lessons, and one to three single-variable hypotheses with
    pre-registered success criteria. It never proposes executable code.
    """

    reflection_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    trace_ref: str
    what_happened: str
    thesis_correctness: str = ""
    lessons: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)


class ExperimentVerdict(str, Enum):
    """Verdict of a learning experiment."""

    PASS = "pass"
    FAIL = "fail"
    PENDING = "pending"


class Experiment(BaseModel):
    """The record of one candidate-versus-baseline experiment for a hypothesis."""

    experiment_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    hypothesis_id: str = ""
    baseline_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidate_skill_id: str = ""
    baseline_result: Optional[ValidationResult] = None
    candidate_result: Optional[ValidationResult] = None
    forward_result: Optional[ForwardResult] = None
    trials_charged: int = 0
    holdout_tranche_id: str = ""
    verdict: ExperimentVerdict = ExperimentVerdict.PENDING
    reasons: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)


class TradeTrace(BaseModel):
    """The full trace of a closed trade fed to the reflection step (read-only)."""

    trace_ref: str
    theme: str = ""
    brief_summary: str = ""
    spec_summary: str = ""
    validation_summary: str = ""
    regime: str = ""
    pnl: float = 0.0
    costs: float = 0.0
    outcome: str = ""
    family: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class LearningResult(BaseModel):
    """Summary of one learning loop run, for the dashboard and audit."""

    period: str
    reflections_count: int = 0
    experiments_run: int = 0
    skills_promoted: int = 0
    skills_demoted: int = 0
    skills_queued_for_approval: int = 0
    suggestions_logged: int = 0
    holdout_budget_consumed: int = 0
    created_at: str = Field(default_factory=_utc_now_iso)
