"""Tests for the independent risk gate (risk.guardrails.RiskGate).

This is the module trusted with real money, so every veto path and the size
shrink logic are exercised explicitly. Each test isolates one limit by keeping
the others permissive, so a failure points straight at the offending check.
"""
from __future__ import annotations

import pytest

from config import Settings
from core.contracts import AccountState, Order, OrderSide, OrderType, Position, RiskDecision
from risk.guardrails import IS_PASSTHROUGH_STUB, RiskGate, evaluate

# Permissive baseline so that, by default, only the limit under test can trip.
DEFAULT_LIMITS = dict(
    risk_per_trade_pct=1.0,
    max_daily_drawdown_pct=3.0,
    max_weekly_drawdown_pct=6.0,
    max_gross_exposure_pct=100.0,
    max_single_name_weight_pct=50.0,
    max_correlated_cluster_exposure_pct=60.0,
    max_leverage=2.0,
    max_adv_participation_pct=5.0,
    min_liquidity_adv=1000,
    correlation_cluster_threshold=0.7,
    correlation_min_periods=5,
)

# Two return series with correlation ~1.0 (identical) and one that is constant
# (zero variance) so it never clusters with anything.
_CORRELATED = [0.01, -0.02, 0.015, 0.03, -0.01, 0.02, -0.005, 0.012, -0.018, 0.022]
_FLAT = [0.005] * 10


@pytest.fixture
def make_settings(tmp_path):
    def _make(**overrides) -> Settings:
        limits = dict(DEFAULT_LIMITS)
        limits.update(overrides)
        return Settings(
            _env_file=None,
            journal_dir=str(tmp_path / "journal"),
            data_cache_dir=str(tmp_path / "cache"),
            kill_switch_file=str(tmp_path / "KILL_SWITCH"),
            **limits,
        )

    return _make


@pytest.fixture
def gate(make_settings):
    def _make(**overrides) -> RiskGate:
        return RiskGate(make_settings(**overrides))

    return _make


def account(**overrides) -> AccountState:
    base = dict(
        equity=100_000.0,
        day_start_equity=100_000.0,
        week_start_equity=100_000.0,
        positions=[],
        prices={"AAPL": 100.0},
        average_daily_volume={"AAPL": 50_000_000.0},
        recent_returns={},
    )
    base.update(overrides)
    return AccountState(**base)


def order(**overrides) -> Order:
    base = dict(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=50,
        order_type=OrderType.LMT,
        limit_price=100.0,
        stop_price=95.0,
    )
    base.update(overrides)
    return Order(**base)


# --------------------------------------------------------------- clean pass


def test_clean_order_is_approved(gate):
    decision = gate().evaluate(order(), account())
    assert isinstance(decision, RiskDecision)
    assert decision.approved is True
    assert decision.adjusted_quantity == 50
    assert decision.vetoes == []
    assert decision.reasons  # always has at least a summary
    assert decision.evaluator == "risk-gate"


# --------------------------------------------------------------- kill switch


