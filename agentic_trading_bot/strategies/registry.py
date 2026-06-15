"""Registry mapping template names to strategy classes.

The signal agent proposes WHICH parameterized template to use, never arbitrary
code. The validation agent builds a concrete strategy from a template name plus
parameters here, so an LLM proposal can be turned into a runnable strategy
safely (no code execution, unknown parameters dropped).
"""
from __future__ import annotations

from typing import Any

from strategies.base import BaseStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.trend_breakout import TrendBreakoutStrategy

REGISTRY: dict[str, type[BaseStrategy]] = {
    "trend_breakout": TrendBreakoutStrategy,
    "mean_reversion": MeanReversionStrategy,
}


def known_templates() -> list[str]:
    """Return the sorted list of valid template names."""
    return sorted(REGISTRY)


def build_strategy(
    template: str, parameters: dict[str, Any] | None = None, symbol: str = ""
) -> BaseStrategy:
    """Instantiate a strategy from a template name and parameters.

    Unknown parameter keys are dropped (the agent does not get to invent knobs).

    Raises:
        KeyError: If the template name is not registered.
    """
    if template not in REGISTRY:
        raise KeyError(f"unknown strategy template {template!r}; known: {known_templates()}")
    cls = REGISTRY[template]
    valid_keys = set(cls.default_params())
    clean = {k: v for k, v in (parameters or {}).items() if k in valid_keys}
    return cls(symbol=symbol, **clean)
