"""Z-score mean reversion with a fixed percentage stop (a reversion subject).

An honest example, not a claimed edge. It computes a rolling z-score of the
close. When price stretches below the band it goes long (betting on reversion
up); when it stretches above, it goes short. It exits to flat when the z-score
returns inside an inner band. The protective stop is a fixed percentage from the
reference price, so the risk gate can size from the entry-to-stop distance.

Because naive mean reversion is dangerous in violent trends, this strategy goes
flat in the Crash and Euphoria regimes when a regime series is supplied.

Causal: the z-score uses a trailing window; the position is built by a forward
state machine that only ever reads the current and past bars.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """Z-score reversion, fixed stop. Illustrative test subject, not a claimed edge."""

    category = "mean_reversion"

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"lookback": 20, "entry_z": 1.5, "exit_z": 0.3, "stop_pct": 0.05}

    def key_parameter_steps(self) -> dict[str, float]:
        return {"lookback": 5.0, "entry_z": 0.25}

    def regimes_to_flatten(self) -> set[str]:
        # Mean reversion against a powerful trend is how reversion books blow up.
        return {"Crash", "Euphoria"}

    def _build_name(self) -> str:
        return f"mean_reversion_{int(self.params['lookback'])}_z{self.params['entry_z']:g}"

    def _raw_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        lookback = int(self.params["lookback"])
        entry_z = float(self.params["entry_z"])
        exit_z = float(self.params["exit_z"])
        stop_pct = float(self.params["stop_pct"])

        close = data["close"].astype(float)
        mean = close.rolling(lookback).mean()
        std = close.rolling(lookback).std()
        zscore = (close - mean) / std

        z_values = zscore.to_numpy()
        weights = np.full(len(close), np.nan)
        state = 0  # -1 short, 0 flat, +1 long
        for i, z in enumerate(z_values):
            if not np.isfinite(z):
                weights[i] = np.nan
                continue
            if state == 0:
                if z < -entry_z:
                    state = 1
                elif z > entry_z:
                    state = -1
            elif state == 1:  # long, exit once reverted back inside the band
                if z > -exit_z:
                    state = 0
            elif state == -1:  # short
                if z < exit_z:
                    state = 0
            weights[i] = float(state)

        weight = pd.Series(weights, index=data.index)
        stop = pd.Series(np.nan, index=data.index)
        long_mask = weight == 1.0
        short_mask = weight == -1.0
        stop[long_mask] = close[long_mask] * (1.0 - stop_pct)
        stop[short_mask] = close[short_mask] * (1.0 + stop_pct)

        return pd.DataFrame({"weight": weight, "stop": stop, "ref": close})
