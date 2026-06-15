"""Strategy interface and reference strategies.

A strategy emits Signal objects (each carrying an intended stop) from bars and an
optional regime. Reference strategies here are honest test SUBJECTS, not claimed
edges: they exist so the validation gate and the rest of the pipeline have
something concrete to run on.
"""
from strategies.base import (
    BaseStrategy,
    GateAdapter,
    Strategy,
    average_true_range,
)
from strategies.mean_reversion import MeanReversionStrategy
from strategies.trend_breakout import TrendBreakoutStrategy

__all__ = [
    "BaseStrategy",
    "GateAdapter",
    "Strategy",
    "average_true_range",
    "MeanReversionStrategy",
    "TrendBreakoutStrategy",
]
