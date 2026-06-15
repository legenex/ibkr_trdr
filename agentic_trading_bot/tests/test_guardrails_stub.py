"""Tests for the stage-2 passthrough guardrails stub.

These pin the STABLE interface (evaluate returns a RiskDecision) and the
passthrough behavior. Stage 3 will replace these expectations with real limit
checks, but the signature must not change.
"""
from __future__ import annotations

from core.contracts import Order, OrderSide, OrderType, RiskDecision
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


def test_stub_flag_is_set():
    assert IS_PASSTHROUGH_STUB is True


def test_evaluate_returns_risk_decision_and_approves():
    decision = evaluate(_order())
    assert isinstance(decision, RiskDecision)
    assert decision.approved is True


def test_evaluate_accepts_optional_context():
    decision = evaluate(_order(), {"equity": 100000})
    assert decision.approved is True
    assert decision.context.get("equity") == 100000
