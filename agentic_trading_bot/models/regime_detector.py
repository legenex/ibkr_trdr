"""Gaussian HMM market regime detector.

A GaussianHMM (hmmlearn) is fit on a small set of engineered, strictly causal
features (returns, realized volatility, a volume z-score, and a trend slope).
The hidden states are then mapped onto an ordered set of human labels
(Crash, Bear, Neutral, Bull, Euphoria) by ranking the states on their mean
return, with realized volatility as the tie-breaker.

Causality is the property that matters here. Two inference paths are offered:

  - `fit(df)` runs Baum-Welch over the FULL history and returns SMOOTHED labels
    (forward-backward). These use future bars and are for research only.
  - `predict_causal(df)` returns FILTERED labels: the regime at bar t is computed
    by the forward algorithm from observations up to and including t only. A
    future bar can never change a past filtered label. This is the path used for
    live trading and for backtests. `predict_last(df)` is the online convenience
    that returns just the final bar's regime.

The fitted model (HMM, scaler, and state-to-label map) is persisted to the
journal directory and reloaded with `load`.
"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

from config import Settings, settings as default_settings
from core.contracts import ORDERED_REGIMES, Regime, RegimeState
from utils.logging import get_logger


def _ts_iso(ts: object) -> str:
    """Render an index value as an ISO-8601 string."""
    try:
        return pd.Timestamp(ts).isoformat()
    except (ValueError, TypeError):
        return str(ts)


def _rolling_slope(series: pd.Series, window: int) -> pd.Series:
    """Causal rolling least-squares slope of `series` over `window` bars.

    Uses only the trailing window ending at each bar, so it never looks ahead.
    """
    x = np.arange(window, dtype=float)
    x_centered = x - x.mean()
    denom = float((x_centered ** 2).sum())

    def slope(y: np.ndarray) -> float:
        y_centered = y - y.mean()
        return float((x_centered * y_centered).sum() / denom)

    return series.rolling(window).apply(slope, raw=True)


class RegimeDetector:
    """Fit, label, and causally infer market regimes with a Gaussian HMM."""

    FEATURE_NAMES = ["ret", "vol", "vol_z", "slope"]
    ARTIFACT_VERSION = 1
    DEFAULT_ARTIFACT_NAME = "regime_model.pkl"

    def __init__(
        self,
        n_states: int = 5,
        window: int = 20,
        covariance_type: str = "diag",
        n_iter: int = 200,
        settings: Settings = default_settings,
        random_seed: Optional[int] = None,
    ) -> None:
        """Create a (not yet fitted) detector.

        Args:
            n_states: Number of hidden HMM states. Five aligns one-to-one with
                the five ordered labels; other counts are mapped by rank.
            window: Lookback window (bars) for volatility, volume z-score, slope.
            covariance_type: GaussianHMM covariance type. "diag" keeps causal
                emission scoring simple and is the default.
            n_iter: Max Baum-Welch iterations.
            settings: Config object (for the journal path and the central seed).
            random_seed: Overrides settings.random_seed for reproducibility.
        """
        self.n_states = n_states
        self.window = window
        self.covariance_type = covariance_type
        self.n_iter = n_iter
        self.settings = settings
        self.random_seed = random_seed if random_seed is not None else settings.random_seed

        self.model: Optional[GaussianHMM] = None
        self.scaler: Optional[StandardScaler] = None
        self._state_to_label: dict[int, Regime] = {}
        self.log = get_logger(__name__)

    # --------------------------------------------------------------- features

    def engineer_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Build the strictly causal feature frame from OHLCV bars.

        Every feature at bar t uses only data at or before t (backward rolling
        windows and lagged differences). Warmup rows that are not fully defined
        are dropped, never backfilled.
        """
        data = df.copy()
        data.columns = [str(c).lower() for c in data.columns]
        if "close" not in data.columns:
            raise ValueError("regime features require a 'close' column")

        close = data["close"].astype(float)
        log_close = np.log(close)
        log_return = log_close.diff()

        volume = data["volume"].astype(float) if "volume" in data.columns else pd.Series(
            0.0, index=data.index
        )
        vol_mean = volume.rolling(self.window).mean()
        vol_std = volume.rolling(self.window).std()
        volume_z = (volume - vol_mean) / vol_std

        feats = pd.DataFrame(
            {
                "ret": log_return,
                "vol": log_return.rolling(self.window).std(),
                "vol_z": volume_z,
                "slope": _rolling_slope(log_close, self.window),
            },
            index=data.index,
        )[self.FEATURE_NAMES]

        # Constant-volume windows make vol_z infinite; treat as undefined and drop.
        feats = feats.replace([np.inf, -np.inf], np.nan).dropna()
        return feats

    # ------------------------------------------------------------------- fit

    def fit(self, df: pd.DataFrame) -> list[RegimeState]:
        """Fit the HMM on full history and return SMOOTHED (research) labels.

        The labels returned here use the full sequence (forward-backward) and are
        NOT causal. Use `predict_causal` for any decision that trades.
        """
        feats = self.engineer_features(df)
        min_rows = self.n_states + self.window
        if len(feats) < min_rows:
            raise ValueError(
                f"need at least {min_rows} feature rows to fit {self.n_states} states, "
                f"got {len(feats)}"
            )

        self.scaler = StandardScaler().fit(feats.values)
        scaled = self.scaler.transform(feats.values)

        self.model = GaussianHMM(
            n_components=self.n_states,
            covariance_type=self.covariance_type,
            n_iter=self.n_iter,
            random_state=self.random_seed,
        )
        self.model.fit(scaled)

        states = self.model.predict(scaled)
        self._state_to_label = self._build_state_label_map(states, feats["ret"].values)
        self.log.info(
            "regime_model_fit",
            rows=len(feats),
            states=self.n_states,
            mapping={int(s): r.value for s, r in self._state_to_label.items()},
        )

        smoothed = self.model.predict_proba(scaled)  # non-causal, research only
        return self._to_regime_states(feats.index, smoothed)

    # ------------------------------------------------------------- inference

    def predict_causal(self, df: pd.DataFrame) -> list[RegimeState]:
        """Return FILTERED, strictly causal regime states for every feature bar.

        The label at bar t depends only on observations up to and including t,
        via the forward algorithm. Future bars never alter a past label.
        """
        self._require_fitted()
        feats = self.engineer_features(df)
        if feats.empty:
            return []
        scaled = self.scaler.transform(feats.values)  # type: ignore[union-attr]
        log_emission = self._log_emission(scaled)
        filtered = self._forward_filter(log_emission)
        return self._to_regime_states(feats.index, filtered)

    def predict_last(self, df: pd.DataFrame) -> Optional[RegimeState]:
        """Online convenience: the causal regime for the most recent bar."""
        states = self.predict_causal(df)
        return states[-1] if states else None

    # ---------------------------------------------------- state to label map

    def _build_state_label_map(
        self, states: np.ndarray, returns: np.ndarray
    ) -> dict[int, Regime]:
        means: dict[int, float] = {}
        vols: dict[int, float] = {}
        for state in range(self.n_states):
            mask = states == state
            if mask.any():
                means[state] = float(np.mean(returns[mask]))
                vols[state] = float(np.std(returns[mask]))
            else:
                # An unvisited state carries no return signal; rank it neutrally.
                means[state] = 0.0
                vols[state] = 0.0
        return self._rank_states_to_labels(means, vols)

    @staticmethod
    def _rank_states_to_labels(
        means: dict[int, float], vols: dict[int, float]
    ) -> dict[int, Regime]:
        """Map hidden states to ordered labels by mean return, vol as tie-break.

        States are sorted ascending by (mean return, realized volatility): the
        lowest mean return maps toward CRASH and the highest toward EUPHORIA.
        When mean returns tie, the higher-volatility state ranks more bullish.
        Ranks are scaled onto the five ordered labels.
        """
        order = sorted(means.keys(), key=lambda s: (means[s], vols[s]))
        k = len(order)
        last_label = len(ORDERED_REGIMES) - 1
        mapping: dict[int, Regime] = {}
        for rank, state in enumerate(order):
            if k == 1:
                index = last_label // 2  # a lone state is Neutral
            else:
                index = round(rank / (k - 1) * last_label)
            mapping[state] = ORDERED_REGIMES[index]
        return mapping

    # ------------------------------------------------ causal emission + filter

    def _log_emission(self, scaled: np.ndarray) -> np.ndarray:
        """Per-bar, per-state Gaussian log-likelihood (diagonal covariance)."""
        model = self.model
        means = np.asarray(model.means_)  # type: ignore[union-attr]
        covars = np.asarray(model.covars_)  # type: ignore[union-attr]
        if covars.ndim == 3:  # (k, d, d) full form -> take the diagonal
            variances = np.stack([np.diag(c) for c in covars])
        else:
            variances = covars
        variances = np.clip(variances, 1e-12, None)

        n = scaled.shape[0]
        k = means.shape[0]
        log_emission = np.empty((n, k))
        for state in range(k):
            diff = scaled - means[state]
            var = variances[state]
            log_emission[:, state] = -0.5 * (
                np.sum((diff ** 2) / var, axis=1) + np.sum(np.log(2.0 * np.pi * var))
            )
        return log_emission

    def _forward_filter(self, log_emission: np.ndarray) -> np.ndarray:
        """Normalized forward (filtered) state distributions P(state_t | obs<=t)."""
        model = self.model
        startprob = np.asarray(model.startprob_)  # type: ignore[union-attr]
        transmat = np.asarray(model.transmat_)  # type: ignore[union-attr]

        n, k = log_emission.shape
        filtered = np.empty((n, k))

        def normalize(log_weights: np.ndarray) -> np.ndarray:
            shifted = log_weights - log_weights.max()
            weights = np.exp(shifted)
            return weights / weights.sum()

        prev = normalize(np.log(startprob + 1e-300) + log_emission[0])
        filtered[0] = prev
        for t in range(1, n):
            predicted = prev @ transmat  # P(state_t | obs<t)
            prev = normalize(np.log(predicted + 1e-300) + log_emission[t])
            filtered[t] = prev
        return filtered

    # --------------------------------------------------------- assemble output

    def _to_regime_states(
        self, index: pd.Index, state_probs: np.ndarray
    ) -> list[RegimeState]:
        results: list[RegimeState] = []
        label_values = [r.value for r in ORDERED_REGIMES]
        for ts, probs in zip(index, state_probs):
            label_probs = {value: 0.0 for value in label_values}
            for state, prob in enumerate(probs):
                label_probs[self._state_to_label[state].value] += float(prob)
            chosen = max(label_probs, key=lambda key: label_probs[key])
            results.append(
                RegimeState(
                    ts_utc=_ts_iso(ts),
                    regime=Regime(chosen),
                    state_index=int(np.argmax(probs)),
                    probabilities=label_probs,
                )
            )
        return results

    # ----------------------------------------------------------- persistence

    def _require_fitted(self) -> None:
        if self.model is None or self.scaler is None or not self._state_to_label:
            raise RuntimeError("RegimeDetector is not fitted; call fit() or load() first")

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Persist the fitted model, scaler, and label map. Returns the path."""
        self._require_fitted()
        target = Path(path) if path else (self.settings.journal_path / self.DEFAULT_ARTIFACT_NAME)
        target.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "version": self.ARTIFACT_VERSION,
            "n_states": self.n_states,
            "window": self.window,
            "covariance_type": self.covariance_type,
            "n_iter": self.n_iter,
            "random_seed": self.random_seed,
            "model": self.model,
            "scaler": self.scaler,
            "state_to_label": {int(s): r.value for s, r in self._state_to_label.items()},
        }
        with open(target, "wb") as handle:
            pickle.dump(artifact, handle)
        self.log.info("regime_model_saved", path=str(target))
        return target

    @classmethod
    def load(
        cls,
        path: Optional[Union[str, Path]] = None,
        settings: Settings = default_settings,
    ) -> "RegimeDetector":
        """Load a detector previously written by `save`."""
        source = Path(path) if path else (settings.journal_path / cls.DEFAULT_ARTIFACT_NAME)
        with open(source, "rb") as handle:
            artifact = pickle.load(handle)
        if artifact.get("version") != cls.ARTIFACT_VERSION:
            raise ValueError(
                f"regime artifact version {artifact.get('version')} is not supported "
                f"(expected {cls.ARTIFACT_VERSION})"
            )
        detector = cls(
            n_states=artifact["n_states"],
            window=artifact["window"],
            covariance_type=artifact["covariance_type"],
            n_iter=artifact["n_iter"],
            settings=settings,
            random_seed=artifact["random_seed"],
        )
        detector.model = artifact["model"]
        detector.scaler = artifact["scaler"]
        detector._state_to_label = {
            int(s): Regime(v) for s, v in artifact["state_to_label"].items()
        }
        return detector


# Colors used by the plotting helper, keyed by regime.
_REGIME_COLORS: dict[Regime, str] = {
    Regime.CRASH: "#7f0000",
    Regime.BEAR: "#d9534f",
    Regime.NEUTRAL: "#9e9e9e",
    Regime.BULL: "#5cb85c",
    Regime.EUPHORIA: "#1f77b4",
}


def plot_regime_bands(
    prices: pd.Series,
    regimes: list[RegimeState],
    title: str = "Price with regime overlay",
):
    """Overlay shaded regime bands behind a price line (for the UI later).

    Args:
        prices: Close price series indexed by timestamp.
        regimes: Causal regime states aligned to (a subset of) the price index.

    Returns:
        A plotly Figure. Imported lazily so importing this module does not pull
        plotly unless the helper is used.
    """
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=list(prices.index),
            y=list(prices.values),
            mode="lines",
            name="Price",
            line=dict(color="#222222", width=1.5),
        )
    )

    if regimes:
        points = [(pd.Timestamp(rs.ts_utc), rs.regime) for rs in regimes]
        # Coalesce consecutive equal regimes into single shaded bands.
        run_start, run_regime = points[0]
        prev_ts = points[0][0]
        for ts, regime in points[1:]:
            if regime != run_regime:
                fig.add_vrect(
                    x0=run_start,
                    x1=ts,
                    fillcolor=_REGIME_COLORS.get(run_regime, "#cccccc"),
                    opacity=0.18,
                    line_width=0,
                    layer="below",
                )
                run_start, run_regime = ts, regime
            prev_ts = ts
        fig.add_vrect(
            x0=run_start,
            x1=prev_ts,
            fillcolor=_REGIME_COLORS.get(run_regime, "#cccccc"),
            opacity=0.18,
            line_width=0,
            layer="below",
        )
        # Legend-only markers so the band colors are identifiable.
        for regime, color in _REGIME_COLORS.items():
            fig.add_trace(
                go.Scatter(
                    x=[None],
                    y=[None],
                    mode="markers",
                    marker=dict(size=10, color=color, opacity=0.5),
                    name=regime.value,
                )
            )

    fig.update_layout(
        title=title,
        template="plotly_dark",
        xaxis_title="Time",
        yaxis_title="Price",
        hovermode="x unified",
    )
    return fig
