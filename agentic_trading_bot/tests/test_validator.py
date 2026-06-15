"""Tests for the backtest engine and the validation gate.

The two headline cases the gate exists for:
  - a deliberately overfit strategy on noise that the gate must FAIL, and
  - a known-good synthetic trend signal the gate must PASS.

Plus unit tests for no-lookahead, the cost model, the Deflated Sharpe Ratio, and
the purged/embargoed cross-validation helper.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.validator import (
    BacktestEngine,
    CostModel,
    ValidationGate,
    deflated_sharpe_ratio,
    purged_kfold_indices,
)
from models.regime_detector import RegimeDetector


# --------------------------------------------------------------- strategies


class MovingAverageCross:
    """Long when the fast MA is above the slow MA, short otherwise."""

    def __init__(self, fast: int = 10, slow: int = 40) -> None:
        self.name = f"ma_cross_{int(fast)}_{int(slow)}"
        self.params = {"fast": fast, "slow": slow}

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        fast = close.rolling(int(self.params["fast"])).mean()
        slow = close.rolling(int(self.params["slow"])).mean()
        signal = pd.Series(np.where(fast > slow, 1.0, -1.0), index=bars.index)
        signal[fast.isna() | slow.isna()] = np.nan
        return signal

    def clone(self, **overrides) -> "MovingAverageCross":
        params = dict(self.params)
        params.update(overrides)
        return MovingAverageCross(**params)

    def key_parameters(self) -> dict[str, float]:
        return {"fast": 2.0, "slow": 5.0}


class MomentumLookback:
    """Long if price rose over the lookback, short otherwise."""

    def __init__(self, lookback: int = 20) -> None:
        self.name = f"momentum_{int(lookback)}"
        self.params = {"lookback": lookback}

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        momentum = close - close.shift(int(self.params["lookback"]))
        signal = pd.Series(np.where(momentum > 0, 1.0, -1.0), index=bars.index)
        signal[momentum.isna()] = np.nan
        return signal

    def clone(self, **overrides) -> "MomentumLookback":
        params = dict(self.params)
        params.update(overrides)
        return MomentumLookback(**params)

    def key_parameters(self) -> dict[str, float]:
        return {"lookback": 5.0}


# --------------------------------------------------------------- data


def trending_data(n: int = 1500, seed: int = 7, segment: int = 35) -> pd.DataFrame:
    """A genuine, persistent trend signal: slowly switching drift plus low noise.

    Drift flips sign every `segment` bars, so a trend follower trades often
    enough to clear the minimum-trade floor while still having a real edge.
    """
    rng = np.random.default_rng(seed)
    mu: list[float] = []
    for start in range(0, n, segment):
        drift = float(rng.choice([1.0, -1.0]) * rng.uniform(0.0012, 0.0018))
        mu.extend([drift] * min(segment, n - start))
    mu_arr = np.array(mu[:n])
    noise = rng.normal(0.0, 0.0035, n)
    close = 100.0 * np.exp(np.cumsum(mu_arr + noise))
    index = pd.date_range("2016-01-01", periods=n, freq="B", tz="UTC")
    volume = rng.uniform(5_000_000, 20_000_000, n)
    return pd.DataFrame(
        {"open": close, "high": close * 1.005, "low": close * 0.995, "close": close, "volume": volume},
        index=index,
    )


def noise_data(n: int = 1500, seed: int = 3) -> pd.DataFrame:
    """A driftless random walk: no edge for anything to find."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, 0.01, n)
    close = 100.0 * np.exp(np.cumsum(rets))
    index = pd.date_range("2016-01-01", periods=n, freq="B", tz="UTC")
    volume = rng.uniform(5_000_000, 20_000_000, n)
    return pd.DataFrame(
        {"open": close, "high": close * 1.005, "low": close * 0.995, "close": close, "volume": volume},
        index=index,
    )


# --------------------------------------------------------------- engine units


def test_costs_make_net_no_better_than_gross():
    bars = trending_data(500)
    signals = MovingAverageCross(10, 40).generate_signals(bars)
    result = BacktestEngine(CostModel()).run(bars, signals)
    assert result.metrics["net"]["total_return"] <= result.metrics["gross"]["total_return"]


