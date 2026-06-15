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

from pydantic import BaseModel, Field, model_validator


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
