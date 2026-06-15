"""Integration test: orders flow through the broker into the REAL risk gate.

Unlike test_ibkr_client.py, which monkeypatches `client._risk_evaluate` with
lambdas to isolate the broker's plumbing, these tests leave the real gate wired.
They submit a sample order through the broker against a mocked ib_async object
(FakeIB) and assert that merge step M1 actually happened: the broker builds an
AccountState from broker-reported equity and data-source history, hands it to
risk.guardrails.RiskGate, and obeys the verdict.

The discriminator that the REAL gate ran (not the old passthrough, and not a test
stub) is `evaluator == "risk-gate"` on an APPROVED decision: the post-M1
fail-closed module path also reports "risk-gate" but never approves, so an
approval carrying that evaluator can only come from RiskGate evaluating a real
AccountState.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.contracts import Order, OrderSide, OrderType
from testsupport.fakes import FakeIB, funded_account, liquid_daily_bars


def _long_position(symbol: str = "AAPL", quantity: float = 100.0, avg_cost: float = 100.0):
    """A broker-reported long position, shaped as FakeIB.positions() returns them."""
    return SimpleNamespace(
        account="DU1",
        contract=SimpleNamespace(symbol=symbol),
        position=quantity,
        avgCost=avg_cost,
    )


def _entry_with_stop(**overrides) -> Order:
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


def _events(audit, event_type: str):
    return [e for e in audit.read_all() if e.event_type == event_type]


# --------------------------------------------------------------- gate runs


def test_real_gate_runs_and_approves_a_clean_order(make_client):
    # Funded, liquid FakeIB defaults: a small priced order with a stop clears the
    # real gate. The approval proves RiskGate evaluated a real AccountState.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)

    result = client.place_order(_entry_with_stop(quantity=10))

    assert result.accepted is True
    assert result.risk_decision.evaluator == "risk-gate"
    assert result.risk_decision.approved is True
    # The order really went to the broker: entry + protective stop.
    assert len(ib.placed) == 2
    # And the decision was assessed against real equity, not a passthrough.
    decision_events = _events(audit, "RISK_DECISION")
    assert decision_events
    assert decision_events[-1].payload["evaluator"] == "risk-gate"
    assert decision_events[-1].payload["equity"] == 100_000.0


# --------------------------------------------------------------- gate shrinks


def test_real_gate_shrinks_oversized_order_before_sending(make_client):
    # Equity 100k, risk-per-trade 0.5% -> $500 budget; stop distance is $5, so the
    # gate caps the position at 100 shares. A 1000-share request is shrunk, not
    # vetoed, and BOTH legs are submitted at the trimmed size.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)

    result = client.place_order(_entry_with_stop(quantity=1000))

    assert result.accepted is True
    assert result.risk_decision.evaluator == "risk-gate"
    assert result.risk_decision.adjusted_quantity == 100
    assert any("shrunk" in r for r in result.risk_decision.reasons)

    # The broker sent the SHRUNK size to the mocked ib_async, on entry and stop.
    assert len(ib.placed) == 2
    for _contract, placed in ib.placed:
        assert placed.totalQuantity == 100
    assert client._intended_positions["AAPL"] == 100

    decision = _events(audit, "RISK_DECISION")[-1].payload
    assert decision["requested_quantity"] == 1000
    assert decision["adjusted_quantity"] == 100


# ----------------------------------------------- gate vetoes, nothing is sent


def test_real_gate_vetoes_illiquid_order_and_sends_nothing(make_client):
    # Drive a genuine veto through real account state: history shows an average
    # daily volume far below the 1,000,000-share liquidity floor.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    ib._bars = liquid_daily_bars(volume=100_000.0)  # ADV 100k < 1,000,000 minimum

    result = client.place_order(_entry_with_stop(quantity=10))

    assert result.accepted is False
    assert result.risk_decision.evaluator == "risk-gate"
    assert any("minimum" in v for v in result.risk_decision.vetoes)
    # The non-negotiable part: a vetoed order is NEVER sent to the broker.
    assert ib.placed == []
    assert result.ib_order_ids == []
    assert "AAPL" not in client._intended_positions
    assert _events(audit, "RISK_VETO")


def test_real_gate_veto_is_driven_by_real_account_equity(make_client):
    # A different real-state veto: with almost no equity, the risk-based size
    # rounds below one share, so the gate refuses to open any position.
    ib = FakeIB()
    ib._account = [
        # SimpleNamespace-style summary row; FakeIB.accountSummary returns this list.
        type("Row", (), {"account": "DU1", "tag": "NetLiquidation", "value": "10", "currency": "USD"})()
    ]
    client, ib, audit = make_client(ib=ib)
    client.connect(base_backoff=0)

    result = client.place_order(_entry_with_stop(quantity=10))

    assert result.accepted is False
    assert result.risk_decision.evaluator == "risk-gate"
    assert any("zero shares" in v for v in result.risk_decision.vetoes)
    assert ib.placed == []


def test_bracket_order_also_passes_through_the_real_gate(make_client):
    # The bracket path uses the same gate; a veto must stop all three legs.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    ib._bars = liquid_daily_bars(volume=100_000.0)  # illiquid -> veto

    order = _entry_with_stop(target_price=110.0)
    result = client.place_bracket_order(order)

    assert result.accepted is False
    assert result.risk_decision.evaluator == "risk-gate"
    assert ib.placed == []
    assert _events(audit, "RISK_VETO")


# ----------------------------------------- drawdown circuit breakers (broker)
#
# These exercise the full M1 path: the BROKER builds the AccountState, including
# the equity baselines it tracks itself (set_equity_baselines), and reports the
# drawn-down NetLiquidation. The gate is the real wired RiskGate, evaluated end to
# end through place_order, not in isolation.


def test_daily_drawdown_breaker_vetoes_new_entry(make_client):
    # Day opened at 100k; the account is now drawn down past the daily limit.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    day_start = 100_000.0
    limit = client.settings.max_daily_drawdown_pct
    drawn_down = day_start * (1 - (limit + 2) / 100.0)  # comfortably past the limit
    ib._account = funded_account(drawn_down)
    client.set_equity_baselines(day_start=day_start)  # week baseline left unset

    result = client.place_order(_entry_with_stop(quantity=10))

    assert result.accepted is False
    assert result.risk_decision.evaluator == "risk-gate"
    assert any("daily drawdown" in v for v in result.risk_decision.vetoes)
    # New risk is blocked and nothing is sent to the broker.
    assert ib.placed == []
    assert "AAPL" not in client._intended_positions
    assert _events(audit, "RISK_VETO")


def test_drawdown_breaker_still_allows_a_risk_reducing_exit(make_client):
    # Same drawn-down day, but now there is an open AAPL long and the order
    # reduces it. The breaker blocks new risk; it must never trap an open
    # position, so this exit is approved and reaches the broker.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    day_start = 100_000.0
    limit = client.settings.max_daily_drawdown_pct
    drawn_down = day_start * (1 - (limit + 2) / 100.0)
    ib._account = funded_account(drawn_down)
    ib._positions = [_long_position("AAPL", quantity=100.0)]
    client.set_equity_baselines(day_start=day_start)

    # A sell that reduces the long. place_order mandates an attached stop, so the
    # exit carries one; the gate sees a risk-reducing exit and bypasses entries.
    exit_order = Order(
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=50,
        order_type=OrderType.LMT,
        limit_price=100.0,
        stop_price=105.0,
    )
    result = client.place_order(exit_order)

    assert result.accepted is True
    assert result.risk_decision.evaluator == "risk-gate"
    assert any("exit" in r for r in result.risk_decision.reasons)
    # The exit really reached the broker (entry leg + protective stop).
    assert len(ib.placed) == 2
    assert client._intended_positions["AAPL"] == -50
    assert _events(audit, "ORDER_SUBMITTED")


def test_weekly_drawdown_breaker_vetoes_new_entry(make_client):
    # Week opened at 100k; the account is now drawn down past the weekly limit.
    # The daily baseline is left unset so only the weekly breaker is in play.
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    week_start = 100_000.0
    limit = client.settings.max_weekly_drawdown_pct
    drawn_down = week_start * (1 - (limit + 2) / 100.0)
    ib._account = funded_account(drawn_down)
    client.set_equity_baselines(week_start=week_start)  # day baseline left unset

    result = client.place_order(_entry_with_stop(quantity=10))

    assert result.accepted is False
    assert result.risk_decision.evaluator == "risk-gate"
    assert any("weekly drawdown" in v for v in result.risk_decision.vetoes)
    assert ib.placed == []
    assert "AAPL" not in client._intended_positions
    assert _events(audit, "RISK_VETO")
