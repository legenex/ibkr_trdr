"""Tests for the reference strategies and the strategy/gate interface."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.validator import ValidationGate
from core.contracts import Signal
from strategies.base import GateAdapter, Strategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.trend_breakout import TrendBreakoutStrategy


def make_bars(n: int = 400, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0003, 0.012, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    index = pd.date_range("2018-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close * (1 + rng.uniform(0, 0.01, n)),
            "low": close * (1 - rng.uniform(0, 0.01, n)),
            "close": close,
            "volume": rng.uniform(5_000_000, 20_000_000, n),
        },
        index=index,
    )


ALL_STRATEGIES = [TrendBreakoutStrategy, MeanReversionStrategy]


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_conforms_to_strategy_protocol(cls):
    strat = cls(symbol="AAPL")
    assert isinstance(strat, Strategy)
    assert strat.spec.name
    assert strat.spec.category in {"breakout", "mean_reversion"}
    assert strat.spec.key_parameters  # advertises sensitivity steps


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_signals_are_causal_series_with_warmup(cls):
    bars = make_bars()
    signals = cls(symbol="AAPL").generate_signals(bars)
    assert len(signals) == len(bars)
    # Warmup is represented as None at the start, never backfilled.
    assert signals.iloc[0] is None
    non_null = [s for s in signals if s is not None]
    assert non_null and all(isinstance(s, Signal) for s in non_null)


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_every_nonflat_signal_carries_a_valid_stop(cls):
    bars = make_bars()
    signals = cls(symbol="AAPL").generate_signals(bars)
    seen_position = False
    for sig in signals:
        if sig is None or sig.is_flat:
            continue
        seen_position = True
        assert sig.stop_price is not None
        # Stop sits on the protective side of the reference price.
        if sig.target_weight > 0:
            assert sig.stop_price < sig.reference_price
        else:
            assert sig.stop_price > sig.reference_price
    assert seen_position, "strategy never took a position on this data"


def test_flat_signal_has_no_stop():
    # Construct directly: a flat target must not require a stop.
    flat = Signal(ts_utc="2020-01-01T00:00:00+00:00", symbol="AAPL", target_weight=0.0)
    assert flat.is_flat and flat.stop_price is None


def test_nonflat_signal_without_stop_is_rejected():
    with pytest.raises(ValueError):
        Signal(ts_utc="2020-01-01T00:00:00+00:00", symbol="AAPL", target_weight=1.0)


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_clone_overrides_params_and_preserves_symbol(cls):
    strat = cls(symbol="MSFT")
    key = next(iter(strat.key_parameters()))
    bumped = strat.clone(**{key: strat.params[key] + strat.key_parameters()[key]})
    assert bumped.params[key] != strat.params[key]
    assert bumped.symbol == "MSFT"


def test_regime_filter_flattens_mean_reversion_in_crash():
    bars = make_bars()
    strat = MeanReversionStrategy(symbol="AAPL")
    # Force every bar into the Crash regime: the strategy must go flat.
    regime = pd.Series("Crash", index=bars.index)
    weights = strat.target_weights(bars, regime=regime)
    active = weights.dropna()
    assert (active == 0.0).all()


@pytest.mark.parametrize("cls", ALL_STRATEGIES)
def test_runs_through_validation_gate(cls):
    bars = make_bars(800)
    gate = ValidationGate()
    # No detector passed: the gate builds a small one. Assert structure, not verdict.
    result = gate.validate(GateAdapter(cls(symbol="AAPL")), bars, n_trials=10)
    assert isinstance(result.passed, bool)
    assert result.approvable == result.passed
    for period in ("in_sample", "out_of_sample", "full"):
        assert "gross" in result.metrics[period]
        assert "net" in result.metrics[period]
    assert isinstance(result.reasons, list)
