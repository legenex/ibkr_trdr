"""Per-run LLM budget meter for the learning loop.

Agent SDK usage draws from a separate monthly credit, so each run carries a hard
token and cost ceiling. When the budget is exhausted, the learning loop skips its
remaining LLM steps and logs it; the deterministic steps still run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agents.provider import ProviderUsage


@dataclass
class BudgetMeter:
    """Tracks tokens and cost spent against per-run ceilings."""

    token_budget: int
    cost_budget_usd: float
    spent_tokens: int = 0
    spent_cost_usd: float = 0.0

    @property
    def exhausted(self) -> bool:
        """True once either the token or the cost ceiling has been reached."""
        return self.spent_tokens >= self.token_budget or self.spent_cost_usd >= self.cost_budget_usd

    def spend(self, usage: ProviderUsage) -> None:
        """Record the usage of one completed LLM step."""
        self.spent_tokens += int(usage.total_tokens or 0)
        self.spent_cost_usd += float(usage.cost_usd or 0.0)

    def snapshot(self) -> dict[str, float]:
        """Serializable view of the meter for logging."""
        return {
            "spent_tokens": self.spent_tokens,
            "token_budget": self.token_budget,
            "spent_cost_usd": self.spent_cost_usd,
            "cost_budget_usd": self.cost_budget_usd,
            "exhausted": self.exhausted,
        }
