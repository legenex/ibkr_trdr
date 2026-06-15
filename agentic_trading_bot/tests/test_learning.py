"""Tests for the self-learning primitives and the experiment runner.

The headline cases:
  - a candidate that only "wins" because many trials were charged must FAIL once
    the CUMULATIVE-trial deflation is applied (invariant 11),
  - a genuinely better candidate on fresh data (low cumulative trials) must PASS,
  - a burned holdout tranche must refuse to serve (invariant 12).

All data is deterministic synthetic.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.validator import (
    ValidationGate,
    deflated_sharpe_from_detail,
    deflated_sharpe_ratio,
)
from core.contracts import ExperimentResult, PreRegisteredCriteria
from learning.holdout_budget import BudgetExhaustedError, HoldoutBudget
from learning.trial_ledger import TrialLedger
from models.regime_detector import RegimeDetector


# --------------------------------------------------------------- strategies


class MovingAverageCross:
    """Long when fast MA > slow MA (or the inverse, for a losing baseline)."""

    def __init__(self, fast: int = 5, slow: int = 20, invert: bool = False) -> None:
        self.params = {"fast": fast, "slow": slow}
        self._invert = invert
        self.name = f"ma_{int(fast)}_{int(slow)}{'_inv' if invert else ''}"

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        close = bars["close"].astype(float)
        fast = close.rolling(int(self.params["fast"])).mean()
        slow = close.rolling(int(self.params["slow"])).mean()
        signal = pd.Series(np.where(fast > slow, 1.0, -1.0), index=bars.index)
        signal[fast.isna() | slow.isna()] = np.nan
        return -signal if self._invert else signal

    def clone(self, **overrides) -> "MovingAverageCross":
        params = dict(self.params)
        params.update(overrides)
        return MovingAverageCross(invert=self._invert, **params)

    def key_parameters(self) -> dict[str, float]:
        return {"fast": 2.0, "slow": 5.0}


def trending_data(n: int = 1500, seed: int = 7, segment: int = 35) -> pd.DataFrame:
    """Persistent, deterministic trend (the stage-5 PASS generator)."""
    rng = np.random.default_rng(seed)
    mu: list[float] = []
    for start in range(0, n, segment):
        drift = float(rng.choice([1.0, -1.0]) * rng.uniform(0.0012, 0.0018))
        mu.extend([drift] * min(segment, n - start))
    close = 100.0 * np.exp(np.cumsum(np.array(mu[:n]) + rng.normal(0.0, 0.0035, n)))
    index = pd.date_range("2016-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
         "volume": rng.uniform(5e6, 2e7, n)},
        index=index,
    )


def small_data(n: int = 200, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    index = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
         "volume": rng.uniform(1e6, 5e6, n)},
        index=index,
    )


CRITERIA = PreRegisteredCriteria(
    target_metric="oos_net_sharpe",
    min_improvement=0.5,
    dsr_threshold=0.95,
    require_candidate_gate_pass=True,
    max_drawdown_degradation=0.10,
    min_profit_factor_ratio=0.8,
    regime_degradation_tolerance=0.5,
)


# --------------------------------------------------------------- trial ledger


def test_ledger_accumulates_and_persists(tmp_path):
    ledger = TrialLedger(tmp_path / "learning.db")
    assert ledger.count("famA") == 0
    assert ledger.charge("famA", 3) == 3
    assert ledger.charge("famA", 2) == 5
    assert ledger.count("famA") == 5
    assert ledger.charge("famB") == 1
    ledger.close()

    reopened = TrialLedger(tmp_path / "learning.db")  # survives a restart
    assert reopened.count("famA") == 5
    assert reopened.all_counts() == {"famA": 5, "famB": 1}
    reopened.close()


# --------------------------------------------------------------- holdout budget


def test_budget_serves_then_burns_and_refuses(tmp_path):
    data = small_data()
    budget = HoldoutBudget(tmp_path / "budget.db", max_evaluations=2)
    ids = budget.reserve(data, n_tranches=2, label="v1")
    assert ids == ["v1-0", "v1-1"]

    first = budget.serve("v1-0")
    assert first.evaluations == 1 and first.burned is False
    assert len(first.bars) > 0
    second = budget.serve("v1-0")
    assert second.evaluations == 2 and second.burned is True

    # Third evaluation of a burned tranche is refused.
    with pytest.raises(BudgetExhaustedError):
        budget.serve("v1-0")

    assert budget.is_burned("v1-0") is True
    assert budget.is_burned("v1-1") is False

    meter = budget.remaining_budget()
    assert meter["total_remaining"] == 2  # only the untouched tranche remains
    assert meter["any_available"] is True
    budget.close()


def test_budget_unknown_tranche_refused(tmp_path):
    budget = HoldoutBudget(tmp_path / "budget.db")
    with pytest.raises(BudgetExhaustedError):
        budget.serve("nope")
    budget.close()


# --------------------------------------------------- cumulative deflation math


def test_cumulative_deflation_is_monotonic_in_trials():
    rng = np.random.default_rng(0)
    detail = deflated_sharpe_ratio(rng.normal(0.001, 0.01, 400), 1)
    at_one = deflated_sharpe_from_detail(detail, 1)
    at_many = deflated_sharpe_from_detail(detail, 1000)
    assert at_one >= at_many  # more trials can only lower the deflated Sharpe
    assert abs(at_one - detail["dsr"]) < 1e-9  # N=1 reproduces the original


# --------------------------------------------------------------- experiment


@pytest.fixture(scope="module")
def experiment_inputs():
    """Shared tranche, baseline (losing), and candidate (winning) trend strategy."""
    bars = trending_data()
    tranche = SimpleNamespace(bars=bars, tranche_id="v1-0")
    baseline = MovingAverageCross(5, 20, invert=True)  # inverse: loses on this trend
    candidate = MovingAverageCross(5, 20)  # genuine trend follower
    return tranche, baseline, candidate


def test_genuinely_better_candidate_passes(tmp_path, experiment_inputs):
    tranche, baseline, candidate = experiment_inputs
    gate = ValidationGate()
    ledger = TrialLedger(tmp_path / "learning.db")  # fresh family: low cumulative
    detector = RegimeDetector(n_iter=20, window=20, random_seed=42)

    result = gate.experiment(
        baseline, candidate, "trend_family", tranche, CRITERIA, ledger,
        trials_charged=1, detector=detector,
    )
    assert isinstance(result, ExperimentResult)
    assert result.passed is True, f"expected PASS, reasons: {result.reasons}"
    assert result.promotable is True
    assert result.cumulative_trials == 1
    assert result.cumulative_deflated_sharpe >= 0.95
    # The candidate genuinely improves the pre-registered target.
    ba = result.before_after["oos_net_sharpe"]
    assert ba["candidate"] > ba["baseline"]
    ledger.close()


def test_candidate_that_wins_only_on_extra_trials_fails(tmp_path, experiment_inputs):
    tranche, baseline, candidate = experiment_inputs
    gate = ValidationGate()
    ledger = TrialLedger(tmp_path / "learning.db")
    # Simulate that this candidate was cherry-picked from an enormous search:
    # the family already carries a huge cumulative trial count.
    ledger.charge("trend_family", 1_000_000)
    detector = RegimeDetector(n_iter=20, window=20, random_seed=42)

    result = gate.experiment(
        baseline, candidate, "trend_family", tranche, CRITERIA, ledger,
        trials_charged=1, detector=detector,
    )
    assert result.passed is False
    assert result.promotable is False
    # The candidate's OWN (per-run) deflated Sharpe is fine; only the cumulative
    # one collapses, and that is the reason it fails.
    assert result.per_run_deflated_sharpe >= 0.95
    assert result.cumulative_deflated_sharpe < 0.95
    assert any("cumulative deflated Sharpe" in r for r in result.reasons)
    # It still improved the target metric: the FAIL is purely the trial penalty.
    ba = result.before_after["oos_net_sharpe"]
    assert ba["candidate"] > ba["baseline"]
    ledger.close()