def test_zero_cost_makes_net_equal_gross():
    bars = trending_data(500)
    signals = MovingAverageCross(10, 40).generate_signals(bars)
    zero = CostModel(
        commission_per_share=0.0,
        commission_min=0.0,
        half_spread_bps=0.0,
        slippage_base_bps=0.0,
        slippage_impact=0.0,
        borrow_rate_annual=0.0,
    )
    result = BacktestEngine(zero).run(bars, signals)
    assert result.metrics["net"]["total_return"] == pytest.approx(
        result.metrics["gross"]["total_return"], rel=1e-9
    )


def test_engine_is_causal_no_lookahead():
    bars = trending_data(300)
    signals = MovingAverageCross(10, 40).generate_signals(bars)
    engine = BacktestEngine(CostModel())
    base = engine.run(bars, signals)

    modified = bars.copy()
    modified.iloc[-1, modified.columns.get_loc("open")] *= 1.3  # change a future bar
    after = engine.run(modified, signals)

    # Altering the last open can only affect the final interval(s); everything
    # earlier must be untouched. Compare the equity curve excluding the tail.
    base_net = base.equity_curve["net"].to_numpy()
    after_net = after.equity_curve["net"].to_numpy()
    assert np.allclose(base_net[:-2], after_net[:-2])


# --------------------------------------------------------------- DSR + CV


def test_dsr_penalizes_more_trials():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.0008, 0.01, 500)  # weak positive edge
    one = deflated_sharpe_ratio(returns, 1)["dsr"]
    fifty = deflated_sharpe_ratio(returns, 50)["dsr"]
    assert fifty < one


def test_purged_kfold_has_no_leakage():
    n, splits, embargo_pct = 100, 5, 0.05
    folds = purged_kfold_indices(n, splits, embargo_pct)
    assert len(folds) == splits
    embargo = int(n * embargo_pct)
    for train, test in folds:
        assert set(train.tolist()).isdisjoint(set(test.tolist()))
        low, high = test[0] - embargo, test[-1] + embargo
        # No training index sits inside the purged+embargoed band around the test.
        assert all(not (low <= ix <= high) for ix in train.tolist())


# --------------------------------------------------------------- the gate


@pytest.fixture(scope="module")
def fast_detector() -> RegimeDetector:
    return RegimeDetector(n_iter=30, window=20, random_seed=42)


def test_known_good_strategy_passes(fast_detector):
    bars = trending_data()
    gate = ValidationGate()
    result = gate.validate(MovingAverageCross(5, 20), bars, n_trials=1, detector=fast_detector)

    assert result.passed, f"expected PASS, reasons: {result.reasons}"
    assert result.approvable is True
    assert result.metrics["out_of_sample"]["net"]["sharpe"] > 0
    assert result.deflated_sharpe >= 0.95
    assert result.regime_breakdown  # regime-conditional breakdown is populated
    assert result.metrics["out_of_sample"]["net"]  # gross and net reported


def test_overfit_strategy_on_noise_fails(fast_detector):
    bars = noise_data()
    gate = ValidationGate()
    # Pretend 50 lookbacks were scanned and this one cherry-picked.
    result = gate.validate(MomentumLookback(20), bars, n_trials=50, detector=fast_detector)

    assert result.passed is False
    assert result.approvable is False
    assert result.reasons  # explicit FAIL reasons present
    # The deflation and/or the out-of-sample test should be among the reasons.
    joined = " ".join(result.reasons).lower()
    assert ("deflated sharpe" in joined) or ("out-of-sample net sharpe" in joined)


def test_fail_result_cannot_be_marked_approvable(fast_detector):
    bars = noise_data()
    gate = ValidationGate()
    result = gate.validate(MomentumLookback(20), bars, n_trials=50, detector=fast_detector)
    # approvable is derived from passed; a FAIL is structurally never approvable.
    assert result.approvable == result.passed == False  # noqa: E712
