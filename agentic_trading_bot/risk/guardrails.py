"""Risk guardrails: the independent, final gate before any order is submitted.

This module is the part of the system trusted with real money. It has ZERO
dependency on the agents, the strategies, or the broker. It knows only about
orders (core.contracts.Order), the current account and portfolio state
(core.contracts.AccountState), and the configured limits (config.Settings). It
can VETO an order or SHRINK its size, but it can never CREATE or grow an order.

The authoritative gate is `RiskGate.evaluate(order, account_state)`. Each check
below is independently able to veto; a single evaluation can return several
reasons at once. Position sizing is the only check that shrinks size; every
other limit vetoes rather than silently resizing.

The module-level `evaluate(order, account_state)` is the stable entry point the
broker calls. As of merge step M1 the broker builds an AccountState and passes
it here, so this function delegates straight to RiskGate. If it is ever called
WITHOUT an AccountState (a legacy dict, or None), it can no longer assess risk,
so it FAILS CLOSED and vetoes rather than approving. There is no passthrough that
waves orders through anymore.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

import numpy as np

from config import Settings, settings as default_settings
from core.contracts import AccountState, Order, OrderSide, RiskDecision
from utils.logging import get_logger

# False since merge step M1: the broker builds an AccountState and calls the real
# RiskGate. There is no longer a passthrough that approves orders un-assessed.
IS_PASSTHROUGH_STUB: bool = False

_EVALUATOR = "risk-gate"


class RiskGate:
    """The independent final risk gate.

    Construct once with the configured limits, then call `evaluate` for every
    order from every source before submission.
    """

    def __init__(self, settings: Settings = default_settings) -> None:
        """Create the gate bound to a set of configured limits."""
        self.s = settings
        self.log = get_logger(__name__)
        # Per-trade dollar risk committed to NEW entries since the session start.
        # Reset by reset_session() (called at session start, like the drawdown
        # anchors) and added to by commit_entry_risk() when an entry is placed.
        # This is the ONLY mutable state on the gate, and it can only tighten.
        self._committed_risk_usd: float = 0.0

    # ----------------------------------------------------------------- public

    def evaluate(self, order: Order, account_state: AccountState) -> RiskDecision:
        """Evaluate an order against every limit and return a decision.

        Args:
            order: The intended order.
            account_state: Current account and portfolio snapshot.

        Returns:
            A RiskDecision with `approved`, `adjusted_quantity` (never larger
            than the requested size), `vetoes` (the blocking reasons), and
            `reasons` (the full set of notes including any size shrink).
        """
        vetoes: list[str] = []
        notes: list[str] = []
        adjusted_quantity: float = order.quantity

        # 1. Kill switch: if the sentinel exists, nothing goes through at all,
        #    including risk-reducing exits. Flattening is a separate human action.
        if self._kill_switch_active():
            return self._decision(
                approved=False,
                adjusted_quantity=adjusted_quantity,
                vetoes=["kill switch engaged: all new order submission halted"],
                notes=[],
            )

        # Exits (pure risk-reducing orders) bypass the entry-only checks below.
        if self._is_exit(order, account_state):
            return self._decision(
                approved=True,
                adjusted_quantity=adjusted_quantity,
                vetoes=[],
                notes=["risk-reducing exit: entry checks bypassed"],
            )

        # Entry path. Sanity on equity first; without it nothing can be sized.
        equity = account_state.equity
        if equity <= 0:
            return self._decision(
                approved=False,
                adjusted_quantity=adjusted_quantity,
                vetoes=[f"account equity must be positive to open risk, got {equity}"],
                notes=[],
            )

        # 3. Position sizing (the only check that shrinks rather than vetoes).
        #    A missing stop is an automatic veto: a position without a stop is a bug.
        entry_price = self._entry_price(order, account_state)
        if order.stop_price is None:
            vetoes.append("order has no protective stop: a position without a stop is a bug")
        elif entry_price is None:
            vetoes.append(f"no reference price available to size {order.symbol}")
        else:
            max_shares = self._risk_based_max_shares(order, entry_price, equity)
            if max_shares < 1:
                vetoes.append(
                    "risk-per-trade sizing rounds to zero shares "
                    f"(equity {equity:.2f}, risk {self.s.risk_per_trade_pct}% , "
                    f"stop distance {abs(entry_price - order.stop_price):.4f})"
                )
            elif order.quantity > max_shares:
                adjusted_quantity = float(max_shares)
                notes.append(
                    f"size shrunk from {order.quantity:g} to {adjusted_quantity:g} shares "
                    f"by risk-per-trade limit ({self.s.risk_per_trade_pct}% of equity)"
                )

        # 3b. Session discovery risk budget: dollar caps that sit UNDERNEATH the
        #     percent limit above and can only tighten size further (invariant 2
        #     and 10). A single idea is capped at max_risk_per_idea_usd; the
        #     session total is capped at session_risk_budget_usd minus what is
        #     already committed today. Either can shrink to fit or veto.
        if order.stop_price is not None and entry_price is not None:
            adjusted_quantity = self._apply_dollar_risk_caps(
                order, entry_price, adjusted_quantity, vetoes, notes
            )

        # 2. Drawdown circuit breakers (entries only; exits already returned).
        vetoes.extend(self._drawdown_vetoes(account_state))

        # Exposure, concentration, cluster, liquidity, and leverage are assessed
        # against the size we would actually send (post-shrink). Skipped if the
        # order cannot be priced (already vetoed above).
        if entry_price is not None:
            vetoes.extend(
                self._exposure_vetoes(order, account_state, adjusted_quantity, entry_price)
            )
            vetoes.extend(self._liquidity_vetoes(order, account_state, adjusted_quantity))

        return self._decision(
            approved=not vetoes,
            adjusted_quantity=adjusted_quantity,
            vetoes=vetoes,
            notes=notes,
        )

    # ------------------------------------------------------------- check: kill

    def _kill_switch_active(self) -> bool:
        return self.s.kill_switch_path.exists()

    # ------------------------------------------------------------- check: exit

    @staticmethod
    def _is_exit(order: Order, account_state: AccountState) -> bool:
        """True if the order purely reduces an existing position (an exit)."""
        position = account_state.position_for(order.symbol)
        if position is None or position.quantity == 0:
            return False
        order_signed = order.quantity if order.side is OrderSide.BUY else -order.quantity
        opposite = (position.quantity > 0) != (order_signed > 0)
        return opposite and abs(order_signed) <= abs(position.quantity) + 1e-9

    # --------------------------------------------------------- check: sizing

    @staticmethod
    def _entry_price(order: Order, account_state: AccountState) -> Optional[float]:
        """Reference entry price: the order's limit, else a known market price."""
        if order.limit_price is not None:
            return order.limit_price
        return account_state.price_for(order.symbol)

    def _risk_based_max_shares(self, order: Order, entry_price: float, equity: float) -> int:
        """Largest share count whose stop loss risks at most risk-per-trade."""
        per_share_risk = abs(entry_price - order.stop_price)  # type: ignore[arg-type]
        if per_share_risk <= 0:
            return 0
        risk_budget = equity * (self.s.risk_per_trade_pct / 100.0)
        return int(math.floor(risk_budget / per_share_risk))

    # ------------------------------------------- check: session risk budget

    def _apply_dollar_risk_caps(
        self,
        order: Order,
        entry_price: float,
        quantity: float,
        vetoes: list[str],
        notes: list[str],
    ) -> float:
        """Shrink quantity to fit the per-idea and session dollar-risk caps.

        Both caps are disabled when their setting is 0. They only ever reduce the
        size; if not even one share fits, a veto is appended. This never grows the
        order and never blocks an exit (exits return before this is reached).
        """
        per_share_risk = abs(entry_price - order.stop_price)  # type: ignore[arg-type]
        if per_share_risk <= 0:
            return quantity

        # Per-idea cap.
        per_idea = self.s.max_risk_per_idea_usd
        if per_idea and per_idea > 0 and quantity * per_share_risk > per_idea + 1e-9:
            max_shares = int(math.floor(per_idea / per_share_risk))
            if max_shares < 1:
                vetoes.append(
                    f"per-idea risk cap ${per_idea:.2f} cannot fit a single share of "
                    f"{order.symbol} (stop distance {per_share_risk:.4f})"
                )
                return quantity
            notes.append(
                f"size shrunk from {quantity:g} to {max_shares:g} shares by the per-idea "
                f"risk cap (${per_idea:.2f})"
            )
            quantity = float(max_shares)

        # Session budget cap (what is left of today's budget for new ideas).
        budget = self.s.session_risk_budget_usd
        if budget and budget > 0:
            remaining = max(0.0, budget - self._committed_risk_usd)
            if quantity * per_share_risk > remaining + 1e-9:
                max_shares = int(math.floor(remaining / per_share_risk))
                if max_shares < 1:
                    vetoes.append(
                        f"session risk budget exhausted: ${remaining:.2f} of ${budget:.2f} "
                        f"remaining today cannot fit a single share of {order.symbol}"
                    )
                    return quantity
                notes.append(
                    f"size shrunk from {quantity:g} to {max_shares:g} shares to fit the "
                    f"${remaining:.2f} remaining of today's ${budget:.2f} session risk budget"
                )
                quantity = float(max_shares)
        return quantity

    # ----------------------------------- session risk budget: state & helpers

    def reset_session(self) -> None:
        """Reset committed session risk to zero (call at session start)."""
        self._committed_risk_usd = 0.0

    def commit_entry_risk(self, dollar_risk: float) -> None:
        """Record the per-trade dollar risk of a newly opened entry."""
        if dollar_risk and dollar_risk > 0:
            self._committed_risk_usd += float(dollar_risk)

    @property
    def committed_risk_usd(self) -> float:
        """Per-trade dollar risk committed to new entries this session."""
        return self._committed_risk_usd

    def remaining_session_budget(self) -> Optional[float]:
        """Dollars of session risk budget left today, or None if the cap is off."""
        budget = self.s.session_risk_budget_usd
        if not budget or budget <= 0:
            return None
        return max(0.0, budget - self._committed_risk_usd)

    def entry_dollar_risk(
        self, order: Order, quantity: float, account_state: AccountState
    ) -> Optional[float]:
        """Per-trade dollar risk of an entry (qty x stop distance), or None.

        Returns None for an exit or when the order cannot be priced/stopped, so a
        caller can commit only genuine new-entry risk.
        """
        if self._is_exit(order, account_state) or order.stop_price is None:
            return None
        entry_price = self._entry_price(order, account_state)
        if entry_price is None:
            return None
        return float(quantity) * abs(entry_price - order.stop_price)

    # ------------------------------------------------------- check: drawdown

    def _drawdown_vetoes(self, account_state: AccountState) -> list[str]:
        vetoes: list[str] = []
        for label, start_equity, limit in (
            ("daily", account_state.day_start_equity, self.s.max_daily_drawdown_pct),
            ("weekly", account_state.week_start_equity, self.s.max_weekly_drawdown_pct),
        ):
            if start_equity is None or start_equity <= 0:
                continue
            drawdown_pct = (start_equity - account_state.equity) / start_equity * 100.0
            if drawdown_pct >= limit:
                message = (
                    f"{label} drawdown circuit breaker tripped: "
                    f"{drawdown_pct:.2f}% >= {limit}% limit. New entries vetoed; exits allowed."
                )
                self.log.warning(
                    "circuit_breaker_tripped",
                    period=label,
                    drawdown_pct=round(drawdown_pct, 4),
                    limit_pct=limit,
                )
                vetoes.append(message)
        return vetoes

    # ------------------------------------------------- check: exposure caps

    def _exposure_vetoes(
        self,
        order: Order,
        account_state: AccountState,
        quantity: float,
        entry_price: float,
    ) -> list[str]:
        vetoes: list[str] = []
        equity = account_state.equity

        symbols = {p.symbol for p in account_state.positions} | {order.symbol}
        order_signed_notional = (
            quantity * entry_price if order.side is OrderSide.BUY else -quantity * entry_price
        )

        def value_after(symbol: str) -> float:
            existing_signed = account_state.signed_value(symbol)
            if symbol == order.symbol:
                return abs(existing_signed + order_signed_notional)
            return abs(existing_signed)

        values_after = {symbol: value_after(symbol) for symbol in symbols}
        gross_after = sum(values_after.values())

        # Single name weight.
        name_weight_pct = values_after[order.symbol] / equity * 100.0
        if name_weight_pct > self.s.max_single_name_weight_pct:
            vetoes.append(
                f"single name weight {name_weight_pct:.2f}% for {order.symbol} exceeds "
                f"{self.s.max_single_name_weight_pct}% cap"
            )

        # Gross exposure cap.
        gross_pct = gross_after / equity * 100.0
        if gross_pct > self.s.max_gross_exposure_pct:
            vetoes.append(
                f"gross exposure {gross_pct:.2f}% exceeds {self.s.max_gross_exposure_pct}% cap"
            )

        # Leverage cap (gross-to-equity ratio).
        leverage = gross_after / equity
        if leverage > self.s.max_leverage:
            vetoes.append(
                f"leverage {leverage:.2f}x exceeds {self.s.max_leverage}x cap"
            )

        # Correlated cluster cap.
        cluster_pct = self._cluster_exposure_pct(order.symbol, symbols, values_after, account_state, equity)
        if cluster_pct > self.s.max_correlated_cluster_exposure_pct:
            vetoes.append(
                f"correlated cluster exposure {cluster_pct:.2f}% exceeds "
                f"{self.s.max_correlated_cluster_exposure_pct}% cap"
            )

        return vetoes

    def _cluster_exposure_pct(
        self,
        order_symbol: str,
        symbols: set[str],
        values_after: Mapping[str, float],
        account_state: AccountState,
        equity: float,
    ) -> float:
        """Combined exposure of the cluster containing the order's symbol.

        Names are grouped by absolute return correlation at or above the
        configured threshold (single-linkage via union-find). Names without
        enough overlapping return history stay in their own singleton cluster.
        """
        members = self._cluster_members(order_symbol, symbols, account_state)
        cluster_value = sum(values_after[symbol] for symbol in members)
        return cluster_value / equity * 100.0

    def _cluster_members(
        self, order_symbol: str, symbols: set[str], account_state: AccountState
    ) -> set[str]:
        symbol_list = sorted(symbols)
        parent = {symbol: symbol for symbol in symbol_list}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        threshold = self.s.correlation_cluster_threshold
        min_periods = self.s.correlation_min_periods
        returns = account_state.recent_returns
        for i, a in enumerate(symbol_list):
            for b in symbol_list[i + 1 :]:
                corr = self._abs_correlation(returns.get(a), returns.get(b), min_periods)
                if corr >= threshold:
                    union(a, b)

        root = find(order_symbol)
        return {symbol for symbol in symbol_list if find(symbol) == root}

    @staticmethod
    def _abs_correlation(
        a: Optional[list[float]], b: Optional[list[float]], min_periods: int
    ) -> float:
        """Absolute Pearson correlation over the overlapping tail, else 0.0."""
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        if n < min_periods:
            return 0.0
        x = np.asarray(a[-n:], dtype=float)
        y = np.asarray(b[-n:], dtype=float)
        if x.std() == 0 or y.std() == 0:
            return 0.0
        corr = np.corrcoef(x, y)[0, 1]
        if np.isnan(corr):
            return 0.0
        return abs(float(corr))

    # ------------------------------------------------- check: liquidity

    def _liquidity_vetoes(
        self, order: Order, account_state: AccountState, quantity: float
    ) -> list[str]:
        vetoes: list[str] = []
        adv = account_state.average_daily_volume.get(order.symbol)
        if adv is None or adv <= 0:
            vetoes.append(
                f"no average daily volume known for {order.symbol}: cannot assess liquidity"
            )
            return vetoes
        if adv < self.s.min_liquidity_adv:
            vetoes.append(
                f"{order.symbol} ADV {adv:.0f} is below the {self.s.min_liquidity_adv} minimum"
            )
        participation_pct = quantity / adv * 100.0
        if participation_pct > self.s.max_adv_participation_pct:
            vetoes.append(
                f"order is {participation_pct:.2f}% of {order.symbol} ADV, exceeding the "
                f"{self.s.max_adv_participation_pct}% participation cap"
            )
        return vetoes

    # ----------------------------------------------------------- build result

    def _decision(
        self,
        approved: bool,
        adjusted_quantity: float,
        vetoes: list[str],
        notes: list[str],
    ) -> RiskDecision:
        reasons = notes + vetoes
        if approved:
            summary = "approved by risk gate" if not notes else "; ".join(notes)
            if not reasons:
                reasons = ["approved by risk gate"]
        else:
            summary = "; ".join(vetoes)
        return RiskDecision(
            approved=approved,
            reason=summary,
            reasons=reasons,
            vetoes=vetoes,
            adjusted_quantity=adjusted_quantity,
            evaluator=_EVALUATOR,
        )


# ---------------------------------------------------------------------------
# Stable module-level entry point. The broker calls this with a built
# AccountState; it delegates to RiskGate, and fails closed without one.
# ---------------------------------------------------------------------------


def evaluate(order: Order, account_state: Optional[Any] = None) -> RiskDecision:
    """Evaluate an order through the real RiskGate.

    With an AccountState (directly, or under account_state["account_state"])
    this delegates to RiskGate. Called without one, it cannot assess risk, so it
    FAILS CLOSED and vetoes; it never approves an un-assessed order.
    """
    if isinstance(account_state, AccountState):
        return RiskGate().evaluate(order, account_state)
    if isinstance(account_state, Mapping) and isinstance(
        account_state.get("account_state"), AccountState
    ):
        return RiskGate().evaluate(order, account_state["account_state"])

    return RiskDecision.veto(
        "risk gate called without an AccountState: cannot assess risk, failing "
        "closed and vetoing the order",
        evaluator=_EVALUATOR,
    )


class RiskGuardrails:
    """Object form of the module-level entry point. Wraps RiskGate."""

    def evaluate(self, order: Order, account_state: Optional[Any] = None) -> RiskDecision:
        """See module-level `evaluate`."""
        return evaluate(order, account_state)
