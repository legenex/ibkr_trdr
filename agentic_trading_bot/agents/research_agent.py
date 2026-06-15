"""Research agent: gathers thematic context with tools, outputs a cited brief.

This is the agent that genuinely benefits from the SDK tool loop. It is given
web search and the operator's read-only research MCP connectors (news, filings)
and asked to assemble a ResearchBrief with sources. It emits a structured brief
only; the PreToolUse guardrail hook makes it structurally impossible for it to
touch an order or broker tool.
"""
from __future__ import annotations

from typing import Any, Optional

from agents.provider import LLMProvider, audit_agent_usage
from core.contracts import ResearchBrief
from utils.logging import get_logger

_log = get_logger(__name__)

_SYSTEM = (
    "You are a buy-side research assistant. Gather honest, sourced context for a "
    "theme or watchlist using only the read-only tools provided (web search, news "
    "and filings connectors, file read). You never trade and never place orders. "
    "Cite every claim with a source. Be skeptical: note disconfirming evidence and "
    "do not assert an edge exists."
)


async def run_research(
    provider: LLMProvider,
    theme: str,
    audit: Any,
    watchlist: Optional[list[str]] = None,
    allowed_tools: Optional[list[str]] = None,
    mcp_servers: Optional[dict[str, Any]] = None,
    hooks: Optional[dict[str, Any]] = None,
    max_turns: int = 8,
) -> ResearchBrief:
    """Produce a research brief for a theme, metering usage to the audit trail.

    Args:
        provider: LLM backend (any model).
        theme: The theme or question to research.
        audit: Audit trail for metering and decisions.
        watchlist: Optional seed symbols.
        allowed_tools: Read-only tools to expose (defaults to web search/fetch/read).
        mcp_servers: Operator's read-only research connectors.
        hooks: The PreToolUse guardrail hooks mapping.
    """
    tools = allowed_tools if allowed_tools is not None else ["WebSearch", "WebFetch", "Read"]
    prompt = (
        f"Theme to research: {theme}\n"
        f"Seed watchlist: {', '.join(watchlist or []) or '(none)'}\n\n"
        "Assemble a research brief: a short summary, the key points (with the "
        "evidence for and against), a refined watchlist of liquid symbols, and a "
        "list of sources. Do not claim a trading edge; just gather context."
    )
    response = await provider.structured(
        agent="research",
        system=_SYSTEM,
        prompt=prompt,
        schema=ResearchBrief,
        allowed_tools=tools,
        mcp_servers=mcp_servers,
        hooks=hooks,
        max_turns=max_turns,
    )
    audit_agent_usage(audit, "research", response.usage, extra={"theme": theme})

    brief: ResearchBrief = response.data
    if not brief.theme:
        brief.theme = theme
    if watchlist and not brief.watchlist:
        brief.watchlist = list(watchlist)
    audit.record(
        "RESEARCH_BRIEF",
        {"theme": brief.theme, "watchlist": brief.watchlist, "n_sources": len(brief.sources)},
        "Research agent produced a brief",
    )
    _log.info("research_brief", theme=brief.theme, sources=len(brief.sources))
    return brief
