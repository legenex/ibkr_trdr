"""Signal agent: turns a brief into candidate strategy specs. Rules only.

A constrained generation (query() is fine, no tools). It proposes one or more
StrategyProposal objects: an explicit, testable parameterized template, a
universe, intended regimes, and a required intended stop. It proposes rules
only; it never runs anything and cannot reach a tool. Proposals naming an
unknown template are dropped.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from agents.provider import LLMProvider, audit_agent_usage
from core.contracts import ResearchBrief, StrategyProposal
from strategies.registry import known_templates
from utils.logging import get_logger

_log = get_logger(__name__)


class StrategyProposalList(BaseModel):
    """Envelope so a single structured generation can return many proposals."""

    proposals: list[StrategyProposal] = Field(default_factory=list)


def _system(templates: list[str]) -> str:
    return (
        "You design explicit, testable trading strategy specifications. You ONLY "
        "propose rules; you never run, backtest, or execute anything. Each proposal "
        f"must choose `template` from this fixed set: {templates}. Provide concrete "
        "`parameters`, a `universe` of liquid symbols, the `intended_regimes` it "
        "should trade in, and a REQUIRED `intended_stop` describing the protective "
        "stop. Do not claim the strategy is profitable; it must be validated."
    )


async def run_signal(
    provider: LLMProvider,
    brief: ResearchBrief,
    audit: Any,
    universe: Optional[list[str]] = None,
    max_proposals: int = 3,
) -> list[StrategyProposal]:
    """Generate candidate strategy specs from a research brief.

    Args:
        provider: LLM backend.
        brief: The research brief to ground the proposals.
        audit: Audit trail for metering.
        universe: Optional override symbols applied to every proposal.
        max_proposals: Cap on returned proposals.
    """
    templates = known_templates()
    prompt = (
        f"Theme: {brief.theme}\nSummary: {brief.summary}\n"
        f"Key points: {brief.key_points}\nWatchlist: {brief.watchlist}\n\n"
        f"Propose up to {max_proposals} candidate strategy specs. Each MUST name a "
        "template from the allowed set and MUST include an intended stop."
    )
    response = await provider.structured(
        agent="signal",
        system=_system(templates),
        prompt=prompt,
        schema=StrategyProposalList,
    )
    audit_agent_usage(audit, "signal", response.usage, extra={"theme": brief.theme})

    proposals: list[StrategyProposal] = []
    dropped: list[str] = []
    for spec in response.data.proposals[:max_proposals]:
        if spec.template not in templates:
            dropped.append(f"{spec.name}:{spec.template}")
            continue
        if universe:
            spec.universe = list(universe)
        elif not spec.universe and brief.watchlist:
            spec.universe = list(brief.watchlist)
        proposals.append(spec)

    audit.record(
        "SIGNAL_PROPOSALS",
        {"count": len(proposals), "dropped_unknown_template": dropped},
        "Signal agent proposed candidate specs",
    )
    _log.info("signal_proposals", count=len(proposals), dropped=len(dropped))
    return proposals
