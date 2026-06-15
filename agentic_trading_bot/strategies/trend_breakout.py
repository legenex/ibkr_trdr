"""Donchian channel breakout with an ATR-based stop (a trend subject).

This is an honest example, not a claimed edge. It goes long when price breaks
above the prior N-bar high channel and short when it breaks below the prior
N-bar low channel, holding the position until the opposite breakout. The
intended protective stop is placed an ATR multiple away from the reference
price, so the risk gate can size the position from the entry-to-stop distance.

Causal: channels use the prior window (shifted to exclude the current bar) and
the ATR uses only trailing data. Warmup bars are left as NaN, not backfilled.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, average_true_range


class TrendBreakoutStrategy(BaseStrategy):
    """Donchian breakout, ATR stop. Illustrative test subject, not a claimed edge."""

    category = "breakout"

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"channel": 20, "atr_window": 14, "atr_mult": 3.0}

    def key_parameter_steps(self) -> dict[str, float]:
        return {"channel": 5.0, "atr_mult": 0.5}

    def _build_name(self) -> str:
        return f"trend_breakout_{int(self.params['channel'])}_atr{self.params['atr_mult']:g}"

    def _raw_signals(self, data: pd.DataFrame) -> pd.DataFrame:
        channel = int(self.params["channel"])
        atr_window = int(self.params["atr_window"])
        atr_mult = float(self.params["atr_mult"])

        high = data["high"].astype(float)
        low = data["low"].astype(float)
        close = data["close"].astype(float)

        # Prior channel: shift(1) so the current bar's own extreme is excluded.
        upper = high.rolling(channel).max().shift(1)
        lower = low.rolling(channel).min().shift(1)
        atr = average_true_range(data, atr_window)

        position = pd.Series(np.nan, index=data.index)
        position[close > upper] = 1.0
        position[close < lower] = -1.0
        position = position.ffill()

        valid = upper.notna() & lower.notna() & atr.notna()
        weight = position.where(valid)

        stop = pd.Series(np.nan, index=data.index)
        long_mask = weight == 1.0
        short_mask = weight == -1.0
        stop[long_mask] = close[long_mask] - atr_mult * atr[long_mask]
        stop[short_mask] = close[short_mask] + atr_mult * atr[short_mask]

        return pd.DataFrame({"weight": weight, "stop": stop, "ref": close})
