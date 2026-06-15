"""Tests for the HMM regime detector.

The two guarantees that matter most are exercised explicitly:
  - the state-to-label ordering logic, and
  - strict causality: a future bar can never change a past regime label.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.contracts import Regime, RegimeState
from models.regime_detector import RegimeDetector, plot_regime_bands


def make_df(n: int = 300, seed: int = 0) -> pd.DataFrame:
    """Deterministic OHLCV with a bearish first half and a bullish second half."""
    rng = np.random.default_rng(seed)
    rets = np.concatenate(
        [
            rng.normal(-0.0015, 0.020, n // 2),  # bearish, higher vol
            rng.normal(0.0015, 0.008, n - n // 2),  # bullish, lower vol
        ]
    )
    close = 100.0 * np.exp(np.cumsum(rets))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    index = pd.date_range("2022-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
        },
        index=index,
    )


@pytest.fixture(scope="module")
def fitted() -> tuple[RegimeDetector, pd.DataFrame]:
    df = make_df()
    detector = RegimeDetector(n_states=5, window=20, random_seed=42)
    detector.fit(df)
    return detector, df


# --------------------------------------------------------------- ordering


def test_rank_orders_states_by_mean_return():
    means = {0: 0.02, 1: -0.03, 2: 0.0, 3: 0.01, 4: -0.01}
    vols = {k: 0.1 for k in means}
    mapping = RegimeDetector._rank_states_to_labels(means, vols)
    assert mapping[1] is Regime.CRASH
    assert mapping[4] is Regime.BEAR
    assert mapping[2] is Regime.NEUTRAL
    assert mapping[3] is Regime.BULL
    assert mapping[0] is Regime.EUPHORIA


def test_rank_tiebreaks_on_volatility():
    # Equal mean returns: higher volatility ranks more bullish.
    means = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    vols = {0: 0.05, 1: 0.04, 2: 0.03, 3: 0.02, 4: 0.01}
    mapping = RegimeDetector._rank_states_to_labels(means, vols)
    assert mapping[4] is Regime.CRASH  # lowest vol
    assert mapping[0] is Regime.EUPHORIA  # highest vol


def test_full_label_set_is_covered_when_five_states():
    means = {0: -2.0, 1: -1.0, 2: 0.0, 3: 1.0, 4: 2.0}
    vols = {k: 0.1 for k in means}
    mapping = RegimeDetector._rank_states_to_labels(means, vols)
    assert set(mapping.values()) == set(Regime)


# --------------------------------------------------------------- fit output


def test_fit_returns_one_state_per_feature_bar(fitted):
    detector, df = fitted
    feats = detector.engineer_features(df)
    states = detector.predict_causal(df)
    assert len(states) == len(feats)
    assert all(isinstance(s, RegimeState) for s in states)


def test_probabilities_are_a_distribution_over_labels(fitted):
    detector, df = fitted
    state = detector.predict_causal(df)[-1]
    assert set(state.probabilities) == {r.value for r in Regime}
    assert state.probabilities[state.regime.value] == pytest.approx(state.confidence)
    assert sum(state.probabilities.values()) == pytest.approx(1.0, abs=1e-6)


def test_detector_separates_the_two_halves(fitted):
    detector, df = fitted
    states = detector.predict_causal(df)
    # The bullish second half should average a higher regime rank than the first.
    ranks = [s.regime.rank for s in states]
    first_half = np.mean(ranks[: len(ranks) // 2])
    second_half = np.mean(ranks[len(ranks) // 2 :])
    assert second_half > first_half


# --------------------------------------------------------------- causality


def test_causal_labels_are_prefix_invariant(fitted):
    detector, df = fitted
    full = detector.predict_causal(df)
    prefix = detector.predict_causal(df.iloc[:250])
    m = len(prefix)
    assert m > 0
    # The first m causal labels must match: filtering at t ignores bars after t.
    assert [s.regime for s in full[:m]] == [s.regime for s in prefix]


def test_future_bar_does_not_change_a_past_label(fitted):
    detector, df = fitted
    base = detector.predict_causal(df)

    # Drastically alter a late ("future") bar's close.
    modified = df.copy()
    p = 280
    cutoff_ts = modified.index[p]
    close_col = modified.columns.get_loc("close")
    modified.iloc[p, close_col] = float(modified.iloc[p, close_col]) * 1.5

    after = detector.predict_causal(modified)

    base_before = [s for s in base if pd.Timestamp(s.ts_utc) < cutoff_ts]
    after_before = [s for s in after if pd.Timestamp(s.ts_utc) < cutoff_ts]
    assert len(base_before) == len(after_before) > 0
    for b, a in zip(base_before, after_before):
        assert b.regime == a.regime
        for label in b.probabilities:
            assert b.probabilities[label] == pytest.approx(a.probabilities[label], abs=1e-9)


# --------------------------------------------------------------- persistence


def test_save_and_load_round_trip(fitted, tmp_path):
    detector, df = fitted
    path = detector.save(tmp_path / "regime_model.pkl")
    assert path.exists()

    reloaded = RegimeDetector.load(path)
    original = detector.predict_causal(df)
    restored = reloaded.predict_causal(df)
    assert [s.regime for s in original] == [s.regime for s in restored]
    assert reloaded._state_to_label == detector._state_to_label


def test_predict_before_fit_raises():
    with pytest.raises(RuntimeError):
        RegimeDetector().predict_causal(make_df(60))


# --------------------------------------------------------------- plotting


def test_plot_regime_bands_returns_figure(fitted):
    import plotly.graph_objects as go

    detector, df = fitted
    states = detector.predict_causal(df)
    fig = plot_regime_bands(df["close"], states)
    assert isinstance(fig, go.Figure)
    assert len(fig.data) >= 1  # at least the price line