def test_kill_switch_vetoes_entry(gate, tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halt")
    decision = gate().evaluate(order(), account())
    assert decision.approved is False
    assert any("kill switch" in v for v in decision.vetoes)


def test_kill_switch_vetoes_even_exits(gate, tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("halt")
    state = account(
        positions=[Position(symbol="AAPL", quantity=100, avg_cost=100, market_price=100)]
    )
    exit_order = order(side=OrderSide.SELL, quantity=100, stop_price=None, order_type=OrderType.MKT, limit_price=None)
    decision = gate().evaluate(exit_order, state)
    assert decision.approved is False
    assert any("kill switch" in v for v in decision.vetoes)


# --------------------------------------------------------------- drawdown


def test_daily_drawdown_breaker_vetoes_entry(gate):
    state = account(equity=95_000.0, day_start_equity=100_000.0, week_start_equity=100_000.0)
    decision = gate().evaluate(order(), state)
    assert decision.approved is False
    assert any("daily drawdown" in v for v in decision.vetoes)


def test_weekly_drawdown_breaker_vetoes_entry(gate):
    # No daily drawdown (day start equals current), but the week is down 8%.
    state = account(equity=92_000.0, day_start_equity=92_000.0, week_start_equity=100_000.0)
    decision = gate().evaluate(order(), state)
    assert decision.approved is False
    assert any("weekly drawdown" in v for v in decision.vetoes)


def test_exit_allowed_during_drawdown(gate):
    state = account(
        equity=90_000.0,
        day_start_equity=100_000.0,
        week_start_equity=100_000.0,
        positions=[Position(symbol="AAPL", quantity=100, avg_cost=100, market_price=90)],
        prices={"AAPL": 90.0},
    )
    # A risk-reducing sell, even without a stop, is allowed while drawn down.
    exit_order = order(side=OrderSide.SELL, quantity=100, stop_price=None, order_type=OrderType.MKT, limit_price=None)
    decision = gate().evaluate(exit_order, state)
    assert decision.approved is True
    assert any("exit" in r for r in decision.reasons)


def test_drawdown_skipped_when_no_period_start(gate):
    # Without period-start equity the breaker cannot be computed and is skipped.
    state = account(day_start_equity=None, week_start_equity=None)
    decision = gate().evaluate(order(), state)
    assert decision.approved is True


# --------------------------------------------------------------- sizing


def test_missing_stop_is_vetoed(gate):
    # Market order with no stop: a position without a stop is a bug.
    naked = order(order_type=OrderType.MKT, limit_price=None, stop_price=None)
    decision = gate().evaluate(naked, account())
    assert decision.approved is False
    assert any("no protective stop" in v for v in decision.vetoes)


def test_no_reference_price_is_vetoed(gate):
    # Market order (no limit) and no known price: cannot size.
    state = account(prices={})
    unpriceable = order(order_type=OrderType.MKT, limit_price=None, stop_price=95.0)
    decision = gate().evaluate(unpriceable, state)
    assert decision.approved is False
    assert any("no reference price" in v for v in decision.vetoes)


def test_size_is_shrunk_to_risk_budget(gate):
    # risk budget = 100000 * 1% = 1000; stop distance = 5 -> max 200 shares.
    decision = gate().evaluate(order(quantity=1000), account())
    assert decision.approved is True
    assert decision.adjusted_quantity == 200
    assert any("shrunk" in r for r in decision.reasons)


def test_size_is_never_grown(gate):
    # A small order well within the risk budget is left untouched.
    decision = gate().evaluate(order(quantity=10), account())
    assert decision.approved is True
    assert decision.adjusted_quantity == 10
    assert all("shrunk" not in r for r in decision.reasons)


def test_size_exactly_at_budget_not_shrunk(gate):
    decision = gate().evaluate(order(quantity=200), account())
    assert decision.approved is True
    assert decision.adjusted_quantity == 200


def test_risk_rounding_to_zero_is_vetoed(gate):
    # A microscopic equity makes the risk-based size round below one share.
    state = account(equity=10.0, day_start_equity=10.0, week_start_equity=10.0)
    decision = gate().evaluate(order(quantity=10), state)
    assert decision.approved is False
    assert any("zero shares" in v for v in decision.vetoes)


def test_zero_equity_is_vetoed(gate):
    state = account(equity=0.0, day_start_equity=0.0, week_start_equity=0.0)
    decision = gate().evaluate(order(), state)
    assert decision.approved is False
    assert any("equity must be positive" in v for v in decision.vetoes)


# --------------------------------------------------------- exposure caps


def test_single_name_weight_cap_vetoes(gate):
    # Order is 10% of equity; cap is 5%.
    decision = gate(max_single_name_weight_pct=5.0).evaluate(order(quantity=100), account())
    assert decision.approved is False
    assert any("single name weight" in v for v in decision.vetoes)


def test_gross_exposure_cap_vetoes(gate):
    # Existing 90% in MSFT plus a 5% AAPL entry pushes gross to 95%, over a 90% cap.
    state = account(
        positions=[Position(symbol="MSFT", quantity=900, avg_cost=100, market_price=100)],
        prices={"AAPL": 100.0, "MSFT": 100.0},
    )
    g = gate(max_gross_exposure_pct=90.0, max_single_name_weight_pct=100.0, max_leverage=10.0,
             max_correlated_cluster_exposure_pct=100.0)
    decision = g.evaluate(order(quantity=50), state)
    assert decision.approved is False
    assert any("gross exposure" in v for v in decision.vetoes)


def test_leverage_cap_vetoes(gate):
    # Gross of 155% against a 1.0x leverage cap (other caps permissive).
    state = account(
        positions=[Position(symbol="MSFT", quantity=1500, avg_cost=100, market_price=100)],
        prices={"AAPL": 100.0, "MSFT": 100.0},
    )
    g = gate(max_leverage=1.0, max_gross_exposure_pct=200.0, max_single_name_weight_pct=100.0,
             max_correlated_cluster_exposure_pct=100.0)
    decision = g.evaluate(order(quantity=50), state)
    assert decision.approved is False
    assert any("leverage" in v for v in decision.vetoes)


def test_correlated_cluster_cap_vetoes(gate):
    # AAPL and MSFT are perfectly correlated; combined 40% exceeds a 30% cap.
    state = account(
        positions=[Position(symbol="MSFT", quantity=200, avg_cost=100, market_price=100)],
        prices={"AAPL": 100.0, "MSFT": 100.0},
        recent_returns={"AAPL": _CORRELATED, "MSFT": _CORRELATED},
    )
    g = gate(max_correlated_cluster_exposure_pct=30.0, max_single_name_weight_pct=100.0,
             max_gross_exposure_pct=200.0, max_leverage=10.0)
    decision = g.evaluate(order(quantity=200), state)
    assert decision.approved is False
    assert any("cluster" in v for v in decision.vetoes)


def test_uncorrelated_names_do_not_cluster(gate):
    # Same exposures, but uncorrelated returns: each name is its own cluster.
    state = account(
        positions=[Position(symbol="MSFT", quantity=200, avg_cost=100, market_price=100)],
        prices={"AAPL": 100.0, "MSFT": 100.0},
        recent_returns={"AAPL": _CORRELATED, "MSFT": _FLAT},
    )
    g = gate(max_correlated_cluster_exposure_pct=30.0, max_single_name_weight_pct=100.0,
             max_gross_exposure_pct=200.0, max_leverage=10.0)
    decision = g.evaluate(order(quantity=200), state)
    assert decision.approved is True


# --------------------------------------------------------- liquidity


def test_adv_below_minimum_is_vetoed(gate):
    state = account(average_daily_volume={"AAPL": 500_000.0})
    decision = gate(min_liquidity_adv=1_000_000).evaluate(order(), state)
    assert decision.approved is False
    assert any("below the" in v and "minimum" in v for v in decision.vetoes)


def test_participation_cap_is_vetoed(gate):
    # 200 shares against a 2000-share ADV is 10%, over a 5% participation cap.
    state = account(average_daily_volume={"AAPL": 2000.0})
    decision = gate(min_liquidity_adv=1000, max_adv_participation_pct=5.0).evaluate(
        order(quantity=200), state
    )
    assert decision.approved is False
    assert any("participation" in v for v in decision.vetoes)


def test_unknown_adv_is_vetoed(gate):
    state = account(average_daily_volume={})
    decision = gate().evaluate(order(), state)
    assert decision.approved is False
    assert any("average daily volume" in v for v in decision.vetoes)


# --------------------------------------------------------- composition


def test_multiple_vetoes_accumulate(gate):
    # No stop AND unknown ADV: both reasons present, gate does not short-circuit.
    state = account(average_daily_volume={})
    naked = order(order_type=OrderType.MKT, limit_price=None, stop_price=None)
    decision = gate().evaluate(naked, state)
    assert decision.approved is False
    assert any("no protective stop" in v for v in decision.vetoes)
    assert any("average daily volume" in v for v in decision.vetoes)
    assert len(decision.vetoes) >= 2


def test_adjusted_quantity_never_exceeds_request_even_on_veto(gate):
    decision = gate(max_single_name_weight_pct=5.0).evaluate(order(quantity=100), account())
    assert decision.adjusted_quantity <= 100


# --------------------------------------------------------- compatibility shim


def test_module_evaluate_delegates_to_real_gate_with_account_state():
    decision = evaluate(order(), account())
    assert decision.evaluator == "risk-gate"
    assert decision.approved is True


def test_module_evaluate_legacy_dict_path_still_approves():
    # The stage-2 broker passes a dict context; it must stay runnable.
    decision = evaluate(order(), {"intended_positions": {}})
    assert decision.approved is True
    assert IS_PASSTHROUGH_STUB is True
