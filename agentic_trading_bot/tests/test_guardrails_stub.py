"""Tests for the module-level risk gate entry point after merge step M1.

These pin the STABLE interface (`evaluate` returns a RiskDecision) and the
post-M1 behavior: with an AccountState it delegates to the real RiskGate, and
without one it fails closed. The passthrough that once approved un-assessed
orders is gone.
"""
from __future__ import annotations

from core.contracts import AccountState, Order, OrderSide, OrderType, RiskDecision
from risk.guardrails import IS_PASSTHROUGH_STUB, evaluate


def _order(**overrides) -> Order:
    base = dict(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LMT,
        limit_price=100.0,
        stop_price=95.0,
    )
    base.update(overrides)
    return Order(**base)


def _account(**overrides) -> AccountState:
    base = dict(
        equity=100_000.0,
        day_start_equity=100_000.0,
        week_start_equity=100_000.0,
        prices={"AAPL": 100.0},
        average_daily_volume={"AAPL": 50_000_000.0},
    )
    base.update(overrides)
    return AccountState(**base)


def test_passthrough_flag_is_cleared_after_m1():
    assert IS_PASSTHROUGH_STUB is False


def test_evaluate_delegates_to_real_gate_with_account_state():
    decision = evaluate(_order(), _account())
    assert isinstance(decision, RiskDecision)
    assert decision.approved is True
    assert decision.evaluator == "risk-gate"


def test_evaluate_without_account_state_fails_closed():
    decision = evaluate(_order())
    assert isinstance(decision, RiskDecision)
    assert decision.approved is False
    assert any("without an AccountState" in r for r in decision.vetoes)


def test_evaluate_with_legacy_dict_fails_closed():
    # A plain dict carries no AccountState; the gate refuses rather than guesses.
    decision = evaluate(_order(), {"equity": 100000})
    assert decision.approved is False
