"""Strategy interface plus shared machinery for reference strategies.

A Strategy turns bars (and an optional per-bar regime) into a series of Signal
objects. Every non-flat signal carries an intended protective stop so the risk
gate can size the position from the entry-to-stop distance. Signals are causal:
the signal at bar t uses only data at or before t. The backtest engine applies
the next-bar execution shift, so strategies must not shift their own signals.

GateAdapter bridges a Strategy to the stage-5 validation gate, which consumes a
plain target-weight Series. This keeps the rich Signal interface for live and
the simple weight interface for validation without duplicating logic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from core.contracts import Signal, StrategySpec


def average_true_range(data: pd.DataFrame, window: int) -> pd.Series:
    """Causal Average True Range over `window` bars.

    True range is the max of the high-low range and the gaps to the previous
    close. The rolling mean uses only the trailing window, so it never looks
    ahead.
    """
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    prev_close = data["close"].astype(float).shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window).mean()


@runtime_checkable
class Strategy(Protocol):
    """A strategy the pipeline can run and validate.

    `generate_signals` returns a Series (indexed by the bars) of Signal objects,
    with None for warmup bars. `regime` is an optional per-bar Series of regime
    labels the strategy may condition on.
    """

    spec: StrategySpec

    def generate_signals(
        self, data: pd.DataFrame, regime: Optional[pd.Series] = None
    ) -> pd.Series: ...

    def target_weights(
        self, data: pd.DataFrame, regime: Optional[pd.Series] = None
    ) -> pd.Series: ...

    def clone(self, **param_overrides: Any) -> "Strategy": ...


class BaseStrategy(ABC):
    """Shared base: parameter handling, signal assembly, and regime filtering."""

    category: str = "generic"

    def __init__(self, symbol: str = "", **params: Any) -> None:
        """Create a strategy with parameter overrides.

        Args:
            symbol: Symbol stamped onto emitted signals (optional).
            **params: Parameter overrides on top of `default_params`.
        """
        self.symbol = symbol
        self.params: dict[str, Any] = {**self.default_params(), **params}
        self.spec = StrategySpec(
            name=self._build_name(),
            category=self.category,
            description=(self.__class__.__doc__ or "").strip().split("\n")[0],
            params=dict(self.params),
            key_parameters=self.key_parameter_steps(),
            symbols=[symbol] if symbol else [],
        )

    # --- subclass hooks ---

    @classmethod
    @abstractmethod
    def default_params(cls) -> dict[str, Any]:
        """Default parameter values for this strategy."""

    @abstractmethod
    def key_parameter_steps(self) -> dict[str, float]:
        """Map key parameters to the perturbation step for sensitivity testing."""

    @abstractmethod
    def _raw_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return a frame with columns 'weight', 'stop', 'ref' indexed by bars.

        'weight' is the target weight in [-1, 1] with NaN during warmup. 'stop'
        is the intended stop price (NaN when flat or in warmup). 'ref' is the
        reference (close) price. Must be strictly causal.
        """

    def regimes_to_flatten(self) -> set[str]:
        """Regime labels in which this strategy goes flat (empty by default)."""
        return set()

    # --- public API ---

    @property
    def name(self) -> str:
        """The strategy's name (from its spec)."""
        return self.spec.name

    def generate_signals(
        self, data: pd.DataFrame, regime: Optional[pd.Series] = None
    ) -> pd.Series:
        """Return a Series of Signal objects (None during warmup)."""
        frame = data.copy()
        frame.columns = [str(c).lower() for c in frame.columns]
        raw = self._raw_signals(frame)
        weight = raw["weight"].astype(float)
        stop = raw["stop"].astype(float)
        ref = raw["ref"].astype(float)

        # Optional regime conditioning: flatten where the regime is unfavorable.
        avoid = self.regimes_to_flatten()
        if regime is not None and avoid:
            labels = regime.reindex(frame.index)
            mask = labels.isin(avoid) & weight.notna()
            weight = weight.mask(mask, 0.0)
            stop = stop.mask(mask, np.nan)

        signals: list[Optional[Signal]] = []
        for ts, w, s, r in zip(frame.index, weight, stop, ref):
            if pd.isna(w):
                signals.append(None)
                continue
            target = float(w)
            stop_price = None if (abs(target) <= 1e-9 or pd.isna(s)) else float(s)
            signals.append(
                Signal(
                    ts_utc=_iso(ts),
                    symbol=self.symbol,
                    target_weight=target,
                    stop_price=stop_price,
                    reference_price=float(r) if not pd.isna(r) else None,
                    reason=self.spec.category,
                )
            )
        return pd.Series(signals, index=frame.index, dtype=object)

    def target_weights(
        self, data: pd.DataFrame, regime: Optional[pd.Series] = None
    ) -> pd.Series:
        """Return target weights as a float Series (NaN during warmup)."""
        signals = self.generate_signals(data, regime)
        return signals.map(lambda sig: np.nan if sig is None else sig.target_weight).astype(
            float
        )

    def clone(self, **param_overrides: Any) -> "BaseStrategy":
        """Return a copy of this strategy with parameter overrides applied."""
        params = {**self.params, **param_overrides}
        return type(self)(symbol=self.symbol, **params)

    def key_parameters(self) -> dict[str, float]:
        """Sensitivity steps (delegates to spec; used by the gate adapter)."""
        return dict(self.spec.key_parameters)

    @abstractmethod
    def _build_name(self) -> str:
        """Human-readable name encoding the key parameters."""


def _iso(ts: Any) -> str:
    try:
        return pd.Timestamp(ts).isoformat()
    except (TypeError, ValueError):
        return str(ts)


class GateAdapter:
    """Presents a BaseStrategy to the stage-5 validation gate.

    The gate consumes a plain target-weight Series via `generate_signals(bars)`
    plus `clone`, `key_parameters`, `params`, and `name`. This adapter derives
    all of that from the wrapped strategy's signals.
    """

    def __init__(self, strategy: BaseStrategy) -> None:
        """Wrap a strategy for validation."""
        self._strategy = strategy
        self.name = strategy.name
        self.params = dict(strategy.params)

    def generate_signals(self, bars: pd.DataFrame) -> pd.Series:
        """Target weights for the gate (regime is handled by the gate itself)."""
        return self._strategy.target_weights(bars)

    def clone(self, **param_overrides: Any) -> "GateAdapter":
        """Clone the underlying strategy and re-wrap it."""
        return GateAdapter(self._strategy.clone(**param_overrides))

    def key_parameters(self) -> dict[str, float]:
        """Perturbation steps for the sensitivity sweep."""
        return self._strategy.key_parameters()
