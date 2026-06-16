"""Meta-reviewer: a lightweight, periodic critic of the system itself.

It reads recent reflections and learning results and writes a human-readable
critique to the learning log (for example, "briefs overestimate catalysts in the
Bear regime"). It SUGGESTS only and changes nothing: no registry writes, no queue
writes, no promotions. Its sole output is an audited META_REVIEW_NOTE.
"""
from __future__ import annotations

from typing import Any, Optional

from agents.provider import LLMProvider, audit_agent_usage
from core.contracts import LearningResult, Reflection
from learning.budget_meter import BudgetMeter
from utils.logging import get_logger

_log = get_logger(__name__)

_META_SYSTEM = (
    "You are a meta-reviewer of an automated trading research loop. Read recent "
    "reflections and learning results and write a short, candid critique of the "
    "system's blind spots and biases (for example, briefs that overestimate "
    "catalysts in a given regime, or hypotheses that keep failing the same way). "
    "You SUGGEST only. You never change anything and never approve anything."
)


async def run_meta_review(
    provider: LLMProvider,
    reflections: list[Reflection],
    learning_results: list[LearningResult],
    audit: Any,
    budget_meter: Optional[BudgetMeter] = None,
) -> str:
    """Produce and audit a META_REVIEW_NOTE. Returns the note text (may be empty)."""
    if budget_meter is not None and budget_meter.exhausted:
        audit.record(
            "AGENT_BUDGET_EXHAUSTED",
            {"step": "meta_review", **budget_meter.snapshot()},
            "Skipped meta-review: per-run budget exhausted",
        )
        return ""

    prompt = (
        "Recent reflections:\n"
        + "\n".join(f"- [{r.regime if hasattr(r, 'regime') else ''}] {r.what_happened} "
                    f"(thesis: {r.thesis_correctness})" for r in reflections)
        + "\n\nRecent learning results:\n"
        + "\n".join(
            f"- {lr.period}: {lr.experiments_run} experiments, {lr.skills_promoted} promoted, "
            f"{lr.skills_queued_for_approval} queued, {lr.skills_demoted} demoted"
            for lr in learning_results
        )
        + "\n\nWrite a brief critique. Suggestions only."
    )
    response = await provider.text(agent="meta_review", system=_META_SYSTEM, prompt=prompt)
    if budget_meter is not None:
        budget_meter.spend(response.usage)
    audit_agent_usage(audit, "meta_review", response.usage)

    note = response.text
    audit.record(
        "META_REVIEW_NOTE",
        {"note": note, "n_reflections": len(reflections), "n_results": len(learning_results)},
        "Meta-reviewer wrote a critique (suggestion only; changed nothing)",
    )
    _log.info("meta_review_note", n_reflections=len(reflections))
    return note
